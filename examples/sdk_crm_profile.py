"""The Living Profile — a CRM memory that rewrites itself while you watch.

This is the single, interactive CRM use case for the Memseek SDK. It runs in two
acts:

  ACT I  · SHOWCASE   The demo seeds an account playbook and a few CRM events for
                      one contact, then waits — live — while the service scores,
                      triggers, and derives a fully-cited profile from nothing.

  ACT II · TERMINAL   It hands you a prompt. Type a CRM note ("Avery wants renewals
                      opened a quarter early") and watch the profile bring itself up
                      to date. It adapts: first it waits for the accumulator trigger
                      to fold the new evidence in on its own (self-maintaining, no API
                      call); if that hasn't landed, it falls back to an explicit,
                      reviewed rebuild that reconstructs the whole profile from the
                      complete corpus and promotes it. Either way a before → after diff
                      shows exactly what moved, and you can open the GLASS BOX on any
                      slot to fall through belief → deriving run → the concrete events
                      it was fused from, each with its importance score.

Every conclusion is falsifiable. Nothing in the profile exists without a citation to
an immutable event, and every slot names the audited run that wrote it. That is the
"wow": a profile that maintains itself, and shows its work all the way down.

This exercises the full public SDK surface — ``catalog.publish``,
``records.ingest``/``ingest_many``, ``document``, ``document_history``, ``runs``/
``run``, ``record``, ``run_processor``/``job``, ``search`` and ``render_artifact`` —
against a running stack. A real provider is required (LLM_FAKE=1 cannot invent the
citation UUIDs the profile is built from):

    make database && source .env.sh
    export PROVIDER_OPENAI_COMPAT_API_KEY=sk-...     # or OPENAI_API_KEY
    uv run memseek migrate
    uv run uvicorn memseek.api:app &                 # terminal A
    uv run memseek worker &                          # terminal B
    uv run python examples/sdk_crm_profile.py        # terminal C — talk to it

Set MEMSEEK_API_KEY to reuse a workspace, or DATABASE_URL to spin up a fresh,
disposable one. Piping input (non-TTY) runs a short scripted session and exits, so
this doubles as a runnable smoke check.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import sys
import textwrap
from pathlib import Path
from typing import Any

from _workspace_explorer import print_workspace_explorer

from memseek.config import get_settings
from memseek.sdk import MemseekClient, MemseekHTTPError

# --- the contact this demo maintains a profile for -------------------------
RUN = secrets.token_hex(3)
CONTACT_NAME = "Avery Chen"
ACCOUNT = "Acme Cloud"
ACCOUNT_ID = "acme-cloud"
ENTITY = "contact:avery-chen"

CATALOG_ROOT = Path(__file__).with_name("crm_profile_catalog")
PACKAGE = "crm_user_profile@2.0.0"
COLLECTION = "crm_events"
RECORD_TYPE = "crm_event"

# The profile's keyed slots, in the order the card lays them out.
SLOTS = ["summary", "role", "commitments", "preferences", "open_threads", "goals"]

# ---------------------------------------------------------------------------
# Terminal styling. Colors and animation only reach a real TTY, and honor
# NO_COLOR (https://no-color.org) and TERM=dumb, so a pipe or CI log stays clean.
# ---------------------------------------------------------------------------
_C = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"


def _s(*codes: int) -> str:
    return ("\033[" + ";".join(map(str, codes)) + "m") if _C else ""


RESET, BOLD, DIM, ITAL = _s(0), _s(1), _s(2), _s(3)
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GREY = (
    _s(31),
    _s(32),
    _s(33),
    _s(34),
    _s(35),
    _s(36),
    _s(90),
)
BCYAN, BGREEN, BYELLOW, BMAG = _s(96), _s(92), _s(93), _s(95)


def paint(text: str, *codes: str) -> str:
    return ("".join(codes) + text + RESET) if _C else text


def short(rid: str | None) -> str:
    return rid[:8] if rid else "—"


def rule(char: str = "─", width: int = 74) -> str:
    return paint(char * width, GREY)


def bar(score: float | None, width: int = 10) -> str:
    """A colored importance meter for one event (1-10)."""

    if score is None:
        return paint("·" * width, GREY) + " "
    filled = max(0, min(width, round(score / 10 * width)))
    color = GREEN if score >= 7 else YELLOW if score >= 4 else RED
    return paint("█" * filled, color) + paint("░" * (width - filled), GREY) + " "


async def reveal(text: str, *codes: str, lead: str = "") -> None:
    """Type a line out character by character (a fast, tasteful flourish).

    Falls back to a plain print when color/animation is off.
    """

    if not _C:
        print(lead + text)
        return
    sys.stdout.write(lead + "".join(codes))
    delay = min(0.010, 0.5 / max(len(text), 1))
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        await asyncio.sleep(delay)
    sys.stdout.write(RESET + "\n")
    sys.stdout.flush()


class Spinner:
    """A live single-line status while the demo blocks on the worker.

    Animates in a background task; update ``label`` and the next frame reflects
    it. A no-op off a TTY.
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str) -> None:
        self.label = label
        self._task: asyncio.Task[None] | None = None
        self._start = 0.0

    async def _run(self) -> None:
        frame = 0
        while True:
            elapsed = asyncio.get_running_loop().time() - self._start
            glyph = paint(self.FRAMES[frame % len(self.FRAMES)], CYAN)
            clock = paint(f"({elapsed:4.1f}s)", GREY)
            sys.stdout.write(f"\r  {glyph} {self.label} {clock}\033[K")
            sys.stdout.flush()
            frame += 1
            await asyncio.sleep(0.1)

    async def __aenter__(self) -> Spinner:
        if _C:
            self._start = asyncio.get_running_loop().time()
            self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


