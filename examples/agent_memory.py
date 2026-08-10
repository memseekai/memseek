"""L0 → L1 → L2 → L3: a four-layer agent memory you can watch build itself.

This is the executable counterpart to docs/agent-memory-example.md. It publishes
the isolated ``examples/agent_memory_catalog`` and then feeds one agent a real
conversation, so you can watch the ladder assemble itself, layer by layer.

Each layer stores one thing and exists for one use. That is the whole design:

    L0  messages   CONVERSATION  raw conversations with full context
        │                        → verify exact wording, timestamps, and sources
        │  l1_extract       segment → recall → dedup
    L1  memories   ATOM          facts, preferences, constraints, and events
        │                        → precise recall of actionable information
        │  scene_synthesis  consolidate into scenarios
    L2  scenes     SCENARIO      knowledge blocks per project or scenario
        │                        → quickly restore a working context
        │  persona          distil what is stable
    L3  persona    CORE          long-term profile, stable patterns, cognition
                                 → let an agent enter a user's context fast

L0 and L1 are immutable events: a claim is never edited, only superseded by a
later claim that cites it. L2 is a bounded set of keyed Markdown scene blocks:
each project, method, or situation has its own current head, so a block can be
updated, merged, or retracted without hiding its neighbours. L3 is keyed traits,
one head per trait.

Nothing in that cascade is scheduled by this script: writing a message makes the
worker fire ``l1_extract``. New L1 memories then update scene blocks, and changed
scene blocks promote stable cross-context patterns into ``persona``. The script
only watches the audited runs land.

Then it does the two things the layers exist for. It renders one bounded context
prompt for a request that must be refused — "rewrite billing in Fastify and deploy
it tonight" — and it opens a single durable trait all the way back down to the
message that produced it, which is the property the whole design is for.

Finally it runs the parallel Skill pipeline: a completed task trace becomes a
reviewed four-section procedure that only takes effect when you promote it.

Run it against a local stack with a real provider (LLM_FAKE=1 cannot invent
citation UUIDs, so every derivation would honestly no-op):

    make database && source .env.sh
    export OPENAI_API_KEY=sk-...                     # models.yaml api_key_env
    uv run memseek migrate
    uv run uvicorn memseek.api:app &                 # terminal A
    uv run memseek worker &                          # terminal B
    export AGENT_MEMORY_RUN_ID=demo                  # stable web lookup
    uv run python examples/agent_memory.py           # terminal C

Set MEMSEEK_API_KEY to reuse a workspace, or DATABASE_URL to create a fresh
disposable one. The interactive web walkthrough can load the reusable workspace's
timeline when it is given the same API key and AGENT_MEMORY_RUN_ID. Piping stdin
runs a short scripted tour instead of the prompt, which makes this a convenient
smoke walkthrough.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import sys
import textwrap
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from _workspace_explorer import print_workspace_explorer

from memseek.config import get_settings
from memseek.sdk import MemseekClient, MemseekHTTPError

RUN = os.environ.get("AGENT_MEMORY_RUN_ID") or secrets.token_hex(3)
if not (
    1 <= len(RUN) <= 32 and all(char.isascii() and (char.isalnum() or char in "_-") for char in RUN)
):
    raise SystemExit("AGENT_MEMORY_RUN_ID must contain 1-32 ASCII letters, digits, '-' or '_'")
AGENT = f"agent.alice-{RUN}"
SKILL = f"skill.duplicate-charges-{RUN}"
PACKAGE = "agent_memory@0.3.0"
CATALOG_ROOT = Path(__file__).resolve().parent / "agent_memory_catalog"

# Terminal styling and animation are TTY-only, and honour NO_COLOR, like the
# other interactive examples in this directory.
_TTY = (
    sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"
)


def _style(*codes: int) -> str:
    return ("\033[" + ";".join(map(str, codes)) + "m") if _TTY else ""


RESET, BOLD, DIM, STRIKE = _style(0), _style(1), _style(2), _style(9)
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GREY = (
    _style(31),
    _style(32),
    _style(33),
    _style(34),
    _style(35),
    _style(36),
    _style(90),
)
BCYAN, BMAGENTA, BYELLOW = _style(96), _style(95), _style(93)

TIER_COLOR = {"L0": GREY, "L1": GREEN, "L2": CYAN, "L3": BMAGENTA}

# What each layer stores, and the one thing it exists for. Printed with the ladder
# so a reader never has to infer a layer's purpose from its contents.
TIER_PURPOSE = {
    "L0": ("conversation", "verify exact wording, timestamps, and sources"),
    "L1": ("atom", "precise recall of actionable information"),
    "L2": ("scenario", "quickly restore a working context"),
    "L3": ("core", "enter a user's and team's context fast"),
}


def paint(value: str, *styles: str) -> str:
    return ("".join(styles) + value + RESET) if _TTY else value


def rule(char: str = "─", width: int = 78) -> str:
    return paint(char * width, GREY)


def short(value: object | None) -> str:
    return str(value)[:8] if value else "—"


_ESCAPES = ((r"\u003c", "<"), (r"\u003e", ">"), (r"\u0026", "&"))


def clean(value: object | None) -> str:
    """Undo the render escaping of a value for display.

    Rendered values arrive with `&`, `<`, and `>` escaped, because record content
    must never be able to close or forge an element in the prompt an author wrote
    around it. A terminal is not a prompt, so this walkthrough shows the original
    characters.
    """

    text = str(value or "")
    for escape, character in _ESCAPES:
        text = text.replace(escape, character)
    return text


def clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1].rstrip() + "…"


def title(heading: str, detail: str = "") -> None:
    print(f"\n{rule('━')}")
    line = f"  {paint(heading, BOLD, BCYAN)}"
    if detail:
        line += paint(f"   {detail}", GREY)
    print(line)
    print(rule("━"))


def note(message: str) -> None:
    print(paint(f"  · {message}", GREY))


def warn(message: str) -> None:
    print(paint(f"  ! {message}", YELLOW))


def wrapped(message: str, *, indent: str = "      ", width: int = 72) -> None:
    for line in textwrap.wrap(message, width=width) or [""]:
        print(indent + line)


async def ainput(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


async def beat(seconds: float = 0.06) -> None:
    """A short pause that only exists when someone is watching."""

    if _TTY:
        await asyncio.sleep(seconds)


class Spinner:
    """A compact wait indicator while worker-owned work lands."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str) -> None:
        self.label = label
        self._task: asyncio.Task[None] | None = None
        self._started = 0.0

    async def _animate(self) -> None:
        frame = 0
        while True:
            elapsed = asyncio.get_running_loop().time() - self._started
            glyph = paint(self.FRAMES[frame % len(self.FRAMES)], CYAN)
            clock = paint(f"({elapsed:4.1f}s)", GREY)
            sys.stdout.write(f"\r  {glyph} {self.label} {clock}\033[K")
            sys.stdout.flush()
            frame += 1
            await asyncio.sleep(0.1)

    async def __aenter__(self) -> Spinner:
        if _TTY:
            self._started = asyncio.get_running_loop().time()
            self._task = asyncio.create_task(self._animate())
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


