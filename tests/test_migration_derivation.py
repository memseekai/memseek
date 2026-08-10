"""Moving a corpus to a new contract by copying it forward with lineage.

Records are immutable, so a migration emits rather than edits.  The mapping Task
is deterministic, which is the part worth testing directly; that it runs inside an
ordinary derivation is what gives it provenance, bounds, and review for free.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from evolution_catalog import build_app, catalog_files, enrich, ingest, publish

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.derive.tasks import task_adapter
from memseek.derive.tasks_migrate import MapRecordsConfig, MapRecordsInput, map_records
from memseek.models import WorkspaceCredential


@pytest.fixture
async def workspace(db_pool: DatabasePool) -> WorkspaceCredential:
    return await create_workspace(db_pool, "migration")


def _input(**content: Any) -> MapRecordsInput:
    return MapRecordsInput.model_validate(
        {"records": [{"id": str(uuid4()), "key": None, "content": {"text": "t", **content}}]}
    )


async def test_mapping_keeps_copies_defaults_and_casts() -> None:
    config = MapRecordsConfig.model_validate(
        {
            "keep": ["text"],
            "set": {
                "channel": {"from": "content.source", "default": "note"},
                "origin": {"value": "notes@1"},
                "weight": {"from": "content.weight_text", "cast": "number"},
            },
        }
    )
    value = _input(source="email", weight_text="2.5")
    result = await map_records(_context(), value, config)
    drafts = result.value
    assert len(drafts) == 1
    assert drafts[0].content == {
        "text": "t",
        "channel": "email",
        "origin": "notes@1",
        "weight": 2.5,
    }
    # Every emitted record cites the row it came from, which becomes derived_from.
    assert drafts[0].citations == (value.records[0].id,)


async def test_a_missing_path_falls_back_to_its_default_or_is_omitted() -> None:
    config = MapRecordsConfig.model_validate(
        {
            "keep": ["text"],
            "set": {
                "channel": {"from": "content.source", "default": "note"},
                "absent": {"from": "content.nothing"},
            },
        }
    )
    result = await map_records(_context(), _input(), config)
    assert result.value[0].content == {"text": "t", "channel": "note"}


async def test_mapping_reads_annotations_and_scores() -> None:
    config = MapRecordsConfig.model_validate(
        {"keep": ["text"], "set": {"tone": {"from": "annotations.tone_v1.label"}}}
    )
    value = MapRecordsInput.model_validate(
        {
            "records": [
                {
                    "id": str(uuid4()),
                    "content": {"text": "t"},
                    "annotations": {"tone_v1": {"label": "warm"}},
                }
            ]
        }
    )
    result = await map_records(_context(), value, config)
    assert result.value[0].content == {"text": "t", "tone": "warm"}


async def test_mapping_carries_a_keyed_slot_forward_when_asked() -> None:
    value = MapRecordsInput.model_validate(
        {"records": [{"id": str(uuid4()), "key": "goals", "content": {"text": "t"}}]}
    )
    carried = await map_records(
        _context(), value, MapRecordsConfig.model_validate({"keep": ["text"]})
    )
    assert carried.value[0].key == "goals"

    dropped = await map_records(
        _context(), value, MapRecordsConfig.model_validate({"keep": ["text"], "carry_key": False})
    )
    assert dropped.value[0].key is None


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"keep": [], "set": {}}, "keep or set must produce it"),
        ({"keep": ["text"], "set": {"text": {"value": "x"}}}, "cannot both produce"),
        ({"keep": [], "drop": ["text"], "set": {"text": {"value": "x"}}}, "text cannot be dropped"),
        (
            {"keep": ["text", "a"], "drop": ["a"]},
            "drop cannot name mapped properties",
        ),
        (
            {"keep": ["text"], "set": {"x": {"from": "content.a", "value": "b"}}},
            "exactly one of from or value",
        ),
        (
            {"keep": ["text"], "set": {"x": {"value": "b", "default": "c"}}},
            "cannot also declare a default",
        ),
        (
            {"keep": ["text"], "set": {"x": {"from": "elsewhere.a"}}},
            "rooted at content, annotations, or scores",
        ),
    ],
)
def test_incoherent_mappings_are_refused(config: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        MapRecordsConfig.model_validate(config)


async def test_a_mapping_that_produces_no_text_fails_loudly() -> None:
    config = MapRecordsConfig.model_validate({"keep": ["text"]})
    value = MapRecordsInput.model_validate(
        {"records": [{"id": str(uuid4()), "content": {"other": "no text here"}}]}
    )
    with pytest.raises(ValueError, match="produced no text"):
        await map_records(_context(), value, config)


async def test_an_uncastable_value_fails_rather_than_guessing() -> None:
    config = MapRecordsConfig.model_validate(
        {"keep": ["text"], "set": {"n": {"from": "content.raw", "cast": "integer"}}}
    )
    with pytest.raises(ValueError, match="cannot cast"):
        await map_records(_context(), _input(raw="not a number"), config)


def test_the_task_is_registered_with_a_stable_implementation_hash() -> None:
    adapter = task_adapter("map_records")
    assert adapter.name == "map_records"
    assert len(adapter.implementation_hash) == 64


async def test_a_migration_derivation_copies_a_corpus_forward_with_provenance(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """End to end: run the fixture's migration and check the emitted lineage."""

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, catalog_files())).status_code == 200
            for index in range(3):
                assert (
                    await ingest(client, headers, text=f"note {index}", channel="email")
                ).status_code == 200
            await enrich(settings, db_pool, workspace.workspace)

            queued = await client.post(
                "/processors/archive_notes/run",
                headers=headers,
                json={"entity": "user:ana"},
            )
            assert queued.status_code == 200, queued.text
    await enrich(settings, db_pool, workspace.workspace)

    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select id, content, derived_from, depth
            from record
            where workspace = %s and collection = 'archive'
            order by seq
            """,
            (workspace.workspace,),
        )
        migrated = list(await result.fetchall())
        sources = await conn.execute(
            "select id from record where workspace = %s and collection = 'notes' order by seq",
            (workspace.workspace,),
        )
        source_ids = {row["id"] for row in await sources.fetchall()}

    assert len(migrated) == 3
    for row in migrated:
        # The mapping carried text and channel over and stamped the origin.
        assert row["content"]["origin"] == "notes@1"
        assert row["content"]["channel"] == "email"
        assert row["content"]["text"].startswith("note ")
        # Lineage, not a copy: each migrated row cites the run that produced it and
        # exactly one original, so the migration is auditable and erasable.
        cited_sources = set(row["derived_from"]) & source_ids
        assert len(cited_sources) == 1
        assert len(row["derived_from"]) == 2
        assert int(row["depth"]) == 1

    # The originals are untouched, because a migration emits rather than edits.
    async with db_pool.connection() as conn:
        remaining = await conn.execute(
            "select count(*) as rows from record where workspace = %s and collection = 'notes'",
            (workspace.workspace,),
        )
        row = await remaining.fetchone()
    assert row is not None
    assert int(row["rows"]) == 3


def _context() -> Any:
    """map_records ignores its context; it is deterministic by construction."""

    return object()


async def test_a_snapshot_migration_refuses_a_corpus_larger_than_its_bound(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """A snapshot source reads its complete scope or refuses; it never truncates.

    That is the right guarantee for a reviewed correctness check, and the reason a
    whole-corpus migration uses a cursor-driven source instead.
    """

    import copy

    import yaml
    from evolution_catalog import MIGRATION

    bounded = copy.deepcopy(MIGRATION)
    bounded["sources"]["legacy"]["max_records"] = 2
    bounded["emit"]["max_records"] = 2
    files = catalog_files()
    files["derivations/archive_notes.yaml"] = yaml.safe_dump(bounded)

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, files)).status_code == 200
            for index in range(5):
                assert (await ingest(client, headers, text=f"note {index}")).status_code == 200
            await enrich(settings, db_pool, workspace.workspace)
            assert (
                await client.post(
                    "/processors/archive_notes/run",
                    headers=headers,
                    json={"entity": "user:ana"},
                )
            ).status_code == 200
    await enrich(settings, db_pool, workspace.workspace)

    async with db_pool.connection() as conn:
        migrated = await conn.execute(
            "select count(*) as rows from record where workspace = %s and collection = 'archive'",
            (workspace.workspace,),
        )
        row = await migrated.fetchone()
        failed = await conn.execute(
            """
            select last_error_kind from job
            where workspace = %s and kind = 'derive' and last_error_kind is not null
            """,
            (workspace.workspace,),
        )
        kinds = {str(item["last_error_kind"]) for item in await failed.fetchall()}

    assert row is not None
    # Nothing was migrated, and nothing was half-migrated either.
    assert int(row["rows"]) == 0
    assert kinds == {"budget"}


async def test_a_cursor_driven_migration_walks_a_whole_corpus_exactly_once(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """The whole-corpus shape: a changes source drains itself, without duplicates."""

    import copy

    import yaml
    from evolution_catalog import MIGRATION

    walking = copy.deepcopy(MIGRATION)
    walking["sources"]["legacy"]["kind"] = "changes"
    # Deliberately smaller than the corpus: the cursor is what covers the rest.
    walking["sources"]["legacy"]["max_records"] = 2
    walking["emit"]["max_records"] = 2
    files = catalog_files()
    files["derivations/archive_notes.yaml"] = yaml.safe_dump(walking)

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await publish(client, headers, files)).status_code == 200
            for index in range(7):
                assert (await ingest(client, headers, text=f"note {index}")).status_code == 200
            await enrich(settings, db_pool, workspace.workspace)
            # One request is enough: the lane queues its own successors until the
            # cursor is drained.
            assert (
                await client.post(
                    "/processors/archive_notes/run",
                    headers=headers,
                    json={"entity": "user:ana"},
                )
            ).status_code == 200
    await enrich(settings, db_pool, workspace.workspace, passes=40)

    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select content ->> 'text' as text, count(*) as copies
            from record
            where workspace = %s and collection = 'archive'
            group by 1
            """,
            (workspace.workspace,),
        )
        rows = {str(item["text"]): int(item["copies"]) for item in await result.fetchall()}

    # Every original migrated, and each exactly once.
    assert len(rows) == 7
    assert set(rows.values()) == {1}

    # Running it again migrates nothing: the cursor is drained.
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/processors/archive_notes/run", headers=headers, json={"entity": "user:ana"}
            )
    await enrich(settings, db_pool, workspace.workspace, passes=20)
    async with db_pool.connection() as conn:
        again = await conn.execute(
            "select count(*) as rows from record where workspace = %s and collection = 'archive'",
            (workspace.workspace,),
        )
        row = await again.fetchone()
    assert row is not None
    assert int(row["rows"]) == 7
