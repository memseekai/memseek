"""Durable Computers — a renewal desk that runs code and agents on your evidence.

THE STORY IT TELLS

This is a renewal desk for one account, "Acme Cloud". Four pieces of evidence
land in it:

  · the signed contract — MSA §7.3, a 99.95% quarterly uptime commitment;
  · QBR minutes where a VP verbally promised a 12% renewal discount if uptime
    ever missed that floor, and nobody wrote it into the contract;
  · a procurement email — quote by Friday, flat budget;
  · an incident report — the quarter closed at 99.91%.

Separately, each is unremarkable. Together they are money: a promise nobody
recorded has been triggered by an outage, and the quote goes out Friday. That is
the thing a memory system should catch and a person paging through a CRM
probably will not. Everything below is in service of finding it, and then
proving it was found honestly.

THE ONE IDEA UNDERNEATH

There are two sides. Memseek decides *what* may run, *on which evidence*, and
*what may be written back*. The Computer — the provider — is the untrusted side
that actually executes. It never sees the database, never writes a record, and
returns only a value plus a receipt. Every claim it makes is re-checked before
anything is committed.

So when the Agent says "high renewal risk", that is not taken on faith. Its
citations must be a subset of the record ids it was handed, so it cannot invent
evidence or cite a record it was never shown. Its output must satisfy the schema
the derivation declared. Its bytes and steps are capped. Anything it wants to
write back must land on a path the Computer definition declared in advance. And
the receipt names the exact Computer, Program, Agent, and context-policy
versions that produced the answer, with hashes — so months later you can still
ask what actually ran.

WHAT EACH ACT IS FOR

  ACT I  · PROGRAM      Not everything needs a model. The contract lands, its
                        write fires `contract_extract`, and deterministic
                        JavaScript runs in `fast_workspace@1` and cites the
                        contract. The pipeline caps LLM calls at 0, so this path
                        *cannot* call a model even if someone later wanted it to.
                        Same machinery, same receipt, no intelligence required.

  ACT II · AGENT        The connection. The nightly `renewal_assessment` hands
                        `renewal_analyst@1` a snapshot of the account; it links
                        the QBR promise to the incident and comes back through
                        the derivation's ordinary `emit` boundary — the same one
                        every other derivation uses, so a Computer-backed result
                        is not a special kind of record. The glass box then opens
                        the risk to the run that wrote it and the two original
                        rows underneath, in their own words.

  ACT III · INVOCATION  Work that outlives a process, and that you hold a
                        conversation with. The same Agent, started as a durable
                        session. It gets far enough to see the problem and then
                        stops to ask: the promise is not in the contract — what
                        guardrail should the proposal hold to? You answer, and it
                        stops a second time, because now it has read the
                        procurement email and a 12% cut against a flat budget is
                        a decision, not a calculation: hold the discount, or
                        trade part of it for a longer term?

                        Those pauses are rows in PostgreSQL, not blocked
                        threads. Each answer is appended to the journal and
                        replayed into the next turn, which is what makes this a
                        conversation instead of a sequence of unrelated
                        requests — and the second question earns its keep, since
                        answering it is what brings the procurement row into the
                        citation set. Talking to the Agent changed what it was
                        able to say: the record that lands cites three rows
                        rather than two.

                        Then it writes two files to its outbox. The observation
                        goes live. The proposed 12% commitment lands as a DRAFT
                        for review — not because the Agent was well behaved, but
                        because the Computer definition says `review: true`. An
                        Agent cannot make a pricing promise on the company's
                        behalf; it can only propose one. The act closes with
                        `happened`: the journal, the accepted citations, the
                        receipt's definition hashes and the write-back routing,
                        all read back out of the server rather than recalled by
                        this script.

  ACT IV · YOUR TURN    A prompt. While the Agent is paused, a bare line is your
                        answer — you are talking to something that just asked you
                        a question, so no verb should stand in the way. Otherwise:
                        file more evidence, re-run the assessment, `attach` a
                        conversation this process never started, fork a session
                        into a what-if that leaves the original untouched, or open
                        any conclusion back to its sources with `why`.

WATCHING IT HAPPEN

The journal is streamed, not summarised. Act III subscribes to
`GET /invocations/{id}/events/stream` and prints entries as the server appends
them, so the ordered list scrolling past is the same one PostgreSQL will replay
in six months. Nothing printed there is this script's account of what it just
did — which is the only version worth trusting.

THE HONEST PART ABOUT THE AGENT

A Computer has to run somewhere, and the in-process test double cannot be
reached from a worker in another container. So by default this script plays the
provider itself: it serves the same signed endpoint the real Cloudflare Worker
serves, and prints every job the worker sends it, so you watch the two processes
talk.

What it runs there is deterministic — a small routine that reads the mounted
evidence, joins a promised uptime floor to the worst reported uptime, keeps a
risk table, and stops when it needs a person. No model, no sandbox, no API key.
That is deliberate. The demo is not trying to impress you with reasoning; it
shows what the system does with an answer once it has one: the citation check,
the schema, the review flag, the receipt, the pause.

    uv run python examples/computer_renewal.py --cloudflare

hands the same catalog to a real Cloudflare Agent instead. Then Workers AI picks
the steps, calls real tools in a network-denied durable workspace, and decides
for itself when to stop and ask you something — so the demo never assumes how
many pauses to expect, and none of the checks change. That mode refuses to run
unless something is already serving the runtime and its `/health` says
`provider: cloudflare`, because a stand-in answering on the same port would
quietly turn a live model run back into a regex. `make computer-demo-cloudflare`
wires it up; the turns are real and billable.

TWO DETAILS THAT LOOK ARBITRARY AND ARE NOT

The contract is filed WITHOUT a key while the standing facts are filed WITH
keys. The Program's source reads unkeyed arrivals, so only a contract landing
fires it; the Agent's mounted artifact renders keyed rows, which is what gives
it something to cite. One collection, two roles: arrivals versus standing facts.

And the workspace is disposable. The demo mints its own and publishes the
fixture's catalog into it, so running this never disturbs a workspace you
already use.

RUNNING IT

    make computer-demo

That brings the Docker stack up with a Computer runtime configured and then runs
this file against it. No API keys are needed: the fixture's model provider is
fake, and the stand-in runs no model at all. The containers reach the runtime
this script serves at `host.docker.internal`, which is why it binds every
interface. For a real Agent instead, see `--cloudflare` above; the catalog is
identical either way.

Without Docker, run the same thing by hand. Put these two lines in `.env` so the
API, the worker, and this demo agree on the runtime:

    COMPUTER_RUNTIME_URL=http://127.0.0.1:8799
    COMPUTER_RUNTIME_TOKEN=local-demo-secret

    make database && source .env.sh
    uv run memseek migrate
    uv run uvicorn memseek.api:app &                 # terminal A
    uv run memseek worker &                          # terminal B
    uv run python examples/computer_renewal.py       # terminal C — talk to it

Add `--cloudflare` in terminal C to drive the deployed Worker or a local
`wrangler dev` instead of the stand-in, and `--pace 0` to stop pacing the
printed journal for reading.

The demo mints its own disposable workspace over DATABASE_URL; set
MEMSEEK_API_KEY to reuse one instead. Piping input (non-TTY) runs a short
scripted session and exits, so this doubles as a runnable smoke check.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from _workspace_explorer import print_workspace_explorer

# FastAPI resolves handler annotations from module globals, so `Request` has to
# be imported here rather than beside the app that uses it.
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from memseek.config import get_settings
from memseek.sdk import MemseekClient, MemseekHTTPError

# --- the account this demo maintains a renewal position for ----------------
RUN = secrets.token_hex(3)
ACCOUNT = "Acme Cloud"
ENTITY = f"account:acme-{RUN}"

CATALOG_ROOT = Path(__file__).with_name("computer_renewal_catalog")
PACKAGE = "computer_renewal_demo@1.0.0"

FAST_COMPUTER = "fast_workspace@1"
RESEARCH_COMPUTER = "research_workspace@1"
ANALYST = "renewal_analyst@1"
EXTRACTOR = "contract_extract@3"
POLICY = "evidence_spine@1"

EVIDENCE = "renewal_evidence"
TERMS = "contract_terms"
RISKS = "renewal_risks"
OBSERVATIONS = "task_observations"
PROPOSALS = "renewal_proposals"

# ---------------------------------------------------------------------------
# Terminal styling. Colors only reach a real TTY, and honor NO_COLOR
# (https://no-color.org) and TERM=dumb, so a pipe or a CI log stays clean.
# ---------------------------------------------------------------------------
_C = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"


def _s(*codes: int) -> str:
    return ("\033[" + ";".join(map(str, codes)) + "m") if _C else ""


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
BCYAN, BGREEN, BYELLOW, BMAG = _s(96), _s(92), _s(93), _s(95)


def paint(text: str, *codes: str) -> str:
    return ("".join(codes) + text + RESET) if _C else text


def rule(char: str = "─", width: int = 76) -> str:
    return paint(char * width, GREY)


def header(title: str, subtitle: str = "") -> None:
    print("\n" + rule("━"))
    line = f"  {paint(title, BOLD, BMAG)}"
    if subtitle:
        line += f"  {paint(subtitle, GREY)}"
    print(line)
    print(rule("━"))


def note(text: str) -> None:
    print(paint(f"  · {text}", GREY))


def short(value: str | None) -> str:
    return value[:8] if value else "—"


def wrap(text: str, width: int = 66) -> list[str]:
    return textwrap.wrap(" ".join(str(text).split()), width=width) or [""]


def block(text: str, *codes: str, lead: str = "      ") -> None:
    for line in wrap(text):
        print(lead + paint(line, *codes))


# ---------------------------------------------------------------------------
# The provider side of the boundary.
#
# `cloudflare/computer-runtime` is the real implementation: a Worker that runs
# Programs in a Dynamic Worker and Agents against Workers AI, inside a
# network-denied durable workspace. This is a stand-in for it — same signed wire
# protocol, same response envelope, same bounds — small enough to read in one
# sitting, so you can watch what Memseek asks a provider for and what it refuses
# to accept back. It sandboxes nothing and calls no model: the two executors
# below are deterministic, which is exactly why the demo is reproducible.
# ---------------------------------------------------------------------------
def canonical_bytes(value: Any) -> bytes:
    """Byte-exact match for `memseek.computers._canonical_bytes`."""

    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


_ROW = re.compile(r"\[id=([0-9a-fA-F-]{36})\][^\n]*?\|\s*([^\n|]+)\s*$", re.MULTILINE)
_PERCENT = re.compile(r"(\d{1,3}(?:\.\d{1,3})?)\s?%")


@dataclass(frozen=True, slots=True)
class Evidence:
    """One record the provider is allowed to look at, and to cite."""

    id: str
    text: str


@dataclass(frozen=True, slots=True)
class Finding:
    """A promised uptime floor, the reported uptime that breaches it, and the
    commercial pressure that decides what to do about it.

    `budget` is the row that makes the second question worth asking: honouring a
    12% discount against a flat budget is a real cut. It is separate because the
    Agent only needs it once the operator has set a guardrail — which is why the
    conversation widens the citation set rather than merely confirming it.
    """

    promise: Evidence
    observed: Evidence
    floor: float
    actual: float
    discount: float
    budget: Evidence | None = None

    @property
    def citations(self) -> list[str]:
        """The breach itself: what the Agent may cite before anyone has spoken."""

        return sorted({self.promise.id, self.observed.id})

    @property
    def full_citations(self) -> list[str]:
        """The breach plus the commercial constraint, once the guardrail is set."""

        ids = set(self.citations)
        if self.budget is not None:
            ids.add(self.budget.id)
        return sorted(ids)


def rows_from_context(files: dict[str, str]) -> list[Evidence]:
    """Recover the rendered evidence rows from an Agent's mounted context.

    Memseek renders each record as `[id=<uuid>] <time> | collection/type | … |
    <text>`, so the exact record ids are in the material the Agent reads. Those
    ids are the only citations the provider may return.
    """

    seen: dict[str, Evidence] = {}
    for path, text in sorted(files.items()):
        if path.endswith(".json"):
            continue
        for match in _ROW.finditer(text):
            seen.setdefault(match.group(1), Evidence(match.group(1), match.group(2).strip()))
    return list(seen.values())


def rows_from_records(records: list[dict[str, Any]]) -> list[Evidence]:
    return [
        Evidence(str(row["id"]), str((row.get("content") or {}).get("text", "")))
        for row in records
        if row.get("id")
    ]


def analyse(rows: list[Evidence]) -> Finding | None:
    """Join a conditional pricing promise to the outcome that triggers it.

    A real Agent reasons its way here with a model and tools. This one uses a
    regex, deliberately: the demo is about what Memseek does with the answer —
    the citation authority, the schema, the review flag — not about the answer.
    """

    promise: tuple[Evidence, float, float] | None = None
    observed: tuple[Evidence, float] | None = None
    budget: Evidence | None = None
    for row in rows:
        lowered = row.text.lower()
        if budget is None and any(word in lowered for word in ("budget", "procurement", "quote")):
            budget = row
        numbers = [float(match.group(1)) for match in _PERCENT.finditer(row.text)]
        uptimes = [value for value in numbers if 90.0 <= value <= 100.0]
        discounts = [value for value in numbers if value < 90.0]
        if not uptimes:
            continue
        conditional = any(word in lowered for word in ("below", "under", "if uptime", "misses"))
        if conditional and discounts:
            promise = (row, min(uptimes), max(discounts))
        else:
            # Anything stating an uptime without a condition is a report of what
            # actually happened. The worst one is the one that matters.
            candidate = min(uptimes)
            if observed is None or candidate < observed[1]:
                observed = (row, candidate)
    if promise is None or observed is None or observed[1] >= promise[1]:
        return None
    return Finding(
        promise=promise[0],
        observed=observed[0],
        floor=promise[1],
        actual=observed[1],
        discount=promise[2],
        budget=budget,
    )


class Trace:
    """The steps of one agent turn: printed as they run, returned as the journal.

    A provider answers with a single envelope, so Memseek's journal only learns
    what happened once the turn ends. Recording the steps once and spending the
    list twice — printed here, and returned as `receipt.events` — keeps the two
    honest: what you watch scroll past in this process is the same ordered list
    that `events` replays out of PostgreSQL a month later.
    """

    def __init__(self, label: str) -> None:
        self._label = label
        self.events: list[dict[str, Any]] = []

    @property
    def steps(self) -> int:
        """What the Agent's `max_steps` limit is actually counted against."""

        return sum(1 for event in self.events if event["kind"] == "tool_call")

    def _record(self, kind: str, payload: dict[str, Any], line: str) -> None:
        self.events.append({"kind": kind, "payload": payload})
        print(paint(f"  [{self._label}] {line}", GREY))

    def tool(self, tool: str, argument: str, outcome: str, **detail: Any) -> None:
        """One tool call and its result — the pair the real Worker also emits.

        The payload keys are the Cloudflare runtime's own (`tool_name`, `input`,
        `output`) rather than invented ones, so the journal has a single dialect
        and one renderer reads either provider's events.
        """

        index = self.steps + 1
        self._record(
            "tool_call",
            {"tool_name": tool, "input": argument, "index": index},
            f"step {index:<2} {paint(tool, BCYAN)} {argument}",
        )
        self._record(
            "tool_result",
            {"tool_name": tool, "index": index, "output": outcome, **detail},
            f"         └─ {outcome}",
        )

    def note(self, reason: str, **detail: Any) -> None:
        """A conclusion the Agent reached without calling anything."""

        self._record("provider_note", {"reason": reason, **detail}, f"         · {reason}")

    def write(self, path: str, content: str) -> str:
        """Record a real write: the byte count and hash are the content's own.

        Emitted inline as a `file_change` rather than through the receipt's
        `files` list, because Memseek journals that list ahead of the provider's
        own events — which would put the write before the steps that produced
        it. In the journal, order is the whole point.
        """

        encoded = content.encode()
        digest = hashlib.sha256(encoded).hexdigest()
        self._record(
            "file_change",
            {"path": path, "bytes": len(encoded), "sha256": digest},
            f"         → wrote {path} ({len(encoded)} bytes, sha256 {digest[:8]})",
        )
        return content


