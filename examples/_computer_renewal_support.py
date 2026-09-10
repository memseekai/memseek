"""Bootstrap, fixtures, and presentation for the renewal examples."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from _computer_common import (
    _C,
    ACCOUNT,
    ANALYST,
    BCYAN,
    BGREEN,
    BMAG,
    BOLD,
    BYELLOW,
    CATALOG_ROOT,
    CYAN,
    ENTITY,
    EVIDENCE,
    GREEN,
    GREY,
    MAGENTA,
    OBSERVATIONS,
    PACKAGE,
    POLICY,
    PROPOSALS,
    RED,
    RESEARCH_COMPUTER,
    RISKS,
    RUN,
    TERMS,
    YELLOW,
    block,
    header,
    note,
    paint,
    rule,
    short,
    wrap,
)
from _computer_runtime import ComputerRuntime
from _workspace_explorer import print_workspace_explorer

# FastAPI resolves handler annotations from module globals, so `Request` has to
# be imported here rather than beside the app that uses it.
from memseek.config import get_settings
from memseek.sdk import MemseekClient, MemseekHTTPError


# ---------------------------------------------------------------------------
# The Memseek side: everything below drives the public API through the SDK.
# ---------------------------------------------------------------------------
class Demo:
    def __init__(self, client: MemseekClient) -> None:
        self.c = client
        self.invocation: str | None = None
        self.session: str | None = None
        self.cursor = 0
        # Ids seen this session, so `attach` accepts a short prefix for them.
        # An invocation from another process has to be named in full: there is
        # no list endpoint, and inventing one here would hide that the id is
        # the whole handle you need to carry between processes.
        self.known: list[str] = []

    # -- catalog -----------------------------------------------------------
    async def publish(self) -> dict[str, Any]:
        """Publish the fixture, with its Computers pointed at a live provider.

        A workspace gets its catalog by publishing one. The only edit the demo
        makes is the one the documentation names: a Computer's `provider`. Every
        Program, Agent, policy, artifact, and derivation reference is untouched.
        """

        files = {
            path.relative_to(CATALOG_ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(CATALOG_ROOT.rglob("*.yaml"))
        }
        rewritten = {
            name: text.replace("provider: fake", "provider: cloudflare")
            for name, text in files.items()
        }
        if rewritten != files:
            note("published with provider: cloudflare (the fixture pins fake for CI)")
        return await self.c.catalog.publish_files(package=PACKAGE, files=rewritten)

    # -- writes ------------------------------------------------------------
    async def ingest(self, kind: str, text: str, key: str | None) -> str:
        result = await self.c.records.ingest(
            entity=ENTITY,
            collection=EVIDENCE,
            type="evidence",
            key=key,
            text=text,
            content={"kind": kind},
        )
        rows = result.get("inserted", []) + result.get("duplicates", [])
        return str(rows[0]["id"])

    async def wait_ready(self, ids: list[str], timeout_s: float = 60.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        pending = list(ids)
        while pending:
            if (await self.c.record(pending[-1])).get("ready"):
                pending.pop()
                continue
            if asyncio.get_running_loop().time() >= deadline:
                raise SystemExit("records were never enriched — is `memseek worker` running?")
            await asyncio.sleep(0.4)

    # -- reads -------------------------------------------------------------
    async def timeline(
        self, collection: str, *, status: str = "active", limit: int = 20
    ) -> list[dict[str, Any]]:
        """Compact rows, newest first. Cheap enough to poll in a loop."""

        page = await self.c._request(
            "GET",
            "/timeline",
            params={
                "entity": ENTITY,
                "collections": collection,
                "status": status,
                "limit": limit,
            },
        )
        return list(page.get("records", []))

    async def rows(
        self, collection: str, *, status: str = "active", limit: int = 20
    ) -> list[dict[str, Any]]:
        """Timeline rows dereferenced to full records — content, provenance, scores."""

        summaries = await self.timeline(collection, status=status, limit=limit)
        return [await self.c.record(str(row["id"])) for row in summaries]

    async def wait_for(
        self, collection: str, *, minimum: int = 1, timeout_s: float = 90.0, why: str = ""
    ) -> list[dict[str, Any]]:
        deadline = asyncio.get_running_loop().time() + timeout_s
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        tick = 0
        while True:
            rows = await self.timeline(collection)
            if len(rows) >= minimum:
                if _C and why:
                    sys.stdout.write("\r" + " " * 72 + "\r")
                return rows
            if asyncio.get_running_loop().time() >= deadline:
                if _C and why:
                    sys.stdout.write("\r" + " " * 72 + "\r")
                return rows
            if _C and why:
                sys.stdout.write(f"\r  {paint(spin[tick % len(spin)], CYAN)} {paint(why, GREY)}")
                sys.stdout.flush()
            tick += 1
            await asyncio.sleep(0.5)

    # -- derivations -------------------------------------------------------
    async def derive(self, name: str, *, timeout_s: float = 150.0) -> dict[str, Any] | None:
        """Run one derivation now and return its newest audited run."""

        job = await self.c.run_processor(name, entity=ENTITY)
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            state = (await self.c.job(job["job_id"])).get("state")
            if state in {"done", "dead"} or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.5)
        return await self.latest_run(name)

    @staticmethod
    def report_run(run: dict[str, Any] | None) -> None:
        content = ((run or {}).get("run", {}) or {}).get("content", {}) or {}
        if content.get("status") == "failed":
            print(paint(f"  the run failed: {content.get('error')}", RED))

    async def latest_run(self, name: str) -> dict[str, Any] | None:
        runs = await self.c.runs(entity=ENTITY, processor=name, operation="derive", limit=5)
        for summary in runs.get("runs", []):
            return await self.c.run(summary["id"])
        return None

    # -- invocations -------------------------------------------------------
    async def start(self, prompt: str, *, session: dict[str, Any] | None = None) -> dict[str, Any]:
        started = await self.c.invocations.start(
            entity=ENTITY,
            computer=RESEARCH_COMPUTER,
            executor={"kind": "agent", "agent": ANALYST, "context_policy": POLICY},
            task={"kind": "answer", "prompt": prompt},
            session=session or {"mode": "new"},
        )
        self.invocation = started["invocation_id"]
        self.session = started["session_id"]
        self.cursor = 0
        self.known.append(self.invocation)
        return await self.settle()

    async def reply(self, prompt: str) -> dict[str, Any]:
        assert self.invocation is not None
        await self.c.invocations.continue_(self.invocation, prompt=prompt)
        return await self.settle()

    async def settle(self, *, timeout_s: float = 300.0) -> dict[str, Any]:
        """Stream one turn; a silent stream and fallback share one deadline."""
        import httpx

        assert self.invocation is not None
        try:
            async with asyncio.timeout(timeout_s):
                stream = self.c.invocations.stream(self.invocation, after=self.cursor)
                try:
                    async for event in stream:
                        self.cursor = max(self.cursor, int(event["ordinal"]))
                        await show_event(event)
                        if str(event["kind"]) in _PAUSING_EVENTS:
                            break
                except MemseekHTTPError, httpx.TransportError:
                    pass
                finally:
                    await stream.aclose()
                while True:
                    state = await self.c.invocations.retrieve(self.invocation)
                    await self.drain_events()
                    if state["status"] in _RESTING_STATUSES:
                        return state
                    await asyncio.sleep(0.4)
        except TimeoutError as exc:
            raise TimeoutError(
                f"Invocation {self.invocation} did not rest within {timeout_s}s"
            ) from exc

    async def follow(self, *, timeout_s: float = 300.0) -> dict[str, Any]:
        assert self.invocation is not None
        return await self.c.invocations.attach(self.invocation).wait(timeout_s=timeout_s)

    async def drain_events(self) -> list[dict[str, Any]]:
        assert self.invocation is not None
        page = await self.c.invocations.events(self.invocation, after=self.cursor, limit=100)
        events = list(page.get("events", []))
        if events:
            self.cursor = int(page.get("cursor", self.cursor))
            print_events(events)
        return events

    async def attach(self, invocation_id: str) -> dict[str, Any]:
        """Pick up an invocation this process did not start.

        Nothing about a durable invocation lives in this script. Killing the
        demo mid-turn loses the terminal, not the work: the journal, the session
        and the pause are all rows, so another process — or this one, restarted
        — can attach to the same id and carry the conversation on.
        """

        state = await self.c.invocations.retrieve(invocation_id)
        self.invocation = invocation_id
        self.session = str(state.get("session_id") or "") or None
        self.cursor = 0
        if invocation_id not in self.known:
            self.known.append(invocation_id)
        return state

    async def find_invocation(self, wanted: str) -> str | None:
        """Resolve what someone typed to an invocation id, if it resolves.

        A full id is taken at face value and looked up — that is the whole point
        of `attach`, and it is how you carry a paused conversation from one
        process to another. A prefix only works for ids this session has already
        printed, because Memseek exposes no listing to search.
        """

        for candidate in self.known:
            if candidate.startswith(wanted):
                return candidate
        try:
            UUID(wanted)
        except ValueError:
            return None
        try:
            await self.c.invocations.retrieve(wanted)
        except MemseekHTTPError:
            return None
        return wanted


# A pause is not a terminal state, so the server keeps the event stream open
# across it. These are the entries after which control belongs to the person.
_PAUSING_EVENTS = frozenset(
    {"awaiting_input", "completed", "failed", "cancelled", "context_exhausted"}
)
_RESTING_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "awaiting_input", "context_exhausted"}
)


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------
_EVENT_COLOR = {
    "queued": GREY,
    "started": CYAN,
    "user_turn": BCYAN,
    "awaiting_input": YELLOW,
    "writeback_ingested": BGREEN,
    "completed": GREEN,
    "failed": RED,
    "context_exhausted": RED,
    "execution_interrupted": YELLOW,
    "tool_call": BCYAN,
    "tool_result": CYAN,
    "file_change": MAGENTA,
    "command": MAGENTA,
    "provider_note": GREY,
    "model_request": BMAG,
    "model_step": MAGENTA,
}

# Only the stand-in returns a whole turn instantly, which makes its journal
# arrive as one burst. Pacing the *printing* by a beat keeps that readable
# without pretending work is still happening — and it is switched off entirely
# when a real Cloudflare Agent is driving, because then the arrival times are
# the model's own and are worth seeing unaltered.
EVENT_PACE = 0.0


def compact(value: Any) -> str:
    """One line for a tool's input or output, whatever shape the provider used."""

    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        # A single-key wrapper is noise; show what is inside it.
        if len(value) == 1:
            return compact(next(iter(value.values())))
        return " ".join(f"{key}={compact(item)}" for key, item in sorted(value.items()))
    if isinstance(value, list):
        return f"{len(value)} item(s): " + "; ".join(compact(item) for item in value[:2])
    return str(value)


