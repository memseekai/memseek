"""Scrape a site twice and watch the second run start from what the first learned.

Run: make site-scrape-demo URL=https://news.ycombinator.com

The Agent runs under the pi harness with the browser-harness skill pack, on the
`local` Computer provider, so the API and the worker must run on this machine
with `pi`, `browser-harness`, and a model key available. See
examples/site_scrape_catalog/README.md for the setup.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from memseek.sdk import MemseekClient, MemseekHTTPError

CATALOG_ROOT = Path(__file__).with_name("site_scrape_catalog")
PACKAGE = "site_scrape@1.0.0"
COMPUTER = "scrape_workspace@1"
AGENT = "site_scraper@1"
POLICY = "scrape_budget@1"
PACK = "browser-harness"
DEFAULT_GOAL = "the main list of items on the page: title, url, and any score or count shown"


async def ensure_workspace() -> str:
    """Reuse MEMSEEK_API_KEY, or mint a throwaway workspace over DATABASE_URL."""

    if key := os.environ.get("MEMSEEK_API_KEY"):
        return key
    from memseek.auth import create_workspace
    from memseek.config import get_settings
    from memseek.db import pool_lifespan

    async with pool_lifespan(get_settings()) as pool:
        credential = await create_workspace(pool, f"site-scrape-{secrets.token_hex(3)}")
    print(f"created workspace {credential.workspace}")
    return credential.api_key


async def wait_ready(client: MemseekClient, record_id: str, timeout_s: float = 60) -> None:
    async with asyncio.timeout(timeout_s):
        while True:
            if (await client.record(record_id)).get("ready"):
                return
            await asyncio.sleep(0.4)


async def scrape(client: MemseekClient, entity: str, url: str, goal: str) -> dict[str, Any]:
    written = await client.records.ingest(
        entity=entity,
        collection="scrape_tasks",
        type="task",
        text=f"Scrape {url}: {goal}",
        content={"url": url, "goal": goal},
    )
    await wait_ready(client, str(written["inserted"][0]["id"]))
    agent = client.invocations.bind(computer=COMPUTER, agent=AGENT, context_policy=POLICY)
    run = await agent.start(entity=entity, prompt=f"Scrape {url}. Extract {goal}.")
    print(f"invocation {run.id} started; waiting for the harness (watch it: make pi-trace)")
    state = await run.wait(timeout_s=1_200)
    if state["status"] != "succeeded":
        raise RuntimeError(f"invocation {run.id} {state['status']}: {state.get('error')}")
    return state["result"]


def report(result: dict[str, Any]) -> None:
    items = (result.get("value") or {}).get("items") or []
    print(f"  {len(items)} items")
    for item in items[:3]:
        print(f"    {json.dumps(item, ensure_ascii=False)[:120]}")
    receipt = result.get("receipt") or {}
    print(f"  harness {receipt.get('harness')} · packs {receipt.get('skillpacks')}")
    print(f"  metrics {json.dumps(receipt.get('metrics'))}")
    if receipt.get("root"):
        print(f"  trace   make pi-trace ROOT={receipt['root']}")
    for item in receipt.get("outbox_rejected") or []:
        where = f"{item['path']}:{item['line']}" if item.get("line") else item["path"]
        print(f"  rejected {where}: {item['reason'][:160]}")


async def show_learnings(client: MemseekClient, entity: str) -> None:
    hits = (await client.query_view("site_learnings", entity=entity)).get("hits") or []
    print(f"  {len(hits)} learnings for {entity}")
    for hit in hits[:8]:
        print(f"    {hit.get('text')}")


async def main(url: str, goal: str) -> None:
    domain = urlsplit(url).hostname
    if not domain:
        raise SystemExit(f"not an absolute URL: {url!r}")
    entity = f"site:{domain}"
    base_url = os.environ.get("MEMSEEK_URL", "http://127.0.0.1:8000")
    async with MemseekClient(base_url, await ensure_workspace()) as client:
        published = await client.catalog.publish(package=PACKAGE, directory=CATALOG_ROOT)
        print(f"published {PACKAGE} ({published.get('catalog_hash', '')[:12]})")

        print(f"\nrun 1 · {url}")
        report(await scrape(client, entity, url, goal))
        await show_learnings(client, entity)

        print(f"\nrun 2 · {url}, starting from the playbook")
        second = await scrape(client, entity, url, goal)
        report(second)
        playbook = Path(second["receipt"]["root"]) / ".agents/skills" / PACK / "PLAYBOOK.md"
        if playbook.is_file():
            print(f"\n{playbook}:\n")
            print(playbook.read_text(encoding="utf-8"))
        else:
            print(f"\nno PLAYBOOK.md at {playbook}: run 1 recorded no learnings")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url")
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    arguments = parser.parse_args()
    try:
        asyncio.run(main(arguments.url, arguments.goal))
    except (MemseekHTTPError, RuntimeError, TimeoutError) as error:
        raise SystemExit(str(error)) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