def risk_table(found: Finding, guardrail: str = "", trade: str = "") -> str:
    """The working file the Agent keeps in `/workspace` while it reasons.

    Real content, so the `file_change` event Memseek journals carries this
    string's true size and hash rather than a number the provider made up.
    """

    lines = [
        f"# {ACCOUNT} — renewal risk",
        "",
        f"- promised floor: {found.floor}% (verbal, not in the signed contract)",
        f"- reported uptime: {found.actual}%",
        f"- triggered discount: {found.discount:g}%",
        f"- promise cited as: {found.promise.id}",
        f"- outcome cited as: {found.observed.id}",
    ]
    if found.budget is not None:
        lines.append(f"- commercial constraint cited as: {found.budget.id}")
    if guardrail:
        lines += ["", "## operator guardrail", "", guardrail]
    if trade:
        lines += ["", "## operator decision on the trade", "", trade]
    return "\n".join(lines) + "\n"


class ComputerRuntime:
    """A local stand-in for the Cloudflare Computer Worker.

    It verifies the same HMAC, answers the same `POST /v1/execute`, and returns
    the same `{value, citation_ids, receipt, steps, awaiting_input}` envelope.
    Everything it returns is then re-checked by Memseek before a single row is
    written, which is the whole point of the boundary.
    """

    def __init__(self, *, secret: str, host: str, port: int) -> None:
        self._secret = secret.encode()
        self._host = host
        self._port = port
        self._server: Any = None
        self._task: asyncio.Task[Any] | None = None
        self.served: list[str] = []
        self.rejected = 0
        self._shapes: set[tuple[tuple[str, int], ...]] = set()

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> ComputerRuntime:
        import uvicorn

        config = uvicorn.Config(
            self._build_app(), host=self._host, port=self._port, log_level="error"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        deadline = asyncio.get_running_loop().time() + 10.0
        while not getattr(self._server, "started", False):
            if self._task.done():
                await self._task
                raise SystemExit(f"the local Computer runtime could not bind port {self._port}")
            if asyncio.get_running_loop().time() >= deadline:
                raise SystemExit("the local Computer runtime did not start")
            await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *_: object) -> None:
        """Give up the port before the process exits, however the demo ended."""

        if self._server is not None:
            self._server.should_exit = True
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except TimeoutError, asyncio.CancelledError:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # -- HTTP boundary -----------------------------------------------------
    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None)

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return {"ok": True, "provider": "local-standin"}

        @app.post("/v1/execute")
        async def execute(request: Request) -> JSONResponse:
            body = await request.body()
            timestamp = request.headers.get("x-memseek-timestamp", "")
            signature = request.headers.get("x-memseek-signature", "")
            expected = hmac.new(
                self._secret, timestamp.encode() + b"." + body, hashlib.sha256
            ).hexdigest()
            fresh = timestamp.isdigit() and abs(int(timestamp) - int(time.time())) <= 300
            if not fresh or not hmac.compare_digest(expected, signature):
                self.rejected += 1
                print(
                    paint(
                        "\n  [runtime] rejected an unsigned or stale request — is "
                        "COMPUTER_RUNTIME_TOKEN the same in .env and here?",
                        RED,
                    )
                )
                return JSONResponse(status_code=401, content={"error": "unauthorized"})
            job = json.loads(body)
            try:
                payload = self._dispatch(job)
            except Exception as error:  # the Worker maps every failure to 422
                print(paint(f"  [runtime] refused the job: {error}", RED))
                return JSONResponse(status_code=422, content={"error": str(error)})
            return JSONResponse(status_code=200, content=payload)

        return app

    # -- execution ---------------------------------------------------------
    def _dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        executor = job["executor"]
        reference = str(executor["ref"])
        self.served.append(f"{job['mode']}:{reference}")
        context = dict(job.get("context_files") or {})
        mounted = sum(len(text.encode()) for text in context.values())
        print(
            paint(
                f"  [runtime] {job['mode']} · {reference} · session "
                f"{short(str(job['session_key']))} · "
                f"{len(job.get('citation_ids') or [])} citable record(s) · "
                f"{len(context)} context file(s), {mounted:,} bytes",
                GREY,
            )
        )
        # Break the mount down the first time a shape appears, and not on every
        # later job with the same one: worth seeing once, noise thereafter.
        shape = tuple(sorted((path, len(text.encode())) for path, text in context.items()))
        if shape and shape not in self._shapes:
            self._shapes.add(shape)
            for path, size in shape:
                print(paint(f"             {path:<34} {size:>8,} bytes", GREY))
        if executor["kind"] == "program":
            return self._program(job)
        return self._agent(job)

    def _envelope(
        self,
        job: dict[str, Any],
        value: Any,
        *,
        citations: list[str],
        steps: int,
        extra: dict[str, Any] | None = None,
        awaiting_input: bool = False,
    ) -> dict[str, Any]:
        allowed = set(job.get("citation_ids") or ())
        encoded = canonical_bytes(value)
        receipt: dict[str, Any] = {
            "provider": "local-standin",
            "backend": job["computer"]["runtime"]["default"],
            "session_key": job["session_key"],
            "output_path": job["output_path"],
            "output_sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
        }
        receipt.update(extra or {})
        return {
            "value": value,
            # Returning only what we were authorized to cite. Memseek rejects the
            # whole run if this is not a subset — a provider cannot mint evidence.
            "citation_ids": sorted(set(citations) & allowed),
            "receipt": receipt,
            "steps": steps,
            "awaiting_input": awaiting_input,
        }

    def _program(self, job: dict[str, Any]) -> dict[str, Any]:
        """`contract_extract@3`: the fixture's `main.js`, deterministic, no model.

        The real Worker loads that entrypoint into a Dynamic Worker with egress
        disabled. Here it is transliterated, so the demo needs no Node.
        """

        if str(job["executor"]["ref"]) != EXTRACTOR:
            raise ValueError(f"unknown Program {job['executor']['ref']}")
        contracts = list((job.get("input") or {}).get("contracts") or [])
        records = [
            {
                "text": f"Extracted renewal term from: {row['content']['text']}",
                "citations": [row["id"]],
                "content": {"kind": "contract_term", "value": row["content"]["text"]},
            }
            for row in contracts
        ]
        return self._envelope(
            job,
            {"records": records},
            citations=[str(row["id"]) for row in contracts],
            steps=1,
            extra={"files": [{"path": "/outbox/result.json", "bytes": len(records)}]},
        )

    def _agent(self, job: dict[str, Any]) -> dict[str, Any]:
        if str(job["executor"]["ref"]) != ANALYST:
            raise ValueError(f"unknown Agent {job['executor']['ref']}")
        if job["mode"] == "derivation":
            return self._assess(job)
        return self._answer(job)

    def _assess(self, job: dict[str, Any]) -> dict[str, Any]:
        """The nightly assessment: emit cited renewal risks, nothing else."""

        trace = Trace("assess")
        supplied = list((job.get("input") or {}).get("evidence") or [])
        context = dict(job.get("context_files") or {})
        evidence = rows_from_records(supplied)
        trace.tool(
            "read",
            "input.evidence",
            f"{len(evidence)} row(s) handed in by the derivation",
            rows=len(evidence),
        )
        mounted = rows_from_context(context)
        if context:
            trace.tool(
                "read",
                " ".join(sorted(context)) or "(none)",
                f"{len(mounted)} row(s) rendered into the mounted artifacts",
                paths=sorted(context),
            )
        evidence += mounted
        found = analyse(evidence)
        if found is None:
            if not evidence:
                raise ValueError("no evidence was made visible to the Agent")
            trace.note("no conditional promise is contradicted by a reported outcome")
            record = {
                "text": "No contradicted pricing promise stands in the current evidence.",
                "citations": [evidence[0].id],
                "content": {"kind": "renewal_risk", "severity": "low"},
            }
            citations = [evidence[0].id]
        else:
            trace.tool(
                "recall",
                '"uptime promise"',
                f"the {found.floor}% floor is promised in {short(found.promise.id)}",
                hits=1,
            )
            trace.tool(
                "recall",
                '"reported uptime"',
                f"the quarter closed at {found.actual}% in {short(found.observed.id)}",
                hits=1,
            )
            trace.note(
                "the promise is verbal and the outcome breaches it",
                floor=found.floor,
                actual=found.actual,
            )
            trace.write("/workspace/risk-table.md", risk_table(found))
            record = {
                "text": (
                    f"Quarterly uptime of {found.actual}% breaches the {found.floor}% floor "
                    f"promised verbally, which activates a {found.discount:g}% renewal discount "
                    "that never entered the signed contract."
                ),
                "citations": found.citations,
                "content": {"kind": "renewal_risk", "severity": "high"},
            }
            citations = found.citations
        return self._envelope(
            job,
            {"records": [record]},
            citations=citations,
            steps=trace.steps,
            extra={"events": trace.events},
        )

    def _answer(self, job: dict[str, Any]) -> dict[str, Any]:
        """The durable session: a conversation, then a write-back under review.

        This is the only executor in the demo that stops mid-task. It pauses
        twice, and the two pauses are not the same shape. The first is a
        *blocker*: the promise is not in the contract, so there is no defensible
        proposal to make until a person sets a bound. The second is a *choice*
        the evidence poses but cannot settle — procurement is holding the budget
        flat, so honouring the discount costs something real, and only the
        operator can say what to spend it on.

        The second question is why the conversation matters rather than merely
        happening: answering it pulls the procurement email into the citation
        set, so the record that finally lands cites three rows instead of two.
        Talking to the Agent changed what it was able to say.
        """

        context = dict(job.get("context_files") or {})
        turns = [
            str(turn.get("prompt", "")).strip()
            for turn in (job.get("input") or {}).get("turns") or []
        ]
        trace = Trace(f"agent·turn {len(turns) + 1}")

        evidence = rows_from_context(context)
        trace.tool(
            "read",
            " ".join(sorted(context)) or "(none)",
            f"{len(evidence)} citable row(s) across {len(context)} mounted file(s)",
            paths=sorted(context),
        )
        if turns:
            # The journal is the memory. Every answer given at an earlier pause
            # is replayed into this turn, which is what makes a durable session
            # a conversation rather than a series of unrelated requests.
            trace.tool(
                "recall",
                "this session's answered pauses",
                f"{len(turns)} operator turn(s) replayed from the journal",
                turns=len(turns),
            )
        found = analyse(evidence)
        if found is None:
            raise ValueError("no contradicted promise is visible in the mounted context")
        trace.note(
            f"{found.actual}% breaches the promised {found.floor}% floor",
            discount=found.discount,
            contracted=False,
        )

        if not turns:
            return self._blocked_on_guardrail(job, trace, found)
        if len(turns) == 1 and found.budget is not None:
            return self._blocked_on_trade(job, trace, found, guardrail=turns[0])
        return self._final(job, trace, found, turns)

    # -- pause one: there is no defensible proposal without a bound ---------
    def _blocked_on_guardrail(
        self, job: dict[str, Any], trace: Trace, found: Finding
    ) -> dict[str, Any]:
        """Pausing is an outcome, not an error.

        The invocation moves to `awaiting_input` and its journal survives in
        PostgreSQL until somebody answers — the process may die in between. A
        paused turn is also forbidden from writing anything back, which is why
        this envelope carries no outbox.
        """

        trace.note("cannot price an uncontracted promise without an operator bound")
        question = (
            f"The QBR promises a {found.discount:g}% renewal discount below "
            f"{found.floor}% uptime, and the quarter closed at {found.actual}%. "
            "That promise is not in the signed contract. What pricing guardrail "
            "should I hold the proposal to?"
        )
        return self._envelope(
            job,
            {"question": question, "citations": found.citations, "blocked_on": "pricing_guardrail"},
            citations=found.citations,
            steps=trace.steps,
            awaiting_input=True,
            extra={"events": trace.events},
        )

    # -- pause two: the evidence poses a choice it cannot settle ------------
    def _blocked_on_trade(
        self, job: dict[str, Any], trace: Trace, found: Finding, *, guardrail: str
    ) -> dict[str, Any]:
        """The pause that widens the citation set.

        Reading the procurement row is what raises this question, so answering
        it is what makes that row part of the finding. The Agent could not have
        reached a three-row conclusion on its own.
        """

        assert found.budget is not None
        trace.tool(
            "recall",
            '"renewal budget"',
            f"procurement is holding the budget flat — {short(found.budget.id)}",
            hits=1,
        )
        trace.write("/workspace/risk-table.md", risk_table(found, guardrail=guardrail))
        trace.note("the guardrail is bounded but the budget is flat; the trade is not mine to make")
        question = (
            f"Your guardrail is: {guardrail} Procurement wants the quote by Friday on a "
            f"flat budget, so a {found.discount:g}% discount is a real cut rather than a "
            "rounding error. Do I propose the full discount as promised, or trade part of "
            "it for a longer committed term?"
        )
        return self._envelope(
            job,
            {
                "question": question,
                "citations": found.full_citations,
                "blocked_on": "discount_versus_term",
            },
            citations=found.full_citations,
            steps=trace.steps,
            awaiting_input=True,
            extra={"events": trace.events},
        )

    # -- the answered turn: the only one allowed to write anything back -----
    def _final(
        self, job: dict[str, Any], trace: Trace, found: Finding, turns: list[str]
    ) -> dict[str, Any]:
        guardrail = turns[0]
        trade = turns[1] if len(turns) > 1 else ""
        citations = found.full_citations if trade else found.citations
        trace.write("/workspace/risk-table.md", risk_table(found, guardrail=guardrail, trade=trade))

        observation = {
            "text": (
                f"Uptime of {found.actual}% breaches the promised {found.floor}% floor, so the "
                f"{found.discount:g}% discount promise is live going into the renewal."
            ),
            "content": {"kind": "observation"},
            "citations": found.citations,
        }
        proposal_text = (
            f"Proposed renewal commitment: honour the {found.discount:g}% discount for the "
            f"renewal term, bounded by the operator guardrail — {guardrail}"
        )
        if trade:
            proposal_text += f" Operator decision on the flat budget — {trade}"
        proposal = {
            "text": proposal_text,
            "content": {"kind": "pricing_commitment"},
            "citations": citations,
        }
        outbox = [
            _outbox_file("/outbox/observations.jsonl", "observations", json.dumps(observation)),
            _outbox_file(
                "/outbox/proposals/renewal-credit.json",
                "maintained_state",
                json.dumps(proposal),
            ),
        ]
        for file in outbox:
            trace.note(
                f"staged {file['path']} for write-back",
                bytes=file["bytes"],
                sha256=file["sha256"][:12],
            )
        brief = (
            f"{ACCOUNT} enters the renewal with a live, uncontracted "
            f"{found.discount:g}% discount promise triggered by {found.actual}% uptime. "
            f"Guardrail recorded: {guardrail}"
        )
        if trade:
            brief += f" Trade decided: {trade}"
        return self._envelope(
            job,
            {"brief": brief, "citations": citations},
            citations=citations,
            steps=trace.steps,
            extra={"outbox": outbox, "events": trace.events},
        )