def header(title: str, subtitle: str = "") -> None:
    print(f"\n{rule('━')}")
    line = f"  {paint(title, BOLD, BCYAN)}"
    if subtitle:
        line += paint(f"   {subtitle}", GREY)
    print(line)
    print(rule("━"))


def note(text: str) -> None:
    print(paint(f"  · {text}", GREY))


# ---------------------------------------------------------------------------
# Seed material for Act I.
# ---------------------------------------------------------------------------
PLAYBOOK_TEXT = (
    "Acme Cloud is a strategic enterprise account. Lead every profile with the "
    "contact's current role and open commitments. Treat stated communication "
    "preferences as binding. Surface renewal timing early and flag any shipped "
    "defects as open threads."
)

SEED_EVENTS = [
    (
        "role",
        "salesforce",
        f"{CONTACT_NAME} was promoted to VP of Product at {ACCOUNT}, owning enterprise "
        "collaboration.",
    ),
    (
        "commitment",
        "hubspot",
        f"{CONTACT_NAME} committed to ship the Northstar beta by September 30.",
    ),
    (
        "preference",
        "support",
        f"{CONTACT_NAME} prefers concise written updates the day before any call.",
    ),
]

# A canned Act II session used when stdin is not a TTY, so the file stays a
# runnable end-to-end check.
SCRIPTED_SESSION = [
    f"{CONTACT_NAME} wants renewal conversations opened a full quarter early.",
    "A security review found the shipped audit-log feature exposed customer data.",
    "why preferences",
    "glass open_threads",
]

# Notes that reliably move a slot — surfaced in the help and again whenever a
# note lands without changing anything. Each is material, business-relevant CRM
# signal the account playbook actually cares about, unlike a low-signal aside
# ("Avery likes pizza") which is stored and searchable but kept out of the slots.
EXAMPLE_NOTES = [
    ("role", f"{CONTACT_NAME} was promoted to SVP and now leads product and design."),
    ("commitment", f"{CONTACT_NAME} committed to sign the enterprise renewal by August 15."),
    ("preference", f"{CONTACT_NAME} now wants a short written recap the day before every call."),
    ("open thread", "A security review found the shipped billing export leaked customer data."),
]


