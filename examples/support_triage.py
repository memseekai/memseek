"""Triage one customer's support inbox with a judgment model.

Run: make support-triage-demo

It publishes examples/support_triage_catalog, writes an account profile and
five tickets for one customer, then shows what TypeSafe's Jev decided: each
ticket's urgency and signals, the routes route_tickets wrote, and the notes
escalate wrote once enough frustration had accumulated. The API and worker
need TYPESAFE_API_KEY and OPENAI_API_KEY (make up passes both through).
"""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path
from typing import Any

import yaml

from memseek.sdk import MemseekClient, MemseekHTTPError

CATALOG_ROOT = Path(__file__).with_name("support_triage_catalog")
PACKAGE = "support_triage@1.0.0"
ENTITY = "customer:northwind"
KEY_HINT = "the api and worker need TYPESAFE_API_KEY and OPENAI_API_KEY set; restart them after"

ACCOUNT = {
    "collection": "accounts",
    "type": "profile",
    "key": "profile",
    "text": "Northwind Traders, a 40-seat wholesale distributor on the basic plan.",
    "content": {
        "plan": "basic",
        "notes": "Basic covers email support and the standard API. SSO and "
        "phone support are premium only.",
    },
}

TICKETS = (
    (
        "Charged twice this month",
        "You charged our card twice for September, $480 each. This is the second "
        "time this year. Refund the duplicate today or we are cancelling.",
    ),
    (
        "Exporting orders",
        "Hi! How do I export last quarter's orders to CSV? No rush, thanks.",
    ),
    (
        "API down",
        "Every call to the orders API has returned 503 since 9am. Our warehouse "
        "cannot pick anything and trucks are waiting.",
    ),
    (
        "SSO",
        "Can we turn on SSO with Okta for everyone? IT keeps asking me.",
    ),
    (
        "hmm",
        "Something looks off with our account since last week.",
    ),
)


async def ensure_workspace() -> str:
    """Reuse MEMSEEK_API_KEY, or mint a throwaway workspace over DATABASE_URL."""

    if key := os.environ.get("MEMSEEK_API_KEY"):
        return key
    from memseek.auth import create_workspace
    from memseek.config import get_settings
    from memseek.db import pool_lifespan

    async with pool_lifespan(get_settings()) as pool:
        credential = await create_workspace(pool, f"support-triage-{secrets.token_hex(3)}")
    print(f"created workspace {credential.workspace}")
    return credential.api_key


async def wait_ready(
    client: MemseekClient, labels: dict[str, str], timeout_s: float
) -> list[dict[str, Any]]:
    record_ids = list(labels)
    details: dict[str, dict[str, Any]] = {}
    try:
        async with asyncio.timeout(timeout_s):
            while len(details) < len(record_ids):
                for record_id in record_ids:
                    if record_id not in details:
                        detail = await client.record(record_id)
                        if detail["ready"]:
                            details[record_id] = detail
                await asyncio.sleep(0.4)
    except TimeoutError:
        errors = {
            error
            for record_id in record_ids
            if (error := (await client.record(record_id)).get("enrichment_error"))
        }
        detail = f" (last error: {sorted(errors)[0]})" if errors else ""
        raise RuntimeError(
            f"records not ready after {timeout_s:.0f}s{detail}; is the worker running? {KEY_HINT}"
        ) from None
    for record_id, label in labels.items():
        if error := details[record_id]["enrichment_error"]:
            raise RuntimeError(f"{label} fell back to defaults: {error}\n{KEY_HINT}")
    return [details[record_id] for record_id in record_ids]


async def wait_for_run(client: MemseekClient, job_id: str, timeout_s: float) -> dict[str, Any]:
    async with asyncio.timeout(timeout_s):
        while True:
            job = await client.job(job_id)
            if job.get("successful_run_id"):
                return await client.run(job["successful_run_id"])
            if job["state"] == "dead":
                runs = job.get("attempt_run_ids") or []
                error = (await client.run(runs[-1]))["run"]["content"]["error"] if runs else None
                raise RuntimeError(
                    f"{job['derivation']} failed ({job['last_error_kind']}): {error}"
                )
            await asyncio.sleep(0.5)


