"""Accumulation smoke test for the SDK CRM profile quickstart.

This runs the exact SDK calls documented in
``docs/sdk-user-profile-quickstart.md`` — ``catalog.publish``,
``records.ingest_many``, ``document``, ``runs``/``run``, ``record``, ``search``,
and ``render_artifact`` — and asserts each step actually did something instead
of only printing it. Every step also dumps the full payload it checked.

Beyond accumulation, it also traces provenance: for every materialized belief it
follows the ``run_id`` to the audited derivation that wrote it (trigger, timing,
keyed divergence) and dereferences each citation into the concrete event it was
fused from (kind, source, occurred_at, text). That belief → run → events lineage
is the auditable answer to "why do we believe this?".

Rather than one big batch, it ingests events in *rounds*: round 1 seeds each
contact's role, commitment, and first preference; every later round adds one new
preference. It waits for each round's derivation before starting the next, so you
can watch the ``preferences`` slot ACCUMULATE across derivation runs (1 → 2 → 3
facts) instead of the model merging everything in a single pass. The profile also
carries a synthesized one-sentence ``summary`` slot that the brief renders first.

It talks to a running service through the shipped ``MemseekClient``. Start the
API and worker first (see the quickstart), export a workspace key, then:

    export MEMSEEK_API_KEY=...            # from `memseek create-workspace`
    export MEMSEEK_BASE_URL=http://127.0.0.1:8000
    uv run python examples/sdk_crm_profile_smoke.py --contacts 3 --fillers-per-round 1

A real provider (``LLM_FAKE=0``) is required: the profile only materializes and
accumulates once the importance scorer and the profile derivation run for real.
The script exits non-zero if any documented step fails, so it is usable as a
manual check.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from _workspace_explorer import print_workspace_explorer

from memseek.sdk import MemseekClient, MemseekHTTPError

CATALOG_ROOT = Path(__file__).with_name("crm_profile_catalog")
PACKAGE = "crm_user_profile@2.0.0"
COLLECTION = "crm_events"
RECORD_TYPE = "crm_event"
INGEST_CHUNK = 50  # stay under the service's MAX_BATCH (100)

# One display name and account per contact slot; the entity id is derived from it.
CONTACTS = [
    ("Avery Chen", "acme-cloud"),
    ("Jordan Lee", "globex"),
    ("Sam Rivera", "initech"),
    ("Priya Nair", "umbrella"),
    ("Marco Alvarez", "hooli"),
]

# Two one-off high-signal facts that anchor the profile. Emitted only in round 1;
# later rounds preserve them via the derivation's current-profile source.
BASE_HIGH_SIGNAL = [
    (
        "role",
        "salesforce",
        "{name} was promoted to VP of Product at {account}, owning enterprise collaboration.",
    ),
    ("commitment", "hubspot", "{name} committed to ship the Northstar beta by September 30."),
]
# One distinct preference per round. Delivered across separate rounds — not all at
# once — so each new preference arrives after the previous derivation has run. That
# is what lets the `preferences` slot ACCUMULATE (the derivation folds new evidence
# into the current profile) instead of the model emitting one merged slot in a
# single pass. The number of rounds equals the number of preferences here.
PREFERENCES = [
    ("preference", "support", "{name} prefers concise written updates the day before any call."),
    ("preference", "salesforce", "{name} wants renewal conversations opened a full quarter early."),
    (
        "preference",
        "product",
        "{name} asks for breaking API changes to land in the shared Slack channel first.",
    ),
]
# Routine interactions: high volume, low importance. These are NOT what fires the
# derivation — the round's stable preference trips the importance accumulator on its
# own. They exist to show low-signal events stay searchable history and never become
# profile facts, so a few per round is plenty. {n} keeps each text distinct.
LOW_SIGNAL = [
    ("interaction", "product", "{name} opened the {account} usage dashboard (visit {n})."),
    ("interaction", "product", "{name} logged into the {account} workspace (session {n})."),
    ("interaction", "hubspot", "{name} viewed a routine product newsletter (issue {n})."),
]


# ---------------------------------------------------------------------------
# Terminal styling. Colors are emitted only to a real TTY, and can be turned
# off with NO_COLOR (https://no-color.org) or TERM=dumb, so piping to a file or
# CI log stays clean.
# ---------------------------------------------------------------------------
_USE_COLOR = (
    sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"
)


def _sgr(*codes: int) -> str:
    return "\033[" + ";".join(str(c) for c in codes) + "m" if _USE_COLOR else ""


RESET = _sgr(0)
BOLD = _sgr(1)
DIM = _sgr(2)
RED = _sgr(31)
GREEN = _sgr(32)
YELLOW = _sgr(33)
CYAN = _sgr(36)
GREY = _sgr(90)


def paint(text: str, *codes: str) -> str:
    """Wrap ``text`` in the given SGR codes (no-op when color is disabled)."""

    return ("".join(codes) + text + RESET) if _USE_COLOR else text


def banner(text: str) -> None:
    """A top-level section header."""

    print("\n" + paint(text, BOLD, CYAN))


def note(text: str, indent: int = 2) -> None:
    """A dimmed, indented line explaining what the next step is doing/checking."""

    print(paint(f"{' ' * indent}· {text}", GREY))


class Status:
    """A live, single-line spinner for the polling waits.

    The script blocks for up to ``--timeout`` seconds waiting on the worker, so
    an animated status keeps it obvious that it is still alive and shows how long
    it has been waiting. It animates in a background task and only draws to a real
    TTY (``_USE_COLOR``); otherwise it is a silent no-op. Update ``label`` at any
    time and the next frame reflects it.
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
            sys.stdout.write(f"\r{glyph} {self.label} {clock}\033[K")
            sys.stdout.flush()
            frame += 1
            await asyncio.sleep(0.1)

    async def __aenter__(self) -> Status:
        if _USE_COLOR:
            self._start = asyncio.get_running_loop().time()
            self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            sys.stdout.write("\r\033[K")  # erase the spinner line
            sys.stdout.flush()