async def climb(from_tier: str, to_tier: str, label: str) -> None:
    """Animate one step up the ladder, so the cascade is visible as motion."""

    if not _TTY:
        print(paint(f"  {from_tier} → {to_tier}  {label}", GREY))
        return
    src = paint(from_tier, TIER_COLOR[from_tier], BOLD)
    dst = paint(to_tier, TIER_COLOR[to_tier], BOLD)
    for step in range(9):
        arrow = paint("─" * step + "▶", TIER_COLOR[to_tier])
        sys.stdout.write(f"\r  {src} {arrow:<12} {dst}   {paint(label, GREY)}\033[K")
        sys.stdout.flush()
        await asyncio.sleep(0.045)
    print()


# ---------------------------------------------------------------------------
# The conversation. Two sessions, deliberately shaped so the second one forces
# the dedup pass to make every decision it can make: one hard rule restated
# (skip), one plan that moved (merge + supersede), and genuinely new evidence
# (store).
# ---------------------------------------------------------------------------

BASE = datetime.now(UTC) - timedelta(days=9)

SESSION_ONE = "s1-platform-review"
SESSION_TWO = "s2-mobile-slip"

MESSAGES: tuple[dict[str, Any], ...] = (
    {
        "session": SESSION_ONE,
        "role": "user",
        "at": BASE,
        "text": (
            "For context on how we build: every new Node service goes out on Fastify now. "
            "The billing API is the one exception — it's still Express because the mobile "
            "app depends on its old response format, so don't migrate billing yet."
        ),
    },
    {
        "session": SESSION_ONE,
        "role": "assistant",
        "at": BASE + timedelta(minutes=1),
        "text": (
            "Understood — Fastify for new services, and billing stays on Express until the "
            "mobile dependency is gone."
        ),
    },
    {
        "session": SESSION_ONE,
        "role": "user",
        "at": BASE + timedelta(minutes=2),
        "text": (
            "One hard rule: never deploy billing changes to production without my explicit "
            "approval. Writing the code is fine, shipping it is not."
        ),
    },
    {
        "session": SESSION_ONE,
        "role": "user",
        "at": BASE + timedelta(minutes=4),
        "text": (
            "The mobile team is planning to drop the legacy fields in mobile release 7, "
            "which should land next month. Production runs PostgreSQL, and migrations are "
            "never applied automatically during a deploy."
        ),
    },
    {
        "session": SESSION_ONE,
        "role": "user",
        "at": BASE + timedelta(minutes=6),
        "text": (
            "Before we remove any compatibility path I want telemetry showing the old fields "
            "have zero traffic. I've been burned by 'nobody uses that' twice this year."
        ),
    },
    {
        "session": SESSION_ONE,
        "role": "assistant",
        "at": BASE + timedelta(minutes=7),
        "text": (
            "Noted: telemetry evidence first, then removal, and billing deploys wait for your "
            "approval."
        ),
    },
)

FOLLOW_UP: tuple[dict[str, Any], ...] = (
    {
        "session": SESSION_TWO,
        "role": "user",
        "at": BASE + timedelta(days=6),
        "text": (
            "Update from mobile: the legacy-field removal slipped out of release 7 and is now "
            "planned for release 8, expected in October. Billing has to stay compatible until "
            "then."
        ),
    },
    {
        "session": SESSION_TWO,
        "role": "user",
        "at": BASE + timedelta(days=6, minutes=2),
        "text": (
            "Reminder because a contractor got this wrong last week: billing changes never go "
            "to production without my explicit approval."
        ),
    },
    {
        "session": SESSION_TWO,
        "role": "user",
        "at": BASE + timedelta(days=6, minutes=5),
        "text": (
            "Also new: we finished moving the reporting database off MySQL, so everything is "
            "PostgreSQL now. And I want a rollback plan attached to any billing deploy."
        ),
    },
)

TASK_TRACE = (
    "TASK: checkout service creates duplicate charges after gateway timeouts.\n"
    "[user] Customers are being charged twice when checkout times out.\n"
    "[assistant] I'll correlate the duplicates by logical payment request first.\n"
    "[tool search_logs] order_id=ORDER-481 → two payment_attempts rows, 31s apart, "
    "same amount, different provider_txn_id; the first attempt timed out at 30s.\n"
    "[assistant] Both attempts carry the same Idempotency-Key, so key derivation is fine. "
    "Checking retention next.\n"
    "[tool read_file src/middleware/idempotency.ts] IDEMPOTENCY_TTL_SECONDS = 30\n"
    "[assistant] The key expires at 30s but the retry horizon is 90s, so the second attempt "
    "passed the existence check. Raising retention to 24h.\n"
    "[tool run_tests payments] 41 passed. Added a regression test that replays the same "
    "request after a simulated timeout: it failed before the change and passes now.\n"
    "[tool sql SELECT count(*) FROM payment_attempts WHERE order_id='ORDER-481'] 1\n"
    "[assistant] One provider charge, one internal payment record, retry returns the original "
    "result. Ledger and payment suites are green.\n"
    "RESULT: resolved — idempotency retention must cover the full retry horizon."
)


