"""Improve an agent's skill from real feedback — the human-in-the-loop cycle.

A customer-support assistant runs on a skill: keyed `steps`, `pitfalls`, and
`examples` records — the playbook it follows when drafting replies to refund
requests. One rule tells it to attach a discount code to every reply, and that
keeps backfiring when the charge was the company's own mistake.

This script closes that loop the way a production integration does, through an
**artifact use**. It binds the exact prompt the assistant ran on
(`daily_agent_prompt`), whose `learning:` declaration names `maintained_skill@1`
as the thing feedback should improve. The bind returns one short ID and, with
it, the exact keyed skill heads that were in force. The application keeps that
ID beside the reply and later reports outcomes against it — nothing else has to
be remembered. Each report becomes an ordinary `learning_signals` record naming
the precise skill version that produced the bad reply. Those signals become the
evidence the shipped `skill` pipeline drafts from, and then — the point — the
script stops and asks YOU whether to ship the draft. Nothing the model writes
goes live on its own.

    install skill            (steps / pitfalls / examples — active immediately)
        │
        ▼
    bind an artifact use     (renders the prompt, snapshots it, and resolves the
        │                     learning target to the heads that were in force)
        ▼
    report outcomes          (thumbs_down / evaluation / correction / task_success
        │                     against that one ID → learning_signals records)
        ▼
    route into evidence      (the one step Memseek leaves to your application —
        │  skill pipeline     see ROUTE below for why, and what decides it)
        ▼
    cited DRAFT candidate     (always all three sections; the discount rule narrowed)
        │  ← YOU review the divergence and decide  ───────────┐
        ▼                                                      │
    promote (atomic)  ── or decline: drafts stay in the audit trail, skill
        │                                          never changed
        ▼
    render maintained_skill  (the playbook the assistant uses from here on)
        │
        ▼
    re-bind                  (the learning target now names the promotion run,
                              so the next signal rebases on what actually shipped)

The terminal animates each beat: a spinner while the worker enriches records
and the model drafts, a revealed diff, and an interactive promote gate. It
degrades to plain, instant output when stdout is not a TTY (or NO_COLOR is
set), so it stays CI-safe.

Run it against a local stack with a REAL provider (LLM_FAKE=1 cannot invent the
citation UUIDs the draft must carry, so the pipeline would no-op honestly):

    make database && source .env.sh
    export PROVIDER_OPENAI_COMPAT_API_KEY=sk-...     # or OPENAI_API_KEY
    uv run memseek migrate
    uv run uvicorn memseek.api:app &                 # terminal A
    uv run memseek worker &                          # terminal B
    uv run python examples/skill_maintenance.py

Set MEMSEEK_API_KEY to reuse a workspace, or DATABASE_URL to create a fresh
one. Set MEMSEEK_AUTO=1 to auto-approve the promote gate (for demos/CI).
"""

from __future__ import annotations

import asyncio
import difflib
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from _catalog import publish_reference_catalog
from _workspace_explorer import print_workspace_explorer

from memseek.config import get_settings
from memseek.sdk import BoundArtifact, MemseekClient, MemseekHTTPError

RUN = secrets.token_hex(3)
ENTITY = f"skill:refund-replies-{RUN}"  # one entity groups the skill's sections
PROMPT_ARTIFACT = "daily_agent_prompt"  # the live prompt the assistant runs on
SKILL_ARTIFACT = "maintained_skill"  # the reviewed artifact feedback improves
AUTO = bool(os.environ.get("MEMSEEK_AUTO")) or not sys.stdin.isatty()

# --- terminal styling (TTY only; honors NO_COLOR) --------------------------
_C = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"


def _s(*c: int) -> str:
    return ("\033[" + ";".join(map(str, c)) + "m") if _C else ""


RESET, BOLD, DIM = _s(0), _s(1), _s(2)
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GREY = (
    _s(31),
    _s(32),
    _s(33),
    _s(34),
    _s(35),
    _s(36),
    _s(90),
)