def _outbox_file(path: str, kind: str, content: str) -> dict[str, Any]:
    """Declare one outbox file with the exact size and hash Memseek re-checks."""

    if kind == "observations":
        content = content + "\n"
    encoded = content.encode()
    return {
        "path": path,
        "type": kind,
        "content": content,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


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
        """Watch one invocation live until it pauses, finishes, or fails.

        The journal is streamed rather than polled: `GET /invocations/{id}/
        events/stream` pushes every entry as the server appends it, so what
        prints here is the ordered journal itself, in real time, and not a
        summary this script assembled. The stream ends when the invocation
        reaches a terminal state; a pause is not terminal, so we stop reading at
        `awaiting_input` ourselves and hand control back to the person.
        """

        assert self.invocation is not None
        deadline = asyncio.get_running_loop().time() + timeout_s
        stream = self.c.invocations.stream(self.invocation, after=self.cursor)
        try:
            async for event in stream:
                self.cursor = max(self.cursor, int(event["ordinal"]))
                await show_event(event)
                if str(event["kind"]) in _PAUSING_EVENTS:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    print(paint(f"  still running after {timeout_s:.0f}s", YELLOW))
                    break
        except MemseekHTTPError:
            # The stream is a convenience, not the source of truth. If it drops,
            # fall back to paging the same journal, then report the real state.
            await self.drain_events()
        finally:
            await stream.aclose()

        state = await self.c.invocations.retrieve(self.invocation)
        if state["status"] in _RESTING_STATUSES:
            return state
        # The stream closed while the invocation was still moving — a dropped
        # connection, or a turn requeued after a transport interruption. Page
        # the rest of the journal the plain way rather than guessing.
        return await self.follow(timeout_s=timeout_s)

    async def follow(self, *, timeout_s: float = 300.0) -> dict[str, Any]:
        """Poll the same journal, for when the event stream is not available."""

        assert self.invocation is not None
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            state = await self.c.invocations.retrieve(self.invocation)
            await self.drain_events()
            if state["status"] in _RESTING_STATUSES:
                return state
            if asyncio.get_running_loop().time() >= deadline:
                raise SystemExit(
                    "the invocation never left the queue — is `memseek worker` running?"
                )
            await asyncio.sleep(0.4)

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


async def scripted(demo: Demo) -> None:
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
cloudflare/computer-runtime. Point it at a deployed Worker and the demo drives
that instead.
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
    runtime: ComputerRuntime | None, *, port: int = 0, provider: str = "unknown"
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
        await act_invocation(demo, interactive=sys.stdin.isatty())
        if sys.stdin.isatty():
            await interactive(demo)
        else:
            await scripted(demo)

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
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    global EVENT_PACE

    args = parse_args(argv)
    settings = get_settings()
    url = settings.computer_runtime_url.strip()
    token = settings.computer_runtime_token.strip()
    if not url or not token:
        raise SystemExit(CLOUDFLARE_SETUP if args.cloudflare else SETUP)

    # --cloudflare means "a real Agent is deciding what happens", so this
    # process must not serve anything: the stand-in would answer on the same
    # port and quietly turn a live model run back into a regex. Insist that
    # something else is already there, and that it says what it is.
    if args.cloudflare:
        EVENT_PACE = 0.0 if args.pace is None else args.pace
        health = await runtime_health(url)
        if health is None:
            raise SystemExit(f"no Computer runtime answered /health at {url}.\n{CLOUDFLARE_SETUP}")
        provider = str(health.get("provider") or "unknown")
        if provider != "cloudflare":
            raise SystemExit(
                f"the runtime at {url} reports provider {provider!r}, not 'cloudflare'.\n"
                "That is this file's own stand-in, or another provider. Stop it and start "
                f"the Worker instead.\n{CLOUDFLARE_SETUP}"
            )
        print(paint(f"  driving the Cloudflare runtime at {url}", BOLD, BCYAN))
        note("Workers AI decides the steps, the tools, and when to stop and ask you")
        await run_demo(None, provider=provider)
        return

    EVENT_PACE = 0.12 if args.pace is None else args.pace
    parts = urlsplit(url)
    port = parts.port or 8799
    if parts.hostname not in SERVED_HOSTS:
        health = await runtime_health(url)
        if health is None:
            raise SystemExit(f"no Computer runtime answered /health at {url}")
        await run_demo(None, provider=str(health.get("provider") or "remote"))
        return
    if health := await runtime_health(f"http://127.0.0.1:{port}"):
        # Something already serves that port — another copy of this demo, or a
        # `wrangler dev`. Use it rather than fighting for the port, but say so:
        # silently talking to somebody else's runtime is a confusing way to run.
        provider = str(health.get("provider") or "unknown")
        print(
            paint(
                f"  a {provider} runtime already answers on port {port}; using it. "
                "Stop it first to serve the one in this file.",
                YELLOW,
            )
        )
        if provider == "cloudflare":
            note("that is a real Cloudflare Agent — `--cloudflare` says so explicitly")
        await run_demo(None, provider=provider)
        return
    # Every interface, so the api and worker containers can reach it across
    # Docker's network. On a shared network that is an open port: it holds no
    # data, runs nothing but the two executors below, and still refuses every
    # request that is not signed with COMPUTER_RUNTIME_TOKEN.
    async with ComputerRuntime(secret=token, host="0.0.0.0", port=port) as runtime:
        await run_demo(runtime, port=port, provider="local-standin")


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except MemseekHTTPError as error:
        raise SystemExit(f"memseek API error: {error}") from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