def event_detail(kind: str, payload: dict[str, Any]) -> str:
    """One line for one journal entry, in the entry's own terms."""

    if kind == "queued":
        refs = payload.get("definition_refs") or {}
        executor = (refs.get("executor") or refs.get("agent") or {}).get("ref", "?")
        return f"{executor} on {(refs.get('computer') or {}).get('ref', '?')}"
    if kind == "started":
        return f"turn handed to {payload.get('executor', '?')}"
    if kind == "completed":
        return f"{len(payload.get('citation_ids') or [])} citation(s) accepted"
    if kind == "writeback_ingested":
        return (
            f"{len(payload.get('inserted_ids') or [])} record(s), "
            f"{payload.get('draft_count', 0)} held for review · "
            f"{', '.join(payload.get('paths') or [])}"
        )
    if kind == "user_turn":
        return f"you said: {str(payload.get('prompt', ''))[:60]}"
    if kind == "awaiting_input":
        return "paused — the journal is durable until someone answers"
    if kind == "tool_call":
        return f"{payload.get('tool_name') or '?'} {compact(payload.get('input'))[:56]}"
    if kind == "tool_result":
        return f"└─ {compact(payload.get('output'))[:64]}"
    if kind == "provider_note":
        return str(payload.get("reason", ""))[:70]
    if kind == "model_request":
        budget = payload.get("prompt_budget") or {}
        if budget:
            # Worth surfacing: the tool schemas are charged on every request and
            # appear nowhere in what Memseek handed the provider.
            return (
                f"{payload.get('target', '?')} · prompt {budget.get('total_bytes', '?')}B "
                f"= system {budget.get('system_bytes', '?')} + input "
                f"{budget.get('input_bytes', '?')} + tool schemas "
                f"{budget.get('tool_bytes', '?')}"
            )
        return str(payload.get("target", ""))[:70]
    if kind == "model_step":
        usage = payload.get("usage") or {}
        tokens = usage.get("totalTokens") or usage.get("total_tokens")
        return f"step {payload.get('index', '?')} · {payload.get('finish_reason', '?')}" + (
            f" · {tokens} tokens" if tokens else ""
        )
    if kind == "file_change":
        # The stand-in knows what it wrote; the real Worker reports a snapshot
        # diff of before/after hashes, with no size.
        if payload.get("bytes") is not None:
            return f"{payload.get('path')} · {payload['bytes']} bytes" + (
                f" · sha256 {short(str(payload['sha256']))}" if payload.get("sha256") else ""
            )
        before, after = payload.get("before_sha256"), payload.get("after_sha256")
        if after is None:
            return f"{payload.get('path')} · deleted"
        return f"{payload.get('path')} · {'created' if before is None else 'changed'} → {short(str(after))}"
    if kind == "failed":
        return str(payload.get("error_kind", ""))
    if kind == "execution_interrupted":
        return f"{payload.get('error_kind')} — requeued, history intact"
    return json.dumps(payload, sort_keys=True)[:70] if payload else ""


