"""A memory that catches itself in a contradiction — the reflective loop.

This runs the four-tier self-auditing loop authored in the catalog on top of
the shipped `reflection` + `contradiction` primitives:

    main observations
        │  reflection            (shipped: cited insights)
        ▼
    reflections
        │  worldview             (new: distil insights into KEYED convictions)
        ▼
    worldview  (identity, strategy, commitments, risks, principles)
        │  belief_conflict       (new: the contradiction detector, pointed at
        ▼                         the agent's OWN convictions -> relations edges)
    relations / self_contradiction
        │  reconcile             (new: census fires at >= 3 standing conflicts;
        ▼                         the agent reflects on its own dissonance)
    reflections / reconciliation ── feeds the next worldview run ──┐
        └──────────────────────────────────────────────────────────┘

The "wow": every conclusion is a glass box. A reconciliation insight opens to
the convictions it reasoned over, each of which opens to the reflections behind
it, each of which opens to the raw, importance-scored observation the agent
actually saw. Provenance `depth` counts the tiers: 0 event -> 1 reflection ->
2 conviction -> 3 reconciliation. Nothing the agent "concluded" is unfalsifiable.

Run it against a local stack with a real provider (offline LLM_FAKE=1 cannot
invent citation UUIDs, so the derivations no-op honestly — use a real model):

    make database && source .env.sh
    export PROVIDER_OPENAI_COMPAT_API_KEY=sk-...     # or OPENAI_API_KEY
    uv run memseek migrate
    uv run uvicorn memseek.api:app &                 # terminal A
    uv run memseek worker &                          # terminal B
    uv run python examples/self_auditing_mind.py

Set MEMSEEK_API_KEY to reuse a workspace, or DATABASE_URL to create a fresh one.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from typing import Any

from _catalog import publish_reference_catalog
from _workspace_explorer import print_workspace_explorer

from memseek.config import get_settings
from memseek.sdk import MemseekClient, MemseekHTTPError

RUN = secrets.token_hex(3)
AGENT = f"strategist:athena-{RUN}"  # an autonomous strategist advising "Northwind"

# --- terminal styling (TTY only; honors NO_COLOR) --------------------------
_C = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"


def _s(*c: int) -> str:
    return ("\033[" + ";".join(map(str, c)) + "m") if _C else ""


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


def paint(t: str, *c: str) -> str:
    return ("".join(c) + t + RESET) if _C else t


def show(title: str, body: str = "") -> None:
    rule = paint("━" * 70, GREY)
    print(f"\n{rule}\n  {paint(title, BOLD, CYAN)}\n{rule}")
    if body:
        print(paint(f"  {body}", DIM))


def note(t: str) -> None:
    print(paint(f"  · {t}", GREY))


def short(rid: str | None) -> str:
    return rid[:8] if rid else "—"


def strip_marker(t: str) -> str:
    return t.split(" [importance=")[0]


def bar(score: float | None, width: int = 10) -> str:
    if score is None:
        return " " * (width + 1)
    filled = max(0, min(width, round(score / 10 * width)))
    color = GREEN if score >= 7 else YELLOW if score >= 4 else RED
    return paint("█" * filled, color) + paint("░" * (width - filled), GREY) + " "


# --- the agent's world: two waves of observations that pull it two ways -----
def obs(text: str, importance: int, tag: str) -> dict[str, Any]:
    return {
        "collection": "main",
        "entity": AGENT,
        "type": "observation",
        "text": f"{text} [importance={importance}]",
        "dedupe_key": f"sam-{RUN}:{tag}",
    }


# Wave 1 establishes a hard, standing OBLIGATION (a signed contract) alongside
# an enterprise-first strategy. Wave 2 pivots the forward strategy to SMB — but
# a signed contract is a durable fact that does not evaporate when strategy
# changes. The bind that survives: "we are contractually obligated to ship the
# enterprise roadmap this quarter" ⇄ "redirect all engineering to SMB now".
FOUNDING = [
    obs(
        "Northwind has signed contracts with three enterprise accounts — Meridian, Halcyon, and Corva — that legally obligate delivery of their SSO and audit-log roadmap by the end of this quarter.",
        9,
        "ent-contract",
    ),
    obs(
        "The bespoke enterprise sales motion — dedicated security features and CSMs — has driven essentially all of Northwind's revenue to date.",
        8,
        "ent-motion",
    ),
    obs(
        "Leadership tells the board Northwind's moat is being the most secure, compliance-first option for regulated industries.",
        8,
        "moat",
    ),
    obs(
        "Two senior engineers resigned, citing burnout from compressed enterprise delivery timelines.",
        7,
        "burnout",
    ),
]

DISRUPTION = [
    obs(
        "A self-serve SMB signup experiment converted 400 small teams in three weeks with zero sales involvement.",
        9,
        "smb-exp",
    ),
    obs(
        "SMB self-serve revenue has grown 40% month over month for four straight months and now rivals enterprise net-new.",
        9,
        "smb-growth",
    ),
    obs(
        "The board has directed Athena to redirect engineering to the SMB self-serve flywheel and to stop starting new bespoke enterprise work.",
        9,
        "board-order",
    ),
    obs(
        "Athena concludes the company's future growth engine is self-serve SMB, not enterprise expansion.",
        8,
        "future-smb",
    ),
]


class Loop:
    def __init__(self, client: MemseekClient) -> None:
        self.c = client

    async def ingest(self, records: list[dict[str, Any]]) -> list[str]:
        res = await self.c.records.ingest_many(records)
        return [r["id"] for r in res.get("inserted", []) + res.get("duplicates", [])]

    async def wait_ready(self, ids: list[str], timeout_s: float = 120.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        pending = list(ids)
        while pending:
            detail = await self.c.record(pending[-1])
            if detail.get("ready"):
                pending.pop()
                continue
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("records not enriched — is `memseek worker` running?")
            await asyncio.sleep(0.5)

    async def derive(self, processor: str) -> list[dict[str, Any]]:
        """Run one derivation for the agent, wait for it, return its newest run's
        output records (each with id, key, content.text, citations)."""
        job = await self.c.run_processor(processor, entity=AGENT)
        job_id = job["job_id"]
        deadline = asyncio.get_running_loop().time() + 150.0
        while True:
            state = (await self.c.job(job_id)).get("state")
            if state in {"done", "dead"} or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(1.0)
        runs = await self.c.runs(entity=AGENT, processor=processor, operation="derive")
        for run in runs.get("runs", []):
            if run.get("output_count"):
                detail = await self.c._request("GET", f"/runs/{run['id']}")
                outputs = detail.get("outputs", [])
                if outputs:
                    await self.wait_ready([o["id"] for o in outputs])
                return outputs
        return []

    async def convictions(self) -> list[dict[str, Any]]:
        doc = await self.c.document(entity=AGENT, collections="worldview")
        return doc.get("beliefs", [])

    async def edges(self) -> list[dict[str, Any]]:
        """Standing self_contradiction edges for the agent, sharpest first."""
        tl = await self.c._request(
            "GET",
            "/timeline",
            params={
                "entity": AGENT,
                "collections": "relations",
                "types": "self_contradiction",
                "limit": 25,
            },
        )
        out = []
        for row in tl.get("records", []):
            out.append(await self.c.record(row["id"]))
        out.sort(key=lambda e: (e.get("content") or {}).get("confidence", 0), reverse=True)
        return out

    async def glass_box(
        self, root: dict[str, Any], _seen: set[str] | None = None, depth: int = 0
    ) -> None:
        """Open a conclusion to its roots by following `derived_from` down the
        tiers. Each record prints its provenance depth and (if scored) importance."""
        seen = _seen if _seen is not None else set()
        rid = root["id"]
        if rid in seen:
            return
        seen.add(rid)
        content = root.get("content") or {}
        text = strip_marker(str(content.get("text", "")))
        tier = f"{root.get('collection')}/{root.get('type')}"
        score = (root.get("scores") or {}).get("importance")
        indent = "    " + "  " * depth
        elbow = paint("└─ ", GREY) if depth else ""
        d = paint(f"depth {root.get('depth')}", GREY)
        print(
            f"{indent}{elbow}{bar(float(score) if score is not None else None)}{paint(short(rid), BOLD)} {paint(tier, CYAN)} {d}"
        )
        print(f"{indent}   {paint(text, GREEN if depth == 0 else YELLOW)}")
        for parent_id in root.get("derived_from") or []:
            try:
                parent = await self.c.record(parent_id)
            except MemseekHTTPError:
                continue
            await self.glass_box(parent, seen, depth + 1)


def print_outputs(rows: list[dict[str, Any]], glyph: str, color: str) -> None:
    for r in rows:
        txt = strip_marker((r.get("content") or {}).get("text") or "")
        key = f"{paint(str(r['key']), BOLD, CYAN)}: " if r.get("key") else ""
        cited = paint(f"[{len(r.get('citations') or [])} cited]", GREY)
        print(f"    {paint(glyph, color)} {key}{paint(txt, color)}  {cited}")


async def ensure_workspace() -> str:
    if key := os.environ.get("MEMSEEK_API_KEY"):
        return key
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("set MEMSEEK_API_KEY (existing workspace) or DATABASE_URL (fresh one)")
    from memseek.auth import create_workspace
    from memseek.db import pool_lifespan

    async with pool_lifespan(get_settings()) as pool:
        cred = await create_workspace(pool, f"selfaudit-{RUN}")
    print(paint(f"created disposable workspace {cred.workspace}", GREY))
    return cred.api_key


async def main() -> None:
    api_key = await ensure_workspace()
    base_url = os.environ.get("MEMSEEK_BASE_URL", "http://127.0.0.1:8000")
    print_workspace_explorer(api_url=base_url, api_key=api_key)
    live = not get_settings().llm_fake

    async with MemseekClient(base_url, api_key) as client:
        await publish_reference_catalog(client)
        active = {
            c["name"]
            for c in (await client._request("GET", "/collections")).get("collections", [])
            if c.get("active")
        }
        if "worldview" not in active:
            raise SystemExit(
                "this workspace has no 'worldview' collection — the self-auditing\n"
                "catalog (collections/worldview.yaml, derivations/worldview.yaml,\n"
                "belief_conflict.yaml, reconcile.yaml) is not loaded. Point the service\n"
                "at this repo tree, or unset MEMSEEK_API_KEY to build a fresh workspace."
            )
        loop = Loop(client)

        show(
            "ATHENA — a strategist agent that audits its own mind",
            f"advising Northwind · run {RUN}",
        )
        print(
            f"  provider: {paint('real model' if live else 'LLM_FAKE=1 (cannot cite — derivations will no-op)', BOLD, GREEN if live else YELLOW)}"
        )

        # ---- WAVE 1: the agent forms its founding convictions ---------------
        show(
            "WEEK 1 — founding evidence",
            "the agent observes, reflects, and forms durable convictions",
        )
        await loop.wait_ready(await loop.ingest(FOUNDING))
        print(paint("\n  reflection — cited insights from the week:", BOLD))
        print_outputs(await loop.derive("reflection"), "✦", MAGENTA)
        print(paint("\n  worldview — insights distilled into KEYED convictions:", BOLD))
        print_outputs(await loop.derive("worldview"), "◆", BLUE)

        # ---- WAVE 2: new evidence pulls the agent the other way -------------
        show(
            "WEEK 2 — the ground shifts",
            "SMB self-serve explodes; the board changes course; a compliance bug ships",
        )
        await loop.wait_ready(await loop.ingest(DISRUPTION))
        print(paint("\n  reflection — insights from the disruption:", BOLD))
        print_outputs(await loop.derive("reflection"), "✦", MAGENTA)
        print(
            paint(
                "\n  worldview — the agent updates SOME convictions (strategy) but not others:",
                BOLD,
            )
        )
        print_outputs(await loop.derive("worldview"), "◆", BLUE)
        print(paint("\n  current convictions now held:", BOLD))
        for b in sorted(await loop.convictions(), key=lambda x: x.get("key") or ""):
            print(
                f"    {paint(str(b['key']), BOLD, CYAN)}: {paint(strip_marker(b.get('text', '')), GREY)}"
            )

        # ---- DETECT: the contradiction machinery, aimed at the agent itself -
        show(
            "THE AGENT CATCHES ITSELF",
            "belief_conflict = the shipped contradiction detector, pointed at worldview",
        )
        note("no new engine — same relations collection, new edge type 'self_contradiction'")
        await loop.derive("belief_conflict")
        edges = await loop.edges()
        for e in edges:
            c = e["content"]
            print(
                f"\n  {paint(f'⚡ self-contradiction (confidence {c.get("confidence")})', BOLD, RED)}  {c.get('text')}"
            )
            print(paint(f"     {c.get('explanation')}", GREY))
            print(
                paint(
                    f"     between {short(c.get('subject_id'))} ⇄ {short(c.get('object_id'))}", GREY
                )
            )
        if not edges:
            print(
                paint(
                    "\n  no standing conflict detected this run — the agent's current convictions",
                    YELLOW,
                )
            )
            print(
                paint(
                    "  came out coherent (or the model harmonized them). Re-run, or sharpen the",
                    YELLOW,
                )
            )
            print(paint("  evidence, so a signed obligation survives the strategy pivot.", YELLOW))
        threshold = 2
        verdict = paint(
            f"{len(edges)} standing conflict(s)", RED if len(edges) >= threshold else YELLOW
        )
        print(f"\n  {verdict} — census fires `reconcile` at ≥ {threshold}.")

        # ---- ESCALATE: reflect on the accumulated dissonance ----------------
        show(
            "RECKONING", "the census escalation: the agent reflects on WHY its own beliefs collide"
        )
        note(
            f"in production the census trigger fires this automatically once ≥{threshold} conflicts stand; here we run it explicitly"
        )
        reconciliation = await loop.derive("reconcile")
        print_outputs(reconciliation, "✧", GREEN)

        # ---- THE WOW: open one reconciliation all the way to raw evidence ---
        show(
            "GLASS BOX", "open one reconciliation to its roots — depth counts the tiers of the loop"
        )
        note("reconciliation (d3) → convictions (d2) → reflections (d1) → the raw observation (d0)")
        target = reconciliation[0] if reconciliation else None
        if target:
            await loop.glass_box(await client.record(target["id"]))
        else:
            print(
                paint(
                    "  (no reconciliation this run — a real provider is needed to cite evidence)",
                    YELLOW,
                )
            )

        note("\n  the reconciliation is a reflection-family record → the NEXT worldview run")
        note("  consumes it as evidence. The loop closes: the mind revises itself, and every")
        note("  step of that revision is cited, timestamped, and replayable.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except MemseekHTTPError as error:
        raise SystemExit(f"memseek API error: {error}") from error
