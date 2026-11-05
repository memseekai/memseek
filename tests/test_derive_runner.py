"""End-to-end derive runner behavior: locking, failure dispositions, and receipts."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

import memseek.derive.runner as runner_module
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.derive.errors import DerivationError
from memseek.derive.runner import enqueue_derivation_job, process_derivation_job
from memseek.jobs import claim_job
from memseek.llm.fake import fake
from memseek.llm.registry import Completion
from memseek.models import ClaimedJob
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records

_NOTES = """collections:
  - name: notes
    version: 1
    active: true
    mode: keyed
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
      additionalProperties: true
    search_profile: pg_default
"""

_DIGEST_OUTPUT = {
    "type": "object",
    "required": ["records"],
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "citations", "content"],
                "properties": {
                    "text": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                    "content": {"type": "object"},
                },
            },
        }
    },
}


def _digest(**overrides: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "name": "note_digest",
        "trigger": {"write": {"collections": ["notes"]}},
        "sources": {
            "new_notes": {
                "kind": "changes",
                "collections": ["notes"],
                "max_records": 5,
                "max_tokens": 2000,
            }
        },
        "model": "cheap",
        "limits": {
            "max_llm_calls": 2,
            "max_retrieved_records": 0,
            "max_visible_records": 5,
            "max_wall_s": 30,
        },
        "tasks": [
            {
                "id": "digest",
                "use": "llm",
                "with": {
                    "output_schema": _DIGEST_OUTPUT,
                    "prompt": "Digest:\n{{new_notes.rendered}}",
                },
            }
        ],
        "emit": {"from": "{{digest.records}}", "collection": "atoms", "type": "atom"},
    }
    definition.update(overrides)
    return definition


def _write_catalog(root: Path, gbrain: Path, digest: dict[str, Any]) -> None:
    shutil.copytree(gbrain, root)
    (root / "collections/notes.yaml").write_text(_NOTES, encoding="utf-8")
    (root / "derivations/note_digest.yaml").write_text(yaml.safe_dump(digest), encoding="utf-8")
    manifest = yaml.safe_load((root / "catalog.yaml").read_text(encoding="utf-8"))
    manifest["collections"]["notes@1"] = "collections/notes.yaml"
    manifest["derivations"]["note_digest"] = "derivations/note_digest.yaml"
    (root / "catalog.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


@pytest.fixture
def digest_settings(gbrain_settings: Settings, tmp_path: Path) -> Settings:
    assert gbrain_settings.catalog_file is not None
    root = tmp_path / "digest_catalog"
    _write_catalog(root, gbrain_settings.catalog_file.parent, _digest())
    return gbrain_settings.model_copy(update={"catalog_file": root / "catalog.yaml"})


async def _write_note(
    pool: DatabasePool,
    catalog: DefinitionCatalog,
    settings: Settings,
    *,
    workspace: str,
    key: str,
    text: str,
) -> UUID:
    result = await insert_public_records(
        pool,
        workspace=workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="maya", collection="notes", key=key, type="note", text=text
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    assert result.inserted[0].ready is True
    return result.inserted[0].id


async def _claim(pool: DatabasePool, settings: Settings) -> ClaimedJob:
    claimed = await claim_job(
        pool,
        worker_id="digest-worker",
        kinds=("derive",),
        derivations=("note_digest",),
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    assert claimed is not None
    return claimed


def _atom(text: str, citation: UUID) -> Completion:
    return Completion(
        json.dumps(
            {
                "records": [
                    {
                        "text": text,
                        "citations": [str(citation)],
                        "content": {"kind": "fact", "confidence": 0.9},
                    }
                ]
            }
        )
    )


async def _waiting_backends(pool: DatabasePool) -> int:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "select count(*) as waiting from pg_stat_activity"
                " where wait_event_type = 'Lock' and datname = current_database()"
            )
        ).fetchone()
    assert row is not None
    return int(row["waiting"])


async def test_concurrent_keyed_write_while_a_run_commits_does_not_deadlock(
    digest_settings: Settings,
    db_pool: DatabasePool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hold the commit between its first and second lock, then race a keyed write.

    The keyed write takes the workspace lock, the entity lock, and then the
    active derive job row (its trigger coalesces into the running job). A
    commit that locks the job row before the entity lock waits on the writer
    while the writer waits on it, so PostgreSQL aborts one side.
    """

    settings = digest_settings
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "derive-lock-order")
    first = await _write_note(
        db_pool, catalog, settings, workspace=credential.workspace, key="a", text="Maya likes tea."
    )
    claimed = await _claim(db_pool, settings)
    fake.reset()
    fake.enqueue(_atom("Maya likes tea.", first))

    first_lock_held = asyncio.Event()
    release = asyncio.Event()
    acquired: list[str] = []
    real_claim = runner_module._claim_owned_tx
    real_entity = runner_module.acquire_entity_locks

    async def gate(name: str) -> None:
        acquired.append(name)
        if len(acquired) == 1:
            first_lock_held.set()
            await release.wait()

    async def claim_then_gate(conn: Any, job: ClaimedJob) -> None:
        await real_claim(conn, job)
        await gate("job")

    async def entity_then_gate(conn: Any, workspace: str, entities: Any) -> tuple[int, ...]:
        keys = await real_entity(conn, workspace, entities)
        await gate("entity")
        return keys

    monkeypatch.setattr(runner_module, "_claim_owned_tx", claim_then_gate)
    monkeypatch.setattr(runner_module, "acquire_entity_locks", entity_then_gate)

    run = asyncio.create_task(
        process_derivation_job(db_pool, claimed=claimed, settings=settings, catalog=catalog)
    )
    await asyncio.wait_for(first_lock_held.wait(), timeout=10)
    write = asyncio.create_task(
        _write_note(
            db_pool,
            catalog,
            settings,
            workspace=credential.workspace,
            key="b",
            text="Maya moved to Lisbon.",
        )
    )
    for _ in range(100):
        if await _waiting_backends(db_pool):
            break
        await asyncio.sleep(0.05)
    assert await _waiting_backends(db_pool) == 1
    release.set()

    result, _ = await asyncio.wait_for(asyncio.gather(run, write), timeout=30)

    assert acquired[:2] == ["entity", "job"]
    assert (result.disposition, result.output_count) == ("done", 1)
    async with db_pool.connection() as conn:
        rows = await (
            await conn.execute(
                "select done_at is not null as done, payload from job"
                " where workspace = %s and kind = 'derive' order by created_at",
                (credential.workspace,),
            )
        ).fetchall()
    assert [row["done"] for row in rows] == [True, False]