def entity_id(name: str) -> str:
    return "contact:" + name.lower().replace(" ", "-")


def generate_rounds(contacts: int, fillers_per_round: int) -> list[list[dict[str, Any]]]:
    """Build one deterministic event batch per round.

    Round 1 seeds role + commitment + the first preference for every contact; each
    later round adds exactly one new preference. The preference is emitted first, so
    it is the lowest-seq event of its round and its own importance (a stable
    preference scores well above the accumulator threshold) fires the derivation —
    guaranteeing it is ready and inside the changes window when the run resolves. The
    ``fillers_per_round`` routine interactions that follow are low-signal history, not
    the trigger. Dedupe keys and occurred_at advance from one per-entity counter, so
    events never collide across rounds.
    """

    base = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    counters: dict[str, int] = {}
    rounds: list[list[dict[str, Any]]] = []
    for round_index in range(len(PREFERENCES)):
        batch: list[dict[str, Any]] = []
        for slot in range(contacts):
            name, account = CONTACTS[slot % len(CONTACTS)]
            entity = entity_id(name)
            specs: list[tuple[str, str, str]] = []
            if round_index == 0:
                specs.extend(BASE_HIGH_SIGNAL)
            specs.append(PREFERENCES[round_index])
            for filler in range(fillers_per_round):
                specs.append(LOW_SIGNAL[filler % len(LOW_SIGNAL)])
            for kind, source, template in specs:
                index = counters.get(entity, 0)
                counters[entity] = index + 1
                occurred = base + timedelta(hours=index, days=slot)
                batch.append(
                    {
                        "collection": COLLECTION,
                        "entity": entity,
                        "type": RECORD_TYPE,
                        "text": template.format(name=name, account=account, n=index + 1),
                        "content": {
                            "source": source,
                            "event_kind": kind,
                            "account_id": account,
                        },
                        "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
                        "dedupe_key": f"smoke:{entity}:{index}",
                    }
                )
        rounds.append(batch)
    return rounds