def classify(text: str) -> tuple[str, str]:
    """Map a free-text note to a (event_kind, source) the collection schema accepts.

    A leading ``kind:`` tag wins; otherwise a light keyword heuristic picks one so
    the story reads naturally. The source is implied by the kind.
    """

    lowered = text.lower()
    kinds = ("role", "commitment", "preference", "interaction")
    for kind in kinds:
        if lowered.startswith(f"{kind}:"):
            return kind, {
                "role": "salesforce",
                "commitment": "hubspot",
                "preference": "support",
                "interaction": "product",
            }[kind]

    def has(*words: str) -> bool:
        return any(w in lowered for w in words)

    if has("promot", "vp ", "cto", "ceo", "director", "head of", "now leads", "title", "role"):
        return "role", "salesforce"
    if has(
        "commit", "will ship", "deliver", "deadline", "promis", "sign", "by septemb", "renew by"
    ):
        return "commitment", "hubspot"
    if has("prefer", "likes", "wants", "asked for", "would rather", "expects", "dislike", "hates"):
        return "preference", "support"
    if has("bug", "exposed", "defect", "outage", "incident", "broke", "security"):
        return "interaction", "support"
    return "interaction", "product"


# ---------------------------------------------------------------------------
# A thin wrapper over MemseekClient with the demo's read/write helpers.
# ---------------------------------------------------------------------------
class Demo:
    def __init__(self, client: MemseekClient) -> None:
        self.c = client
        self._seq = 0

    def _next_key(self) -> str:
        self._seq += 1
        return f"live-{RUN}-{self._seq}"

    # -- writes -------------------------------------------------------------
    async def seed_playbook(self) -> str:
        result = await self.c.records.ingest(
            collection="playbooks",
            entity=ENTITY,
            type="playbook",
            key="playbook",
            text=PLAYBOOK_TEXT,
            dedupe_key=f"playbook:{ENTITY}",
        )
        rows = result.get("inserted", []) + result.get("duplicates", [])
        return str(rows[0]["id"])

    async def ingest_event(self, kind: str, source: str, text: str) -> str:
        result = await self.c.records.ingest(
            collection=COLLECTION,
            entity=ENTITY,
            type=RECORD_TYPE,
            text=text,
            content={"source": source, "event_kind": kind, "account_id": ACCOUNT_ID},
            dedupe_key=self._next_key(),
        )
        rows = result.get("inserted", []) + result.get("duplicates", [])
        return str(rows[0]["id"])

    async def ingest_seed_events(self) -> list[str]:
        result = await self.c.records.ingest_many(
            [
                {
                    "collection": COLLECTION,
                    "entity": ENTITY,
                    "type": RECORD_TYPE,
                    "text": text,
                    "content": {"source": source, "event_kind": kind, "account_id": ACCOUNT_ID},
                    "dedupe_key": self._next_key(),
                }
                for kind, source, text in SEED_EVENTS
            ]
        )
        rows = result.get("inserted", []) + result.get("duplicates", [])
        return [str(r["id"]) for r in rows]

    # -- reads / waits ------------------------------------------------------
    async def wait_ready(self, ids: list[str], label: str, timeout_s: float = 120.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        pending = list(ids)
        async with Spinner(label) as spin:
            while pending:
                detail = await self.c.record(pending[-1])
                if detail.get("ready"):
                    pending.pop()
                    continue
                spin.label = f"{label} — {len(ids) - len(pending)}/{len(ids)} enriched"
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("records not enriched — is `memseek worker` running?")
                await asyncio.sleep(0.5)

    async def beliefs(self) -> dict[str, dict[str, Any]]:
        doc = await self.c.document(entity=ENTITY, collections="user_profiles")
        return {str(b["key"]): b for b in doc.get("beliefs", []) if b.get("key")}

    async def _ok_run_count(self, processor: str = "crm_profile") -> int:
        runs = await self.c.runs(entity=ENTITY, processor=processor, operation="derive")
        return sum(1 for r in runs.get("runs", []) if r.get("status") == "ok")

    async def redive(self, label: str, *, trigger_window_s: float = 22.0) -> tuple[str, str | None]:
        """Bring the profile up to date with the newest evidence — adaptively.

        First it waits a short window for the *automatic* accumulator trigger to
        fold the new event in on its own (the incremental path — a self-maintaining
        memory, no API call required). If the trigger produces a fresh derivation,
        we're done. Otherwise it falls back to an explicit, reviewed **rebuild**:
        the whole profile is reconstructed from the complete evidence corpus, every
        slot re-cited from scratch, and the reviewed candidate is promoted.

        Returns ``(path, run_id)`` where path is "trigger", "rebuild", or "none".
        Either way the profile ends fully cited and backed by an audited run.

        The one-line ``summary`` is not written here: it is its own chained
        derivation (``crm_summary``) that re-synthesizes from the slots whenever one
        actually changes. So once the slots move we wait a beat for that summary to
        catch up before returning, keeping the rendered card whole.
        """

        baseline = await self._ok_run_count("crm_profile")
        summary_baseline = await self._ok_run_count("crm_summary")
        deadline = asyncio.get_running_loop().time() + trigger_window_s
        path, run_id = "none", None
        async with Spinner(f"{label} — watching for the self-update trigger") as spin:
            while asyncio.get_running_loop().time() < deadline:
                runs = await self.c.runs(entity=ENTITY, processor="crm_profile", operation="derive")
                ok = [r for r in runs.get("runs", []) if r.get("status") == "ok"]
                if len(ok) > baseline:
                    path, run_id = "trigger", str(ok[0]["id"])
                    break
                await asyncio.sleep(0.75)
            else:
                spin.label = f"{label} — reconstructing & reviewing the full profile"
                rebuilt = await self._rebuild_and_promote()
                path, run_id = ("rebuild", rebuilt) if rebuilt else ("none", None)
        if run_id:
            await self._await_summary(summary_baseline)
        return path, run_id

    async def _await_summary(self, baseline: int, timeout_s: float = 30.0) -> None:
        """Wait for the chained summary derivation to re-synthesize from the slots.

        Best-effort: the slots (and their citations) are already authoritative, so
        a slow or missing summary never blocks the story — we just give it a short
        window to land so the card leads with a fresh one-line read.
        """

        deadline = asyncio.get_running_loop().time() + timeout_s
        async with Spinner("the summary is re-synthesizing from the updated slots"):
            while asyncio.get_running_loop().time() < deadline:
                if await self._ok_run_count("crm_summary") > baseline:
                    return
                await asyncio.sleep(0.5)

    async def _rebuild_and_promote(self, timeout_s: float = 120.0) -> str | None:
        """Run the snapshot rebuild, wait for review, and promote the candidate."""

        queued = await self.c.run_processor("crm_profile_rebuild", entity=ENTITY)
        job_id = str(queued["job_id"])
        deadline = asyncio.get_running_loop().time() + timeout_s
        run_id: str | None = None
        while True:
            job = await self.c.job(job_id)
            if job.get("successful_run_id"):
                run_id = str(job["successful_run_id"])
                break
            if job.get("state") == "dead":
                return None
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.75)

        # Draft outputs must finish their own enrichment before they are promotable.
        while True:
            review = await self.c.run(run_id)
            outputs = review.get("outputs", [])
            if (
                outputs
                and len(outputs) == review.get("output_count")
                and all(row.get("ready") for row in outputs)
            ):
                break
            if any(row.get("enrichment_error") for row in outputs):
                return None
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.75)

        with contextlib.suppress(MemseekHTTPError):
            await self.c.promote(
                entity=ENTITY, source_run_id=run_id, artifact="crm_profile_candidate"
            )
        return run_id


