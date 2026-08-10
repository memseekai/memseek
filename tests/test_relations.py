"""Public typed relations expressed through ordinary derivations."""

from __future__ import annotations

import pytest

from memseek.auth import create_workspace
from memseek.catalog_views import processors_payload
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.derive.emission import emission_effect
from memseek.derive.runner import DerivationError, _validate_collection_content
from memseek.llm.fake import fake
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records
from memseek.worker import WorkerRuntime, run_worker_once


def test_contradiction_is_an_ordinary_yaml_derivation(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)

    definition = catalog.derivations["contradiction"]
    collection = catalog.resolve_collection("relations")
    processor = next(
        item
        for item in processors_payload(catalog)["processors"]
        if item["name"] == definition.name
    )

    assert not hasattr(catalog, "relations")
    assert catalog.resolve_processor("contradiction") is definition
    assert emission_effect(definition.emit) == "append"
    assert definition.emit.collection == "relations"
    assert definition.emit.collection_version == 1
    assert definition.emit.type == "contradiction"
    assert definition.driver.keyed is True
    assert collection.mode == "event"
    assert set(collection.content_schema["required"]) == {
        "text",
        "subject_id",
        "object_id",
        "explanation",
        "confidence",
    }
    assert processor["shape"] == "pipeline"
    assert processor["emit"]["type"] == "contradiction"


def test_generic_event_output_is_checked_against_its_collection_schema(
    settings: Settings,
) -> None:
    catalog = load_definition_catalog(settings)
    emit = catalog.derivations["contradiction"].emit
    valid = {
        "text": "The facts conflict",
        "subject_id": "00000000-0000-4000-8000-000000000001",
        "object_id": "00000000-0000-4000-8000-000000000002",
        "explanation": "Only one can be true",
        "confidence": 0.9,
    }

    _validate_collection_content(valid, emit=emit, catalog=catalog)
    with pytest.raises(DerivationError, match="confidence"):
        _validate_collection_content(
            {**valid, "confidence": 2.0},
            emit=emit,
            catalog=catalog,
        )


async def test_relation_is_available_as_a_user_selected_public_type(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "public-relation-type")

    inserted = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="maria",
                    collection="relations",
                    type="relation",
                    text="A typed user-authored relation",
                    content={
                        "subject_id": "00000000-0000-4000-8000-000000000001",
                        "object_id": "00000000-0000-4000-8000-000000000002",
                        "explanation": "The application chose this relation type",
                        "confidence": 1.0,
                    },
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )

    assert inserted.inserted[0].ready is False


async def _insert_key(
    pool: DatabasePool,
    *,
    workspace: str,
    key: str,
    text: str,
    settings: Settings,
    catalog: DefinitionCatalog,
):
    result = await insert_public_records(
        pool,
        workspace=workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="maria",
                    collection="profiles",
                    type="fact",
                    key=key,
                    text=text,
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    return result.inserted[0].id


async def test_contradiction_runs_in_the_generic_worker_and_emits_a_public_relation(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    fake.reset()
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "public-relations")
    runtime = WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool)

    object_id = await _insert_key(
        db_pool,
        workspace=credential.workspace,
        key="role",
        text="Maria leads the platform team.",
        settings=settings,
        catalog=catalog,
    )
    first = await run_worker_once(runtime, worker_id="relation-derivation")
    assert first.derivation_jobs == 1

    subject_id = await _insert_key(
        db_pool,
        workspace=credential.workspace,
        key="employment",
        text="[conflict] Maria does not lead the platform team.",
        settings=settings,
        catalog=catalog,
    )
    second = await run_worker_once(runtime, worker_id="relation-derivation")
    assert second.derivation_jobs == 1

    # Derived public events use the same readiness/enrichment path as every
    # other public record.
    await run_worker_once(runtime, worker_id="relation-enrichment")

    async with db_pool.connection() as conn:
        relation_result = await conn.execute(
            """
            select collection, type, key, content, derived_from, run_id, enriched_at
            from record
            where workspace = %s and collection = 'relations' and type = 'contradiction'
            """,
            (credential.workspace,),
        )
        relation = await relation_result.fetchone()
        run_result = await conn.execute(
            """
            select content
            from record
            where workspace = %s and collection = '_system' and type = 'run'
              and content->>'processor' = 'contradiction'
            order by seq desc
            limit 1
            """,
            (credential.workspace,),
        )
        run = await run_result.fetchone()

    assert relation is not None
    assert relation["collection"] == "relations"
    assert relation["type"] == "contradiction"
    assert relation["key"] is None
    assert relation["content"] == {
        "text": "Changed key conflicts with a current key",
        "subject_id": str(subject_id),
        "object_id": str(object_id),
        "explanation": "deterministic conflict",
        "confidence": 1.0,
    }
    assert relation["derived_from"] == [relation["run_id"], subject_id, object_id]
    assert relation["enriched_at"] is not None
    assert run is not None
    assert run["content"]["status"] == "ok"
    assert run["content"]["output_ids"]
