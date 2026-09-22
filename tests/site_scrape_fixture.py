"""The site-scrape catalog wired to the fixture harness and fixture skill pack.

The catalog is the shipped one with two words changed, `pi` to `echo` and
`browser-harness` to `echo-pack`, which is the claim under test: a harness and
a skill pack are each only a directory.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.enrichment import enrich_once
from memseek.invocations import (
    InvocationCreate,
    InvocationError,
    create_invocation,
    execute_invocation,
    read_invocation,
)
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = REPOSITORY_ROOT / "examples" / "site_scrape_catalog"
FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures"


def site_scrape_settings(settings: Settings, tmp_path: Path) -> Settings:
    root = tmp_path / "catalog"
    shutil.copytree(CATALOG_ROOT, root)
    for path in root.rglob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        text = text.replace("harness: pi\n", "harness: echo\n")
        text = text.replace("pack: browser-harness\n", "pack: echo-pack\n")
        path.write_text(text, encoding="utf-8")
    return settings.model_copy(
        update={
            "models_file": root / "conf/models.yaml",
            "processors_file": root / "conf/processors.yaml",
            "rank_default_file": root / "conf/rank_default.yaml",
            "search_profiles_file": root / "conf/search_profiles.yaml",
            "collections_dir": root / "collections",
            "derivations_dir": None,
            "triggers_dir": None,
            "views_dir": root / "views",
            "artifacts_dir": root / "artifacts",
            "computers_dir": root / "computers",
            "programs_dir": None,
            "agents_dir": root / "agents",
            "context_policies_dir": root / "context_policies",
            "toolsets_dir": root / "toolsets",
            "mcp_dir": None,
            "packages_dir": root / "packages",
            "harness_paths": (tmp_path / "harnesses", FIXTURES / "harnesses"),
            "skillpack_paths": (FIXTURES / "skillpacks",),
            "local_computer_root": tmp_path / "computers",
        }
    )


class ScrapeWorkspace:
    """One workspace driven the way the API and worker drive it, minus HTTP."""

    def __init__(self, pool: DatabasePool, settings: Settings, workspace: str) -> None:
        self.pool = pool
        self.settings = settings
        self.workspace = workspace
        self.catalog: DefinitionCatalog = load_definition_catalog(settings)

    async def create(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into workspace (id, api_key_hash) values (%s, %s)",
                (self.workspace, "a" * 64),
            )

    async def settle(self) -> None:
        for _ in range(32):
            if (await enrich_once(self.pool, self.settings, self.catalog)).kind == "none":
                return
        raise AssertionError("records never became ready")

    async def write(self, entity: str, collection: str, type_: str, **content: Any) -> UUID:
        text = str(content.pop("text"))
        result = await insert_public_records(
            self.pool,
            workspace=self.workspace,
            request=RecordBatchRequest(
                records=(
                    PublicRecordInput(
                        entity=entity,
                        collection=collection,
                        type=type_,
                        text=text,
                        content=content,
                    ),
                )
            ),
            catalog=self.catalog,
            settings=self.settings,
        )
        await self.settle()
        return result.inserted[0].id

    async def write_task(self, entity: str, url: str, goal: str) -> UUID:
        return await self.write(
            entity, "scrape_tasks", "task", text=f"Scrape {url}: {goal}", url=url, goal=goal
        )

    async def invoke(
        self, entity: str, prompt: str, options: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Run the Agent to completion and return the invocation as read back."""

        task: dict[str, Any] = {"kind": "answer", "prompt": prompt}
        if options:
            task["input"] = options
        created = await create_invocation(
            self.pool,
            workspace=self.workspace,
            request=InvocationCreate.model_validate(
                {
                    "entity": entity,
                    "computer": "scrape_workspace@1",
                    "executor": {
                        "kind": "agent",
                        "agent": "site_scraper@1",
                        "context_policy": "scrape_budget@1",
                    },
                    "task": task,
                }
            ),
            catalog=self.catalog,
        )
        invocation_id = UUID(created["invocation_id"])
        # A failed run is recorded on the invocation, which is what callers read.
        with contextlib.suppress(InvocationError):
            await execute_invocation(
                self.pool,
                workspace=self.workspace,
                invocation_id=invocation_id,
                catalog=self.catalog,
                settings=self.settings,
                final_attempt=True,
            )
        await self.settle()
        return await read_invocation(
            self.pool, workspace=self.workspace, invocation_id=invocation_id
        )

    async def learnings(self, entity: str) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "select content, derived_from, status from record "
                "where workspace = %s and entity = %s and collection = 'skill_learnings' "
                "order by seq",
                (self.workspace, entity),
            )
            return [dict(row) for row in await result.fetchall()]