# ---------------------------------------------------------------------------
# Rendering: the profile card, the before → after diff, and the glass box.
# ---------------------------------------------------------------------------
def _wrap(text: str, width: int = 66) -> list[str]:
    return textwrap.wrap(text, width=width) or [""]


def render_card(
    beliefs: dict[str, dict[str, Any]],
    changed: dict[str, str] | None = None,
) -> None:
    """Draw the profile as an open card. ``changed`` maps a slot to 'added' /
    'changed' / 're-cited' so a diff can mark what just moved."""

    changed = changed or {}
    title = paint(CONTACT_NAME.upper(), BOLD, BCYAN)
    print(f"\n {paint('╭─', GREY)} {title} {paint('—', GREY)} {paint(ACCOUNT, GREY)}")
    print(f" {paint('│', GREY)}")

    summary = beliefs.get("summary")
    if summary:
        for i, line in enumerate(_wrap(str(summary.get("text", "")), 68)):
            prefix = paint("“", ITAL, GREY) if i == 0 else " "
            print(f" {paint('│', GREY)}  {prefix}{paint(line, ITAL)}")
        print(f" {paint('│', GREY)}")

    for key in SLOTS:
        if key == "summary":
            continue
        belief = beliefs.get(key)
        if not belief:
            continue
        mark_kind = changed.get(key)
        if mark_kind == "added":
            marker, mcolor, tcolor = "+", BGREEN, GREEN
        elif mark_kind == "changed":
            marker, mcolor, tcolor = "▲", BYELLOW, YELLOW
        elif mark_kind == "re-cited":
            # text unchanged, only provenance widened — mark it quietly so the
            # growing citation count has a visible cause without reading as a
            # content change.
            marker, mcolor, tcolor = "◦", GREY, RESET
        else:
            marker, mcolor, tcolor = "▸", CYAN, RESET
        cited = paint(f"  ({len(belief.get('citations') or [])} cited)", GREY)
        label = paint(f"{key:<13}", BOLD, mcolor)
        lines = _wrap(str(belief.get("text", "")), 60)
        print(
            f" {paint('│', GREY)}  {paint(marker, mcolor)} {label}{paint(lines[0], tcolor)}{cited}"
        )
        for line in lines[1:]:
            print(f" {paint('│', GREY)}      {'':<13}{paint(line, tcolor)}")
    print(f" {paint('╰─', GREY)}")


