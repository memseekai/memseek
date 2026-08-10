"""Runtime evaluation matrix for the extended trigger conditions.

Each case builds one real catalog from the shipped deployment assets plus
standalone trigger files, inserts canonical rows directly, and drives
``evaluate_entity_triggers_tx`` — the same transaction hook the ready
transition and successful runs call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb
from reference_catalog import materialize_reference_catalog

from memseek.config import Settings
from memseek.db import DatabaseConnection, DatabasePool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.triggers import evaluate_entity_triggers_tx

_STANDALONE_TRIGGERS = {
    "write_debounce.yaml": """name: profile.write_debounce
processor: profile
write:
  collections: [main]
  types: [observation]
  statuses: [active]
debounce_s: 60
""",
    "session_quiet.yaml": """name: profile.session_quiet
processor: profile
quiet:
  collections: [main]
  types: [chat]
  statuses: [active]
  after_s: 120
""",
    "calendar_at.yaml": """name: reflection.calendar_at
processor: reflection
at:
  collections: [calendar_events]
  statuses: [active]
  field: starts_at
  offset_s: -600
""",
    "head_changed.yaml": """name: contradiction.head_changed
processor: contradiction
changed:
  collections: [profiles]
  statuses: [active]
  transitions: [added, changed]
""",
    "head_retracted.yaml": """name: contradiction.head_retracted
processor: contradiction
retraction:
  collections: [profiles]
  statuses: [active]
""",
    "obs_census.yaml": """name: skill.obs_census
processor: skill
census:
  collections: [main]
  types: [observation]
  statuses: [active]
  threshold: 3
""",
    "first_seen.yaml": """name: reflection.first_seen
processor: reflection
lifecycle:
  first_record: true
""",
    "max_importance.yaml": """name: reflection.max_importance
processor: reflection
accumulator:
  metric: {scorer: importance, aggregate: max}
  threshold: 5