def chunked(items: Sequence[dict[str, Any]], size: int) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def dump(label: str, payload: Any) -> None:
    """Print a labelled, fully-expanded view of whatever a step returned."""

    print(f"    {paint(label + ':', DIM)}")
    if isinstance(payload, str):
        text = payload if payload.strip() else "(empty)"
        for line in text.splitlines() or [text]:
            print(paint(f"      {line}", GREY))
        return
    rendered = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    for line in rendered.splitlines():
        print(paint(f"      {line}", GREY))


class Report:
    """Collects PASS/FAIL checks and prints them as they happen."""

    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks += 1
        if not ok:
            self.failures += 1
        mark = paint(" PASS ", BOLD, GREEN) if ok else paint(" FAIL ", BOLD, RED)
        suffix = paint(f" — {detail}", GREY) if detail else ""
        print(f"  [{mark}] {name}{suffix}")
        return ok

    def summary(self) -> int:
        passed = self.checks - self.failures
        if self.failures:
            line = paint(f"{passed}/{self.checks} checks passed", BOLD, YELLOW) + paint(
                f"; {self.failures} FAILED", BOLD, RED
            )
        else:
            line = paint(f"{passed}/{self.checks} checks passed", BOLD, GREEN)
        print("\n" + line)
        return 1 if self.failures else 0


async def ingest_all(
    client: MemseekClient, events: Sequence[dict[str, Any]]
) -> tuple[int, int, list[str | None]]:
    """Ingest in chunks and return counts plus each event's server id.

    ``ids[i]`` is the stored record id for ``events[i]`` — for a fresh insert or
    an exact-duplicate retry alike — so callers can trace a specific event (e.g.
    this round's novel preference) into the citations of the profile it drives.
    """

    inserted = duplicates = 0
    ids: list[str | None] = [None] * len(events)
    offset = 0
    for batch in chunked(events, INGEST_CHUNK):
        result = await client.records.ingest_many(batch)
        for item in (*result.get("inserted", []), *result.get("duplicates", [])):
            ids[offset + int(item["index"])] = str(item["id"])
        inserted += len(result.get("inserted", []))
        duplicates += len(result.get("duplicates", []))
        offset += len(batch)
    return inserted, duplicates, ids


async def wait_for_profile(client: MemseekClient, entity: str, timeout_s: float) -> dict[str, Any]:
    """Poll /document until the profile materializes or the timeout elapses."""

    deadline = asyncio.get_running_loop().time() + timeout_s
    document: dict[str, Any] = {}
    async with Status(f"waiting for {paint(entity, BOLD)} profile to materialize"):
        while True:
            document = await client.document(entity=entity, collections="user_profiles")
            if document.get("beliefs"):
                return document
            if asyncio.get_running_loop().time() >= deadline:
                return document
            await asyncio.sleep(1.0)


def belief_for(document: dict[str, Any], key: str) -> dict[str, Any] | None:
    """The current belief occupying one profile slot, if any."""

    return next((b for b in document.get("beliefs", []) if b.get("key") == key), None)


class SlotSnapshot:
    """The state of one profile slot at a moment: which run wrote it, its
    citations, and its text. Comparing two snapshots shows a re-derivation."""

    def __init__(self, belief: dict[str, Any] | None) -> None:
        self.run_id = str(belief.get("run_id")) if belief and belief.get("run_id") else None
        self.citations = set(belief.get("citations", []) or []) if belief else set()
        self.text = str(belief.get("text", "")).strip() if belief else ""
        self.exists = belief is not None


async def snapshot_slot(client: MemseekClient, entity: str, key: str) -> SlotSnapshot:
    document = await client.document(entity=entity, collections="user_profiles")
    return SlotSnapshot(belief_for(document, key))


async def trigger_reasons_of(client: MemseekClient, run_id: str | None) -> list[str]:
    """The trigger reasons recorded for the run that produced a belief."""

    if not run_id:
        return []
    detail = await client.run(run_id)
    return list(detail.get("run", {}).get("content", {}).get("trigger_reasons", []))