async def test_failed_run_receipt_does_not_fold_trigger_reasons_into_the_retry(
    digest_settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
) -> None:
    """A note written before the trigger existed is run manually; the retry stays manual."""

    assert digest_settings.catalog_file is not None
    untriggered_root = tmp_path / "untriggered_catalog"
    _write_catalog(
        untriggered_root,
        digest_settings.catalog_file.parent,
        _digest(trigger=None),
    )
    untriggered = load_definition_catalog(
        digest_settings.model_copy(update={"catalog_file": untriggered_root / "catalog.yaml"})
    )
    catalog = load_definition_catalog(digest_settings)
    credential = await create_workspace(db_pool, "derive-failed-receipt")
    await _write_note(
        db_pool,
        untriggered,
        digest_settings,
        workspace=credential.workspace,
        key="a",
        text="Maya likes tea.",
    )
    job_id, _, _ = await enqueue_derivation_job(
        db_pool, workspace=credential.workspace, derivation="note_digest", entity="maya"
    )
    claimed = await _claim(db_pool, digest_settings)
    fake.reset()
    fake.enqueue("not json", "still not json")

    with pytest.raises(DerivationError) as failure:
        await process_derivation_job(
            db_pool, claimed=claimed, settings=digest_settings, catalog=catalog
        )

    assert failure.value.kind == "parse"
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select payload, dead_at is null as retrying from job where id = %s", (job_id,)
            )
        ).fetchone()
    assert row is not None
    assert (row["payload"], row["retrying"]) == ({"manual": True}, True)


async def _job_state(pool: DatabasePool, job_id: UUID) -> tuple[bool, str | None]:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "select dead_at is not null as dead, last_error_kind from job where id = %s",
                (job_id,),
            )
        ).fetchone()
    assert row is not None
    return bool(row["dead"]), row["last_error_kind"]


async def test_run_over_its_wall_clock_is_retried_not_dead_lettered(
    gbrain_settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert gbrain_settings.catalog_file is not None
    root = tmp_path / "slow_catalog"
    digest = _digest()
    digest["limits"]["max_wall_s"] = 1
    _write_catalog(root, gbrain_settings.catalog_file.parent, digest)
    settings = gbrain_settings.model_copy(update={"catalog_file": root / "catalog.yaml"})
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "derive-wall-clock")
    await _write_note(
        db_pool, catalog, settings, workspace=credential.workspace, key="a", text="Maya likes tea."
    )
    claimed = await _claim(db_pool, settings)

    async def stalled_provider(*_: Any, **__: Any) -> Any:
        await asyncio.sleep(30)

    monkeypatch.setattr(runner_module, "complete", stalled_provider)

    with pytest.raises(DerivationError) as failure:
        await process_derivation_job(db_pool, claimed=claimed, settings=settings, catalog=catalog)

    assert failure.value.kind == "timeout"
    assert await _job_state(db_pool, claimed.id) == (False, "timeout")
