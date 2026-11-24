"""Focused tests for the shared canonical record insertion boundary."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from memseek.auth import create_workspace
from memseek.canonical_records import (
    CanonicalRecordInvariantError,
    CanonicalRecordWrite,
    insert_canonical_record_tx,
    insert_canonical_records_tx,
)
from memseek.config import Settings
from memseek.db import DatabaseConnection, DatabasePool


class _Result:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    async def fetchone(self) -> dict[str, Any] | None:
        return self.row


class _Connection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.queries: list[str] = []

    async def execute(self, query: str, _params: object = None) -> _Result:
        self.queries.append(query)
        return _Result(self.row)


def _settings(**changes: Any) -> Settings:
    return Settings(llm_fake=True).model_copy(update=changes)


def _write(record_id: UUID | None = None) -> CanonicalRecordWrite:
    return CanonicalRecordWrite(
        id=record_id or uuid4(),
        workspace="workspace",
        collection="main",
        collection_version=1,
        collection_hash="0" * 64,
        entity="entity",
        type="event",
        content={"text": "canonical"},
    )


async def test_canonical_writer_uses_one_full_insert_and_checks_returned_identity() -> None:
    write = replace(_write(), ready=True)
    connection = _Connection({"id": write.id, "seq": 7, "enriched_at": datetime.now(UTC)})
    inserted = await insert_canonical_record_tx(
        cast(DatabaseConnection, connection),
        write,
        _settings(),
    )
    assert inserted is not None
    assert (inserted.id, inserted.seq, inserted.ready) == (write.id, 7, True)
    assert len(connection.queries) == 1
    assert connection.queries[0].lower().count("insert into record") == 1


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"collection": "_private"}, "reserved_collection"),
        ({"type": "run"}, "reserved_type"),
        ({"content": {"text": "bad", "value": math.nan}}, "finite_json"),
        ({"derived_from": (UUID(int=1), UUID(int=1))}, "duplicate_parent"),
        ({"depth": 5}, "depth_limit"),
    ],
)
async def test_canonical_writer_rejects_shared_invariants_before_sql(
    changes: dict[str, Any], code: str
) -> None:
    connection = _Connection(None)
    with pytest.raises(CanonicalRecordInvariantError) as raised:
        await insert_canonical_record_tx(
            cast(DatabaseConnection, connection),
            replace(_write(), **changes),
            _settings(max_derivation_depth=4),
        )
    assert raised.value.code == code
    assert connection.queries == []


@pytest.mark.parametrize(
    ("write", "bounded_settings", "code"),
    [
        (_write(), _settings(max_content_bytes=1), "content_too_large"),
        (
            replace(
                _write(),
                collection="_system",
                type="run",
                content={"text": "run"},
            ),
            _settings(max_run_content_bytes=1),
            "run_too_large",
        ),
    ],
)
async def test_canonical_writer_applies_type_specific_content_bounds(
    write: CanonicalRecordWrite, bounded_settings: Settings, code: str
) -> None:
    with pytest.raises(CanonicalRecordInvariantError) as raised:
        await insert_canonical_record_tx(
            cast(DatabaseConnection, _Connection(None)),
            write,
            bounded_settings,
        )
    assert raised.value.code == code


@pytest.mark.parametrize("field", ["annotations", "annotation_meta", "enrichment_meta"])
async def test_canonical_writer_bounds_each_annotation_or_metadata_entry(field: str) -> None:
    with pytest.raises(CanonicalRecordInvariantError) as raised:
        await insert_canonical_record_tx(
            cast(DatabaseConnection, _Connection(None)),
            replace(_write(), **{field: {"processor": "12345678901"}}),
            _settings(max_annotation_bytes=12),
        )
    assert raised.value.code == "annotation_too_large"


async def test_canonical_writer_does_not_apply_per_entry_limit_to_aggregate_map() -> None:
    write = replace(
        _write(),
        annotations={"first": "1234567890", "second": "1234567890"},
    )
    connection = _Connection({"id": write.id, "seq": 1, "enriched_at": None})
    inserted = await insert_canonical_record_tx(
        cast(DatabaseConnection, connection),
        write,
        _settings(max_annotation_bytes=12),
    )
    assert inserted is not None


async def test_only_explicit_public_dedupe_may_return_no_inserted_row() -> None:
    connection = cast(DatabaseConnection, _Connection(None))
    with pytest.raises(CanonicalRecordInvariantError, match="returned no row") as raised:
        await insert_canonical_record_tx(connection, _write(), _settings())
    assert raised.value.code == "insert_return"

    duplicate = await insert_canonical_record_tx(
        connection,
        replace(_write(), dedupe_key="source:1"),
        _settings(),
        dedupe_conflict="return_none",
    )
    assert duplicate is None


async def test_canonical_writer_rejects_an_unexpected_returning_row() -> None:
    write = _write()
    connection = _Connection({"id": uuid4(), "seq": 1, "enriched_at": None})
    with pytest.raises(CanonicalRecordInvariantError) as raised:
        await insert_canonical_record_tx(
            cast(DatabaseConnection, connection),
            write,
            _settings(),
        )
    assert raised.value.code == "insert_return"


async def test_canonical_writer_rejects_missing_and_foreign_parents(
    db_pool: DatabasePool, settings: Settings
) -> None:
    await create_workspace(db_pool, "home")
    await create_workspace(db_pool, "away")
    async with db_pool.connection() as conn, conn.transaction():
        foreign = await insert_canonical_record_tx(
            conn, replace(_write(), workspace="away"), settings
        )
        local = await insert_canonical_record_tx(
            conn, replace(_write(), workspace="home"), settings
        )
    assert foreign is not None
    assert local is not None

    cases = (
        ((local.id, uuid4()), "parent_not_found"),
        ((local.id, foreign.id), "parent_workspace"),
    )
    for parents, code in cases:
        async with db_pool.connection() as conn, conn.transaction():
            with pytest.raises(CanonicalRecordInvariantError) as raised:
                await insert_canonical_record_tx(
                    conn,
                    replace(_write(), workspace="home", derived_from=parents),
                    settings,
                )
        assert raised.value.code == code

    async with db_pool.connection() as conn, conn.transaction():
        child = await insert_canonical_record_tx(
            conn, replace(_write(), workspace="home", derived_from=(local.id,)), settings
        )
    assert child is not None
    assert child.seq > local.seq


async def test_batched_writer_inserts_in_order_and_proves_each_rows_parents(
    db_pool: DatabasePool, settings: Settings
) -> None:
    await create_workspace(db_pool, "home")
    async with db_pool.connection() as conn, conn.transaction():
        parent = await insert_canonical_record_tx(
            conn, replace(_write(), workspace="home"), settings
        )
    assert parent is not None

    children = [replace(_write(), workspace="home", derived_from=(parent.id,)) for _ in range(3)]
    async with db_pool.connection() as conn, conn.transaction():
        inserted = await insert_canonical_records_tx(conn, children, settings)
    assert [item.id for item in inserted] == [child.id for child in children]
    assert [item.seq for item in inserted] == sorted(item.seq for item in inserted)
    assert inserted[0].seq > parent.seq

    orphan = replace(_write(), workspace="home", derived_from=(uuid4(),))
    async with db_pool.connection() as conn:
        with pytest.raises(CanonicalRecordInvariantError) as raised:
            async with conn.transaction():
                await insert_canonical_records_tx(
                    conn, [replace(_write(), workspace="home"), orphan], settings
                )
        count = await (
            await conn.execute("select count(*) as rows from record where workspace = 'home'")
        ).fetchone()
    assert raised.value.code == "parent_not_found"
    assert count is not None
    assert count["rows"] == 4
