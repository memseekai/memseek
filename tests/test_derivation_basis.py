"""Evaluation Basis and Candidate Set contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from memseek.config import Settings
from memseek.definitions import load_definition_catalog
from memseek.derive import basis as basis_module
from memseek.derive.basis import (
    ChangesBasisAdapter,
    EvaluationBasis,
    ExpectedHead,
    source_contract_hash,
)
from memseek.derive.candidates import compile_candidate_set
from memseek.derive.emission import emission_effect, emission_status
from memseek.derive.errors import DerivationError
from memseek.derive.schema import CurrentSource, EmitDefinition, SnapshotWindow, StreamSource


def test_complete_keyed_emit_infers_replace() -> None:
    emit = EmitDefinition.model_validate(
        {
            "from": "{{result.records}}",
            "collection": "profiles",
            "type": "fact",
            "keys": ("role",),
            "complete": True,
            "review": "required",
        }
    )

    assert emission_effect(emit) == "replace"
    assert emission_status(emit) == "draft"


def test_complete_emit_requires_declared_keys() -> None:
    with pytest.raises(ValidationError, match="complete emission requires keys"):
        EmitDefinition.model_validate(
            {
                "from": "{{result.records}}",
                "collection": "main",
                "type": "event",
                "complete": True,
            }
        )


async def test_changes_cursor_rejects_source_scope_drift_without_author_transition_knob(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = load_definition_catalog(settings).derivations["profile"]
    predecessor = uuid4()
    monkeypatch.setattr(
        basis_module,
        "_watermark",
        AsyncMock(return_value=(12, predecessor, "0" * 64)),
    )

    with pytest.raises(DerivationError, match="source scope differs"):
        await ChangesBasisAdapter().resolve(
            AsyncMock(),
            workspace="test",
            entity="person:1",
            definition=definition,
        )


async def test_changes_cursor_continues_when_only_computation_changes(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = load_definition_catalog(settings).derivations["profile"]
    predecessor = uuid4()
    monkeypatch.setattr(
        basis_module,
        "_watermark",
        AsyncMock(return_value=(12, predecessor, source_contract_hash(definition))),
    )
    read_rows = AsyncMock(return_value=None)
    monkeypatch.setattr(basis_module, "_read_rows", read_rows)
    monkeypatch.setattr(basis_module, "_changes_input", AsyncMock(return_value=()))
    monkeypatch.setattr(basis_module, "_expected_heads", AsyncMock(return_value=()))

    receipt = await ChangesBasisAdapter().resolve(
        AsyncMock(),
        workspace="test",
        entity="person:1",
        definition=definition,
    )

    assert receipt is not None
    assert receipt.watermark == 12
    assert receipt.predecessor_source_hash == source_contract_hash(definition)
    read_rows.assert_not_awaited()


async def test_current_source_waits_for_latest_matching_record_to_be_ready() -> None:
    result = AsyncMock()
    result.fetchall.return_value = [{"enriched_at": None}]
    conn = AsyncMock()
    conn.execute.return_value = result
    source = CurrentSource.model_validate(
        {
            "kind": "current",
            "collections": ["profiles"],
            "collection_versions": {"profiles": [1]},
            "types": ["fact"],
            "keys": ["role"],
        }
    )

    selected = await basis_module._current_source_rows(
        conn,
        workspace="test",
        entity="person:1",
        source=source,
    )

    assert selected is None


def test_source_contract_hash_tracks_membership_not_order_or_computation(
    settings: Settings,
) -> None:
    definition = load_definition_catalog(settings).derivations["skill"]
    driver = definition.driver
    reordered_driver = driver.model_copy(
        update={
            "collections": tuple(reversed(driver.collections)),
            "types": tuple(reversed(driver.types)),
            "statuses": tuple(reversed(driver.statuses)),
            "collection_versions": dict(reversed(driver.collection_versions.items())),
        }
    )
    reordered = definition.model_copy(
        update={
            "sources": {
                **definition.sources,
                definition.driver_name: reordered_driver,
            }
        }
    )
    first_task = definition.tasks[0]
    changed_computation = definition.model_copy(
        update={
            "tasks": (
                first_task.model_copy(
                    update={
                        "config": {
                            **first_task.config,
                            "max_output_tokens": 1199,
                        }
                    }
                ),
                *definition.tasks[1:],
            )
        }
    )
    narrowed_driver = driver.model_copy(update={"types": driver.types[:-1]})
    narrowed = definition.model_copy(
        update={
            "sources": {
                **definition.sources,
                definition.driver_name: narrowed_driver,
            }
        }
    )

    expected = source_contract_hash(definition)
    assert source_contract_hash(reordered) == expected
    assert source_contract_hash(changed_computation) == expected
    assert source_contract_hash(narrowed) != expected


def test_candidate_set_supports_structured_keyed_content_and_divergence(
    settings: Settings,
) -> None:
    catalog = load_definition_catalog(settings)
    source_id = uuid4()
    active_id = uuid4()
    basis = EvaluationBasis(
        mode="changes",
        from_seq=10,
        through_seq=11,
        predecessor_run_id=None,
        predecessor_source_hash=None,
        input_rows=(),
        read_rows={},
        expected_heads=(
            ExpectedHead(
                collection="profiles",
                key="role",
                record_id=active_id,
                content={"text": "Engineer"},
            ),
        ),
    )
    emit = EmitDefinition.model_validate(
        {
            "from": "{{result.records}}",
            "collection": "profiles",
            "type": "fact",
            "keys": ("role",),
            "review": "required",
        }
    )

    candidate = compile_candidate_set(
        [
            {
                "key": "role",
                "content": {"text": "Engineering manager", "confidence": 0.9},
                "citations": [str(source_id)],
            }
        ],
        emit=emit,
        basis=basis,
        visible=frozenset({source_id}),
        settings=settings,
        catalog=catalog,
    )

    assert candidate.effect == "patch"
    assert candidate.coverage == "partial"
    assert candidate.records[0].content["confidence"] == 0.9
    assert candidate.divergence[0] == {
        "collection": "profiles",
        "key": "role",
        "change": "changed",
        "active_record_id": str(active_id),
        "candidate_record_id": str(candidate.records[0].id),
    }


def test_driver_key_emit_is_limited_to_the_captured_single_key(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    source_id = uuid4()
    active_id = uuid4()
    basis = EvaluationBasis(
        mode="changes",
        from_seq=10,
        through_seq=11,
        predecessor_run_id=None,
        predecessor_source_hash=None,
        input_rows=(),
        read_rows={},
        expected_heads=(
            ExpectedHead(
                collection="profiles",
                key="role",
                record_id=active_id,
                content={"text": "Engineer"},
            ),
        ),
    )
    emit = EmitDefinition.model_validate(
        {
            "from": "{{result.records}}",
            "collection": "profiles",
            "type": "fact",
            "driver_key": True,
            "max_records": 1,
        }
    )

    candidate = compile_candidate_set(
        [{"key": "role", "text": "Engineering manager", "citations": [str(source_id)]}],
        emit=emit,
        basis=basis,
        visible=frozenset({source_id}),
        settings=settings,
        catalog=catalog,
    )

    assert candidate.effect == "patch"
    assert candidate.records[0].key == "role"
    with pytest.raises(DerivationError, match="invalid or duplicate emission key"):
        compile_candidate_set(
            [{"key": "preferences", "text": "Incorrect key", "citations": [str(source_id)]}],
            emit=emit,
            basis=basis,
            visible=frozenset({source_id}),
            settings=settings,
            catalog=catalog,
        )


def test_bounded_dynamic_key_emit_tracks_existing_and_new_heads(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    source_id = uuid4()
    existing_id = uuid4()
    basis = EvaluationBasis(
        mode="changes",
        from_seq=10,
        through_seq=11,
        predecessor_run_id=None,
        predecessor_source_hash=None,
        input_rows=(),
        read_rows={},
        expected_heads=(
            ExpectedHead(
                collection="profiles",
                key="billing-api",
                record_id=existing_id,
                content={"text": "Old billing context"},
            ),
        ),
    )
    emit = EmitDefinition.model_validate(
        {
            "from": "{{result.records}}",
            "collection": "profiles",
            "type": "fact",
            "dynamic_keys": True,
            "max_active_keys": 2,
            "max_records": 2,
        }
    )

    candidate = compile_candidate_set(
        [
            {
                "key": "billing-api",
                "text": "Updated billing context",
                "citations": [str(source_id)],
            },
            {"key": "ui-guidelines", "text": "Use blue", "citations": [str(source_id)]},
        ],
        emit=emit,
        basis=basis,
        visible=frozenset({source_id}),
        settings=settings,
        catalog=catalog,
    )

    assert candidate.effect == "patch"
    assert [record.key for record in candidate.records] == ["billing-api", "ui-guidelines"]
    assert [item["change"] for item in candidate.divergence] == ["changed", "added"]
    assert [(head.key, head.record_id) for head in candidate.basis.expected_heads] == [
        ("billing-api", existing_id),
        ("ui-guidelines", None),
    ]

    with pytest.raises(DerivationError, match="max_active_keys"):
        compile_candidate_set(
            [
                {"key": "ui-guidelines", "text": "Use blue", "citations": [str(source_id)]},
                {"key": "ops-guidelines", "text": "Use green", "citations": [str(source_id)]},
            ],
            emit=emit,
            basis=basis,
            visible=frozenset({source_id}),
            settings=settings,
            catalog=catalog,
        )


def test_dynamic_key_emit_requires_a_bound() -> None:
    with pytest.raises(ValidationError, match="requires max_active_keys"):
        EmitDefinition.model_validate(
            {
                "from": "{{result.records}}",
                "collection": "profiles",
                "type": "fact",
                "dynamic_keys": True,
            }
        )


def test_complete_replacement_allows_uncited_explicit_absence(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    basis = EvaluationBasis(
        mode="corpus",
        from_seq=None,
        through_seq=0,
        predecessor_run_id=None,
        predecessor_source_hash=None,
        input_rows=(),
        read_rows={},
        expected_heads=(ExpectedHead(collection="profiles", key="role", record_id=None),),
    )
    emit = EmitDefinition.model_validate(
        {
            "from": "{{result.records}}",
            "collection": "profiles",
            "type": "fact",
            "keys": ("role",),
            "complete": True,
            "review": "required",
        }
    )

    candidate = compile_candidate_set(
        [{"key": "role", "retract": True, "citations": []}],
        emit=emit,
        basis=basis,
        visible=frozenset(),
        settings=settings,
        catalog=catalog,
    )

    assert candidate.coverage == "complete"
    assert candidate.records[0].citations == ()
    assert candidate.divergence[0]["change"] == "unchanged"


@pytest.mark.parametrize(
    ("draft", "message"),
    [
        (
            {"key": "role", "text": "Lead", "citations": [], "op": "replace"},
            "Extra inputs are not permitted",
        ),
        (
            {
                "key": "role",
                "content": {"text": "", "tombstone": True},
                "citations": [],
            },
            "reserved system fields",
        ),
    ],
)
def test_record_draft_rejects_transition_fields(
    settings: Settings,
    draft: dict[str, object],
    message: str,
) -> None:
    catalog = load_definition_catalog(settings)
    emit = EmitDefinition.model_validate(
        {
            "from": "{{result.records}}",
            "collection": "profiles",
            "type": "fact",
            "keys": ["role"],
            "complete": True,
            "review": "required",
        }
    )
    basis = EvaluationBasis(
        mode="corpus",
        from_seq=None,
        through_seq=0,
        predecessor_run_id=None,
        predecessor_source_hash=None,
        input_rows=(),
        read_rows={},
        expected_heads=(ExpectedHead(collection="profiles", key="role", record_id=None),),
    )

    with pytest.raises(DerivationError, match=message):
        compile_candidate_set(
            [draft],
            emit=emit,
            basis=basis,
            visible=frozenset(),
            settings=settings,
            catalog=catalog,
        )


def test_uncited_retraction_is_limited_to_complete_snapshot(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    emit = EmitDefinition.model_validate(
        {
            "from": "{{result.records}}",
            "collection": "profiles",
            "type": "fact",
            "keys": ["role"],
            "complete": True,
            "review": "required",
        }
    )
    basis = EvaluationBasis(
        mode="changes",
        from_seq=0,
        through_seq=1,
        predecessor_run_id=None,
        predecessor_source_hash=None,
        input_rows=(),
        read_rows={},
        expected_heads=(ExpectedHead(collection="profiles", key="role", record_id=None),),
    )

    with pytest.raises(DerivationError, match="non-empty citations"):
        compile_candidate_set(
            [{"key": "role", "retract": True, "citations": []}],
            emit=emit,
            basis=basis,
            visible=frozenset(),
            settings=settings,
            catalog=catalog,
        )


def test_crm_example_exposes_incremental_and_rebuild_intent(settings: Settings) -> None:
    root = Path("examples/crm_profile_catalog")
    crm_settings = settings.model_copy(
        update={
            "models_file": root / "conf/models.yaml",
            "processors_file": root / "conf/processors.yaml",
            "collections_dir": root / "collections",
            "triggers_dir": root / "triggers",
            "views_dir": root / "views",
            "artifacts_dir": root / "artifacts",
            "mcp_dir": root / "mcp",
            "packages_dir": root / "packages",
            "derivations_dir": root / "derivations",
        }
    )
    catalog = load_definition_catalog(crm_settings)

    incremental = catalog.derivations["crm_profile"]
    rebuild = catalog.derivations["crm_profile_rebuild"]
    assert (incremental.driver.kind, emission_effect(incremental.emit)) == (
        "changes",
        "patch",
    )
    assert (rebuild.driver.kind, emission_effect(rebuild.emit)) == ("snapshot", "replace")
    assert emission_status(rebuild.emit) == "draft"
    assert "goals" in rebuild.emit.keys
    assert catalog.resolve_artifact("crm_profile_candidate").candidate_processor == (
        "crm_profile_rebuild"
    )


def _snapshot_source(**overrides: object) -> StreamSource:
    payload: dict[str, object] = {
        "kind": "snapshot",
        "collections": ["crm_events"],
        "types": ["crm_event"],
        "max_records": 200,
        "max_tokens": 24_000,
    }
    payload.update(overrides)
    return StreamSource.model_validate(payload)


def _snapshot_row(seq: int) -> dict[str, object]:
    return {
        "id": uuid4(),
        "seq": seq,
        "collection": "crm_events",
        "collection_version": 1,
        "entity": "contact:avery-chen",
        "key": None,
        "type": "crm_event",
        "status": "active",
        "content": {"text": f"event {seq}"},
        "scores": {},
        "occurred_at": datetime(2026, 7, 20, tzinfo=UTC),
        "depth": 0,
        "enriched_at": datetime(2026, 7, 20, tzinfo=UTC),
    }


def _mock_snapshot_conn(*, checkpoint: int, rows: list[dict[str, object]]) -> AsyncMock:
    checkpoint_result = AsyncMock()
    checkpoint_result.fetchone.return_value = {"high_seq": checkpoint}
    rows_result = AsyncMock()
    rows_result.fetchall.return_value = rows
    conn = AsyncMock()
    conn.execute.side_effect = [checkpoint_result, rows_result]
    return conn


def test_snapshot_window_requires_exactly_one_mode() -> None:
    with pytest.raises(ValidationError, match="requires recent or since/until"):
        SnapshotWindow.model_validate({})
    with pytest.raises(ValidationError, match="either recent or since/until, not both"):
        SnapshotWindow.model_validate({"recent": 10, "since": "2026-01-01T00:00:00Z"})
    with pytest.raises(ValidationError, match="since must be earlier than until"):
        SnapshotWindow.model_validate(
            {"since": "2026-07-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"}
        )


def test_window_only_allowed_on_snapshot_source() -> None:
    with pytest.raises(ValidationError, match="window is only valid on a snapshot source"):
        _snapshot_source(kind="changes", window={"recent": 10})
    # A plain snapshot and a windowed snapshot both validate.
    assert _snapshot_source().window is None
    assert _snapshot_source(window={"recent": 50}).window == SnapshotWindow(recent=50)


async def test_snapshot_recent_window_takes_newest_rows_ascending() -> None:
    source = _snapshot_source(max_records=200, window={"recent": 2})
    # The DB returns the newest two rows descending; the resolver presents them
    # ascending and records the tail lower bound as from_seq.
    conn = _mock_snapshot_conn(checkpoint=250, rows=[_snapshot_row(250), _snapshot_row(248)])

    through_seq, from_seq, inputs = await basis_module._snapshot_input(
        conn, workspace="test", entity="contact:avery-chen", source=source
    )

    assert through_seq == 250
    assert from_seq == 248
    assert inputs is not None
    assert [row.seq for row in inputs] == [248, 250]
    # The rows query is ordered descending and limited to `recent`.
    rows_call = conn.execute.call_args_list[1]
    assert "order by record.seq desc" in rows_call.args[0]
    assert rows_call.args[1][-1] == 2


async def test_snapshot_recent_window_over_max_records_fails() -> None:
    source = _snapshot_source(max_records=2, window={"recent": 3})
    conn = _mock_snapshot_conn(
        checkpoint=250,
        rows=[_snapshot_row(250), _snapshot_row(249), _snapshot_row(248)],
    )

    with pytest.raises(DerivationError, match="exceeds max_records"):
        await basis_module._snapshot_input(
            conn, workspace="test", entity="contact:avery-chen", source=source
        )


async def test_snapshot_date_window_bounds_checkpoint_and_membership() -> None:
    source = _snapshot_source(
        window={"since": "2026-07-01T00:00:00Z", "until": "2026-07-15T00:00:00Z"}
    )
    conn = _mock_snapshot_conn(checkpoint=248, rows=[_snapshot_row(247), _snapshot_row(248)])

    through_seq, from_seq, inputs = await basis_module._snapshot_input(
        conn, workspace="test", entity="contact:avery-chen", source=source
    )

    assert through_seq == 248
    assert from_seq == 247
    assert inputs is not None
    # The occurred_at range constrains both the checkpoint and the row selection.
    checkpoint_sql = conn.execute.call_args_list[0].args[0]
    assert "record.occurred_at >= %s" in checkpoint_sql
    assert "record.occurred_at <= %s" in checkpoint_sql
    rows_sql = conn.execute.call_args_list[1].args[0]
    assert "order by record.seq asc" in rows_sql


async def test_snapshot_unready_window_row_defers_run() -> None:
    source = _snapshot_source(window={"recent": 2})
    unready = _snapshot_row(250)
    unready["enriched_at"] = None
    conn = _mock_snapshot_conn(checkpoint=250, rows=[unready, _snapshot_row(248)])

    through_seq, from_seq, inputs = await basis_module._snapshot_input(
        conn, workspace="test", entity="contact:avery-chen", source=source
    )

    assert through_seq == 250
    assert from_seq is None
    assert inputs is None