def print_event(event: dict[str, Any]) -> None:
    kind = str(event["kind"])
    color = _EVENT_COLOR.get(kind, MAGENTA)
    detail = event_detail(kind, dict(event.get("payload") or {}))
    print(
        f"    {paint(f'{event["ordinal"]:>3}', GREY)} {paint(f'{kind:<20}', color)}"
        f" {paint(detail, GREY)}"
    )


def print_events(events: list[dict[str, Any]]) -> None:
    for event in events:
        print_event(event)


async def show_event(event: dict[str, Any]) -> None:
    """Print one entry, then yield the loop for the reading beat.

    `await` rather than `sleep`: this process may also be serving the Computer
    runtime the worker is talking to, and a blocking pause here would stall the
    very job whose journal is being printed.
    """

    print_event(event)
    if EVENT_PACE and _C:
        await asyncio.sleep(EVENT_PACE)


def print_records(rows: list[dict[str, Any]], glyph: str, color: str) -> None:
    if not rows:
        print(paint("      (none)", GREY))
        return
    for row in rows:
        content = row.get("content") or {}
        text = row.get("text") or content.get("text") or ""
        badge = ""
        if content.get("severity"):
            badge = paint(f"[{content['severity']}] ", BOLD, YELLOW)
        if row.get("status") == "draft":
            badge += paint("[draft · review required] ", BOLD, BYELLOW)
        lines = wrap(str(text))
        print(
            f"    {paint(glyph, color)} {paint(short(str(row.get('id'))), BOLD)} {badge}".rstrip()
        )
        for line in lines:
            print(paint(f"        {line}", color))


def print_receipt(run: dict[str, Any] | None) -> None:
    """Show what the Computer actually returned, and against which definitions."""

    trace = ((run or {}).get("run", {}).get("content", {}) or {}).get("computer_trace") or []
    for entry in trace:
        refs = entry.get("definition_refs") or {}
        print(f"    {paint('receipt', BOLD)} task {paint(str(entry.get('task')), CYAN)}")
        print(
            paint(
                f"        provider {entry.get('provider')} · backend {entry.get('backend')} · "
                f"output sha256 {short(str(entry.get('output_sha256')))}",
                GREY,
            )
        )
        for name, ref in sorted(refs.items()):
            print(
                paint(
                    f"        {name:<15} {ref.get('ref')} @ {short(str(ref.get('hash')))}",
                    GREY,
                )
            )


# ---------------------------------------------------------------------------
# The account's evidence.
#
# The contract arrives UNKEYED, as an event: `contract_extract` reads unkeyed
# arrivals (`keyed: false`), so that write — and only that write — fires the
# deterministic Program. The standing account facts are KEYED, which is what the
# Agent's mounted artifact renders (a document block reads keyed rows). One
# collection, two roles.
# ---------------------------------------------------------------------------
CONTRACT = (
    "Acme Cloud master agreement §7.3: the platform tier carries a 99.95% quarterly "
    "uptime commitment, with service credits applied against the renewal term."
)
STANDING = [
    (
        "meeting",
        "qbr-2024-06",
        "June QBR minutes: our VP of Customer Success told Acme that if quarterly uptime "
        "misses 99.95%, they get a 12% discount on the renewal. It was never written into "
        "the contract.",
    ),
    (
        "email",
        "procurement-thread",
        "Acme procurement wants the renewal quote by Friday and says next year's budget is "
        "flat year over year.",
    ),
    (
        "incident",
        "incident-q3",
        "Q3 closed at 99.91% uptime after the June 14 control-plane outage.",
    ),
]

# What a piped run types. The two bare lines in the middle are the point: at a
# pause, talking to the Agent is just talking, and it takes two answers to get
# a proposal out of it.
SCRIPTED_SESSION = [
    "risks",
    "drafts",
    "recall discount",
    "memory",
    "incident Q4 is tracking at 99.89% uptime after two regional failovers.",
    "assess",
    "ask Draft the renewal position for Friday's quote.",
    "Keep it at 12% and name the outage explicitly.",
    "Trade half of it for a two-year term; anything deeper needs the CFO.",
    "happened",
    "attach",
    "events",
    "fork Model the downside if legal voids the verbal promise.",
    "cancel",
    "terms",
    "receipt contract_extract",
]


