"""Teach a finance agent from its own traces, in two cycles.

Run: make trace-learning-demo

It publishes examples/agent_trace_learning, loads Northwind Traders' October
payment webhooks and payout statement, and asks a traced agent two questions
about them. Jev judges each run from its trace against the statement, the skill
derivation turns the wrong answers into a draft skill, and the script promotes
it. Then it asks two new questions and compares the cycles.

The agent runs on the local Computer provider under the pi harness, so the API
and the worker must run on this machine with tracing on. The guide,
docs/agent-trace-learning-example.md, has the setup.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memseek.sdk import MemseekClient

ROOT = Path(__file__).with_name("agent_trace_learning")
DATA = ROOT / "data" / "northwind-payments-2026-10.json"
PACKAGE = "agent_trace_learning@1.0.0"
ENTITY = "account:northwind"
TRACES_UI = os.environ.get("MEMSEEK_TRACES_UI", "http://127.0.0.1:10428/select/vmui/")
LOGS_UI = os.environ.get("MEMSEEK_LOGS_UI", "http://127.0.0.1:9428/select/vmui/")


@dataclass(frozen=True)
class Question:
    text: str
    correct: str
    checks: tuple[str, ...]


CYCLE_1 = (
    Question(
        "What did Northwind Traders collect in October 2026, net of refunds? "
        "Give the amount in euros.",
        "€1,953.99",
        (r"1953\.99",),
    ),
    Question(
        "How many successful charges did Northwind Traders take in October 2026, "
        "and what was their total in euros?",
        "12 charges, €2,037.98",
        (r"2037\.98",),
    ),
)
CYCLE_2 = (
    Question(
        "What was the largest refund Northwind Traders issued in October 2026, "
        "and which customer received it?",
        "€60.00, to Larkspur Hotel",
        (r"\b60(\.00)?\b", r"larkspur"),
    ),
    Question(
        "What was Northwind Traders' average successful charge in October 2026, in euros?",
        "€169.83 (12 charges from 6 customers)",
        (r"169\.83",),
    ),
)


@dataclass
class Result:
    question: Question
    answer: str
    right: bool
    steps: int | None
    tool_calls: int | None
    tool_errors: int | None
    wall_s: float | None
    trace_id: str | None
    job_trace_id: str
    diagnosis: str | None = None


async def ensure_workspace() -> str:
    """Reuse MEMSEEK_API_KEY, or mint a throwaway workspace over DATABASE_URL."""

    if key := os.environ.get("MEMSEEK_API_KEY"):
        return key
    from memseek.auth import create_workspace
    from memseek.config import get_settings
    from memseek.db import pool_lifespan

    async with pool_lifespan(get_settings()) as pool:
        credential = await create_workspace(pool, f"trace-learning-{secrets.token_hex(3)}")
    print(f"created workspace {credential.workspace}")
    return credential.api_key


async def load_data(client: MemseekClient) -> None:
    document = json.loads(DATA.read_text(encoding="utf-8"))
    deliveries = [
        {
            "entity": ENTITY,
            "collection": "payment_events",
            "type": "webhook",
            "key": delivery["delivery_id"],
            "text": f"{delivery['event']['type']} {delivery['event']['id']} "
            f"(delivery {delivery['delivery_id']})",
            "content": {
                "delivery_id": delivery["delivery_id"],
                "received_at": delivery["received_at"],
                "event": delivery["event"],
            },
        }
        for delivery in document["deliveries"]
    ]
    statement = document["statement"]
    await client.records.ingest_many(
        [
            *deliveries,
            {
                "entity": ENTITY,
                "collection": "payouts",
                "type": "statement",
                "key": statement["period"],
                "text": f"Payout statement for {statement['period']}",
                "content": statement,
            },
        ]
    )
    print(f"loaded {len(deliveries)} webhook deliveries and the October payout statement")


def is_right(answer: str, question: Question) -> bool:
    plain = re.sub(r"(?<=\d),(?=\d{3})", "", answer.lower())
    return all(re.search(check, plain) for check in question.checks)


async def runs_since(client: MemseekClient, processor: str, since: str) -> list[dict[str, Any]]:
    listed = await client.runs(entity=ENTITY, processor=processor, limit=50)
    return [run for run in listed.get("runs", []) if run["created_at"] > since]


async def wait_for_answers(
    client: MemseekClient, task_ids: Sequence[str], since: str, timeout_s: float
) -> dict[str, dict[str, Any]]:
    """The successful run_task run for each task, by task id."""

    deadline = time.monotonic() + timeout_s
    found: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        for run in await runs_since(client, "run_task", since):
            if run["status"] != "ok" or run["id"] in {f["run"]["id"] for f in found.values()}:
                continue
            detail = await client.run(run["id"])
            for task_id in detail["run"]["content"]["basis"].get("input_ids", ()):
                if task_id in task_ids:
                    found[task_id] = detail
        if len(found) == len(task_ids):
            return found
        await asyncio.sleep(5)
    missing = len(task_ids) - len(found)
    print(f"  {missing} question(s) got no answer in {timeout_s:.0f} s; see the worker's log")
    return found


async def wait_for_review(
    client: MemseekClient, trace_ids: set[str], since: str
) -> tuple[dict[str, str], str | None]:
    """The diagnosis of each run Jev judged wrong, by trace id, and the skill run it led to."""

    verdicts: dict[str, str] = {}
    judged: set[str] = set()
    last_flagging = ""
    seen: set[str] = set()
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline and not trace_ids <= judged:
        for run in await runs_since(client, "silent_failures", since):
            if run["id"] in seen or run["status"] not in {"ok", "noop"}:
                continue
            seen.add(run["id"])
            detail = await client.run(run["id"])
            content = detail["run"]["content"]
            judged |= {read["trace_id"] for read in content.get("trace_reads", ())}
            for output in detail.get("outputs", ()):
                # Each diagnosis cites the run record of the run Jev judged wrong.
                run_record = await client.record(output["citations"][0])
                verdicts[run_record["content"]["trace_id"]] = output["content"]["text"]
                last_flagging = max(last_flagging, run["created_at"])
        if not trace_ids <= judged:
            await asyncio.sleep(5)
    if not last_flagging:
        return verdicts, None
    deadline = time.monotonic() + 240
    drafted: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        drafted = await runs_since(client, "skill", last_flagging)
        if ok := [r for r in drafted if r["status"] == "ok"]:
            return verdicts, ok[0]["id"]
        await asyncio.sleep(5)
    kinds = sorted({str(r["error_kind"]) for r in drafted if r["status"] == "failed"})
    print(f"  the skill derivation drafted nothing in 240 s (failed: {', '.join(kinds) or 'none'})")
    return verdicts, None


def result_of(question: Question, detail: dict[str, Any] | None) -> Result:
    if detail is None:
        return Result(question, "(no answer)", False, None, None, None, None, None, "")
    content = detail["run"]["content"]
    receipt = next(iter(content.get("computer_trace") or ()), {})
    metrics = receipt.get("metrics") or {}
    texts = [str(o["content"]["text"]) for o in detail.get("outputs", ())]
    answer = " ".join(texts) or "(no answer)"
    return Result(
        question=question,
        answer=answer,
        right=is_right(answer, question),
        steps=metrics.get("steps"),
        tool_calls=metrics.get("tool_calls"),
        tool_errors=metrics.get("tool_errors"),
        wall_s=metrics.get("wall_s"),
        trace_id=receipt.get("trace_id"),
        job_trace_id=detail["run"]["id"].replace("-", ""),
    )


async def cycle(
    client: MemseekClient, number: int, questions: Sequence[Question]
) -> tuple[list[Result], str | None]:
    print(f"\n=== Cycle {number}: {len(questions)} questions ===")
    # The API and worker run on this machine, so its clock orders runs well enough.
    since = datetime.now(UTC).isoformat()
    task_ids = []
    for question in questions:
        written = await client.records.ingest(
            entity=ENTITY, collection="tasks", type="task", text=question.text, content={}
        )
        task_ids.append(str(written["inserted"][0]["id"]))
        print(f"  asked: {question.text}")
    print("  waiting for the agent (each run takes about 10 to 60 s)...")
    answers = await wait_for_answers(client, task_ids, since, timeout_s=150 * len(questions))
    results = [result_of(q, answers.get(t)) for q, t in zip(questions, task_ids, strict=True)]
    print("  waiting for Jev to judge the runs from their traces...")
    traced = {r.trace_id for r in results if r.trace_id}
    verdicts, skill_run = await wait_for_review(client, traced, since)
    for result in results:
        result.diagnosis = verdicts.get(result.trace_id or "")
    report(results)
    if skill_run is not None:
        await show_skill(client, skill_run)
    return results, skill_run


def report(results: Sequence[Result]) -> None:
    for index, r in enumerate(results, start=1):
        mark = "right" if r.right else "WRONG"
        print(f"\n  Q{index}. {r.question.text}")
        print(f"      answer   {r.answer[:300]}")
        print(f"      correct  {r.question.correct}  ->  {mark}")
        print(
            f"      run      {r.steps} model calls, {r.tool_calls} tool calls "
            f"({r.tool_errors} failed), {r.wall_s} s"
        )
        print(f"      Jev      {'judged it wrong' if r.diagnosis else 'judged it right'}")
        if r.diagnosis:
            print(f"      why      {r.diagnosis}")
        if r.trace_id:
            print(f"      trace    {TRACES_UI}#/trace?trace_id={r.trace_id}")
        print(f"      job      {TRACES_UI}#/trace?trace_id={r.job_trace_id}")


async def show_skill(client: MemseekClient, run_id: str) -> None:
    detail = await client.run(run_id)
    print(f"\n  The skill derivation drafted (run {run_id}):")
    for output in detail.get("outputs", ()):
        print(f"    [{output['key']}] {output['content'].get('text', '(empty)')}")
    print(f"      job trace {TRACES_UI}#/trace?trace_id={run_id.replace('-', '')}")


def compare(first: Sequence[Result], second: Sequence[Result]) -> None:
    def summary(results: Sequence[Result]) -> str:
        right = sum(r.right for r in results)
        wall = sum(r.wall_s or 0 for r in results)
        errors = sum(r.tool_errors or 0 for r in results)
        calls = sum(r.steps or 0 for r in results)
        return f"{right}/{len(results)} right, {calls} model calls, {errors} failed tool calls, {wall:.0f} s"

    print("\n=== Before and after the promoted skill ===")
    print(f"  cycle 1 (no skill):     {summary(first)}")
    print(f"  cycle 2 (learned skill): {summary(second)}")
    print(f"\nFollow any run in the traces UI ({TRACES_UI}) and the logs UI ({LOGS_UI}).")


async def main(promote: bool) -> None:
    base_url = os.environ.get("MEMSEEK_URL", "http://127.0.0.1:8000")
    async with MemseekClient(base_url, await ensure_workspace()) as client:
        published = await client.catalog.publish(package=PACKAGE, directory=ROOT)
        print(f"published {PACKAGE} ({published.get('catalog_hash', '')[:12]})")
        await load_data(client)

        first, skill_run = await cycle(client, 1, CYCLE_1)
        if not any(r.diagnosis for r in first):
            print("\nJev judged every answer right, so there is nothing to learn. Stopping here.")
            return
        if skill_run is None:
            print("\nThe skill derivation drafted nothing; see the worker's log. Stopping here.")
            return
        if not promote:
            print(
                "\nThe drafts wait for review. Promote them with\n"
                f"  client.promote(entity={ENTITY!r}, source_run_id={skill_run!r}, "
                "artifact='learned_skill')"
            )
            return
        promoted = await client.promote(
            entity=ENTITY, source_run_id=skill_run, artifact="learned_skill"
        )
        print(f"\npromoted {promoted['promoted']} skill sections; a person would review them first")

        second, _ = await cycle(client, 2, CYCLE_2)
        compare(first, second)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--no-promote",
        action="store_true",
        help="stop after cycle 1 and leave the drafted skill for you to review",
    )
    arguments = parser.parse_args()
    asyncio.run(main(promote=not arguments.no_promote))
