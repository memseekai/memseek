"""Deterministic HTTP stand-in; no sandbox and no model."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from _computer_common import ACCOUNT, ANALYST, BCYAN, EXTRACTOR, GREY, RED, paint, short
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


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


async def serve() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    token = os.environ.get("COMPUTER_RUNTIME_TOKEN", "")
    if not token:
        raise SystemExit("COMPUTER_RUNTIME_TOKEN is required")
    async with ComputerRuntime(secret=token, host="0.0.0.0", port=args.port):
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
