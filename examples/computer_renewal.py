"""A Computer-backed renewal in three steps. Run: make computer-demo.

The stand-in is deterministic; MODE=cloudflare selects a real model explicitly.
For the interactive desk (recall, fork, receipts), use ADVANCED=1.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from _computer_common import (
    ANALYST,
    ENTITY,
    OBSERVATIONS,
    POLICY,
    PROPOSALS,
    RESEARCH_COMPUTER,
    RISKS,
    TERMS,
)
from _computer_renewal_support import (
    CONTRACT,
    SCRIPTED_ANSWERS,
    STANDING,
    Demo,
    ainput,
    ensure_workspace,
    main,
    pending_question,
)
from _computer_runtime import ComputerRuntime

from memseek.sdk import MemseekClient, MemseekHTTPError


def require(condition: Any, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def walkthrough(
    runtime: ComputerRuntime | None,
    *,
    port: int = 0,
    provider: str = "local-standin",
    scripted: bool = False,
) -> None:
    api_key = await ensure_workspace()
    base_url = os.environ.get("MEMSEEK_BASE_URL", "http://127.0.0.1:8000")
    interactive = sys.stdin.isatty() and not scripted
    print(f"Renewal example · {provider} · {ENTITY}")
    async with MemseekClient(base_url, api_key) as client:
        demo = Demo(client)
        await demo.publish()

        # 1. An unkeyed contract arrival triggers deterministic Program extraction.
        contract = await demo.ingest("contract", CONTRACT, key=None)
        await demo.wait_ready([contract])
        require(await demo.wait_for(TERMS), "Program produced no contract terms")
        print("Program: contract terms extracted")

        # 2. Keyed standing facts become the Agent's citable context.
        evidence = [await demo.ingest(kind, text, key) for kind, key, text in STANDING]
        await demo.wait_ready(evidence)
        print("Agent: assessing the standing evidence…")
        await demo.derive("renewal_assessment")
        require(await demo.wait_for(RISKS), "Agent produced no renewal risk")
        risks = await demo.rows(RISKS)
        require(all(row.get("derived_from") for row in risks), "Risk has no citations")
        print("Agent: cited renewal risk found")

        # 3. Configure exact references once, then start and answer durable work.
        analyst = client.invocations.bind(
            computer=RESEARCH_COMPUTER,
            agent=ANALYST,
            context_policy=POLICY,
        )
        run = await analyst.start(
            entity=ENTITY,
            prompt="Prepare the renewal position before Friday's quote.",
        )
        print(f"Invocation: {run.id}")
        state = await run.wait()
        replies = 0
        while state["status"] == "awaiting_input" and replies < 8:
            print(pending_question(state))
            fallback = SCRIPTED_ANSWERS[min(replies, len(SCRIPTED_ANSWERS) - 1)]
            answer = (
                (await ainput("Your answer: ")).strip() or fallback if interactive else fallback
            )
            await run.reply(answer)
            replies += 1
            state = await run.wait()
        require(state["status"] == "succeeded", f"Invocation {run.id}: {state}")
        if provider == "local-standin":
            require(replies == 2, f"Expected two answered pauses, got {replies}")
        result = state.get("result") or {}
        require(result.get("citation_ids"), "Invocation returned no citations")
        require(await demo.rows(OBSERVATIONS), "Expected an active observation")
        require(await demo.rows(PROPOSALS, status="draft"), "Expected a review-required proposal")
        require(not await demo.rows(PROPOSALS), "Proposal unexpectedly became active")
        print(json.dumps(result.get("value"), indent=2))
        print("PASS: terms, cited risk, answered invocation, active observation, draft proposal")


if __name__ == "__main__":
    try:
        asyncio.run(main(walkthrough=walkthrough))
    except (MemseekHTTPError, RuntimeError, TimeoutError) as error:
        raise SystemExit(str(error)) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