def paint(t: str, *c: str) -> str:
    return ("".join(c) + t + RESET) if _C else t


def show(title: str, body: str = "") -> None:
    rule = paint("━" * 72, GREY)
    print(f"\n{rule}\n  {paint(title, BOLD, CYAN)}\n{rule}")
    if body:
        print(paint(f"  {body}", DIM))


def note(t: str) -> None:
    """One bulleted aside.

    A leading newline starts a new group: it becomes a real blank line and the
    bullet that follows opens at the margin, so `note("\\n  text")` reads as a
    first line rather than as an indented continuation of nothing.
    """
    if t.startswith("\n"):
        print()
        t = t.lstrip("\n").lstrip(" ")
    print(paint(f"  · {t}", GREY))


def short(rid: str | None) -> str:
    return rid[:8] if rid else "—"


# --- animation helpers (all no-op to plain output when not a TTY) ----------
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


async def spin(label: str, coro: Any) -> Any:
    """Await `coro` while animating a spinner on one line; return its result."""
    if not _C:
        print(paint(f"  · {label}…", GREY))
        return await coro
    task = asyncio.ensure_future(coro)
    i = 0
    while not task.done():
        sys.stdout.write(
            f"\r  {paint(SPINNER[i % len(SPINNER)], CYAN)} {paint(label + '…', DIM)}   "
        )
        sys.stdout.flush()
        i += 1
        await asyncio.sleep(0.08)
    sys.stdout.write("\r" + " " * (len(label) + 14) + "\r")
    result = await task  # re-raises if the task failed
    print(f"  {paint('✓', GREEN)} {label}")
    return result


async def typewriter(text: str, *color: str, indent: str = "    ", delay: float = 0.008) -> None:
    """Reveal `text` character by character (instant when not a TTY)."""
    for line in text.splitlines() or [""]:
        if not _C:
            print(indent + paint(line, *color))
            continue
        sys.stdout.write(indent + "".join(color))
        for ch in line:
            sys.stdout.write(ch)
            sys.stdout.flush()
            await asyncio.sleep(delay)
        sys.stdout.write(RESET + "\n")


async def reveal_diff(old: str, new: str) -> None:
    """Animate a unified-style diff between two multi-line blocks."""
    for token in difflib.ndiff(old.splitlines(), new.splitlines()):
        code, body = token[:2], token[2:]
        if code == "- ":
            glyph, color = "-", RED
        elif code == "+ ":
            glyph, color = "+", GREEN
        elif code == "  ":
            glyph, color = " ", GREY
        else:
            continue  # difflib "? " hint lines
        line = f"    {paint(glyph, color)} {paint(body, color, DIM if glyph == ' ' else RESET)}"
        print(line)
        if _C:
            await asyncio.sleep(0.14)


async def ask(prompt: str) -> str:
    """Read one line without blocking the event loop; auto-answers in AUTO mode."""
    if AUTO:
        print(prompt + paint("y", BOLD, GREEN) + paint("   (MEMSEEK_AUTO)", GREY))
        return "y"
    loop = asyncio.get_running_loop()
    return (await loop.run_in_executor(None, input, prompt)).strip().lower()


# --- the playbook, and the reply that backfired ----------------------------
STEPS = (
    "1. Greet the customer by name and thank them for reaching out.\n"
    "2. Restate the specific order or charge they mentioned, so it's clear you understood.\n"
    "3. Give the refund decision in the first two sentences — don't bury it.\n"
    "4. If approved, state the amount and that it arrives in 5-10 business days.\n"
    "5. Always include a discount code for their next order before closing."
)
PITFALLS = (
    "- Don't ask for information the customer already gave you in their first message.\n"
    "- Don't promise a refund before it's actually approved.\n"
    "- Don't close the reply without a clear next step or timeline."
)
EXAMPLES = (
    '"I was charged twice for order #A-2231": confirm the duplicate charge, approve the '
    "refund, state the amount and the 5-10 day timeline, and apologize for the hassle.\n"
    '"I want to cancel and get this month back": confirm the plan, explain what is and '
    "isn't refundable, and give the exact amount being returned."
)