async def ainput(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


# ---------------------------------------------------------------------------
# Acts.
# ---------------------------------------------------------------------------
async def act_program(demo: Demo) -> None:
    """Not everything needs a model, and this path could not call one anyway."""

    header("ACT I · PROGRAM", "deterministic code, in a Computer, with no model at all")
    note("ingesting the signed contract — its write fires the `contract_extract` trigger")
    await demo.wait_ready([await demo.ingest("contract", CONTRACT, key=None)])
    await demo.wait_for(TERMS, why="the worker is running the Program in fast_workspace@1")
    print(paint("\n  contract_terms emitted by the Program:", BOLD))
    print_records(await demo.rows(TERMS), "▪", CYAN)
    run = await demo.latest_run("contract_extract")
    content = (run or {}).get("run", {}).get("content", {}) or {}
    print()
    print(
        f"    {paint('model calls', BOLD)}: {paint(str(len(content.get('model_calls') or [])), BGREEN)}"
        f"   {paint('— the pipeline caps LLM calls at 0; this path cannot call one', GREY)}"
    )
    print_receipt(run)


async def act_agent(demo: Demo) -> None:
    """The connection: a verbal promise, joined to the outage that triggers it.

    The result comes back through the derivation's ordinary `emit` boundary, so
    a Computer-backed conclusion is not a privileged kind of record — it is
    cited, versioned, and openable like any other.
    """

    header("ACT II · AGENT", "the nightly assessment, running as a Computer-backed derivation")
    note("seeding the standing account facts — the buried promise, and the incident")
    ids = [await demo.ingest(kind, text, key=key) for kind, key, text in STANDING]
    await demo.wait_ready(ids)
    note("`renewal_assessment` is a cron derivation; running it now instead of at 02:00")
    run = await demo.derive("renewal_assessment")
    demo.report_run(run)
    await demo.wait_for(RISKS, why="the Agent is working in research_workspace@1")
    rows = await demo.rows(RISKS)
    print(paint("\n  renewal_risks emitted through the derivation's `emit` boundary:", BOLD))
    print_records(rows, "▲", YELLOW)
    print()
    print_receipt(run)
    if rows:
        print(paint("\n  GLASS BOX — the risk opens to the records it cited:", BOLD))
        await glass_box(demo, str(rows[0]["id"]))


# The answers the demo gives itself when nobody is at the keyboard. They are
# the *fallbacks*, not the script: a person at a TTY types over each one, and
# the Agent decides how many it needs, so the loop never assumes a count.
SCRIPTED_ANSWERS = [
    "Hold at the promised 12%; anything larger needs the CFO.",
    "Trade half of it for a two-year committed term, and name the outage in the quote.",
]


def pending_question(state: dict[str, Any]) -> str:
    """What the Agent is blocked on, whatever shape it phrased it in.

    The stand-in answers with a `question` field because this demo's catalog
    asks it to. A real Cloudflare Agent composes its own envelope, so fall back
    to showing the whole value rather than a blank prompt — the demo must not
    depend on a model having chosen our key name.
    """

    value = (state.get("result") or {}).get("value")
    if isinstance(value, dict):
        for field in ("question", "prompt", "ask", "blocked_on_question"):
            if isinstance(value.get(field), str) and value[field].strip():
                return str(value[field]).strip()
        return json.dumps(value, indent=2, sort_keys=True)[:900]
    return json.dumps(value, indent=2, sort_keys=True)[:900] if value else "(no question given)"


async def converse(
    demo: Demo,
    state: dict[str, Any],
    *,
    interactive: bool,
    label: str = "your answer",
    fallbacks: list[str] | None = None,
    max_turns: int = 8,
) -> dict[str, Any]:
    """Talk to a running invocation for as long as it keeps asking.

    This is the whole point of a durable invocation, so the loop is deliberately
    dumb: while the Agent is paused, show what it asked, take a line, send it,
    and watch the next turn stream in. Nothing here knows how many pauses to
    expect — the stand-in takes two, a real Agent takes as many as it needs, and
    a person can end the conversation at any pause by answering `stop`.
    """

    queued = list(fallbacks or SCRIPTED_ANSWERS)
    for _ in range(max_turns):
        if state["status"] != "awaiting_input":
            return state
        steps = (state.get("result") or {}).get("steps", "?")
        print(paint(f"\n  the Agent paused after {steps} step(s) and asked:", BOLD))
        block(pending_question(state), BCYAN)
        note("this pause is a row, not a blocked thread — nothing is holding it open")
        if demo.invocation is not None:
            note(f"another terminal can take over from here: attach {demo.invocation}")

        answer = queued.pop(0) if queued else "Proceed on your best reading of the evidence."
        if interactive:
            try:
                typed = (await ainput(paint(f"\n  {label} ▸ ", BOLD, BCYAN))).strip()
            except EOFError, KeyboardInterrupt:
                print(paint("\n  left paused. It is still there — `attach` to pick it up.", YELLOW))
                return state
            if typed.lower() in {"stop", "cancel"}:
                assert demo.invocation is not None
                return await demo.c.invocations.cancel(demo.invocation)
            answer = typed or answer
        else:
            print(paint(f"\n  {label} ▸ {answer}", BOLD, BCYAN))
        state = await demo.reply(answer)
    print(paint(f"  stopping after {max_turns} turns", YELLOW))
    return state


async def act_invocation(demo: Demo, *, interactive: bool) -> None:
    """Work that outlives a process, and that you hold a conversation with.

    The pause is a row in PostgreSQL rather than a blocked thread, so the
    exchange below is not one process waiting on stdin: each answer is a
    `user_turn` appended to a journal, and each resumption is a fresh job that
    replays every earlier answer. That is what lets it pause twice, and why the
    second question can widen what the Agent is able to cite.

    What it may write on the far side was decided in the catalog, not by the
    Agent: an observation lands active, a new commercial commitment lands as a
    draft for review.
    """

    header("ACT III · DURABLE INVOCATION", "the same Agent, as a session you can talk to")
    note("a separate answering session over the same account — the account is the data")
    note("boundary; neither Computer is registered to it")
    note("the journal below is streamed from the server as it is written, not replayed after")
    state = await demo.start("Prepare the renewal position for this account before Friday's quote.")
    state = await converse(demo, state, interactive=interactive, label="your guardrail")

    if state["status"] != "succeeded":
        print(paint(f"\n  the invocation is {state['status']}", YELLOW))
        if state["status"] == "awaiting_input":
            note("still paused and still durable — answer it in Act IV with a bare line")
        elif state.get("error"):
            block(json.dumps(state["error"]), RED)
        return

    value = (state.get("result") or {}).get("value") or {}
    print(paint("\n  the Agent's brief:", BOLD))
    block(str(value.get("brief") or json.dumps(value, sort_keys=True)), GREEN)
    await explain(demo)


async def explain(demo: Demo) -> None:
    """Exactly what happened, in the order it happened, from the server's rows.

    Everything printed here is read back out of Memseek — the journal, the
    accepted citations, the receipt's definition hashes, the write-back routing.
    None of it is this script's recollection of what it just did, which is the
    only version worth showing: it is what somebody auditing the decision in six
    months would be able to recover.
    """

    assert demo.invocation is not None
    state = await demo.c.invocations.retrieve(demo.invocation)
    result = state.get("result") or {}
    receipt = result.get("receipt") or {}
    refs = state.get("definition_refs") or {}

    print(paint("\n  WHAT HAPPENED — read back from the server, not from this script:", BOLD))

    page = await demo.c.invocations.events(demo.invocation, after=0, limit=200)
    events = list(page.get("events", []))
    turns = [event for event in events if event["kind"] == "user_turn"]
    tools = [event for event in events if event["kind"] == "tool_call"]
    print(
        paint(
            f"      {len(events)} journal entries · {len(tools)} tool call(s) · "
            f"{len(turns)} answer(s) from you · status {state['status']}",
            GREY,
        )
    )

    accepted = list(result.get("citation_ids") or [])
    print(f"\n    {paint('citations', BOLD)} the Agent returned {len(accepted)}, all accepted:")
    for record_id in accepted:
        try:
            row = await demo.c.record(record_id)
        except MemseekHTTPError:
            print(paint(f"      {short(record_id)} (not readable)", YELLOW))
            continue
        print(
            f"      {paint(short(record_id), BOLD)} "
            f"{paint(str(row.get('key') or row.get('type') or ''), MAGENTA)}"
        )
        block(str((row.get("content") or {}).get("text", "")), YELLOW, lead="         ")
    note("a returned id outside the mounted set fails the whole turn; none were dropped here")

    print(
        f"\n    {paint('receipt', BOLD)} provider "
        f"{receipt.get('provider') or state.get('provider')} · "
        f"backend {receipt.get('backend')} · {receipt.get('bytes', '?')} bytes · "
        f"{(state.get('result') or {}).get('steps', '?')} step(s) in the last turn · "
        f"output sha256 {short(str(receipt.get('output_sha256')))}"
    )
    for name, ref in sorted(refs.items()):
        if isinstance(ref, dict):
            print(
                paint(f"        {name:<15} {ref.get('ref')} @ {short(str(ref.get('hash')))}", GREY)
            )

    print(f"\n    {paint('write-back', BOLD)} the outbox, routed by the Computer definition:")
    print(
        paint("      /outbox/observations.jsonl → task_observations   review: false → active", GREY)
    )
    print_records(await demo.rows(OBSERVATIONS), "◆", GREEN)
    print(
        paint("      /outbox/proposals          → renewal_proposals   review: TRUE  → draft", GREY)
    )
    print_records(await demo.rows(PROPOSALS, status="draft"), "◇", BYELLOW)
    note("the commercial commitment is a draft because the Computer definition says")
    note("`review: true` — not because the Agent behaved well. It cannot promote its own promise.")

    artifacts = await demo.c.invocations.artifacts(demo.invocation)
    preserved = artifacts.get("artifacts", [])
    if preserved:
        print(f"\n    {paint('preserved', BOLD)} what survives the workspace being torn down:")
    for item in preserved:
        print(
            paint(
                f"      {item['path']} · {item['size_bytes']} bytes · "
                f"sha256 {short(item['sha256'])}",
                GREY,
            )
        )


# ---------------------------------------------------------------------------
# Interactive session.
# ---------------------------------------------------------------------------
async def glass_box(demo: Demo, record_id: str) -> None:
    try:
        record = await demo.c.record(record_id)
    except MemseekHTTPError:
        print(paint(f"  no record {record_id}", YELLOW))
        return
    content = record.get("content") or {}
    print(
        f"    {paint(short(record['id']), BOLD)} "
        f"{paint(f'{record.get("collection")}/{record.get("type")}', CYAN)} "
        f"{paint(f'depth {record.get("depth")}', GREY)}"
    )
    block(str(content.get("text", "")), GREEN, lead="      ")
    if record.get("run_id"):
        print(paint(f"      written by run {short(str(record['run_id']))}", GREY))
    parents = list(record.get("derived_from") or [])
    if not parents:
        print(paint("      (an original — nothing was derived to reach it)", GREY))
    for parent_id in parents:
        try:
            parent = await demo.c.record(parent_id)
        except MemseekHTTPError:
            continue
        if parent.get("collection") == "_system":
            continue  # the audited run itself; named above
        parent_content = parent.get("content") or {}
        print(
            f"      {paint('└─', GREY)} {paint(short(parent['id']), BOLD)} "
            f"{paint(f'{parent.get("collection")}/{parent.get("type")}', CYAN)} "
            f"{paint(str(parent.get('key') or ''), MAGENTA)}"
        )
        block(str(parent_content.get("text", "")), YELLOW, lead="         ")


HELP = f"""
  {paint("You are talking to a renewal desk backed by two Computers.", BOLD)}

  {paint("contract <text>", BCYAN)}     file an unkeyed contract event → fires the Program
  {paint("meeting|email|incident <text>", BCYAN)}
                        add a keyed standing fact the Agent can read and cite
  {paint("assess", BCYAN)}              run the nightly assessment derivation now
  {paint("ask <prompt>", BCYAN)}        start a durable invocation (it may pause and ask)
  {paint("<anything else>", BCYAN)}     while the Agent is paused, a bare line IS your answer
  {paint("reply <text>", BCYAN)}        the same thing, said explicitly
  {paint("happened", BCYAN)}            exactly what the last invocation did, read back from
                        the server: journal, citations, receipt, write-back
  {paint("attach <id-prefix>", BCYAN)}  pick up any invocation, including one this process
                        never started — the work is rows, not a thread
  {paint("fork <prompt>", BCYAN)}       branch the session into a new invocation
  {paint("cancel", BCYAN)}              cancel the current invocation
  {paint("events", BCYAN)}              replay this invocation's ordered journal
  {paint("memory", BCYAN)}              the Evidence Spine: working brief + episode receipts
  {paint("recall <query>", BCYAN)}      search this session's journal without widening scope
  {paint("drafts", BCYAN)}              writeback held for human review
  {paint("risks | terms | evidence | observations", BCYAN)}
                        list what the account currently holds
  {paint("why <id-prefix>", BCYAN)}     open any conclusion to the records it cited
  {paint("receipt [derivation]", BCYAN)}  the last run's Computer receipt
  {paint("help", BCYAN)}                show this
  {paint("quit", BCYAN)}                leave
"""

# Verbs that mean something even while the Agent is waiting on you. Anything
# else typed at a pause is treated as the answer, because at a pause that is
# overwhelmingly what a person means — having to prefix `reply` to talk to
# something that just asked you a question is the wrong shape.
_VERBS = frozenset(
    {
        "quit",
        "exit",
        "q",
        "help",
        "?",
        "h",
        "assess",
        "ask",
        "reply",
        "happened",
        "attach",
        "fork",
        "cancel",
        "events",
        "memory",
        "recall",
        "drafts",
        "why",
        "receipt",
        "risks",
        "terms",
        "evidence",
        "observations",
        "contract",
        "meeting",
        "email",
        "incident",
    }
)

_KINDS = {"contract", "meeting", "email", "incident"}
_LISTS = {
    "risks": (RISKS, "▲", YELLOW),
    "terms": (TERMS, "▪", CYAN),
    "evidence": (EVIDENCE, "•", GREY),
    "observations": (OBSERVATIONS, "◆", GREEN),
}


async def handle(demo: Demo, line: str) -> bool:
    line = line.strip()
    if not line:
        return True
    verb, _, rest = line.partition(" ")
    verb = verb.lower()
    rest = rest.strip()

    # A paused Agent asked you something. Answer it the way you would answer a
    # person: by talking. Only a known verb is read as a command here.
    if verb not in _VERBS and demo.invocation is not None:
        state = await demo.c.invocations.retrieve(demo.invocation)
        if state["status"] == "awaiting_input":
            report_invocation(await demo.reply(line))
            return True

    if verb in {"quit", "exit", "q"}:
        return False
    if verb in {"help", "?", "h"}:
        print(HELP)
        return True
    if verb in _LISTS:
        collection, glyph, color = _LISTS[verb]
        print_records(await demo.rows(collection), glyph, color)
        return True
    if verb == "drafts":
        rows = await demo.rows(PROPOSALS, status="draft")
        print(paint(f"  {len(rows)} proposal(s) awaiting human review:", BOLD))
        print_records(rows, "◇", BYELLOW)
        return True
    if verb in _KINDS:
        if not rest:
            print(paint(f"  usage: {verb} <text>", YELLOW))
            return True
        key = None if verb == "contract" else f"{verb}-{secrets.token_hex(2)}"
        before = len(await demo.timeline(TERMS))
        await demo.wait_ready([await demo.ingest(verb, rest, key=key)])
        if verb == "contract":
            await demo.wait_for(TERMS, minimum=before + 1, why="the Program is extracting the term")
            fresh = await demo.rows(TERMS)
            print_records(fresh[: max(1, len(fresh) - before)], "▪", CYAN)
        else:
            note("stored, scored, and citable — run `assess` or `ask` to put it to work")
        return True
    if verb == "assess":
        run = await demo.derive("renewal_assessment")
        demo.report_run(run)
        print_records(await demo.rows(RISKS), "▲", YELLOW)
        print_receipt(run)
        return True
    if verb == "ask":
        if not rest:
            print(paint("  usage: ask <prompt>", YELLOW))
            return True
        state = await demo.start(rest)
        report_invocation(state)
        return True
    if verb == "reply":
        if demo.invocation is None:
            print(paint("  no invocation yet — start one with `ask <prompt>`", YELLOW))
            return True
        if not rest:
            print(paint("  usage: reply <text>", YELLOW))
            return True
        report_invocation(await demo.reply(rest))
        return True
    if verb == "fork":
        if demo.session is None:
            print(paint("  nothing to fork — start one with `ask <prompt>`", YELLOW))
            return True
        note(f"forking session {short(demo.session)} — the parent stays exactly as it was")
        state = await demo.start(
            rest or "Re-run the renewal position under a stricter guardrail.",
            session={"mode": "fork", "session_id": demo.session},
        )
        report_invocation(state)
        return True
    if verb == "cancel":
        if demo.invocation is None:
            print(paint("  no invocation to cancel", YELLOW))
            return True
        state = await demo.c.invocations.cancel(demo.invocation)
        print(paint(f"  invocation {short(demo.invocation)} is {state['status']}", YELLOW))
        return True
    if verb == "happened":
        if demo.invocation is None:
            print(paint("  no invocation yet — start one with `ask <prompt>`", YELLOW))
            return True
        await explain(demo)
        return True
    if verb == "attach":
        if not rest and demo.invocation is not None:
            # Re-attach the current one: drop every scrap of in-process state
            # for it and rebuild from the server. Worth doing at least once —
            # it is the same code path a different process would take.
            rest = demo.invocation
            note("re-attaching from scratch; this process keeps nothing it cannot re-read")
        if not rest:
            print(paint("  usage: attach <invocation-id>", YELLOW))
            return True
        found = await demo.find_invocation(rest)
        if found is None:
            print(
                paint(
                    f"  nothing here starts with {rest}. An invocation this process never "
                    "printed has to be named in full — there is no listing to search.",
                    YELLOW,
                )
            )
            return True
        mine = found in demo.known
        state = await demo.attach(found)
        note(
            f"attached to {short(found)}"
            + (" — rebuilt from the server's rows alone" if mine else " — started elsewhere")
        )
        report_invocation(state)
        if state["status"] == "awaiting_input":
            note("it is waiting on you; a bare line answers it")
        return True
    if verb == "events":
        if demo.invocation is None:
            print(paint("  no invocation yet", YELLOW))
            return True
        page = await demo.c.invocations.events(demo.invocation, after=0, limit=100)
        print_events(list(page.get("events", [])))
        return True
    if verb == "memory":
        if demo.invocation is None:
            print(paint("  no invocation yet", YELLOW))
            return True
        nodes = await demo.c.invocations.memory(demo.invocation)
        for node in nodes.get("nodes", []):
            print(
                f"    {paint(str(node['kind']), BOLD, MAGENTA)} "
                f"{paint(f'ordinals {node["start_ordinal"]}-{node["end_ordinal"]}', GREY)}"
            )
            block(json.dumps(node.get("content"), sort_keys=True), GREY, lead="        ")
        return True
    if verb == "recall":
        if demo.invocation is None or not rest:
            print(paint("  usage: recall <query> (after `ask`)", YELLOW))
            return True
        hits = await demo.c.invocations.recall(demo.invocation, query=rest)
        rows = hits.get("hits", [])
        print(paint(f"  {len(rows)} hit(s) inside this session:", BOLD))
        for hit in rows:
            label = hit.get("event_kind") or hit.get("memory_kind")
            print(f"    {paint(str(label), CYAN)} {paint(short(str(hit.get('id'))), GREY)}")
        return True
    if verb == "why":
        if not rest:
            print(paint("  usage: why <id-prefix>", YELLOW))
            return True
        for collection in (RISKS, PROPOSALS, OBSERVATIONS, TERMS, EVIDENCE):
            for status in ("active", "draft"):
                for row in await demo.timeline(collection, status=status):
                    if str(row["id"]).startswith(rest):
                        await glass_box(demo, str(row["id"]))
                        return True
        print(paint(f"  no record starts with {rest}", YELLOW))
        return True
    if verb == "receipt":
        print_receipt(await demo.latest_run(rest or "renewal_assessment"))
        return True

    print(paint("  unknown command — try `help`", YELLOW))
    return True


def report_invocation(state: dict[str, Any]) -> None:
    status = str(state["status"])
    color = {"succeeded": GREEN, "awaiting_input": YELLOW}.get(status, RED)
    print(f"\n  invocation {paint(str(state['invocation_id']), BOLD)} → {paint(status, color)}")
    value = (state.get("result") or {}).get("value") or {}
    for field in ("question", "brief"):
        if value.get(field):
            block(str(value[field]), BCYAN if field == "question" else GREEN)
    if state.get("error"):
        block(json.dumps(state["error"]), RED)
    if status == "awaiting_input":
        note("just talk to it — a bare line is your answer. The pause is durable either way.")


async def interactive(demo: Demo) -> None:
    header("ACT IV · YOUR TURN", "the desk is yours")
    print(HELP)
    while True:
        try:
            line = await ainput(paint("\n  renewal ▸ ", BOLD, BCYAN))
        except EOFError, KeyboardInterrupt:
            print()
            break
        try:
            if not await handle(demo, line):
                break
        except MemseekHTTPError as error:
            print(paint(f"  API error: {error}", RED))
    print(paint("\n  The session persists. Every risk, observation, and draft is still", GREY))
    print(paint("  cited, hashed, and replayable from its journal.", GREY))


async def scripted_session(demo: Demo) -> None:
    header("ACT IV · SCRIPTED SESSION", "stdin is not a TTY — running a canned session")
    for line in SCRIPTED_SESSION:
        print(paint(f"\n  ▸ {line}", BOLD, BCYAN))
        await handle(demo, line)


# ---------------------------------------------------------------------------
# Workspace, runtime wiring, entrypoint.
# ---------------------------------------------------------------------------
async def ensure_workspace() -> str:
    """Mint a throwaway workspace, so an existing one keeps its own catalog."""

    if key := os.environ.get("MEMSEEK_API_KEY"):
        return key
    from psycopg import OperationalError

    from memseek.auth import create_workspace
    from memseek.db import pool_lifespan

    settings = get_settings()
    try:
        async with pool_lifespan(settings) as pool:
            credential = await create_workspace(pool, f"renewal-{RUN}")
    except OperationalError as error:
        target = urlsplit(settings.database_url)
        raise SystemExit(
            f"cannot reach the database at {target.hostname}:{target.port} to mint a "
            "workspace.\nStart the stack with `make computer-demo`, or set "
            "MEMSEEK_API_KEY to reuse a workspace."
        ) from error
    print(paint(f"  created disposable workspace {credential.workspace}", GREY))
    return credential.api_key


SETUP = """
This demo needs a Computer runtime, because a Computer is where Programs and
Agents actually run. The one command that wires all of it up is:

    make computer-demo

It brings the Docker stack up with the runtime configured and runs this file,
which serves the runtime itself. To do it by hand instead, put these two lines
in .env — the API, the worker, and this script all read them — and restart the
API and the worker:

    COMPUTER_RUNTIME_URL=http://127.0.0.1:8799
    COMPUTER_RUNTIME_TOKEN=local-demo-secret

Naming this machine (127.0.0.1, or host.docker.internal from a container) makes
this script serve the runtime, as a small stand-in for
cloudflare/computer-runtime. Use --cloudflare explicitly to drive a real Worker.
"""


# Hostnames that mean "this machine serves it". A container reaches the host at
# host.docker.internal, so a compose stack names that — but the name does not
# resolve on the host itself, which is why the demo binds and probes by address
# instead of by the name the containers use.
SERVED_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "0.0.0.0", "::1", "host.docker.internal", "gateway.docker.internal"}
)