def diff(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Classify each slot as added / changed / re-cited against the prior snapshot.

    A slot is 'changed' only when its belief text moved. When the text is
    identical but the citation set grew or shifted — e.g. a rebuild re-cited the
    slot against the full corpus and folded in the newly-logged event — it is
    're-cited': the assertion is the same, only its provenance widened.
    """

    out: dict[str, str] = {}
    for key, belief in after.items():
        prior = before.get(key)
        if prior is None:
            out[key] = "added"
        elif str(prior.get("text", "")).strip() != str(belief.get("text", "")).strip():
            out[key] = "changed"
        elif set(prior.get("citations") or []) != set(belief.get("citations") or []):
            out[key] = "re-cited"
    return out


async def show_run_line(demo: Demo, path: str, run_id: str | None, changed: dict[str, str]) -> None:
    """One line naming the audited run that just rewrote the profile, and how."""

    if not run_id:
        print(
            paint(
                "  (the profile did not update — confirm the worker is running with a "
                "real provider)",
                YELLOW,
            )
        )
        return
    detail = await demo.c.run(run_id)
    content = detail.get("run", {}).get("content", {}) or {}
    ms = content.get("ms")
    timing = paint(f"{ms / 1000:.1f}s", GREY) if ms is not None else ""
    moved = ", ".join(f"{k} {v}" for k, v in changed.items()) or "no visible change"
    if path == "rebuild":
        how = paint("reviewed rebuild → promoted", MAGENTA)
        glyph = paint("↻", BMAG)
    else:
        reasons = content.get("trigger_reasons") or []
        how = "self-updated  " + (
            paint("trigger [" + ", ".join(reasons) + "]", CYAN) if reasons else paint("[—]", GREY)
        )
        glyph = paint("⚡", BYELLOW)
    print(
        f"  {glyph} run {paint(short(run_id), BOLD)} {how}   {timing}  {paint('· ' + moved, GREY)}"
    )


async def glass_box(demo: Demo, key: str) -> None:
    """Open one profile slot all the way to the raw events behind it.

    belief (the slot) → the audited run that wrote it (trigger, timing, how it
    classified this key) → every cited event (importance meter, source, when it
    happened, and its text). Nothing the profile asserts is unfalsifiable.
    """

    beliefs = await demo.beliefs()
    belief = beliefs.get(key)
    if not belief:
        print(
            paint(
                f"  no '{key}' slot yet — try: {', '.join(k for k in SLOTS if k in beliefs)}",
                YELLOW,
            )
        )
        return

    print(f"\n  {paint('◆ ' + key, BOLD, BCYAN)}")
    for line in _wrap(str(belief.get("text", "")), 68):
        print(paint(f"    {line}", RESET))

    run_id = belief.get("run_id")
    if run_id:
        detail = await demo.c.run(str(run_id))
        content = detail.get("run", {}).get("content", {}) or {}
        reasons = content.get("trigger_reasons") or []
        ms = content.get("ms")
        change = next(
            (
                str(e.get("change"))
                for e in (content.get("candidate_set", {}) or {}).get("divergence", []) or []
                if e.get("key") == key
            ),
            None,
        )
        facets = [f"trigger {paint('[' + ', '.join(reasons) + ']', CYAN)}" if reasons else ""]
        if ms is not None:
            facets.append(paint(f"{ms / 1000:.1f}s", GREY))
        if change:
            facets.append(paint(f"this key: {change}", GREY))
        facets = [f for f in facets if f]
        print(
            f"    {paint('└─', GREY)} derived by run {paint(short(str(run_id)), BOLD)}"
            f"   {paint('·', GREY).join('  ' + f + '  ' for f in facets)}"
        )
    else:
        print(f"    {paint('└─ no deriving run recorded', YELLOW)}")

    citations = list(belief.get("citations") or [])
    print(
        paint(f"       cited evidence — {len(citations)} concrete event(s), sharpest first:", GREY)
    )
    events: list[dict[str, Any]] = []
    for cid in citations:
        with contextlib.suppress(MemseekHTTPError):
            events.append(await demo.c.record(cid))
    events.sort(key=lambda r: (r.get("scores") or {}).get("importance") or 0, reverse=True)
    for record in events:
        content = record.get("content") or {}
        score = (record.get("scores") or {}).get("importance")
        tag = "/".join(
            p for p in (content.get("source"), content.get("event_kind")) if p
        ) or "/".join(p for p in (record.get("collection"), record.get("type")) if p)
        when = str(record.get("occurred_at", "")).replace("T", " ").replace("Z", "")[:16]
        meter = bar(float(score) if score is not None else None)
        print(
            f"       {meter}{paint(short(record.get('id')), BOLD)}  "
            f"{paint(tag, CYAN)}  {paint(when, GREY)}"
        )
        for line in _wrap(str(content.get("text", "")), 60):
            print(paint(f"          {line}", GREEN))
    if not events:
        print(paint("       (no citations to dereference)", YELLOW))


async def show_history(demo: Demo, key: str) -> None:
    """Every version of one slot, newest first — the slot's self-revision log."""

    try:
        history = await demo.c.document_history(entity=ENTITY, collection="user_profiles", key=key)
    except MemseekHTTPError as error:
        print(paint(f"  history unavailable: {error}", YELLOW))
        return
    versions = history.get("versions") or history.get("records") or []
    if not versions:
        print(paint(f"  no history for '{key}' yet", YELLOW))
        return
    print(f"\n  {paint('◷ ' + key, BOLD, BCYAN)} — {len(versions)} version(s), newest first")
    for i, version in enumerate(versions):
        text = str(version.get("text") or (version.get("content") or {}).get("text") or "")
        run_id = version.get("run_id") or version.get("source_run_id")
        cites = len(version.get("citations") or [])
        elbow = paint("┌─" if i == 0 else "├─", GREY)
        tag = paint("current" if i == 0 else f"v-{len(versions) - i}", CYAN)
        print(
            f"    {elbow} {tag}  run {paint(short(str(run_id)) if run_id else '—', BOLD)}  {paint(f'{cites} cited', GREY)}"
        )
        for line in _wrap(text, 64):
            print(paint(f"       {line}", GREY if i else GREEN))


# ---------------------------------------------------------------------------
# Act I — the showcase.
# ---------------------------------------------------------------------------
async def showcase(demo: Demo) -> None:
    header("ACT I · THE PROFILE BUILDS ITSELF", f"{CONTACT_NAME} · {ACCOUNT} · run {RUN}")
    note("nothing exists yet. We seed an account playbook and three CRM events,")
    note("then wait while the service scores, triggers, and derives a cited profile.")

    playbook_id = await demo.seed_playbook()
    await demo.wait_ready([playbook_id], "enriching the account playbook")
    print(f"  {paint('✓', GREEN)} account playbook stored and indexed")

    seed_ids = await demo.ingest_seed_events()
    print(
        f"  {paint('→', CYAN)} ingested {len(seed_ids)} CRM events (role · commitment · preference)"
    )
    await demo.wait_ready(seed_ids, "scoring event importance")

    path, run_id = await demo.redive("the profile is deriving itself from evidence")
    beliefs = await demo.beliefs()
    if not beliefs:
        raise SystemExit(
            "the profile did not materialize — confirm the worker is running with a real "
            "provider (LLM_FAKE=0)."
        )
    await show_run_line(demo, path, run_id, dict.fromkeys(beliefs, "added"))
    render_card(beliefs, dict.fromkeys(beliefs, "added"))
    if beliefs.get("summary"):
        print()
        await reveal(
            str(beliefs["summary"]["text"]), ITAL, BCYAN, lead="  the profile's one-line read: "
        )


# ---------------------------------------------------------------------------
# Act II — the interactive terminal.
# ---------------------------------------------------------------------------
def _example_lines(indent: str = "    ") -> str:
    return "\n".join(
        f"{indent}{paint(f'{slot:<12}', BOLD, GREEN)}{paint(note, GREY)}"
        for slot, note in EXAMPLE_NOTES
    )


HELP = f"""
  {paint("You are now feeding CRM signal to a memory that maintains itself.", BOLD)}
  {paint("This is a curated *business* profile, not an event log: the model follows the", GREY)}
  {paint('account playbook and keeps low-signal or off-topic notes ("Avery likes pizza")', GREY)}
  {paint("out of the slots — they are still stored, scored, and searchable, just not", GREY)}
  {paint("promoted into the profile. Material CRM signal moves it. Try one of these:", GREY)}

{_example_lines()}

  {paint("<any note>", BCYAN)}          add a CRM event and watch the profile re-derive
                        (prefix with role: / commitment: / preference: to force a kind)
  {paint("why <slot>", BCYAN)}          open the GLASS BOX: belief → run → cited events
  {paint("glass <slot>", BCYAN)}        alias for `why`
  {paint("history <slot>", BCYAN)}      show every past version of a slot and who wrote it
  {paint("profile", BCYAN)}             redraw the current profile card
  {paint("search <query>", BCYAN)}      full-text search this contact's raw event history
  {paint("brief", BCYAN)}              render the live profile brief artifact
  {paint("help", BCYAN)}                show this
  {paint("quit", BCYAN)}                leave

  slots: {", ".join(SLOTS)}
"""


async def ainput(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


def explain_no_change(text: str) -> None:
    """A note landed but no slot moved — say why, and point somewhere useful.

    Almost always this means the deriving model, following the account playbook,
    judged the note immaterial to a business profile (the classic case: a personal
    aside like "Avery likes pizza"). The event still exists and is cited, so we
    steer the user to `search` for it and to notes that will actually move a slot.
    """

    probe = next(
        (w.strip(".,!?") for w in text.split() if len(w) > 3),
        text.split()[-1] if text.split() else "",
    )
    print()
    note("no slot moved — the model kept this out of the business profile (probably")
    note("low-signal or off-topic for a sales profile). It is still stored and cited:")
    if probe:
        print(
            f"      {paint('search ' + probe, CYAN)}   {paint('· find the raw event you just added', GREY)}"
        )
    note("to move a slot, feed material CRM signal, e.g.:")
    print(_example_lines("      "))


async def add_note(demo: Demo, text: str) -> None:
    kind, source = classify(text)
    before = await demo.beliefs()
    print(f"  {paint('→', CYAN)} logging as {paint(kind, BOLD)} (source: {source})")
    event_id = await demo.ingest_event(kind, source, text)
    await demo.wait_ready([event_id], "scoring importance")
    path, run_id = await demo.redive("the profile is rewriting itself")
    after = await demo.beliefs()
    changed = diff(before, after)
    await show_run_line(demo, path, run_id, changed)
    render_card(after, changed)
    if run_id and not changed:
        explain_no_change(text)


async def handle(demo: Demo, line: str) -> bool:
    """Dispatch one line of the interactive session. Returns False to quit."""

    line = line.strip()
    if not line:
        return True
    lowered = line.lower()
    if lowered in {"quit", "exit", "q"}:
        return False
    if lowered in {"help", "?", "h"}:
        print(HELP)
        return True
    if lowered in {"profile", "p"}:
        render_card(await demo.beliefs())
        return True
    if lowered == "brief":
        brief = await demo.c.render_artifact(
            "crm_profile_brief", entity=ENTITY, query="role commitments preferences open threads"
        )
        rendered = str(brief.get("rendered", "")).strip()
        print("\n" + rule())
        for bl in rendered.splitlines():
            print(paint("  " + bl, GREY))
        print(rule())
        return True
    for verb in ("why", "glass"):
        if lowered.startswith(verb + " ") or lowered == verb:
            slot = line[len(verb) :].strip() or "summary"
            await glass_box(demo, slot)
            return True
    if lowered.startswith("history"):
        slot = line[len("history") :].strip() or "preferences"
        await show_history(demo, slot)
        return True
    if lowered.startswith("search"):
        query = line[len("search") :].strip()
        if not query:
            print(paint("  usage: search <query>", YELLOW))
            return True
        hits = await demo.c.search(
            query=query,
            collections=[COLLECTION],
            entity=ENTITY,
            mode="hybrid",
            k=6,
            include=["text", "scores", "occurred_at"],
        )
        rows = hits.get("hits", [])
        print(f"\n  {paint(f'{len(rows)} hit(s)', BOLD)} for {paint(query, CYAN)}")
        for row in rows:
            content = row.get("content") or {}
            score = (row.get("scores") or {}).get("importance")
            print(
                f"    {bar(float(score) if score is not None else None)}{paint(short(row.get('id')), GREY)}"
            )
            for bl in _wrap(str(content.get("text") or row.get("text") or ""), 62):
                print(paint(f"       {bl}", RESET))
        return True

    # Anything else is a CRM note.
    await add_note(demo, line)
    return True


async def interactive(demo: Demo) -> None:
    header("ACT II · YOUR TURN", "feed it CRM signal; watch it maintain itself")
    print(HELP)
    while True:
        try:
            line = await ainput(paint(f"\n  {CONTACT_NAME.split()[0].lower()} ▸ ", BOLD, BCYAN))
        except EOFError, KeyboardInterrupt:
            print()
            break
        try:
            if not await handle(demo, line):
                break
        except MemseekHTTPError as error:
            print(paint(f"  API error: {error}", RED))
    print(
        paint("\n  The profile persists. Every slot in it is still cited, still replayable.", GREY)
    )


async def scripted(demo: Demo) -> None:
    """Non-TTY fallback: run a short canned session so the file stays runnable."""

    header("ACT II · SCRIPTED SESSION", "stdin is not a TTY — running a canned demo")
    for line in SCRIPTED_SESSION:
        print(paint(f"\n  ▸ {line}", BOLD, BCYAN))
        await handle(demo, line)


# ---------------------------------------------------------------------------
# Workspace + entrypoint.
# ---------------------------------------------------------------------------
async def ensure_workspace() -> str:
    if key := os.environ.get("MEMSEEK_API_KEY"):
        return key
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("set MEMSEEK_API_KEY (existing workspace) or DATABASE_URL (fresh one)")
    from memseek.auth import create_workspace
    from memseek.db import pool_lifespan

    async with pool_lifespan(get_settings()) as pool:
        cred = await create_workspace(pool, f"living-profile-{RUN}")
    print(paint(f"  created disposable workspace {cred.workspace}", GREY))
    return cred.api_key


async def main() -> None:
    api_key = await ensure_workspace()
    base_url = os.environ.get("MEMSEEK_BASE_URL", "http://127.0.0.1:8000")
    print_workspace_explorer(api_url=base_url, api_key=api_key)
    live = not get_settings().llm_fake

    print(rule("━"))
    print(
        f"  {paint('THE LIVING PROFILE', BOLD, BMAG)}  {paint('a CRM memory that rewrites itself', GREY)}"
    )
    print(rule("━"))
    provider = (
        paint("real model", BOLD, GREEN)
        if live
        else paint("LLM_FAKE=1 — cannot cite; the profile will not materialize", BOLD, YELLOW)
    )
    print(f"  provider: {provider}   ·   talking to {paint(base_url, GREY)}")

    async with MemseekClient(base_url, api_key) as client:
        catalog = await client.catalog.publish(package=PACKAGE, directory=CATALOG_ROOT)
        print(
            f"  catalog: {paint(str(catalog.get('package', {}).get('name')), BOLD)} "
            f"{paint('@ ' + str(catalog.get('catalog_hash'))[:12], GREY)}"
        )

        demo = Demo(client)
        await showcase(demo)
        if sys.stdin.isatty():
            await interactive(demo)
        else:
            await scripted(demo)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except MemseekHTTPError as error:
        raise SystemExit(f"memseek API error: {error}") from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