SKILL_SECTIONS = {"steps": STEPS, "pitfalls": PITFALLS, "examples": EXAMPLES}

# The request the assistant handled, and the reply its own playbook produced.
# Memseek renders the prompt and never sees either one: an artifact use is not
# an invocation, and there is nowhere in it to put a prompt or a response.
TASK = "Refund request from Dana: charged twice for order #A-2231, asking for the duplicate back."
DRAFTED_REPLY = (
    "Hi Dana — thanks for flagging this. You were charged twice for order #A-2231; I've "
    "approved a full refund of $79.00, and it will arrive in 5-10 business days.\n\n"
    "As a thank-you, here's 10% off your next order: WELCOME10."
)

# What came back about that one reply. Each entry is submitted verbatim as
# feedback keyword arguments against the use ID — the only thing the application
# had to keep. `kind` becomes the signal record's type; `source` records who is
# reporting without Memseek ever weighting one reporter above another.
SIGNALS: list[dict[str, Any]] = [
    {
        "kind": "task_success",
        "source": "application",
        "label": "clean_refund_close",
        "comment": "Replies that state the refund decision in the first two sentences and give "
        "the 5-10 day timeline get quick, friendly acknowledgements and close on the first reply.",
    },
    {
        "kind": "thumbs_down",
        "source": "end_user",
        "label": "tone_deaf_upsell",
        "comment": "You charged me twice by mistake and your answer is a coupon? I'd like to "
        "speak to a manager.",
        "actual_excerpt": DRAFTED_REPLY,
    },
    {
        "kind": "evaluation",
        "source": "evaluator",
        "score": 0.2,
        "label": "tone_deaf_upsell",
        "comment": "Automated review of billing-error replies: the always-attach-a-discount rule "
        "fires on charges the company itself caused, which reads as profiting from our own error.",
    },
    {
        "kind": "correction",
        "source": "operator",
        "label": "discount_scope",
        "comment": "A support lead's guidance after the escalation.",
        "expected": "When the charge was our own mistake, apologize and fix it — don't attach a "
        "discount offer. Save discount codes for goodwill and retention, not for cases where we "
        "are at fault.",
    },
]

# Signal kind → (evidence type, evidence kind) in the `outcomes` collection.
#
# This table is the application's own policy, and that is the point: Memseek
# records what happened and who said it, and never interprets a signal for you.
# Deciding that a thumbs_down is evidence of a *failure* the skill pipeline
# should read — rather than noise, or a model bug, or a missing-data problem —
# is a product judgement, so it lives in application code and not in the
# catalog. See `Cycle.route` for why the copy is needed at all.
ROUTE = {
    "task_success": ("outcome", "success"),
    "thumbs_up": ("outcome", "success"),
    "thumbs_down": ("exception", "failure"),
    "task_failure": ("exception", "failure"),
    "exception": ("exception", "failure"),
    "correction": ("feedback", "review"),
}


def route_signal(kind: str, score: float | None) -> tuple[str, str]:
    """Classify one signal. An `evaluation` is graded, so its score decides."""

    if kind == "evaluation":
        return ("outcome", "success") if (score or 0.0) >= 0.5 else ("exception", "failure")
    return ROUTE[kind]