async def runtime_health(url: str) -> dict[str, Any] | None:
    """Ask a runtime what it is. `None` means nothing answered."""

    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url.rstrip('/')}/health")
        if response.status_code != 200:
            return None
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except httpx.HTTPError, ValueError:
        return None


async def runtime_is_live(url: str) -> bool:
    return await runtime_health(url) is not None


CLOUDFLARE_SETUP = """
--cloudflare drives a real Cloudflare Agent: Workers AI decides what to do,
the tools it calls are real, and it chooses for itself when to stop and ask
you something. Nothing in this file executes the Agent in that mode — this
process only reads the journal Memseek writes.

Something has to be serving it. Either deploy the Worker, or run it locally:

    cd cloudflare/computer-runtime && npx wrangler dev --port 8799

`wrangler dev` still binds the remote AI binding, so the model turns are real
and billable. Then point the API, the worker, and this demo at it:

    COMPUTER_RUNTIME_URL=http://host.docker.internal:8799   # or your Worker's URL
    COMPUTER_RUNTIME_TOKEN=<MEMSEEK_RUNTIME_SECRET from .dev.vars>

The token has to match on both sides, and the catalog does not change: the
fixture already targets `workers_ai:@cf/meta/llama-3.3-70b-instruct-fp8-fast`,
which is the one thing the Cloudflare runtime insists on.
"""