def show_tickets(tickets: list[dict[str, Any]], labels: dict[str, str]) -> None:
    print("tickets")
    for ticket in tickets:
        signals = ticket["annotations"]["signals"]
        topic = signals["topic"]
        print(
            f"  {labels[ticket['id']]}  urgency {ticket['scores']['urgency']:4.1f}"
            f" · needs_reply {signals['needs_reply']['noul']:.2f}"
            f" · topic {topic['choice']} ({topic['confidence']:.2f})"
            f" · frustration {signals['frustration']['score']:.1f}"
            f" · {ticket['content'].get('subject', '')}"
        )


async def route(client: MemseekClient, labels: dict[str, str]) -> None:
    queued = await client.run_processor("route_tickets", entity=ENTITY)
    result = await wait_for_run(client, queued["job_id"], timeout_s=120)
    routes = {output["citations"][0]: output["content"] for output in result["outputs"]}
    print(f"\nroute_tickets: {len(routes)} routed, {len(labels) - len(routes)} left for a human")
    for ticket_id, label in labels.items():
        content = routes.get(ticket_id)
        if content is None:
            print(f"  {label}  left for a human: no team reached confidence 0.6")
            continue
        team, covered = content["judgment"]["team"], content["judgment"]["covered"]
        print(
            f"  {label}  → {team['choice']:<9} confidence {team['confidence']:.2f}"
            f" · covered by plan {covered['noul']:.2f}"
        )


async def show_escalations(
    client: MemseekClient,
    tickets: list[dict[str, Any]],
    labels: dict[str, str],
    timeout_s: float,
) -> None:
    derivation = yaml.safe_load((CATALOG_ROOT / "derivations" / "escalate.yaml").read_text())
    threshold = derivation["trigger"]["accumulator"]["threshold"]
    total = sum(ticket["annotations"]["signals"]["frustration"]["score"] for ticket in tickets)
    print(f"\nescalate: frustration sum {total:.1f}, threshold {threshold}")
    try:
        async with asyncio.timeout(timeout_s):
            while True:
                runs = (await client.runs(entity=ENTITY, processor="escalate"))["runs"]
                finished = [run for run in runs if run["status"] in {"ok", "noop", "failed"}]
                if finished:
                    break
                await asyncio.sleep(0.5)
    except TimeoutError:
        if total < threshold:
            print(
                f"  no run: the trigger fires at {threshold} and these tickets sum to {total:.1f}"
            )
        else:
            print(f"  no run finished within {timeout_s:.0f}s although the sum reached {threshold}")
        return
    result = await client.run(finished[0]["id"])
    content = result["run"]["content"]
    if content["status"] == "failed":
        raise RuntimeError(
            f"escalate failed ({content['error_kind']}): {content['error']}\n{KEY_HINT}"
        )
    print(f"  the trigger fired; {len(result['outputs'])} escalation notes")
    for output in result["outputs"]:
        label = labels.get(output["citations"][0], output["citations"][0])
        print(f"  {label}  [{output['content']['severity']}] {output['content']['text']}")


async def demo(client: MemseekClient, *, escalation_timeout_s: float = 60) -> None:
    published = await client.catalog.publish(package=PACKAGE, directory=CATALOG_ROOT)
    print(f"published {PACKAGE} ({published.get('catalog_hash', '')[:12]})")
    written = await client.records.ingest_many(
        [
            {"entity": ENTITY, **ACCOUNT},
            *(
                {
                    "entity": ENTITY,
                    "collection": "tickets",
                    "type": "ticket",
                    "text": text,
                    "content": {"subject": subject},
                }
                for subject, text in TICKETS
            ),
        ]
    )
    account_id, *ticket_ids = (str(row["id"]) for row in written["inserted"])
    print(f"wrote the account profile and {len(ticket_ids)} tickets for {ENTITY}\n")
    labels = {ticket_id: f"T{index}" for index, ticket_id in enumerate(ticket_ids, 1)}
    await wait_ready(client, {account_id: "account"}, timeout_s=120)
    tickets = await wait_ready(client, labels, timeout_s=120)
    show_tickets(tickets, labels)
    await route(client, labels)
    await show_escalations(client, tickets, labels, escalation_timeout_s)


async def main() -> None:
    base_url = os.environ.get("MEMSEEK_URL", "http://127.0.0.1:8000")
    async with MemseekClient(base_url, await ensure_workspace()) as client:
        await demo(client)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (MemseekHTTPError, RuntimeError, TimeoutError) as error:
        raise SystemExit(str(error)) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