# --- the cycle -------------------------------------------------------------
class Cycle:
    def __init__(self, client: MemseekClient) -> None:
        self.c = client

    async def ingest(self, records: list[dict[str, Any]]) -> list[str]:
        res = await self.c.records.ingest_many(records)
        return [r["id"] for r in res.get("inserted", []) + res.get("duplicates", [])]

    async def wait_ready(self, ids: list[str], timeout_s: float = 120.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        pending = list(ids)
        while pending:
            if (await self.c.record(pending[-1])).get("ready"):
                pending.pop()
                continue
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("records not enriched — is `memseek worker` running?")
            await asyncio.sleep(0.5)

    async def bind_use(self, *, snapshot: bool = True) -> BoundArtifact:
        """Render the prompt the assistant runs on and register the use.

        `handle.use()` binds once, keeps the correlation attributes active while
        the caller's own SDK call runs, and never inspects that call's request or
        response — so it stays correct for any provider. With
        `memseek[opentelemetry]` installed it also opens a span carrying
        `use.telemetry_attributes`; without it the body still runs, unwrapped.
        """

        now = datetime.now(UTC)
        handle = self.c.artifact(PROMPT_ARTIFACT)
        async with handle.use(
            {
                "entity": ENTITY,
                "task": TASK,
                "start": now.isoformat(),
                "end": (now + timedelta(days=7)).isoformat(),
            },
            # A snapshot persists this exact render from the same resolution, so
            # the signal can cite it in `derived_from` and erasure closure reaches
            # anything derived from it. A run cannot be snapshotted after the
            # fact, so this is decided here, before execution.
            snapshot=snapshot,
        ) as use:
            # The application's own model call belongs here, with `use.content`
            # as the rendered prompt. This script narrates the reply instead of
            # paying a provider for it — and a use ID never claimed a model ran.
            _ = DRAFTED_REPLY
            return use

    async def report(self, use_id: str) -> list[dict[str, Any]]:
        """Submit each selected outcome against the one ID we kept.

        Feedback needs the use ID and nothing else the application had to store.
        The dedupe key is namespaced under the use internally, so it can never
        collide with one of our own record keys.
        """

        return [
            await self.c.feedback.submit(
                use_id=use_id, dedupe_key=f"reply:{RUN}:{signal['kind']}", **signal
            )
            for signal in SIGNALS
        ]

    async def route(self, submissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Copy each signal into the evidence scope the skill pipeline reads.

        This is the one hop Memseek leaves to the application, for two structural
        reasons worth understanding rather than working around:

        * A signal is routed to entity `artifact:maintained_skill` — one
          improvement backlog per reviewed artifact, shared by every entity that
          renders it. `learning_target.entity` is the field that says which
          subject it was actually about.
        * A pipeline source only ever reads the entity its run is for; there is
          no cross-entity source scope. The shipped `skill` pipeline reads
          `main` and `outcomes` under the skill's own entity.

        So the application copies each signal into that entity, `derived_from`
        the signal, which keeps provenance connected end to end: promoted skill
        → cited evidence record → learning signal → prompt snapshot.
        """

        records = []
        for submission in submissions:
            signal = await self.c.record(submission["record_id"])
            content = signal["content"]
            evidence_type, evidence_kind = route_signal(
                content["signal"]["kind"], content["signal"].get("score")
            )
            use = content["artifact_use"]
            target = use.get("learning_target") or {}
            records.append(
                {
                    "entity": target.get("entity") or ENTITY,
                    "collection": "outcomes",
                    "type": evidence_type,
                    # The signal's own deterministic text projection: a header
                    # line plus its evidence lines. It is what a candidate
                    # derivation is meant to read, so it is copied as-is.
                    "text": content["text"],
                    "content": {
                        "kind": evidence_kind,
                        "signal": content["signal"],
                        "use_id": use["id"],
                        **(
                            {"base_version_id": target["base_run_id"]}
                            if target.get("base_run_id")
                            else {}
                        ),
                    },
                    "derived_from": [submission["record_id"]],
                    "dedupe_key": f"{ENTITY}:signal:{submission['record_id']}",
                }
            )
        return records

    async def draft_candidate(self) -> dict[str, Any]:
        """Enqueue the `skill` pipeline, wait for it, return its run detail
        (outputs = the three DRAFT sections; run.content.candidate_set = the
        deterministic keyed divergence)."""
        job = await self.c.run_processor("skill", entity=ENTITY)
        job_id = job["job_id"]
        deadline = asyncio.get_running_loop().time() + 160.0
        while True:
            status = await self.c.job(job_id)
            if (
                status.get("state") in {"done", "dead"}
                or asyncio.get_running_loop().time() >= deadline
            ):
                break
            await asyncio.sleep(1.0)
        run_id = status.get("successful_run_id")
        if not run_id:
            raise RuntimeError(
                "the skill pass produced no reviewed candidate — with a real provider this "
                "usually means the evidence did not justify a change. Check `memseek worker` logs."
            )
        return await self.c.run(run_id)


async def ensure_workspace() -> str:
    if key := os.environ.get("MEMSEEK_API_KEY"):
        return key
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("set MEMSEEK_API_KEY (existing workspace) or DATABASE_URL (fresh one)")
    from memseek.auth import create_workspace
    from memseek.db import pool_lifespan

    async with pool_lifespan(get_settings()) as pool:
        cred = await create_workspace(pool, f"skillmaint-{RUN}")
    print(paint(f"created disposable workspace {cred.workspace}", GREY))
    return cred.api_key


async def main() -> None:
    api_key = await ensure_workspace()
    base_url = os.environ.get("MEMSEEK_BASE_URL", "http://127.0.0.1:8000")
    print_workspace_explorer(api_url=base_url, api_key=api_key)
    live = not get_settings().llm_fake

    async with MemseekClient(base_url, api_key) as client:
        await publish_reference_catalog(client)
        active = {
            c["name"]
            for c in (await client._request("GET", "/collections")).get("collections", [])
            if c.get("active")
        }
        published = {
            a["name"] for a in (await client._request("GET", "/artifacts")).get("artifacts", [])
        }
        missing = sorted(
            ({"skills", "outcomes", "learning_signals"} - active)
            | ({PROMPT_ARTIFACT, SKILL_ARTIFACT} - published)
        )
        if missing:
            raise SystemExit(
                f"this workspace is missing: {', '.join(missing)}.\n"
                "Load the shipped catalog (collections/core.yaml, collections/learning.yaml,\n"
                "derivations/skill.yaml, artifacts/skill.yaml, artifacts/agent_prompt.yaml) —\n"
                "point the service at this repo tree, or unset MEMSEEK_API_KEY to build a\n"
                "fresh workspace from it."
            )
        cyc = Cycle(client)

        show(
            "REFUND REPLIES — a support assistant that improves its playbook from feedback",
            f"entity {ENTITY}",
        )
        print(
            f"  provider: {paint('real model' if live else 'LLM_FAKE=1 (cannot cite — the draft will be empty)', BOLD, GREEN if live else YELLOW)}"
        )

        # ---- 1. install the skill ------------------------------------------
        show(
            "STEP 1 — install the playbook",
            "three keyed sections — steps, pitfalls, examples — active the moment you write them",
        )
        records = [
            {
                "entity": ENTITY,
                "collection": "skills",
                "type": "skill",
                "key": key,
                "text": text,
                "dedupe_key": f"{ENTITY}:v1:{key}",
            }
            for key, text in SKILL_SECTIONS.items()
        ]
        await spin("worker enriching the skill sections", cyc.wait_ready(await cyc.ingest(records)))
        for key, text in SKILL_SECTIONS.items():
            print(paint(f"\n  {key}", BOLD, CYAN))
            await typewriter(text, GREY)

        # ---- 2. bind the prompt the assistant actually ran on ---------------
        show(
            "STEP 2 — bind an artifact use",
            f"one handle correlating this exact render of {PROMPT_ARTIFACT} with whatever happens next",
        )
        note("the artifact declares  learning: {target_block: skill, artifact: maintained_skill@1}")
        note("so the bind resolves which maintained value feedback should improve — the client")
        note("reporting an outcome never has to decide that for itself.")
        use = await spin(
            "rendering the prompt, snapshotting it, resolving the learning target", cyc.bind_use()
        )

        print(paint("\n  the handle", BOLD, CYAN))
        for field, value in (
            ("use id", use.id),
            ("artifact", f"{use.artifact.get('name')}@{use.artifact.get('version')}"),
            ("render_sha256", use.render_sha256[:16] + "…"),
            ("snapshot", short(use.snapshot_id)),
            ("truncated", str(use.truncated)),
        ):
            print(f"    {paint(field.ljust(14), GREY)}{value}")
        note("\n  use.id is the ONE field to store beside your own result — like a payment-intent")
        note("  id. use.content is the rendered prompt you pass to your own SDK; Memseek never")
        note("  inspects the request or the response, so this stays correct for any provider.")
        if use.truncated:
            print(
                paint(
                    "    ⚠ a block hit its token budget — a packing failure, not a skill failure",
                    YELLOW,
                )
            )

        target = dict(use.learning_target or {})
        print(
            paint("\n  the learning target — the exact keyed heads that were in force", BOLD, CYAN)
        )
        for head in target.get("heads", []):
            print(
                f"    {paint('·', GREY)} {paint(head['key'].ljust(10), BOLD)} "
                f"{paint('record ' + short(head['record_id']), GREY)}  "
                f"{paint('run ' + short(head['run_id']), GREY)}"
            )
        base = target.get("base_run_id")
        print(f"\n    {paint('base_run_id'.ljust(14), GREY)}{base or paint('null', YELLOW)}")
        if not base:
            note("  null is a claim, not a gap: we wrote these sections directly, so no promotion")
            note("  produced them and there is no single base version to name. Watch this field")
            note("  after the promote — that is the loop closing.")

        # ---- what happened (narration, not data) ---------------------------
        show(
            "WHAT WENT WRONG", "the assistant followed its own playbook — and a customer got upset"
        )
        await typewriter(DRAFTED_REPLY, GREY)
        for line in (
            "",
            "Steps 1-4 were exactly right: greeting, the restated charge, the decision up",
            "front, the amount and the timeline. Then step 5 fired and attached a coupon.",
            "Dana replied that being upsold right after a billing error felt tone-deaf,",
            "and asked to speak to a manager.",
        ):
            print(paint(f"    {line}", YELLOW if "tone-deaf" in line else GREY))
            if _C:
                await asyncio.sleep(0.4)

        # ---- 3. report the outcomes against that one ID ---------------------
        show(
            "STEP 3 — report the outcomes",
            "four reports, one use ID — each becomes an ordinary learning_signals record",
        )
        glyphs = {
            "task_success": (GREEN, "✓", "the application: what closed cleanly"),
            "thumbs_down": (RED, "⚠", "the customer who received it"),
            "evaluation": (MAGENTA, "◆", "an automated judge"),
            "correction": (BLUE, "✎", "a support lead, with the rule they approved"),
        }
        for signal in SIGNALS:
            color, glyph, who = glyphs[signal["kind"]]
            grade = f" score={signal['score']:g}" if "score" in signal else ""
            print(
                f"\n  {paint(glyph + ' ' + who, BOLD, color)}  "
                f"{paint('(' + signal['kind'] + ' from ' + signal['source'] + grade + ')', GREY)}"
            )
            await typewriter(signal.get("expected") or signal["comment"], GREY)
        submissions = await spin("submitting feedback against the use", cyc.report(use.id))

        print(paint("\n  the records that got written", BOLD, CYAN))
        for submission in submissions:
            print(
                f"    {paint(short(submission['record_id']), BOLD)} "
                f"{paint(submission['collection'] + '/' + submission['type'], GREY)}  "
                f"{paint('entity ' + submission['entity'], GREY)}"
                f"{paint('  (duplicate)', YELLOW) if submission.get('duplicate') else ''}"
            )
        note("\n  entity is artifact:maintained_skill — one improvement backlog per reviewed")
        note("  artifact, and the signal kind is the record type, so a derivation selects the")
        note("  subset it cares about with an ordinary scope. learning_signals declares no")
        note("  processors, so a signal is searchable and trigger-eligible the moment it commits:")
        note("  feedback never waits on an embedding queue.")

        # ---- 4. route the signals into the pipeline's evidence scope --------
        show(
            "STEP 4 — route the signals into evidence",
            "the one hop Memseek leaves to your application — and it is deliberate",
        )
        note("a pipeline source only reads the entity its run is for; there is no cross-entity")
        note("source scope. The shipped `skill` pipeline reads main/outcomes under the SKILL's")
        note(
            f"entity, while the signals landed on artifact:{SKILL_ARTIFACT}. learning_target.entity"
        )
        note(f"is the field that says where they belong: {ENTITY}")
        note("\n  Memseek never interprets a signal for you — see ROUTE in this file. Calling a")
        note("  thumbs_down evidence of a skill failure (rather than a missing-data, retrieval,")
        note("  packing, or model failure) is a product judgement, so it stays in your code.")

        evidence = await cyc.route(submissions)
        print()
        for record in evidence:
            print(
                f"    {paint(record['type'].ljust(10), BOLD)}"
                f"{paint(record['content']['signal']['kind'].ljust(14), GREY)}"
                f"{paint('derived_from ' + short(record['derived_from'][0]), GREY)}"
            )
        await spin("worker enriching the evidence", cyc.wait_ready(await cyc.ingest(evidence)))
        note("each evidence record is derived_from its signal, so provenance stays connected:")
        note("promoted skill → cited evidence → learning signal → prompt snapshot.")

        # ---- 5. let the model draft a candidate ----------------------------
        show(
            "STEP 5 — the skill pipeline drafts a cited candidate",
            "bounded: ≤2 tasks, ≤4 model calls — and every section must cite visible records",
        )
        detail = await spin(
            "the model is drafting (this calls your real provider)", cyc.draft_candidate()
        )
        candidate = detail.get("run", {}).get("content", {}).get("candidate_set", {})
        outputs = {o["key"]: o for o in detail.get("outputs", []) if o.get("key")}

        # ---- 6. review the divergence — DETERMINISTIC, not model-asserted ---
        show(
            "STEP 6 — review what would change",
            "the divergence is computed by the runtime, not claimed by the model",
        )
        marks = {
            "changed": (YELLOW, "~ changed"),
            "unchanged": (GREY, "· unchanged"),
            "added": (GREEN, "+ added"),
            "removed": (RED, "- removed"),
        }
        changed_keys = []
        for row in candidate.get("divergence", []):
            color, label = marks.get(row.get("change"), (GREY, row.get("change", "?")))
            print(f"    {paint(label, color)}  {paint(row['key'], BOLD)}")
            if row.get("change") == "changed":
                changed_keys.append(row["key"])

        for key in changed_keys:
            new_text = (outputs.get(key, {}).get("content") or {}).get("text", "")
            cites = outputs.get(key, {}).get("citations") or []
            print(
                paint(f"\n  {key} — proposed change  ", BOLD, CYAN)
                + paint(
                    f"cites {len(cites)} record(s): " + ", ".join(short(c) for c in cites), GREY
                )
            )
            await reveal_diff(SKILL_SECTIONS.get(key, ""), new_text)

        if not changed_keys:
            print(
                paint(
                    "\n  the candidate reproduced every section unchanged — nothing to promote.",
                    YELLOW,
                )
            )
            note("with a real provider, sharpen the evidence or re-run; there is nothing to ship.")
            return

        # ---- the interactive gate: YOU decide ------------------------------
        show(
            "YOUR CALL",
            "the model proposed this — nothing is live yet. Promotion is the deliberate human step.",
        )
        note("decline and the drafts stay in the audit trail; the active skill never changed.")
        answer = await ask(paint("\n  Promote this candidate to the live playbook? [y/N] ", BOLD))

        if answer not in {"y", "yes"}:
            show(
                "DECLINED",
                "the live skill is untouched; the candidate remains in history for later review",
            )
            note("this is exactly what protects production from one persuasive message.")
            return

        # ---- 7. promote atomically, then render the prompt -----------------
        # `skills` requires embedding_v1, and promotion refuses a source row that
        # is not enriched — so wait for the drafts rather than racing the worker.
        await spin(
            "worker enriching the draft sections",
            cyc.wait_ready([o["id"] for o in detail.get("outputs", []) if o.get("id")]),
        )
        result = await spin(
            "promoting all sections atomically",
            client.promote(
                entity=ENTITY, source_run_id=detail["run"]["id"], artifact=SKILL_ARTIFACT
            ),
        )
        promotion_run = result.get("promotion_run_id")
        print(
            paint(
                f"  promoted {result.get('promoted')} section(s), skipped {result.get('skipped')} — one transaction, full history kept",
                GREEN,
            )
        )

        show(
            "THE PLAYBOOK THE ASSISTANT NOW USES",
            "rendered deterministically from the promoted records — no model call",
        )
        rendered = await client.render_artifact(SKILL_ARTIFACT, entity=ENTITY)
        await typewriter(rendered.get("rendered", ""), CYAN, indent="  ", delay=0.003)

        note(
            f"\n  {len(changed_keys)} of 3 sections changed — each traceable through its citations back to"
        )
        note("  the upset customer, the evaluator's score, and the support lead's correction that")
        note(
            "  justified it. The assistant now apologizes for billing errors instead of upselling."
        )

        # ---- 8. the loop is closed: the next bind names the promotion -------
        show(
            "STEP 8 — bind again", "the same call as step 2 — but the base version is now nameable"
        )
        after = await spin(
            "re-rendering the prompt and re-resolving the learning target", cyc.bind_use()
        )
        next_target = dict(after.learning_target or {})
        next_base = next_target.get("base_run_id")
        for head in next_target.get("heads", []):
            print(
                f"    {paint('·', GREY)} {paint(head['key'].ljust(10), BOLD)} "
                f"{paint('record ' + short(head['record_id']), GREY)}  "
                f"{paint('run ' + short(head['run_id']), GREY)}"
            )
        print(
            f"\n    {paint('base_run_id'.ljust(14), GREY)}"
            f"{paint(short(next_base), BOLD, GREEN) if next_base else paint('null', YELLOW)}"
        )
        print(f"    {paint('promotion run'.ljust(14), GREY)}{short(promotion_run)}")
        if next_base and next_base == promotion_run:
            print(
                paint(
                    "\n  ✓ every head shares the promotion run, so that run IS the exact base",
                    GREEN,
                )
            )
            note("from here, feedback on a reply names the version that actually shipped. A")
            note("candidate is rebased on what influenced the run — not on whatever happens")
            note("to be active when the complaint finally lands.")
        note("\n  a use is operational metadata and expires (ARTIFACT_USE_RETENTION_DAYS); this")
        note(f"  one at {after.expires_at}. The signals and the snapshot are canonical")
        note("  records and keep their own retention, so retiring a handle takes no history.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except MemseekHTTPError as error:
        raise SystemExit(paint(f"\nAPI error: {error}", RED)) from error
    except (RuntimeError, TimeoutError) as error:
        raise SystemExit(paint(f"\n{error}", YELLOW)) from error
    except KeyboardInterrupt:
        raise SystemExit(paint("\ninterrupted", GREY)) from None