""",
}


@pytest.fixture(scope="module")
def trigger_catalog(tmp_path_factory: pytest.TempPathFactory) -> DefinitionCatalog:
    root = tmp_path_factory.mktemp("trigger-conditions") / "catalog"
    root.mkdir()
    materialize_reference_catalog(root)
    for name, document in _STANDALONE_TRIGGERS.items():
        (root / "triggers" / name).write_text(document, encoding="utf-8")
    return load_definition_catalog(
        Settings(
            models_file=root / "conf/models.yaml",
            processors_file=root / "conf/processors.yaml",
            rank_default_file=root / "conf/rank_default.yaml",
            search_profiles_file=root / "conf/search_profiles.yaml",
            collections_dir=root / "collections",
            derivations_dir=root / "derivations",
            triggers_dir=root / "triggers",
            views_dir=root / "views",
            artifacts_dir=root / "artifacts",
            mcp_dir=root / "mcp",
            packages_dir=root / "packages",
            llm_fake=True,
        )
    )


async def _create_workspace(conn: DatabaseConnection, workspace: str) -> None:
    await conn.execute(
        "insert into workspace (id, api_key_hash) values (%s, %s)",
        (workspace, "0" * 64),
    )


async def _insert_record(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    collection: str,
    type_: str,
    content: dict[str, Any],
    key: str | None = None,
    scores: dict[str, Any] | None = None,
    ready: bool = True,
) -> int:
    result = await conn.execute(
        """
        insert into record (workspace, collection, collection_version, collection_hash,
                            entity, key, type, status, content, scores, enriched_at)
        values (%s, %s, 1, %s, %s, %s, %s, 'active', %s, %s,
                case when %s then clock_timestamp() end)
        returning seq
        """,
        (
            workspace,
            collection,
            "a" * 64,
            entity,
            key,
            type_,
            Jsonb(content),
            Jsonb(scores or {}),
            ready,
        ),
    )
    row = await result.fetchone()
    assert row is not None
    return int(row["seq"])


async def _record_run(
    conn: DatabaseConnection,
    *,
    workspace: str,
    entity: str,
    processor: str,
    high_seq: int,
    completed_at: datetime | None = None,
) -> None:
    stamp = (completed_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    await _insert_record(
        conn,
        workspace=workspace,
        entity=entity,
        collection="_system",
        type_="run",
        content={
            "text": "run",
            "operation": "derive",
            "processor": processor,
            "status": "ok",
            "high_seq": high_seq,
            "completed_at": stamp,
        },
    )


async def _active_job(
    conn: DatabaseConnection,
    *,
    workspace: str,
    derivation: str,
    entity: str,
) -> Any:
    result = await conn.execute(
        """
        select payload, run_after from job
        where workspace = %s and kind = 'derive' and derivation = %s and entity = %s
          and done_at is null and dead_at is null
        """,
        (workspace, derivation, entity),
    )
    return await result.fetchone()


async def _clear_jobs(conn: DatabaseConnection, workspace: str) -> None:
    await conn.execute("delete from job where workspace = %s", (workspace,))


async def _now(conn: DatabaseConnection) -> datetime:
    result = await conn.execute("select clock_timestamp() as now")
    row = await result.fetchone()
    assert row is not None
    return row["now"].astimezone(UTC)


async def test_write_debounce_extends_the_mailbox_deadline(
    db_pool: DatabasePool, trigger_catalog: DefinitionCatalog
) -> None:
    async with db_pool.connection() as conn, conn.transaction():
        await _create_workspace(conn, "ws")
        entity = "user:debounce"
        await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="main",
            type_="observation",
            content={"text": "first"},
        )
        before = await _now(conn)
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="profile", entity=entity)
        assert job is not None
        assert job["payload"].get("trigger:profile.write_debounce:write") is True
        first_deadline = job["run_after"]
        assert first_deadline >= before + timedelta(seconds=55)

        await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="main",
            type_="observation",
            content={"text": "second"},
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="profile", entity=entity)
        assert job is not None
        assert job["run_after"] >= first_deadline


async def test_quiet_trigger_waits_for_arrivals_to_settle(
    db_pool: DatabasePool, trigger_catalog: DefinitionCatalog
) -> None:
    async with db_pool.connection() as conn, conn.transaction():
        await _create_workspace(conn, "ws")
        entity = "user:quiet"
        await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="main",
            type_="chat",
            content={"text": "hello"},
        )
        before = await _now(conn)
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="profile", entity=entity)
        assert job is not None
        assert job["payload"] == {"trigger:profile.session_quiet:quiet": True}
        first_deadline = job["run_after"]
        assert first_deadline >= before + timedelta(seconds=115)

        await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="main",
            type_="chat",
            content={"text": "one more"},
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="profile", entity=entity)
        assert job is not None
        assert job["run_after"] >= first_deadline


async def test_at_trigger_schedules_and_retires_record_deadlines(
    db_pool: DatabasePool, trigger_catalog: DefinitionCatalog
) -> None:
    async with db_pool.connection() as conn, conn.transaction():
        await _create_workspace(conn, "ws")
        entity = "user:deadline"
        starts_at = datetime.now(UTC) + timedelta(hours=1)
        iso = starts_at.isoformat().replace("+00:00", "Z")
        await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="calendar_events",
            type_="event",
            content={
                "text": "standup",
                "title": "standup",
                "starts_at": iso,
                "ends_at": iso,
            },
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="reflection", entity=entity)
        assert job is not None
        assert job["payload"] == {"trigger:reflection.calendar_at:at": True}
        expected = starts_at - timedelta(seconds=600)
        assert abs((job["run_after"] - expected).total_seconds()) < 5

        # A successful run after the deadline retires it.
        await _clear_jobs(conn, "ws")
        await _record_run(
            conn,
            workspace="ws",
            entity=entity,
            processor="reflection",
            high_seq=1_000_000,
            completed_at=starts_at + timedelta(hours=1),
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        assert (
            await _active_job(conn, workspace="ws", derivation="reflection", entity=entity)
        ) is None


async def test_changed_trigger_ignores_noop_rewrites(
    db_pool: DatabasePool, trigger_catalog: DefinitionCatalog
) -> None:
    async with db_pool.connection() as conn, conn.transaction():
        await _create_workspace(conn, "ws")
        entity = "user:changed"
        first = await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="profiles",
            type_="fact",
            key="role",
            content={"text": "engineer"},
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="contradiction", entity=entity)
        assert job is not None
        assert job["payload"].get("trigger:contradiction.head_changed:changed") is True

        # A byte-identical rewrite above the watermark is not a change.
        await _clear_jobs(conn, "ws")
        rewrite = await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="profiles",
            type_="fact",
            key="role",
            content={"text": "engineer"},
        )
        await _record_run(
            conn, workspace="ws", entity=entity, processor="contradiction", high_seq=first
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="contradiction", entity=entity)
        payload = job["payload"] if job is not None else {}
        assert "trigger:contradiction.head_changed:changed" not in payload

        # A real value change fires again.
        await _clear_jobs(conn, "ws")
        await _record_run(
            conn, workspace="ws", entity=entity, processor="contradiction", high_seq=rewrite
        )
        await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="profiles",
            type_="fact",
            key="role",
            content={"text": "manager"},
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="contradiction", entity=entity)
        assert job is not None
        assert job["payload"].get("trigger:contradiction.head_changed:changed") is True


async def test_retraction_trigger_fires_on_tombstones_only(
    db_pool: DatabasePool, trigger_catalog: DefinitionCatalog
) -> None:
    async with db_pool.connection() as conn, conn.transaction():
        await _create_workspace(conn, "ws")
        entity = "user:retract"
        await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="profiles",
            type_="fact",
            key="role",
            content={"text": "engineer"},
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="contradiction", entity=entity)
        assert job is not None
        assert "trigger:contradiction.head_retracted:retraction" not in job["payload"]

        await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="profiles",
            type_="fact",
            key="role",
            content={"text": "retracted", "tombstone": True},
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="contradiction", entity=entity)
        assert job is not None
        assert job["payload"].get("trigger:contradiction.head_retracted:retraction") is True


async def test_census_trigger_requires_floor_and_fresh_driver_data(
    db_pool: DatabasePool, trigger_catalog: DefinitionCatalog
) -> None:
    async with db_pool.connection() as conn, conn.transaction():
        await _create_workspace(conn, "ws")
        entity = "user:census"
        for index in range(2):
            await _insert_record(
                conn,
                workspace="ws",
                entity=entity,
                collection="main",
                type_="observation",
                content={"text": f"obs {index}"},
            )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="skill", entity=entity)
        payload = job["payload"] if job is not None else {}
        assert "trigger:skill.obs_census:census" not in payload

        await _clear_jobs(conn, "ws")
        last = await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="main",
            type_="observation",
            content={"text": "obs 2"},
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="skill", entity=entity)
        assert job is not None
        assert job["payload"].get("trigger:skill.obs_census:census") is True

        # Census alone cannot refire after its data is consumed.
        await _clear_jobs(conn, "ws")
        await _record_run(conn, workspace="ws", entity=entity, processor="skill", high_seq=last)
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="skill", entity=entity)
        payload = job["payload"] if job is not None else {}
        assert "trigger:skill.obs_census:census" not in payload


async def test_lifecycle_first_record_fires_once(
    db_pool: DatabasePool, trigger_catalog: DefinitionCatalog
) -> None:
    async with db_pool.connection() as conn, conn.transaction():
        await _create_workspace(conn, "ws")
        entity = "user:new"
        seq = await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="main",
            type_="observation",
            content={"text": "first ever"},
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="reflection", entity=entity)
        assert job is not None
        assert job["payload"].get("trigger:reflection.first_seen:lifecycle") is True

        await _clear_jobs(conn, "ws")
        await _record_run(conn, workspace="ws", entity=entity, processor="reflection", high_seq=seq)
        await _insert_record(
            conn,
            workspace="ws",
            entity=entity,
            collection="main",
            type_="observation",
            content={"text": "second"},
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=entity, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="reflection", entity=entity)
        payload = job["payload"] if job is not None else {}
        assert "trigger:reflection.first_seen:lifecycle" not in payload


async def test_accumulator_max_aggregate_fires_on_single_spike(
    db_pool: DatabasePool, trigger_catalog: DefinitionCatalog
) -> None:
    async with db_pool.connection() as conn, conn.transaction():
        await _create_workspace(conn, "ws")
        spiky = "user:spike"
        await _insert_record(
            conn,
            workspace="ws",
            entity=spiky,
            collection="main",
            type_="observation",
            content={"text": "urgent"},
            scores={"importance": 7},
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=spiky, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="reflection", entity=spiky)
        assert job is not None
        assert job["payload"].get("trigger:reflection.max_importance:threshold") is True

        calm = "user:calm"
        await _insert_record(
            conn,
            workspace="ws",
            entity=calm,
            collection="main",
            type_="observation",
            content={"text": "routine"},
            scores={"importance": 3},
        )
        await evaluate_entity_triggers_tx(
            conn, workspace="ws", entity=calm, catalog=trigger_catalog
        )
        job = await _active_job(conn, workspace="ws", derivation="reflection", entity=calm)
        payload = job["payload"] if job is not None else {}
        assert "trigger:reflection.max_importance:threshold" not in payload
