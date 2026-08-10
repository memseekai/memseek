"""A minimal, self-contained catalog used by the definition-evolution tests.

Kept separate from any one test module so several can build on the same fixture
without importing each other's internals.  It is deliberately the smallest
catalog the loader accepts: one event collection, one archive collection to
migrate into, a constant score, an LLM annotation, one migration derivation, one
view, and one artifact.
"""

from __future__ import annotations

import copy
from typing import Any

import httpx
import yaml

from memseek.api import create_app
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog

PACKAGE = "evolving@1.0.0"

COLLECTION: dict[str, Any] = {
    "name": "notes",
    "version": 1,
    "active": True,
    "mode": "event",
    "schema": {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
        "additionalProperties": True,
    },
    "required_processors": ["embedding_v1"],
    "search_profile": "pg_default",
}

ARCHIVE: dict[str, Any] = {
    "name": "archive",
    "version": 1,
    "active": True,
    "mode": "event",
    "schema": {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string"},
            "channel": {"type": "string"},
            "origin": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "required_processors": [],
    "search_profile": "pg_default",
}

# A migration derivation: read one contract, emit another, keep the lineage.
# No trigger, so it only runs when an operator asks for it.
MIGRATION: dict[str, Any] = {
    "name": "archive_notes",
    "sources": {
        "legacy": {
            "kind": "snapshot",
            "collections": ["notes"],
            "collection_versions": {"notes": [1]},
            "statuses": ["active"],
            "keyed": False,
            "max_records": 100,
            "max_tokens": 40_000,
            "allow_empty": False,
        }
    },
    "model": None,
    "limits": {
        "max_tasks": 1,
        "max_llm_calls": 0,
        "max_retrieved_records": 0,
        "max_visible_records": 100,
        "max_total_tokens": 40_000,
        "max_wall_s": 30,
    },
    "tasks": [
        {
            "id": "migrated",
            "use": "map_records",
            "input": {"records": "{{legacy.records}}"},
            "with": {
                "keep": ["text", "channel"],
                "set": {"origin": {"value": "notes@1"}},
                "carry_key": False,
            },
        }
    ],
    "emit": {
        "from": "{{migrated}}",
        "collection": "archive",
        "collection_version": 1,
        "type": "note",
        "max_records": 100,
    },
}

PROCESSORS: list[dict[str, Any]] = [
    {"name": "embedding_v1", "kind": "embedding", "input": {"collections": ["notes"]}},
    # A constant score keeps the fixture deterministic while still satisfying
    # CONTEXT_DOC_ORDER_SCORE and giving ranking a score to read.
    {
        "name": "importance",
        "kind": "score",
        "source": "constant",
        "input": {"collections": ["notes"]},
        "scale": [1, 10],
        "value": 5,
    },
    {
        "name": "tone_v1",
        "kind": "json",
        "source": "llm",
        "input": {"collections": ["notes"]},
        "model": "cheap",
        "prompt": "Classify the tone.",
        "output_schema": {
            "type": "object",
            "required": ["label"],
            "properties": {"label": {"type": "string"}},
        },
        "default_output": {"label": "neutral"},
    },
]


# A catalog must declare at least one view and artifact, so the fixture carries
# the smallest useful pair rather than leaving them out.
VIEW: dict[str, Any] = {
    "name": "recent_notes",
    "version": 1,
    "active": True,
    "parameters": {"entity": {"type": "string", "required": True}},
    "query": {
        "q": "",
        "mode": "recent",
        "scope": {"entities": ["{{entity}}"], "collections": ["notes"]},
        "k": 20,
    },
}

ARTIFACT: dict[str, Any] = {
    "name": "note_digest",
    "version": 1,
    "active": True,
    "kind": "prompt",
    "lifecycle": "live",
    "parameters": {"entity": {"type": "string", "required": True}},
    "blocks": {
        "notes": {
            "view": "recent_notes@1",
            "args": {"entity": "{{entity}}"},
            "max_tokens": 2_000,
        }
    },
    "template": "Notes for {{entity}}:\n{{notes}}\n",
}

RANK_DEFAULT = """
candidates: 200
variants:
  hybrid: [sum, [[product, 1.0, [normalize, [max, [[similarity], [text_match]]]]]]]
  vector: [sum, [[product, 1.0, [normalize, [similarity]]]]]
  text: [sum, [[product, 1.0, [normalize, [text_match]]]]]
  recent:
    - sum
    - - [product, 1.0, [decay, [age_hours, occurred_at], {midpoint: 24, exponent: 1}]]
"""


def catalog_files(
    *,
    collections: list[dict[str, Any]] | None = None,
    processors: list[dict[str, Any]] | None = None,
    package_collections: list[str] | None = None,
    package_processors: list[str] | None = None,
    version: str = "1.0.0",
) -> dict[str, str]:
    """Build a minimal, self-contained catalog upload."""

    collection_list = [
        *(collections or [copy.deepcopy(COLLECTION)]),
        copy.deepcopy(ARCHIVE),
    ]
    processor_list = processors or copy.deepcopy(PROCESSORS)
    return {
        "collections/notes.yaml": yaml.safe_dump({"collections": collection_list}),
        "conf/processors.yaml": yaml.safe_dump({"processors": processor_list}),
        "conf/rank_default.yaml": RANK_DEFAULT,
        "derivations/archive_notes.yaml": yaml.safe_dump(copy.deepcopy(MIGRATION)),
        "views/recent_notes.yaml": yaml.safe_dump({"views": [copy.deepcopy(VIEW)]}),
        "artifacts/note_digest.yaml": yaml.safe_dump({"artifacts": [copy.deepcopy(ARTIFACT)]}),
        "packages/evolving.yaml": yaml.safe_dump(
            {
                "packages": [
                    {
                        "name": "evolving",
                        "version": version,
                        "collections": package_collections
                        or [f"{item['name']}@{item['version']}" for item in collection_list],
                        # Derive processors are listed alongside per-record ones.
                        "processors": package_processors
                        or [*(item["name"] for item in processor_list), "archive_notes"],
                        "views": ["recent_notes@1"],
                        "artifacts": ["note_digest@1"],
                        "search_profiles": ["pg_default"],
                    }
                ]
            }
        ),
    }


async def publish(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    files: dict[str, str],
    *,
    package: str = PACKAGE,
    dry_run: bool = False,
) -> httpx.Response:
    return await client.post(
        "/catalog",
        headers=headers,
        params={"dry_run": "true"} if dry_run else None,
        json={"package": package, "files": files},
    )


async def enrich(
    settings: Settings, db_pool: DatabasePool, workspace: str, *, passes: int = 6
) -> None:
    """Drive real enrichment so ingested rows become ready and searchable."""

    from memseek.worker import WorkerRuntime, run_worker_once
    from memseek.workspace_catalog import WorkspaceCatalogRegistry

    catalog = load_definition_catalog(settings)
    runtime = WorkerRuntime(
        settings=settings,
        catalog=catalog,
        pool=db_pool,
        catalog_registry=WorkspaceCatalogRegistry(db_pool, settings, catalog),
    )
    for _ in range(passes):
        if not (await run_worker_once(runtime, worker_id="evolution-test")).busy:
            return


def build_app(settings: Settings) -> Any:
    return create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )


async def ingest(
    client: httpx.AsyncClient, headers: dict[str, str], **content: Any
) -> httpx.Response:
    record: dict[str, Any] = {
        "collection": "notes",
        "entity": "user:ana",
        "type": "note",
        "text": content.pop("text", "a note"),
    }
    if content:
        record["content"] = content
    return await client.post("/records", headers=headers, json={"records": [record]})