def short(run_id: str | None) -> str:
    return run_id[:8] if run_id else "—"


def _event_line(record: dict[str, Any]) -> str:
    """A one-line description of a dereferenced source record.

    Shows what kind of concrete evidence it is (collection + event_kind +
    source), when it occurred, and its text — the atom a belief was built from.
    """

    content = record.get("content", {}) or {}
    kind = content.get("event_kind")
    source = content.get("source")
    tag = "/".join(part for part in (record.get("collection"), kind, source) if part)
    occurred = str(record.get("occurred_at", "")).replace("T", " ").replace("Z", "")
    text = str(content.get("text", "")).strip()
    return f"{paint(tag, CYAN)}  {paint(occurred, GREY)}  {text}"


def _divergence_for(run_content: dict[str, Any], key: str) -> str | None:
    """How the deriving run classified this belief's key (added/changed/…)."""

    manifest = run_content.get("candidate_set", {}) or {}
    for entry in manifest.get("divergence", []) or []:
        if entry.get("key") == key:
            return str(entry.get("change"))
    return None


async def trace_belief_provenance(
    client: MemseekClient,
    entity: str,
    document: dict[str, Any],
    report: Report,
    *,
    cache: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Follow every belief down to the run and the concrete events behind it.

    This is the showcase: for each materialized belief we (1) follow its
    ``run_id`` to the audited derivation that wrote it — its trigger, timing,
    and how the run classified this key (added/changed) — and (2) dereference
    each citation into the *actual* stored event it was folded from (kind,
    source, occurred_at, text). Not counts: the real lineage, belief → run →
    events, that lets anyone ask "why do we believe this?" and get an auditable
    answer grounded in immutable records.
    """

    beliefs = document.get("beliefs", [])
    record_cache: dict[str, dict[str, Any]] = cache if cache is not None else {}
    run_cache: dict[str, dict[str, Any]] = {}
    arrow = paint("←", CYAN)

    resolved_runs = 0
    resolved_citations = 0
    missing_citations = 0
    max_event_evidence = 0

    for belief in beliefs:
        key = str(belief.get("key"))
        run_id = belief.get("run_id")
        citations = list(belief.get("citations", []) or [])

        print(f"\n  {paint('belief', BOLD)}[{paint(key, BOLD, CYAN)}]  {belief.get('text', '')}")

        # (1) belief → the derivation run that wrote it
        run_content: dict[str, Any] = {}
        if run_id:
            if run_id not in run_cache:
                run_cache[run_id] = await client.run(run_id)
            detail = run_cache[run_id]
            run_content = detail.get("run", {}).get("content", {}) or {}
            if run_content.get("status") == "ok":
                resolved_runs += 1
            reasons = run_content.get("trigger_reasons", []) or []
            trigger = paint("[" + ", ".join(reasons) + "]", CYAN) if reasons else paint("[—]", GREY)
            ms = run_content.get("ms")
            effect = run_content.get("candidate_set", {}).get("effect")
            change = _divergence_for(run_content, key)
            facets = [f"trigger {trigger}"]
            if ms is not None:
                facets.append(paint(f"{ms}ms", GREY))
            if effect:
                facets.append(paint(f"effect={effect}", GREY))
            if change:
                facets.append(paint(f"key '{key}' {change}", GREY))
            print(
                f"    {arrow} derived by run {paint(short(run_id), BOLD)}  " + "  ·  ".join(facets)
            )
        else:
            print(f"    {arrow} {paint('no deriving run recorded', YELLOW)}")

        # (2) belief → the concrete events it was fused from
        events: list[dict[str, Any]] = []
        for cid in citations:
            if cid not in record_cache:
                try:
                    record_cache[cid] = await client.record(cid)
                except MemseekHTTPError:
                    record_cache[cid] = {}
            record = record_cache[cid]
            if record:
                resolved_citations += 1
                events.append(record)
            else:
                missing_citations += 1
        event_records = [r for r in events if (r.get("content") or {}).get("event_kind")]
        max_event_evidence = max(max_event_evidence, len(event_records))

        if events:
            print(f"    {arrow} fused from {len(events)} concrete record(s):")
            for record in events:
                print(
                    paint("       • ", GREY) + f"{short(record.get('id'))}  " + _event_line(record)
                )
        else:
            print(f"    {arrow} {paint('no citations to dereference', YELLOW)}")

    report.check(
        "every belief resolves to an ok deriving run",
        bool(beliefs) and resolved_runs == len(beliefs),
        f"{resolved_runs}/{len(beliefs)} belief(s) traced to a run",
    )
    report.check(
        "every citation dereferences to a stored record",
        missing_citations == 0 and resolved_citations > 0,
        f"{resolved_citations} resolved, {missing_citations} missing",
    )
    report.check(
        "a belief traces to multiple concrete source events (accumulated evidence)",
        max_event_evidence >= 2,
        f"deepest belief cites {max_event_evidence} event(s)",
    )


async def ok_derivation_count(client: MemseekClient, entity: str) -> int:
    """How many crm_profile derivations have completed ok for this entity."""

    runs = await client.runs(entity=entity, processor="crm_profile", operation="derive")
    return sum(1 for row in runs.get("runs", []) if row.get("status") == "ok")


async def wait_for_new_derivation(
    client: MemseekClient, entity: str, baseline: int, timeout_s: float
) -> int:
    """Poll until a derivation completes beyond ``baseline`` or the timeout elapses.

    Waiting for a *new* run (rather than a fixed count) keeps each round's evidence
    from merging into the next round's derivation, so the slots accumulate step by
    step.
    """

    deadline = asyncio.get_running_loop().time() + timeout_s
    async with Status("") as status:
        while True:
            count = await ok_derivation_count(client, entity)
            status.label = (
                f"waiting for {paint(entity, BOLD)} derivation — {count}/{baseline + 1} ok run(s)"
            )
            if count > baseline:
                return count
            if asyncio.get_running_loop().time() >= deadline:
                return count
            await asyncio.sleep(1.0)


async def verify_contact(
    client: MemseekClient, entity: str, report: Report, timeout_s: float
) -> None:
    banner(f"◆ {entity}")

    # (3) current profile document + freshness
    note("reading the materialized profile: it should carry beliefs, each citing evidence")
    document = await wait_for_profile(client, entity, timeout_s)
    beliefs = document.get("beliefs", [])
    report.check("profile materialized", bool(beliefs), f"{len(beliefs)} slot(s)")
    report.check(
        "every belief cites evidence",
        bool(beliefs) and all(b.get("citations") for b in beliefs),
    )
    summary = belief_for(document, "summary")
    report.check(
        "profile carries a synthesized summary",
        summary is not None and bool(str(summary.get("text", "")).strip()),
        summary.get("text", "") if summary else "no summary slot",
    )
    dump("profile document", document)

    # (4) audited derivation run
    note("checking the audit trail: a crm_profile derivation ran ok and named its trigger")
    runs = await client.runs(entity=entity, processor="crm_profile", operation="derive")
    run_rows = runs.get("runs", [])
    ok_run = next((r for r in run_rows if r.get("status") == "ok"), None)
    report.check("crm_profile run recorded ok", ok_run is not None, f"{len(run_rows)} run(s)")
    dump("derivation runs", run_rows)
    if ok_run is not None:
        detail = await client.run(str(ok_run["id"]))
        reasons = detail["run"]["content"].get("trigger_reasons", [])
        report.check("run names its trigger reasons", bool(reasons), ", ".join(reasons))
        dump("run detail", detail)

    # (4b) provenance trace: every belief followed down to the run that wrote
    # it and the concrete events it was fused from. This is the "why do we
    # believe this?" answer — auditable lineage, not a summary.
    note("tracing each belief to its deriving run and the concrete events behind it")
    await trace_belief_provenance(client, entity, document, report)

    # (5) text search over the events, scoped to this contact
    # websearch_to_tsquery ANDs terms, so the query must sit inside one event
    # (here, the commitment event); a query spanning several events matches none.
    note("full-text search over this contact's raw events (the profile's source history)")
    hits = await client.search(
        query="Northstar beta September",
        collections=[COLLECTION],
        entity=entity,
        mode="text",
        k=5,
        include=["text", "scores", "occurred_at"],
    )
    hit_rows = hits.get("hits", [])
    report.check("search returns hits", bool(hit_rows), f"{len(hit_rows)} hit(s)")
    dump("search hits", hit_rows)

    # (6) live artifact render — must carry the profile (summary + slots), not
    # only the supporting events.
    note("rendering the brief artifact: it must weave the profile in, not just the events")
    brief = await client.render_artifact(
        "crm_profile_brief", entity=entity, query="role commitments preferences"
    )
    rendered = brief.get("rendered", "")
    report.check("brief renders non-empty text", bool(rendered.strip()), f"{len(rendered)} chars")
    report.check(
        "brief includes the profile section",
        "CRM PROFILE" in rendered and "SUPPORTING CRM EVENTS" in rendered,
    )
    if summary is not None:
        report.check(
            "brief carries the synthesized summary",
            str(summary.get("text", "")).strip() in rendered,
        )
    dump("rendered brief", rendered)


async def ingest_round(
    client: MemseekClient,
    *,
    round_index: int,
    total_rounds: int,
    events: Sequence[dict[str, Any]],
    entities: Sequence[str],
    report: Report,
    timeout_s: float,
) -> bool:
    """Ingest one round and show the `preferences` slot RE-DERIVE from it.

    For each contact this snapshots the slot before the round, ingests the round's
    one novel preference event, waits for a fresh derivation, then prints a
    before → after view proving the re-derivation was driven by that new event: a
    new ``run_id`` wrote the slot, and the new event's id is now among its citations
    (with the earlier citations preserved — accumulation, not replacement).
    """

    banner(f"═══ Round {round_index + 1}/{total_rounds}: ingesting {len(events)} event(s) ═══")
    note(
        "round 1 seeds role + commitment + the first preference; later rounds add one "
        "novel preference each, and we watch the slot re-derive to fold it in"
    )
    baselines = {entity: await ok_derivation_count(client, entity) for entity in entities}
    before = {entity: await snapshot_slot(client, entity, "preferences") for entity in entities}
    try:
        inserted, duplicates, ids = await ingest_all(client, events)
    except MemseekHTTPError as error:
        report.check(
            f"round {round_index + 1} ingest succeeded",
            False,
            f"HTTP {error.status_code}: {error.payload}",
        )
        return False
    report.check(
        f"round {round_index + 1} events accepted",
        inserted + duplicates == len(events),
        f"{inserted} inserted, {duplicates} duplicate",
    )

    # The one novel preference event per contact this round — the new evidence we
    # expect to drive a fresh derivation and land in the accumulated slot.
    novel: dict[str, str | None] = dict.fromkeys(entities)
    novel_text: dict[str, str] = {}
    for event, record_id in zip(events, ids, strict=True):
        if record_id and event["content"]["event_kind"] == "preference":
            novel[event["entity"]] = record_id
            novel_text[event["entity"]] = event["text"]

    for entity in entities:
        prior = before[entity]
        count = await wait_for_new_derivation(client, entity, baselines[entity], timeout_s)
        report.check(
            f"round {round_index + 1} derivation fired for {entity}",
            count > baselines[entity],
            f"{count} ok run(s)",
        )

        after = await snapshot_slot(client, entity, "preferences")
        reasons = await trigger_reasons_of(client, after.run_id)
        added = after.citations - prior.citations
        rederived = after.run_id is not None and after.run_id != prior.run_id
        folds_novel = novel[entity] is not None and novel[entity] in after.citations
        preserved = prior.citations <= after.citations

        # before → after re-derivation view
        arrow = paint("→", CYAN)
        print(f"\n  {paint(entity, BOLD)}")
        print(
            paint(f"    novel event {short(novel[entity])}  “{novel_text.get(entity, '')}”", GREY)
        )
        run_change = f"{short(prior.run_id)} {arrow} {paint(short(after.run_id), BOLD)}"
        trigger = paint("[" + ", ".join(reasons) + "]", CYAN) if reasons else paint("[—]", GREY)
        print(f"    re-derived by run {run_change}   trigger {trigger}")
        delta = paint(f"(+{len(added)})", GREEN if added else GREY)
        print(
            f"    citations {len(prior.citations)} {arrow} {len(after.citations)} {delta}"
            + ("  " + paint("✓ cites this round's new event", GREEN) if folds_novel else "")
        )
        print(paint(f"    before: {prior.text or '(none yet)'}", GREY))
        print(f"    after:  {paint(after.text or '(none yet)', GREEN)}")

        report.check(
            f"round {round_index + 1} re-derived the preferences slot for {entity}",
            rederived,
            f"run {run_change}",
        )
        report.check(
            f"round {round_index + 1} folded the novel event into the slot for {entity}",
            folds_novel,
            f"{len(added)} new citation(s)",
        )
        if prior.exists:
            report.check(
                f"round {round_index + 1} preserved earlier evidence (accumulated) for {entity}",
                preserved,
                f"{len(prior.citations)} prior citation(s) still cited",
            )

    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contacts", type=int, default=int(os.environ.get("SMOKE_CONTACTS", "3")))
    parser.add_argument(
        "--fillers-per-round",
        type=int,
        default=int(os.environ.get("SMOKE_FILLERS_PER_ROUND", "1")),
        help="routine low-signal events per round (history noise; the preference "
        "fires the trigger). Keep this small so the changes window stays focused",
    )
    parser.add_argument(
        "--timeout", type=float, default=float(os.environ.get("SMOKE_TIMEOUT", "90"))
    )
    args = parser.parse_args()
    if not 1 <= args.contacts <= len(CONTACTS):
        parser.error(f"--contacts must be between 1 and {len(CONTACTS)}")
    if args.fillers_per_round < 0:
        parser.error("--fillers-per-round must be non-negative")

    api_key = os.environ.get("MEMSEEK_API_KEY")
    if not api_key:
        raise SystemExit("MEMSEEK_API_KEY is required; create a workspace first")
    base_url = os.environ.get("MEMSEEK_BASE_URL", "http://127.0.0.1:8000")
    print_workspace_explorer(api_url=base_url, api_key=api_key)

    rounds = generate_rounds(args.contacts, args.fillers_per_round)
    entities = sorted({event["entity"] for batch in rounds for event in batch})
    report = Report()

    banner("MEMSEEK · SDK CRM profile accumulation smoke test")
    note(
        "watch each contact's `preferences` slot grow 1 → 2 → 3 facts as evidence "
        "arrives in separate rounds — proof the profile accumulates, not overwrites",
        indent=0,
    )
    note(f"talking to {base_url}", indent=0)

    async with MemseekClient(base_url, api_key) as client:
        banner("Publishing catalog")
        note(f"registering package {PACKAGE} from {CATALOG_ROOT.name}/")
        catalog = await client.catalog.publish(package=PACKAGE, directory=CATALOG_ROOT)
        report.check(
            "catalog published",
            catalog.get("package", {}).get("name") == "crm_user_profile",
            f"hash {str(catalog.get('catalog_hash'))[:12]}…",
        )
        dump("catalog", catalog)

        total = sum(len(batch) for batch in rounds)
        banner("Ingesting events")
        note(
            f"{total} events across {len(entities)} contact(s) in {len(rounds)} round(s); "
            f"each round waits for its derivation before the next begins"
        )
        for round_index, batch in enumerate(rounds):
            ok = await ingest_round(
                client,
                round_index=round_index,
                total_rounds=len(rounds),
                events=batch,
                entities=entities,
                report=report,
                timeout_s=args.timeout,
            )
            if not ok:
                return report.summary()

        banner("Final verification")
        note("re-checking each contact end to end: profile, audit, search, and rendered brief")
        for entity in entities:
            await verify_contact(client, entity, report, args.timeout)

    return report.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