async def run_demo(
    runtime: ComputerRuntime | None,
    *,
    port: int = 0,
    provider: str = "unknown",
    scripted: bool = False,
) -> None:
    api_key = await ensure_workspace()
    base_url = os.environ.get("MEMSEEK_BASE_URL", "http://127.0.0.1:8000")
    print_workspace_explorer(api_url=base_url, api_key=api_key)

    print("\n" + rule("━"))
    print(
        f"  {paint('DURABLE COMPUTERS', BOLD, BMAG)}  "
        f"{paint(f'a renewal desk for {ACCOUNT} · run {RUN}', GREY)}"
    )
    print(rule("━"))
    if runtime is not None:
        where = f"this process · deterministic stand-in on port {port}"
        colour: tuple[str, ...] = (BOLD, GREEN)
    else:
        where = f"{provider} · already serving"
        colour = (BOLD, BCYAN)
    print(f"  computer runtime: {paint(where, *colour)}   ·   API {paint(base_url, GREY)}")
    if runtime is not None:
        note("no model runs in this mode; `--cloudflare` hands the same catalog to Workers AI")

    async with MemseekClient(base_url, api_key) as client:
        demo = Demo(client)
        published = await demo.publish()
        package = published.get("package", {})
        print(
            f"  catalog: {paint(str(package.get('name')), BOLD)}"
            f"@{package.get('version')} "
            f"{paint('· hash ' + str(published.get('catalog_hash'))[:12], GREY)}"
        )
        print(f"  entity:  {paint(ENTITY, BOLD)}")

        await act_program(demo)
        await act_agent(demo)
        await act_invocation(demo, interactive=sys.stdin.isatty() and not scripted)
        if sys.stdin.isatty() and not scripted:
            await interactive(demo)
        else:
            await scripted_session(demo)

    if runtime is not None:
        print(
            paint(
                f"\n  the local runtime served {len(runtime.served)} job(s) "
                f"and rejected {runtime.rejected} unsigned request(s).",
                GREY,
            )
        )