class Memory:
    """The SDK calls this walkthrough needs, and nothing else."""

    def __init__(self, client: MemseekClient, *, live_model: bool) -> None:
        self.client = client
        self.live_model = live_model
        self._sequence = 0

    def _dedupe_key(self, kind: str) -> str:
        self._sequence += 1
        return f"agent-memory:{RUN}:{kind}:{self._sequence}"

    @staticmethod
    def _ids(result: dict[str, Any]) -> list[str]:
        rows = result.get("inserted", []) + result.get("duplicates", [])
        return [str(row["id"]) for row in rows]

    # -- L0 -----------------------------------------------------------------

    async def ingest_messages(self, messages: Sequence[dict[str, Any]]) -> list[str]:
        payload = []
        for ordinal, message in enumerate(messages):
            payload.append(
                {
                    "collection": "messages",
                    "entity": AGENT,
                    "type": "message",
                    "text": message["text"],
                    "occurred_at": message["at"].isoformat(),
                    "content": {
                        "text": message["text"],
                        "role": message["role"],
                        "session_id": message["session"],
                        "ordinal": ordinal,
                    },
                    "dedupe_key": self._dedupe_key("message"),
                }
            )
        return self._ids(await self.client.records.ingest_many(payload))

    async def say(self, text: str, *, session: str, ordinal: int) -> str:
        result = await self.client.records.ingest(
            collection="messages",
            entity=AGENT,
            type="message",
            text=text,
            occurred_at=datetime.now(UTC).isoformat(),
            content={
                "text": text,
                "role": "user",
                "session_id": session,
                "ordinal": ordinal,
            },
            dedupe_key=self._dedupe_key("message"),
        )
        return self._ids(result)[0]

    async def ingest_trace(self, text: str) -> str:
        result = await self.client.records.ingest(
            collection="traces",
            entity=SKILL,
            type="trace",
            text=text,
            occurred_at=datetime.now(UTC).isoformat(),
            content={"text": text, "outcome": "resolved", "task_ref": f"demo-{RUN}"},
            dedupe_key=self._dedupe_key("trace"),
        )
        return self._ids(result)[0]

    async def wait_ready(
        self, record_ids: Sequence[str], label: str, timeout_s: float = 120.0
    ) -> None:
        pending = list(record_ids)
        total = len(pending)
        deadline = asyncio.get_running_loop().time() + timeout_s
        async with Spinner(label) as spinner:
            while pending:
                record = await self.client.record(pending[-1])
                if record.get("ready"):
                    pending.pop()
                    spinner.label = f"{label} — {total - len(pending)}/{total} enriched"
                    continue
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("records were not enriched — is `memseek worker` running?")
                await asyncio.sleep(0.4)

    # -- runs ---------------------------------------------------------------

    async def run_ids(self, processor: str, *, entity: str | None = None) -> set[str]:
        runs = await self.client.runs(
            entity=entity or AGENT, processor=processor, operation="derive", limit=100
        )
        return {str(run["id"]) for run in runs.get("runs", [])}

    async def wait_run(
        self,
        processor: str,
        known: set[str],
        label: str,
        *,
        entity: str | None = None,
        timeout_s: float = 240.0,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Wait for one fresh audited run of a worker-fired pipeline."""

        deadline = asyncio.get_running_loop().time() + timeout_s
        async with Spinner(label):
            while True:
                runs = await self.client.runs(
                    entity=entity or AGENT, processor=processor, operation="derive", limit=100
                )
                fresh = next(
                    (run for run in runs.get("runs", []) if str(run["id"]) not in known), None
                )
                if fresh is not None:
                    detail = await self.client.run(str(fresh["id"]))
                    content = (detail.get("run") or {}).get("content") or {}
                    outputs = list(detail.get("outputs") or [])
                    if outputs:
                        await self.wait_ready(
                            [str(output["id"]) for output in outputs], "enriching new records"
                        )
                    return content, outputs
                if asyncio.get_running_loop().time() >= deadline:
                    return None
                await asyncio.sleep(0.6)

    # -- reads --------------------------------------------------------------

    async def view(self, name: str, **params: Any) -> dict[str, Any]:
        return await self.client.query_view(name, **params)

    async def memories(self) -> list[dict[str, Any]]:
        result = await self.view("memory_audit", entity=AGENT)
        return list(result.get("hits") or [])

    async def instructions(self, min_priority: float = 80) -> list[dict[str, Any]]:
        result = await self.view("standing_instructions", entity=AGENT, min_priority=min_priority)
        return list(result.get("hits") or [])

    async def session(self, session_id: str) -> list[dict[str, Any]]:
        result = await self.view("session_window", entity=AGENT, session=session_id)
        return list(result.get("hits") or [])

    async def beliefs(self, collection: str, *, entity: str | None = None) -> list[dict[str, Any]]:
        document = await self.client.document(entity=entity or AGENT, collections=collection)
        return list(document.get("beliefs") or [])

    async def scene_blocks(self) -> list[dict[str, Any]]:
        """Return the real L2 blocks, never the implementation's navigation index."""

        beliefs = await self.beliefs("scenes")
        records = await asyncio.gather(
            *(self.client.record(str(belief["id"])) for belief in beliefs),
        )
        return sorted(records, key=lambda record: str(record.get("key") or ""))

    async def message_count(self) -> int:
        timeline = await self.client._request(
            "GET",
            "/timeline",
            params={"entity": AGENT, "collections": "messages", "types": "message", "limit": 100},
        )
        return len(timeline.get("records") or [])

    async def counts(self) -> dict[str, Any]:
        rows = await self.memories()
        known = {str(hit.get("id")) for hit in rows}
        superseded = {
            str(parent)
            for hit in rows
            for parent in (hit.get("fields") or {}).get("supersedes") or []
            if str(parent) in known
        }
        scenes = await self.scene_blocks()
        return {
            "messages": await self.message_count(),
            "memories": len(rows),
            "superseded": len(superseded),
            "scenes": len(scenes),
            "traits": len(await self.beliefs("persona")),
        }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

KIND_GLYPH = {"persona": ("◆", BLUE), "episodic": ("●", GREEN), "instruction": ("▲", RED)}


def priority_bar(priority: float | None, width: int = 10) -> str:
    if priority is None:
        return " " * (width + 1)
    filled = max(0, min(width, round(float(priority) / 100 * width)))
    colour = RED if priority >= 90 else YELLOW if priority >= 70 else GREY
    return paint("█" * filled, colour) + paint("░" * (width - filled), GREY) + " "


def render_messages(messages: Sequence[dict[str, Any]]) -> None:
    for message in messages:
        role = message["role"]
        colour = BCYAN if role == "user" else GREY
        print(f"    {paint(role.rjust(9), colour, BOLD)}  {paint(message['session'], GREY)}")
        wrapped(message["text"], indent="               ")


async def render_ladder(memory: Memory, *, animate: bool = True) -> None:
    counts = await memory.counts()
    rows = (
        ("L3", "persona", "◆" * counts["traits"], f"{counts['traits']} durable trait(s)"),
        ("L2", "scenes", "▣" * counts["scenes"], f"{counts['scenes']} scenario block(s)"),
        (
            "L1",
            "memories",
            "●" * min(counts["memories"], 24),
            f"{counts['memories']} atomic claim(s)"
            + (f", {counts['superseded']} superseded" if counts["superseded"] else ""),
        ),
        ("L0", "messages", "▪" * min(counts["messages"], 24), f"{counts['messages']} message(s)"),
    )
    print()
    for tier, name, glyphs, detail in rows:
        colour = TIER_COLOR[tier]
        line = (
            f"  {paint(tier, colour, BOLD)}  {paint(name.ljust(9), colour)}"
            f"{paint(glyphs.ljust(26), colour)}{paint(detail, GREY)}"
        )
        print(line)
        role, purpose = TIER_PURPOSE[tier]
        print(paint(f"      {role.ljust(13)}→ {purpose}", GREY))
        if animate:
            await beat(0.05)


def render_memories(rows: Sequence[dict[str, Any]], *, limit: int = 40) -> None:
    if not rows:
        note("no atomic memories yet")
        return
    # Only a claim can be superseded by a claim. A model may name an evidence id
    # here — that passes citation validation but means nothing, so it is ignored.
    known = {str(hit.get("id")) for hit in rows}
    superseded = {
        str(parent)
        for hit in rows
        for parent in (hit.get("fields") or {}).get("supersedes") or []
        if str(parent) in known
    }
    for hit in rows[:limit]:
        fields = hit.get("fields") or {}
        kind = clean(fields.get("memory_kind")) or "episodic"
        glyph, colour = KIND_GLYPH.get(kind, ("●", GREEN))
        priority = fields.get("priority")
        text = clip(clean(hit.get("text")), 96)
        dead = str(hit.get("id")) in superseded
        body = paint(text, GREY, STRIKE) if dead else paint(text, colour)
        print(
            f"    {priority_bar(float(priority) if priority is not None else None)}"
            f"{paint(glyph, colour)} {paint(short(hit.get('id')), BOLD)} "
            f"{paint(kind.ljust(11), GREY)} {body}"
        )
        detail = []
        if fields.get("scene_name"):
            detail.append(f"topic “{clip(clean(fields['scene_name']), 44)}”")
        if clean(fields.get("decision")) == "merge":
            detail.append("merged")
        for parent in fields.get("supersedes") or []:
            detail.append(f"supersedes {short(parent)}")
        if dead:
            detail.append("superseded — kept for audit, absent from the current read")
        if detail:
            print(paint("                   " + " · ".join(detail), GREY))
    if len(rows) > limit:
        note(f"{len(rows) - limit} more")


def render_instructions(rows: Sequence[dict[str, Any]], floor: float) -> None:
    if not rows:
        note(f"no standing instruction at priority ≥ {floor:g}")
        return
    for hit in rows:
        fields = hit.get("fields") or {}
        priority = float(fields.get("priority") or 0)
        stamp = paint(f"p{priority:>3.0f}", BOLD, RED if priority >= 90 else YELLOW)
        print(f"    {stamp}  {paint(clip(clean(hit.get('text')), 100), BOLD)}")


def _scene_title(key: object | None) -> str:
    return str(key or "untitled-scene").replace("_", " ").replace("-", " ").title()


def _scene_body(value: object | None) -> str:
    text = clean(value)
    marker = "-----META-END-----"
    return text.partition(marker)[2].strip() if marker in text else text.strip()


def render_scene_blocks(scenes: Sequence[dict[str, Any]]) -> None:
    if not scenes:
        note("no scenario blocks yet")
        return
    print(paint(f"    {len(scenes)} scenario block(s)", BOLD, CYAN))
    for scene in scenes:
        content = scene.get("content") or {}
        print(f"\n    {paint('▣ ' + _scene_title(scene.get('key')), BOLD, CYAN)}")
        for line in _scene_body(content.get("text")).splitlines():
            if line.startswith("## "):
                print(paint(f"      {line[3:]}", BOLD, CYAN))
            elif line.strip():
                wrapped(line.strip(), indent="        ")
        cited = ", ".join(short(item) for item in (scene.get("citations") or [])[:6])
        print(paint(f"      cites {cited}", GREY))


def render_traits(beliefs: Sequence[dict[str, Any]], *, changed: set[str] | None = None) -> None:
    if not beliefs:
        note("no persona traits yet")
        return
    for belief in sorted(beliefs, key=lambda item: str(item.get("key"))):
        key = str(belief.get("key"))
        mark = paint(" ← changed by this run", BYELLOW) if changed and key in changed else ""
        print(f"    {paint('◆', BMAGENTA)} {paint(key, BOLD, BMAGENTA)}{mark}")
        wrapped(clean(belief.get("text")))


def render_run(content: dict[str, Any], *, label: str) -> None:
    status = str(content.get("status") or "unknown")
    colour = {"ok": GREEN, "noop": GREY, "review": YELLOW}.get(status, RED)
    usage = content.get("usage") or {}
    tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    reasons = ", ".join(content.get("trigger_reasons") or []) or "manual"
    print(
        f"  {paint('▸', colour)} {paint(label, BOLD)} "
        + paint(
            f"{status} · {len(content.get('output_ids') or [])} record(s) · {tokens} tokens · "
            f"{content.get('ms', 0)} ms · fired by {reasons}",
            GREY,
        )
    )
    if content.get("error"):
        warn(f"{content.get('error_kind')}: {content.get('error')}")
    divergence = (content.get("candidate_set") or {}).get("divergence") or []
    if divergence:
        parts = []
        for item in divergence:
            state = str(item.get("change") or "?")
            colour = {"added": GREEN, "changed": YELLOW, "removed": RED}.get(state, GREY)
            parts.append(paint(f"{item.get('key')}:{state}", colour))
        print("    " + paint("divergence  ", GREY) + " ".join(parts))


def render_wave(
    outputs: Sequence[dict[str, Any]], *, messages: int, had_memory: bool = True
) -> None:
    """State what the dedup pass actually did, from the records it wrote."""

    merges = [
        output for output in outputs if (output.get("content") or {}).get("decision") == "merge"
    ]
    replaced = sum(len((output.get("content") or {}).get("supersedes") or []) for output in merges)
    if not merges:
        replaced = 0
    print(
        "  "
        + paint("dedup verdict  ", GREY)
        + paint(f"{len(outputs)} written", GREEN)
        + paint(" · ", GREY)
        + paint(f"{len(merges)} merged over {replaced} earlier claim(s)", YELLOW)
        + paint(f" · from {messages} new message(s)", GREY)
    )
    if had_memory:
        note("a skipped candidate leaves no record at all: that is what skip means here")


def changed_keys(content: dict[str, Any]) -> set[str]:
    divergence = (content.get("candidate_set") or {}).get("divergence") or []
    return {
        str(item.get("key"))
        for item in divergence
        if str(item.get("change")) in {"added", "changed"}
    }


async def glass_box(
    memory: Memory,
    record: dict[str, Any],
    *,
    seen: set[str] | None = None,
    depth: int = 0,
) -> None:
    """Open one derived value all the way down to the words that caused it."""

    seen = seen if seen is not None else set()
    record_id = str(record.get("id"))
    if record_id in seen or depth > 5:
        return
    seen.add(record_id)
    content = record.get("content") or {}
    tier = f"{record.get('collection')}/{record.get('type')}"
    label = {"messages": "L0", "memories": "L1", "scenes": "L2", "persona": "L3"}.get(
        str(record.get("collection")), "  "
    )
    colour = TIER_COLOR.get(label, GREY)
    indent = "    " + "   " * depth
    elbow = paint("└─ ", GREY) if depth else ""
    text = str(content.get("text") or "")
    if str(record.get("collection")) == "scenes":
        scenes = content.get("scenes") or []
        text = " / ".join(clean(scene.get("title")) for scene in scenes) or text
    print(
        f"{indent}{elbow}{paint(label, colour, BOLD)} {paint(short(record_id), BOLD)} "
        + paint(f"{tier} · depth {record.get('depth')}", GREY)
    )
    key = record.get("key")
    prefix = f"{paint(str(key), CYAN)}: " if key else ""
    wrapped(prefix + clean(text), indent=indent + "       ")
    run_id = record.get("run_id")
    parents = [
        parent
        for parent in (record.get("derived_from") or [])
        if run_id is None or str(parent) != str(run_id)
    ]
    for parent in parents[:3]:
        try:
            upstream = await memory.client.record(str(parent))
        except MemseekHTTPError:
            continue
        await glass_box(memory, upstream, seen=seen, depth=depth + 1)


# ---------------------------------------------------------------------------
# the walkthrough
# ---------------------------------------------------------------------------


async def cascade(memory: Memory, *, label: str) -> dict[str, dict[str, Any]]:
    """Watch the worker climb the ladder after new messages land."""

    known = {
        name: await memory.run_ids(name) for name in ("l1_extract", "scene_synthesis", "persona")
    }
    results: dict[str, dict[str, Any]] = {}

    await climb("L0", "L1", "l1_extract — segment, recall, dedup")
    landed = await memory.wait_run(
        "l1_extract", known["l1_extract"], f"extracting claims ({label})"
    )
    if landed is None:
        warn("l1_extract did not run — is `memseek worker` running?")
        return results
    content, outputs = landed
    render_run(content, label="l1_extract")

    # A model that miscopies one citation UUID loses the whole Candidate Set: the
    # engine will not store a claim whose evidence it cannot verify. The cursor did
    # not advance, so the same messages can simply be re-extracted.
    attempt = 1
    while str(content.get("status")) == "failed" and attempt <= 3:
        note("the whole candidate set was rejected — nothing partial was stored")
        note("the pipeline cursor did not move, so the same messages can be re-extracted")
        known["l1_extract"] |= {str(content.get("run_id"))}
        await memory.client.run_processor("l1_extract", entity=AGENT)
        landed = await memory.wait_run(
            "l1_extract", known["l1_extract"], f"re-extracting claims (attempt {attempt + 1})"
        )
        if landed is None:
            return results
        content, outputs = landed
        render_run(content, label="l1_extract")
        attempt += 1

    results["l1_extract"] = content
    results["_l1_outputs"] = {"records": outputs}
    if not outputs:
        note(
            "the run committed nothing: every candidate was already covered, or the model declined"
        )
        if not memory.live_model:
            warn("LLM_FAKE=1 cannot cite record UUIDs, so extraction honestly no-ops")
        return results

    await climb("L1", "L2", "scene_synthesis — update scene blocks")
    scene_outputs: list[dict[str, Any]] = []
    landed = await memory.wait_run(
        "scene_synthesis", known["scene_synthesis"], "consolidating scenes"
    )
    if landed is None:
        warn("scene_synthesis did not run")
    else:
        content, scene_outputs = landed
        results["scene_synthesis"] = content
        render_run(content, label="scene_synthesis")
        if not scene_outputs:
            note("no scene block needed to change")

    if not scene_outputs or not any(
        (output.get("content") or {}).get("tombstone") is not True for output in scene_outputs
    ):
        note("no changed scene block needed a persona update")
        return results

    await climb("L2", "L3", "persona — distil changed scene blocks")
    landed = await memory.wait_run("persona", known["persona"], "updating durable traits")
    if landed is None:
        warn("persona did not run")
        return results
    content, _ = landed
    results["persona"] = content
    render_run(content, label="persona")
    return results


async def act_one(memory: Memory) -> None:
    title(
        "ACT I · L0 — WHAT WAS LITERALLY SAID",
        "immutable evidence, written before anything reads it",
    )
    note(f"entity {AGENT}")
    render_messages(SESSION_ONE_VIEW := MESSAGES)
    ids = await memory.ingest_messages(SESSION_ONE_VIEW)
    await memory.wait_ready(ids, "embedding messages")
    print(paint(f"\n  {len(ids)} message(s) stored, ordered, and searchable", GREEN))

    title("ACT II · THE CASCADE", "one write, three layers — none of it scheduled here")
    runs = await cascade(memory, label="session 1")

    if "l1_extract" in runs:
        print()
        render_wave(
            (runs.get("_l1_outputs") or {}).get("records") or [],
            messages=len(MESSAGES),
            had_memory=False,
        )
        print(paint("\n  L1 — atomic claims, priority-ranked, each citing its messages:", BOLD))
        render_memories(await memory.memories())
    if "scene_synthesis" in runs:
        print(paint("\n  L2 — independently maintained scene blocks:", BOLD))
        render_scene_blocks(await memory.scene_blocks())
    if "persona" in runs:
        print(paint("\n  L3 — what is stable across scenes:", BOLD))
        render_traits(await memory.beliefs("persona"), changed=changed_keys(runs["persona"]))

    await render_ladder(memory)


async def act_two(memory: Memory) -> None:
    title("ACT III · THE SECOND WAVE", "a restated rule, a plan that moved, and something new")
    note("the same three claims arrive again in different words — watch what is NOT written")
    render_messages(FOLLOW_UP)
    ids = await memory.ingest_messages(FOLLOW_UP)
    await memory.wait_ready(ids, "embedding messages")
    runs = await cascade(memory, label="session 2")

    print()
    render_wave((runs.get("_l1_outputs") or {}).get("records") or [], messages=len(FOLLOW_UP))
    print(paint("\n  L1 after the second wave — superseded claims struck through:", BOLD))
    render_memories(await memory.memories())
    note("a merged claim cites what it replaced, so supersession is provenance, not a flag")
    note("nothing was deleted: the superseded claim stays readable for audit")

    if "scene_synthesis" in runs:
        print(paint("\n  L2 — scene blocks updated:", BOLD))
        render_scene_blocks(await memory.scene_blocks())
    if "persona" in runs:
        moved = changed_keys(runs["persona"])
        heading = (
            "L3 — only the traits the new evidence changed were rewritten:"
            if moved
            else "L3 — unchanged: a project date moving is not a change of character:"
        )
        print(paint(f"\n  {heading}", BOLD))
        render_traits(await memory.beliefs("persona"), changed=moved)
    await render_ladder(memory)


async def act_recall(memory: Memory) -> None:
    title("ACT V · RECALL", "the request the memory has to push back on")
    request = "Rewrite billing in Fastify and deploy it tonight."
    print(f"    {paint('request', BOLD, BCYAN)}  {paint(request, BOLD)}")

    print(paint("\n  exact half — standing rules, structured, no relevance guessing:", BOLD))
    render_instructions(await memory.instructions(80), 80)

    print(paint("\n  relevance half — one question fused across all three layers:", BOLD))
    fused = await memory.view("memory_recall", entity=AGENT, task=request)
    rows = await memory.memories()
    known = {str(hit.get("id")) for hit in rows}
    superseded = {
        str(parent)
        for hit in rows
        for parent in (hit.get("fields") or {}).get("supersedes") or []
        if str(parent) in known
    }
    stale = [hit for hit in (fused.get("hits") or []) if str(hit.get("id")) in superseded]
    for hit in (fused.get("hits") or [])[:8]:
        origin = str(hit.get("collection") or "?")
        colour = {"persona": BMAGENTA, "scenes": CYAN, "memories": GREEN}.get(origin, GREY)
        mark = paint("  ← superseded", YELLOW) if str(hit.get("id")) in superseded else ""
        print(
            f"    {paint(origin.ljust(9), colour)} {paint(short(hit.get('id')), BOLD)} "
            + paint(clip(clean(hit.get("text")), 82), colour)
            + mark
        )
    if stale:
        warn(f"{len(stale)} superseded claim(s) still surfaced by relevance search")
        note("`where` filters a record's own declared fields, so no view can exclude")
        note('"claims some later claim superseded" — supersession is not a join here.')
        note("This is why the keyed layers, not L1, are the current-truth surfaces:")
        note("each L2 scene block and each L3 trait carry their own current head.")

    print(paint("\n  the assembled prompt — five blocks, each with its own budget:", BOLD))
    render = await memory.client.artifact("agent_context").render(
        {"entity": AGENT, "task": request, "skill": SKILL}
    )
    manifest = render.get("manifest") or {}
    for name, block in (manifest.get("blocks") or {}).items():
        state = (
            paint("omitted", YELLOW)
            if block.get("omitted")
            else paint("truncated", YELLOW)
            if block.get("truncated")
            else paint("packed", GREEN)
        )
        print(
            f"    {paint(name.ljust(13), BOLD)} {state}  "
            + paint(f"{block.get('tokens')} tokens · {len(block.get('ids') or [])} record(s)", GREY)
        )
    print(
        paint(
            f"    total {manifest.get('tokens')} tokens · sha256 "
            f"{str(manifest.get('rendered_sha256'))[:12]}…",
            GREY,
        )
    )
    print(rule())
    for line in str(render.get("rendered") or "").splitlines():
        print("  " + paint(line, GREY))
    print(rule())
    note(
        "a priority-100 rule reached the prompt through an exact filter, not a vector neighbourhood"
    )


async def act_glass_box(memory: Memory) -> None:
    title("ACT VI · GLASS BOX", "open one durable trait down to the words that caused it")
    traits = await memory.beliefs("persona")
    if not traits:
        warn("no trait to open yet")
        return
    preferred = next(
        (item for item in traits if item.get("key") == "interaction_protocol"), traits[0]
    )
    record = await memory.client.record(str(preferred["id"]))
    await glass_box(memory, record)
    note("L3 → L2 → L1 → L0. Every hop is a stored citation, validated when it was written.")


async def act_procedure(memory: Memory) -> None:
    title("ACT IV · THE PARALLEL PIPELINE", "a task trace becomes a reviewed procedure")
    note(f"the procedure gets its own entity: {SKILL}")
    known = await memory.run_ids("skill_extract", entity=SKILL)
    trace_id = await memory.ingest_trace(TASK_TRACE)
    await memory.wait_ready([trace_id], "embedding the task trace")
    landed = await memory.wait_run(
        "skill_extract", known, "generalizing the trace into a procedure", entity=SKILL
    )
    if landed is None:
        warn("skill_extract did not run")
        return
    content, outputs = landed
    render_run(content, label="skill_extract")
    if not outputs:
        note("no reusable procedure was proposed from this trace")
        return
    print(paint("\n  the candidate is a DRAFT — complete, cited, and inert until promoted:", BOLD))
    for output in outputs:
        key = str(output.get("key") or "?")
        body = clean((output.get("content") or {}).get("text"))
        print(f"\n    {paint('§ ' + key, BOLD, YELLOW)} {paint(short(output.get('id')), GREY)}")
        wrapped(body)
    run_id = str(content.get("run_id"))

    print()
    promoted = await memory.client.promote(
        entity=SKILL, source_run_id=run_id, artifact="maintained_procedure@1"
    )
    print(
        paint(
            f"  promoted {promoted.get('promoted', 0)} section(s) in promotion run "
            f"{short(promoted.get('promotion_run_id'))} — now the active procedure",
            GREEN,
        )
    )
    note("promotion copied the drafts into new active successors; the drafts are untouched")
    active = await memory.beliefs("procedures", entity=SKILL)
    for belief in sorted(active, key=lambda item: str(item.get("key"))):
        print(f"    {paint('✔', GREEN)} {paint(str(belief.get('key')), BOLD)}")

    print(paint("\n  and the loop the source design does not have:", BOLD))
    # Bound without a snapshot on purpose. A snapshot of this prompt sits at
    # provenance depth 4 — messages 0, memories 1, scenes 2, persona 3 — so a signal
    # citing it would be depth 5 and the default MAX_DERIVATION_DEPTH=4 rejects it.
    # Without a snapshot the signal carries the artifact identity and render hash as
    # metadata and claims no provenance edge, which is the documented trade: raise
    # the deployment limit if a four-layer ladder needs snapshot-cited signals.
    bound = await memory.client.artifact("agent_context").bind(
        {"entity": AGENT, "task": "Diagnose duplicate charges on checkout", "skill": SKILL},
        snapshot=False,
    )
    target = bound.learning_target or {}
    print(
        paint(
            f"    use {short(bound.id)} · render {bound.render_sha256[:12]}… · "
            f"target {target.get('artifact', {}).get('name', '—')} "
            f"base run {short(target.get('base_run_id'))}",
            GREY,
        )
    )
    signal = await memory.client.feedback.for_use(bound.id).correction(
        expected="Check the retry horizon before raising retention, and attach a rollback plan.",
        comment="The procedure skipped the rollback plan this user always requires.",
    )
    print(
        paint(
            f"    outcome stored as an ordinary {signal.get('type')} record "
            f"{short(signal.get('record_id'))} in {signal.get('collection')}",
            GREEN,
        )
    )
    note("that signal names the exact promoted heads that were in force, so the next")
    note("candidate revises the version that actually influenced the run")


# ---------------------------------------------------------------------------
# interactive surface
# ---------------------------------------------------------------------------

HELP = f"""
  {paint("say", BOLD, BCYAN)} <text>      append a message and watch the cascade climb
  {paint("recall", BOLD, BCYAN)} <task>   render the bounded context prompt for a request
  {paint("rules", BOLD, BCYAN)} [floor]   standing instructions at or above a priority floor
  {paint("memories", BOLD, BCYAN)}        L1 atom · every atomic claim, sharpest first
  {paint("scenes", BOLD, BCYAN)}          L2 scene blocks · one editable context document per key
  {paint("persona", BOLD, BCYAN)}         L3 core · the durable traits
  {paint("procedure", BOLD, BCYAN)}       the promoted procedure sections
  {paint("l0", BOLD, BCYAN)} [session]    L0 conversation · replay it exactly as it happened
  {paint("trace", BOLD, BCYAN)} <id>      open any record down to its evidence
  {paint("search", BOLD, BCYAN)} <query>  hybrid search across the memory layers
  {paint("answer", BOLD, BCYAN)} <q>      a cited answer from this agent's memory
  {paint("status", BOLD, BCYAN)}          the ladder
  {paint("help", BOLD, BCYAN)}  ·  {paint("quit", BOLD, BCYAN)}
"""


class Session:
    """Live conversation state for the interactive prompt."""

    def __init__(self) -> None:
        self.session_id = f"s3-live-{RUN}"
        self.ordinal = 0

    def next_ordinal(self) -> int:
        self.ordinal += 1
        return self.ordinal


async def handle(memory: Memory, live: Session, line: str) -> bool:
    line = line.strip()
    if not line:
        return True
    verb, _, argument = line.partition(" ")
    verb = verb.lower()
    argument = argument.strip()

    if verb in {"quit", "exit", "q"}:
        return False
    if verb in {"help", "?", "h"}:
        print(HELP)
        return True
    if verb in {"status", "s"}:
        await render_ladder(memory)
        return True
    if verb in {"memories", "m"}:
        render_memories(await memory.memories())
        return True
    if verb in {"scenes", "sc"}:
        render_scene_blocks(await memory.scene_blocks())
        return True
    if verb in {"persona", "p"}:
        render_traits(await memory.beliefs("persona"))
        return True
    if verb in {"procedure", "skill"}:
        beliefs = await memory.beliefs("procedures", entity=SKILL)
        if not beliefs:
            note("no promoted procedure yet")
        for belief in sorted(beliefs, key=lambda item: str(item.get("key"))):
            print(f"\n    {paint('§ ' + str(belief.get('key')), BOLD, GREEN)}")
            wrapped(clean(belief.get("text")))
        return True
    if verb == "rules":
        floor = float(argument) if argument else 80.0
        render_instructions(await memory.instructions(floor), floor)
        return True
    if verb == "l0":
        session_id = argument or SESSION_ONE
        rows = await memory.session(session_id)
        if not rows:
            note(f"no messages in session {session_id!r}")
        for hit in rows:
            fields = hit.get("fields") or {}
            role = str(fields.get("role") or "?")
            print(
                f"    {paint(str(fields.get('ordinal')).rjust(3), GREY)} "
                f"{paint(role.rjust(9), BCYAN if role == 'user' else GREY, BOLD)}  "
                + paint(clip(clean(hit.get("text")), 130), GREY)
            )
        return True
    if verb == "say":
        if not argument:
            warn("usage: say <text>")
            return True
        record_id = await memory.say(argument, session=live.session_id, ordinal=live.next_ordinal())
        print(f"  {paint('→', CYAN)} message {paint(short(record_id), BOLD)} stored")
        await memory.wait_ready([record_id], "embedding message")
        await cascade(memory, label="live")
        render_memories((await memory.memories())[:6])
        await render_ladder(memory)
        return True
    if verb == "recall":
        if not argument:
            warn("usage: recall <task>")
            return True
        render = await memory.client.artifact("agent_context").render(
            {"entity": AGENT, "task": argument, "skill": SKILL}
        )
        print(rule())
        for text_line in str(render.get("rendered") or "").splitlines():
            print("  " + paint(text_line, GREY))
        print(rule())
        return True
    if verb == "trace":
        if not argument:
            warn("usage: trace <record id prefix>")
            return True
        candidates = [
            *(await memory.memories()),
            *[{"id": item["id"]} for item in await memory.scene_blocks()],
            *[{"id": item["id"]} for item in await memory.beliefs("persona")],
        ]
        match = next(
            (item for item in candidates if str(item.get("id", "")).startswith(argument)), None
        )
        target = str(match["id"]) if match else argument
        try:
            record = await memory.client.record(target)
        except MemseekHTTPError:
            warn(f"no record starting with {argument!r}")
            return True
        await glass_box(memory, record)
        return True
    if verb == "search":
        if not argument:
            warn("usage: search <query>")
            return True
        result = await memory.view("memory_recall", entity=AGENT, task=argument)
        for hit in (result.get("hits") or [])[:10]:
            origin = str(hit.get("collection") or "?")
            print(
                f"    {paint(origin.ljust(9), GREY)} {paint(short(hit.get('id')), BOLD)} "
                + clip(clean(hit.get("text")), 110)
            )
        return True
    if verb == "answer":
        if not argument:
            warn("usage: answer <question>")
            return True
        if not memory.live_model:
            note("answer needs a real model; LLM_FAKE=1 cannot produce citations")
            return True
        # Scoped to this agent. Without `entities` the synthesis would range over
        # every entity in the answerable collections — including the procedure
        # entity, and in a real deployment every other customer.
        result = await memory.client.answer(question=argument, entities=[AGENT])
        wrapped(clean(result.get("answer")), indent="    ")
        cited = ", ".join(short(item) for item in (result.get("citations") or [])[:8])
        print(paint(f"    cites {cited}", GREY))
        for gap in result.get("gaps") or []:
            print(paint(f"    gap: {clean(gap)}", YELLOW))
        return True
    warn(f"unknown command {verb!r}; type help")
    return True


async def interactive(memory: Memory) -> None:
    title("ACT VII · YOUR TURN", "the agent is isolated; add memory or interrogate it")
    print(HELP)
    live = Session()
    while True:
        try:
            line = await ainput(paint("\n  memory ▸ ", BOLD, BCYAN))
        except EOFError, KeyboardInterrupt:
            print()
            break
        try:
            if not await handle(memory, live, line):
                break
        except MemseekHTTPError as error:
            print(paint(f"  API error: {error}", RED))
    print(paint("\n  Every layer stays auditable: nothing was overwritten, only superseded.", GREY))


async def scripted(memory: Memory) -> None:
    title("ACT VII · SCRIPTED TOUR", "stdin is not a TTY")
    live = Session()
    for command in ("status", "rules 90", "memories", "scenes", "persona", "l0 " + SESSION_ONE):
        print(f"\n  {paint('▸ ' + command, BOLD, BCYAN)}")
        await handle(memory, live, command)


async def ensure_workspace() -> str:
    if api_key := os.environ.get("MEMSEEK_API_KEY"):
        return api_key
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("set MEMSEEK_API_KEY (existing workspace) or DATABASE_URL (fresh one)")
    from memseek.auth import create_workspace
    from memseek.db import pool_lifespan

    async with pool_lifespan(get_settings()) as pool:
        credential = await create_workspace(pool, f"agent-memory-{RUN}")
    print(paint(f"  created disposable workspace {credential.workspace}", GREY))
    return credential.api_key


async def main() -> None:
    api_key = await ensure_workspace()
    base_url = os.environ.get("MEMSEEK_BASE_URL", "http://127.0.0.1:8000")
    print_workspace_explorer(api_url=base_url, api_key=api_key)
    live_model = not get_settings().llm_fake

    title("AGENT MEMORY ON MEMSEEK", "L0 → L1 → L2 → L3, plus the Skill pipeline")
    provider = paint("real model", BOLD, GREEN) if live_model else paint("LLM_FAKE=1", BOLD, YELLOW)
    print(f"  provider: {provider}   ·   service: {paint(base_url, GREY)}")
    if not live_model:
        warn("LLM_FAKE=1 cannot cite record UUIDs: L0 and every read surface work, but")
        warn("extraction, scenes, persona, and skills will honestly produce nothing")

    async with MemseekClient(base_url, api_key) as client:
        published = await client.catalog.publish(package=PACKAGE, directory=CATALOG_ROOT)
        package = published.get("package") or {}
        print(
            paint(
                f"  published isolated catalog "
                f"{package.get('name', 'agent_memory')}@{package.get('version', '?')}",
                GREY,
            )
        )
        listing = await client._request("GET", "/collections")
        active = {row["name"] for row in listing.get("collections", []) if row.get("active")}
        missing = sorted(
            {"messages", "memories", "scenes", "persona", "traces", "procedures"} - active
        )
        if missing:
            raise SystemExit(
                "the agent_memory package did not activate: missing " + ", ".join(missing)
            )

        memory = Memory(client, live_model=live_model)
        await act_one(memory)
        await act_two(memory)
        # The procedure is promoted before recall so the assembled prompt has a
        # procedure block to carry — which is also the order a real deployment
        # reaches: a skill exists before the request that needs it arrives.
        await act_procedure(memory)
        await act_recall(memory)
        await act_glass_box(memory)
        if sys.stdin.isatty():
            await interactive(memory)
        else:
            await scripted(memory)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except MemseekHTTPError as error:
        raise SystemExit(f"memseek API error: {error}") from error
    except TimeoutError as error:
        raise SystemExit(str(error)) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
