"""Tests for the bounded, deterministic page-fact index."""

from __future__ import annotations

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import load_definition_catalog
from memseek.derive.tasks_facts import (
    ExtractFactsConfig,
    ExtractFactsInput,
    extract_page_fact_index,
)
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records
from memseek.worker import WorkerRuntime, run_worker_once


def _page(identifier: str, key: str, body: str) -> dict[str, object]:
    return {
        "id": identifier,
        "key": key,
        "content": {"type": "person", "body": body},
    }


def test_page_fact_index_is_deterministic_and_parses_only_declared_facts() -> None:
    pages = ExtractFactsInput.model_validate(
        {
            "records": [
                _page(
                    "00000000-0000-0000-0000-000000000002",
                    "people/maya",
                    """# Maya

## Facts
- Founded [Acme](companies/acme).
- Invested in Orbit
  alongside Nora.

## Notes
- This is not a declared fact.
""",
                ),
                _page(
                    "00000000-0000-0000-0000-000000000001",
                    "people/lee",
                    """## Facts
```markdown
- Ignore this fenced item.
```
1. Advises Atlas.
""",
                ),
            ],
            "changed_records": [],
        }
    )

    first = extract_page_fact_index(pages.records, ExtractFactsConfig())
    second = extract_page_fact_index(tuple(reversed(pages.records)), ExtractFactsConfig())

    assert [item.model_dump(mode="json") for item in second.content.facts] == [
        item.model_dump(mode="json") for item in first.content.facts
    ]
    assert [item.model_dump(mode="json") for item in first.content.facts] == [
        {"page_key": "people/lee", "text": "Advises Atlas."},
        {"page_key": "people/maya", "text": "Founded [Acme](companies/acme)."},
        {"page_key": "people/maya", "text": "Invested in Orbit alongside Nora."},
    ]
    assert first.content.page_keys == ("people/lee", "people/maya")
    assert first.content.truncated is False
    assert first.content.omitted_facts == 0
    assert "This is not a declared fact" not in first.text
    assert "Ignore this fenced item" not in first.text


async def test_page_writes_replace_the_static_entity_fact_index(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "facts-index")
    initial = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="people/maya",
                    type="page",
                    text="Maya facts",
                    content={
                        "title": "Maya",
                        "type": "person",
                        "body": "## Facts\n- Founded Acme.\n- Invested in Orbit.",
                    },
                ),
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="people/lee",
                    type="page",
                    text="Lee facts",
                    content={
                        "title": "Lee",
                        "type": "person",
                        "body": "## Facts\n- Advises Atlas.",
                    },
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )

    first_pass = await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="facts-initial",
    )
    assert first_pass.enrichment_ready == 2
    assert first_pass.derivation_jobs == 3
    async with db_pool.connection() as conn:
        first_row = await (
            await conn.execute(
                """
                select content, derived_from
                from record
                where workspace = %s and collection = 'facts' and key = 'page_facts'
                  and status = 'active'
                order by seq desc
                limit 1
                """,
                (credential.workspace,),
            )
        ).fetchone()
    assert first_row is not None
    assert first_row["content"]["facts"] == [
        {"page_key": "people/lee", "text": "Advises Atlas."},
        {"page_key": "people/maya", "text": "Founded Acme."},
        {"page_key": "people/maya", "text": "Invested in Orbit."},
    ]
    assert initial.inserted[0].id in first_row["derived_from"]
    assert initial.inserted[1].id in first_row["derived_from"]

    updated = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="people/maya",
                    type="page",
                    text="Maya facts revised",
                    content={
                        "title": "Maya",
                        "type": "person",
                        "body": "## Facts\n- Founded Acme.\n- Joined Pioneer.",
                    },
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    second_pass = await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="facts-revised",
    )
    assert second_pass.enrichment_ready >= 1
    await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="facts-revised-followup",
    )
    async with db_pool.connection() as conn:
        second_row = await (
            await conn.execute(
                """
                select content, derived_from
                from record
                where workspace = %s and collection = 'facts' and key = 'page_facts'
                  and status = 'active'
                order by seq desc
                limit 1
                """,
                (credential.workspace,),
            )
        ).fetchone()
    assert second_row is not None
    assert second_row["content"]["facts"] == [
        {"page_key": "people/lee", "text": "Advises Atlas."},
        {"page_key": "people/maya", "text": "Founded Acme."},
        {"page_key": "people/maya", "text": "Joined Pioneer."},
    ]
    assert updated.inserted[0].id in second_row["derived_from"]
    assert "Invested in Orbit." not in second_row["content"]["text"]