def parse_args(argv: list[str] | None = None) -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="computer_renewal.py",
        description=(
            "A renewal desk backed by two Computers. By default this process "
            "serves a deterministic stand-in for the Computer runtime, so the "
            "demo needs no API keys and produces the same answer every time. "
            "--cloudflare instead drives a real Cloudflare Agent."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--cloudflare",
        action="store_true",
        help=(
            "drive a real Cloudflare Agent. Requires something already serving "
            "the runtime (a deployed Worker, or `wrangler dev`) whose /health "
            "reports provider: cloudflare. Real Workers AI turns; real cost."
        ),
    )
    mode.add_argument(
        "--standin",
        action="store_true",
        help="the default: serve the deterministic stand-in from this process.",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "delay between printed journal entries, so a burst is readable "
            "(default 0.12 for the stand-in, 0 for --cloudflare, where the "
            "arrival times are the model's own)."
        ),
    )
    parser.add_argument("--scripted", action="store_true", help="Supply fixed replies and exit.")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None, *, walkthrough: Any = run_demo) -> None:
    global EVENT_PACE
    args = parse_args(argv)
    settings = get_settings()
    url = settings.computer_runtime_url.strip()
    token = settings.computer_runtime_token.strip()
    if not url or not token:
        raise SystemExit(CLOUDFLARE_SETUP if args.cloudflare else SETUP)
    EVENT_PACE = args.pace if args.pace is not None else (0 if args.cloudflare else 0.12)
    parts = urlsplit(url)
    port = parts.port or 8799
    probe = f"http://127.0.0.1:{port}" if parts.hostname in SERVED_HOSTS else url
    health = await runtime_health(probe)
    expected = "cloudflare" if args.cloudflare else "local-standin"
    if health is not None:
        if health.get("provider") != expected:
            raise SystemExit(
                f"Expected {expected} runtime at {probe}; got {health.get('provider')!r}."
            )
        await walkthrough(None, provider=expected, scripted=args.scripted)
    elif args.cloudflare or parts.hostname not in SERVED_HOSTS:
        raise SystemExit(f"No {expected} runtime answered /health at {probe}.")
    else:
        async with ComputerRuntime(secret=token, host="0.0.0.0", port=port) as runtime:
            await walkthrough(runtime, port=port, provider=expected, scripted=args.scripted)
