"""Golden-corpus tests for the zero-LLM structural graph extractor."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog
from memseek.derive.runner import process_derivation_job
from memseek.derive.tasks import task_adapter
from memseek.derive.tasks_graph import (
    ExtractRelationsConfig,
    ExtractRelationsInput,
    extract_page_edges,
)
from memseek.enrichment import enrich_once
from memseek.jobs import claim_job
from memseek.llm.fake import fake
from memseek.llm.registry import Completion
from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records
from memseek.worker import WorkerRuntime, run_worker_once


@asynccontextmanager
async def _client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _general_graph_settings(gbrain_settings: Settings, tmp_path: Path) -> Settings:
    """Add a second graph whose names and predicates have no gbrain vocabulary."""

    assert gbrain_settings.collections_dir is not None  # set by the gbrain fixture
    source = Path(gbrain_settings.collections_dir).parent
    root = tmp_path / "general_graph_catalog"
    shutil.copytree(source, root)
    (root / "collections/dependencies.yaml").write_text(
        """collections:
  - name: dependencies
    version: 1
    active: true
    mode: event
    schema:
      type: object
      required: [text, from_node, to_node, relationship]
      properties:
        text: {type: string}
        from_node: {type: string}
        to_node: {type: string}
        relationship: {type: string}
        metadata: {type: object}
      additionalProperties: false
    fields:
      from_node: {path: content.from_node, type: string, filter: true, project: true}
      to_node: {path: content.to_node, type: string, filter: true, project: true}
      relationship: {path: content.relationship, type: string, filter: true, project: true}
    search_profile: pg_default
  - name: components
    version: 1
    active: true
    mode: keyed
    schema:
      type: object
      required: [text, owner]
      properties:
        text: {type: string}
        owner: {type: string}
      additionalProperties: false
    search_profile: pg_default
""",
        encoding="utf-8",
    )
    (root / "views/dependencies.yaml").write_text(
        """views:
  - name: dependency_graph
    version: 1
    active: true
    kind: graph
    graph:
      edges: dependencies
      subject: from_node
      object: to_node
      predicate: relationship
    parameters:
      seed: {type: string, required: true}
      predicates: {type: string_array, default: [], max_items: 20}
      direction: {type: string, default: out, enum: [out, in, both]}
      depth: {type: integer, default: 1, minimum: 1, maximum: 4}
      limit: {type: integer, default: 20, minimum: 1, maximum: 100}
  - name: dependency_orphans
    version: 1
    active: true
    kind: graph_orphans
    graph:
      edges: dependencies
      subject: from_node
      object: to_node
      predicate: relationship
      nodes: components
    parameters:
      limit: {type: integer, default: 50, minimum: 1, maximum: 100}
""",
        encoding="utf-8",
    )
    package = root / "packages/gbrain.yaml"
    package.write_text(
        package.read_text(encoding="utf-8")
        .replace("  - edges@1\n", "  - edges@1\n  - dependencies@1\n  - components@1\n")
        .replace(
            "  - graph_query@1\n",
            "  - graph_query@1\n  - dependency_graph@1\n  - dependency_orphans@1\n",
        ),
        encoding="utf-8",
    )
    return gbrain_settings.model_copy(
        update={
            "models_file": root / "conf/models.yaml",
            "processors_file": root / "conf/processors.yaml",
            "collections_dir": root / "collections",
            "derivations_dir": root / "derivations",
            "triggers_dir": root / "triggers",
            "views_dir": root / "views",
            "artifacts_dir": root / "artifacts",
            "mcp_dir": root / "mcp",
            "packages_dir": root / "packages",
            "search_profiles_file": root / "conf/search_profiles.yaml",
            "rank_default_file": root / "conf/rank_default.yaml",
        }
    )


def _page(
    identifier: str,
    key: str,
    *,
    page_type: str,
    body: str,
    title: str | None = None,
) -> dict[str, object]:
    content: dict[str, object] = {"type": page_type, "body": body}
    if title is not None:
        content["title"] = title
    return {
        "id": identifier,
        "key": key,
        "content": content,
    }


def test_extract_relations_golden_corpus_is_deterministic_and_ignores_code() -> None:
    source = ExtractRelationsInput.model_validate(
        {
            "records": [
                _page(
                    "00000000-0000-0000-0000-000000000002",
                    "people/maya",
                    page_type="person",
                    body=(
                        "Maya is a venture partner. Her portfolio includes companies/orbit.\n"
                        "```markdown\n[Ignore](companies/hidden)\n```\n"
                        "She founded [Acme](companies/acme) and invested in "
                        "[[companies/zenith|Zenith]]."
                    ),
                ),
                _page(
                    "00000000-0000-0000-0000-000000000001",
                    "meetings/demo-day",
                    page_type="meeting",
                    body="Attendees included [Maya](people/maya).",
                ),
            ]
        }
    )

    config = ExtractRelationsConfig(context_chars=40)
    first = extract_page_edges(source.records, config)
    second = extract_page_edges(tuple(reversed(source.records)), config)

    assert [(edge.text, edge.content.link_source, edge.content.confidence) for edge in first] == [
        ("meetings/demo-day attended people/maya", "markdown", 1.0),
        ("people/maya invested_in companies/orbit", "bare-slug", 0.95),
        ("people/maya founded companies/acme", "markdown", 0.95),
        ("people/maya invested_in companies/zenith", "markdown", 0.95),
    ]
    assert [edge.model_dump(mode="json") for edge in second] == [
        edge.model_dump(mode="json") for edge in first
    ]
    assert all(edge.content.object != "companies/hidden" for edge in first)


def test_extract_relations_emits_mentions_only_when_enabled_and_honors_overrides() -> None:
    source = ExtractRelationsInput.model_validate(
        {
            "records": [
                _page(
                    "00000000-0000-0000-0000-000000000003",
                    "people/lee",
                    page_type="person",
                    body="Lee collaborates with companies/atlas and mentioned companies/echo.",
                )
            ]
        }
    )

    default = extract_page_edges(source.records, ExtractRelationsConfig())
    configured = extract_page_edges(
        source.records,
        ExtractRelationsConfig(
            emit_mentions=True,
            context_chars=40,
            predicate_regex_overrides={"works_at": r"collaborates with"},
        ),
    )

    assert default == []
    assert [(edge.content.object, edge.content.predicate) for edge in configured] == [
        ("companies/atlas", "works_at"),
        ("companies/echo", "mentions"),
    ]


def test_extract_relations_resolves_bare_wikilinks_against_titles_and_keeps_ambiguity() -> None:
    source = ExtractRelationsInput.model_validate(
        {
            "records": [
                _page(
                    "00000000-0000-0000-0000-000000000001",
                    "people/maya",
                    page_type="person",
                    body="Maya wrote [[Acme]] into the meeting notes.",
                )
            ],
            "known_pages": [
                _page(
                    "00000000-0000-0000-0000-000000000002",
                    "companies/acme",
                    page_type="company",
                    body="",
                    title="Acme",
                ),
                _page(
                    "00000000-0000-0000-0000-000000000003",
                    "projects/acme",
                    page_type="project",
                    body="",
                    title="Acme",
                ),
            ],
        }
    )

    edges = extract_page_edges(
        source.records, ExtractRelationsConfig(context_chars=40), known_pages=source.known_pages
    )

    assert [(edge.text, edge.content.link_source, edge.content.confidence) for edge in edges] == [
        ("people/maya wikilink_basename companies/acme", "wikilink-resolved", 1.0),
        ("people/maya wikilink_basename projects/acme", "wikilink-resolved", 1.0),
    ]
    assert all(edge.citations == (source.records[0].id,) for edge in edges)


def test_gbrain_catalog_registers_the_model_less_extraction_pipeline(
    gbrain_settings: Settings,
) -> None:
    catalog = load_definition_catalog(gbrain_settings)

    definition = catalog.derivations["link_extraction"]
    assert definition.model is None
    assert definition.limits.max_llm_calls == 0
    assert definition.emit.type == "edge"
    assert task_adapter("extract_relations").name == "extract_relations"
    assert task_adapter("graph").name == "graph"
    assert catalog.resolve_package("gbrain", "0.13.0").collections == (
        "pages@1",
        "edges@1",
        "syntheses@2",
        "atoms@1",
        "facts@1",
        "patterns@1",
        "concepts@1",
        "takes@1",
        "transcripts@1",
    )
    package = catalog.resolve_package("gbrain", "0.13.0")
    assert "pattern_detection" in package.processors
    assert "concept_synthesis" in package.processors
    assert "consolidate" in package.processors
    assert "enrich_thin" in package.processors
    assert "repair_synthesis" in package.processors
    assert "concept_synthesis.default" in package.triggers
    assert "consolidate.default" in package.triggers
    assert "enrich_thin.default" in package.triggers
    assert "repair_synthesis.default" in package.triggers
    assert len(package.retentions) == 1
    assert package.retentions[0].name == "purge_pages"
    assert package.retentions[0].collection == "pages@1"
    assert package.views == ("gbrain_search@1", "graph_query@1", "orphan_pages@1")


async def test_ready_page_write_runs_model_less_extraction_and_emits_an_edge(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "graph-write")
    result = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="people/maya",
                    type="page",
                    text="Maya founded Acme.",
                    content={
                        "title": "Maya",
                        "body": "Maya founded [Acme](companies/acme).",
                        "type": "person",
                    },
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    assert result.inserted[0].ready is False

    worker_result = await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="graph-extractor",
    )
    assert worker_result.enrichment_ready == 1
    assert worker_result.derivation_jobs == 3

    async with db_pool.connection() as conn:
        rows = await (
            await conn.execute(
                """
                select content, derived_from
                from record
                where workspace = %s and collection = 'edges'
                """,
                (credential.workspace,),
            )
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["content"] == {
        "text": "people/maya founded companies/acme",
        "subject": "people/maya",
        "object": "companies/acme",
        "predicate": "founded",
        "link_source": "markdown",
        "context": "Maya founded [Acme](companies/acme).",
        "confidence": 0.95,
    }
    assert result.inserted[0].id in rows[0]["derived_from"]
    assert len(rows[0]["derived_from"]) == 2


async def test_page_write_resolves_a_bare_wikilink_from_the_bounded_current_page_source(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "graph-wikilink-resolution")
    result = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="people/maya",
                    type="page",
                    text="Maya links Acme.",
                    content={
                        "title": "Maya",
                        "body": "Maya wrote [[Acme]] in her notes.",
                        "type": "person",
                    },
                ),
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="companies/acme",
                    type="page",
                    text="Acme page.",
                    content={"title": "Acme", "body": "Acme profile.", "type": "company"},
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )

    await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="graph-wikilink-resolution",
    )

    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                select content, derived_from
                from record
                where workspace = %s and collection = 'edges'
                  and content->>'subject' = 'people/maya'
                """,
                (credential.workspace,),
            )
        ).fetchone()
    assert row is not None
    assert row["content"] == {
        "text": "people/maya wikilink_basename companies/acme",
        "subject": "people/maya",
        "object": "companies/acme",
        "predicate": "wikilink_basename",
        "link_source": "wikilink-resolved",
        "context": "Maya wrote [[Acme]] in her notes.",
        "confidence": 1.0,
    }
    assert result.inserted[0].id in row["derived_from"]


async def test_ready_transcript_write_emits_a_cited_atom(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "atom-write")
    transcript = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="maya",
                    collection="transcripts",
                    type="transcript",
                    text="I will send the Acme proposal on Friday.",
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    transcript_id = transcript.inserted[0].id

    enrichment = await enrich_once(db_pool, settings, catalog)
    assert enrichment.ready == 1
    claimed = await claim_job(
        db_pool,
        worker_id="atom-extractor",
        kinds=("derive",),
        derivations=("atom_extraction",),
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    assert claimed is not None
    fake.reset()
    fake.enqueue(
        Completion(
            '{"records":[{"text":"Maya committed to send the Acme proposal on Friday.",'
            '"citations":["'
            + str(transcript_id)
            + '"],"content":{"kind":"commitment","confidence":0.95}}]}'
        )
    )

    result = await process_derivation_job(
        db_pool,
        claimed=claimed,
        settings=settings,
        catalog=catalog,
    )

    assert result.disposition == "done"
    assert result.output_count == 1
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                select content, derived_from
                from record
                where workspace = %s and collection = 'atoms'
                """,
                (credential.workspace,),
            )
        ).fetchone()
    assert row is not None
    assert row["content"] == {
        "text": "Maya committed to send the Acme proposal on Friday.",
        "kind": "commitment",
        "confidence": 0.95,
    }
    assert transcript_id in row["derived_from"]
    assert len(row["derived_from"]) == 2


async def test_ready_edges_emit_a_bounded_cited_pattern(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "pattern-write")
    edges = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="maya",
                    collection="edges",
                    type="edge",
                    text="Maya founded Acme",
                    content={
                        "text": "Maya founded Acme",
                        "subject": "people/maya",
                        "object": "companies/acme",
                        "predicate": "founded",
                        "link_source": "markdown",
                        "context": "Maya founded Acme.",
                        "confidence": 0.95,
                    },
                ),
                PublicRecordInput(
                    entity="maya",
                    collection="edges",
                    type="edge",
                    text="Maya invested in Orbit",
                    content={
                        "text": "Maya invested in Orbit",
                        "subject": "people/maya",
                        "object": "companies/orbit",
                        "predicate": "invested_in",
                        "link_source": "markdown",
                        "context": "Maya invested in Orbit.",
                        "confidence": 0.95,
                    },
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    edge_ids = [record.id for record in edges.inserted]

    enrichment = await enrich_once(db_pool, settings, catalog)
    assert enrichment.ready == 2
    claimed = await claim_job(
        db_pool,
        worker_id="pattern-detector",
        kinds=("derive",),
        derivations=("pattern_detection",),
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    assert claimed is not None
    fake.reset()
    fake.enqueue(
        Completion(
            '{"records":[{"text":"Maya repeatedly connects company formation and early investment.",'
            '"citations":["'
            + str(edge_ids[0])
            + '","'
            + str(edge_ids[1])
            + '"],"content":{"confidence":0.9}}]}'
        )
    )

    result = await process_derivation_job(
        db_pool,
        claimed=claimed,
        settings=settings,
        catalog=catalog,
    )

    assert result.disposition == "done"
    assert result.output_count == 1
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                select content, derived_from
                from record
                where workspace = %s and collection = 'patterns'
                """,
                (credential.workspace,),
            )
        ).fetchone()
    assert row is not None
    assert row["content"] == {
        "text": "Maya repeatedly connects company formation and early investment.",
        "confidence": 0.9,
    }
    assert set(edge_ids) <= set(row["derived_from"])


async def test_ready_atom_replaces_the_static_cited_concept_index(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "concept-index")
    atoms = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="maya",
                    collection="atoms",
                    type="atom",
                    text="Maya consistently makes warm introductions before board meetings.",
                    content={"kind": "commitment", "confidence": 0.95},
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    atom_id = atoms.inserted[0].id

    enrichment = await enrich_once(db_pool, settings, catalog)
    assert enrichment.ready == 1
    claimed = await claim_job(
        db_pool,
        worker_id="concept-synthesizer",
        kinds=("derive",),
        derivations=("concept_synthesis",),
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    assert claimed is not None
    fake.reset()
    fake.enqueue(
        Completion(
            '{"records":[{"key":"concept_index",'
            '"text":"Maya builds trust through deliberate warm introductions.",'
            '"citations":["'
            + str(atom_id)
            + '"],"content":{"concepts":[{"title":"Relationship-first coordination",'
            '"text":"Maya uses warm introductions to prepare collaborators for board-level work.",'
            '"confidence":0.9}],"truncated":false,"omitted_concepts":0}}]}'
        )
    )

    result = await process_derivation_job(
        db_pool,
        claimed=claimed,
        settings=settings,
        catalog=catalog,
    )

    assert result.disposition == "done"
    assert result.output_count == 1
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                select key, content, derived_from
                from record
                where workspace = %s and collection = 'concepts' and status = 'active'
                """,
                (credential.workspace,),
            )
        ).fetchone()
    assert row is not None
    assert row["key"] == "concept_index"
    assert row["content"] == {
        "text": "Maya builds trust through deliberate warm introductions.",
        "concepts": [
            {
                "title": "Relationship-first coordination",
                "text": "Maya uses warm introductions to prepare collaborators for board-level work.",
                "confidence": 0.9,
            }
        ],
        "truncated": False,
        "omitted_concepts": 0,
    }
    assert atom_id in row["derived_from"]


async def test_consolidate_replaces_one_bounded_cited_take_array(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "take-index")
    atoms = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="maya",
                    collection="atoms",
                    type="atom",
                    text="Maya committed to introduce Nora to the Acme board.",
                    content={"kind": "commitment", "confidence": 0.95},
                ),
                PublicRecordInput(
                    entity="maya",
                    collection="atoms",
                    type="atom",
                    text="Maya uses warm introductions before board meetings.",
                    content={"kind": "preference", "confidence": 0.9},
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    atom_ids = [record.id for record in atoms.inserted]

    enrichment = await enrich_once(db_pool, settings, catalog)
    assert enrichment.ready == 2
    claimed = await claim_job(
        db_pool,
        worker_id="consolidator",
        kinds=("derive",),
        derivations=("consolidate",),
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    assert claimed is not None
    fake.reset()
    fake.enqueue(
        Completion(
            '{"records":[{"key":"take_index",'
            '"text":"Maya prepares board relationships through deliberate warm introductions.",'
            '"citations":["'
            + str(atom_ids[0])
            + '","'
            + str(atom_ids[1])
            + '"],"content":{"takes":[{"title":"Prepare relationships before the board room",'
            '"claim":"Maya turns introductions into deliberate preparation for board-level collaboration.",'
            '"confidence":0.92,"citations":["'
            + str(atom_ids[0])
            + '","'
            + str(atom_ids[1])
            + '"]}],"truncated":false,"omitted_takes":0}}]}'
        )
    )

    result = await process_derivation_job(
        db_pool,
        claimed=claimed,
        settings=settings,
        catalog=catalog,
    )

    assert result.disposition == "done"
    assert result.output_count == 1
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                select key, content, derived_from
                from record
                where workspace = %s and collection = 'takes' and status = 'active'
                """,
                (credential.workspace,),
            )
        ).fetchone()
    assert row is not None
    assert row["key"] == "take_index"
    assert row["content"]["takes"] == [
        {
            "title": "Prepare relationships before the board room",
            "claim": "Maya turns introductions into deliberate preparation for board-level collaboration.",
            "confidence": 0.92,
            "citations": [str(atom_ids[0]), str(atom_ids[1])],
        }
    ]
    assert set(atom_ids) <= set(row["derived_from"])


async def test_enrich_thin_directly_replaces_the_triggering_live_page_once(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "thin-page-enrichment")
    inserted = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="maya",
                    collection="pages",
                    key="people/maya",
                    type="page",
                    text="Maya\n\nMaya founded Acme.",
                    content={
                        "title": "Maya",
                        "body": "Maya founded Acme.",
                        "type": "person",
                    },
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    page_id = inserted.inserted[0].id

    enrichment = await enrich_once(db_pool, settings, catalog)
    assert enrichment.ready == 1
    claimed = await claim_job(
        db_pool,
        worker_id="thin-page-enricher",
        kinds=("derive",),
        derivations=("enrich_thin",),
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    assert claimed is not None
    fake.reset()
    fake.enqueue(
        Completion(
            '{"records":[{"key":"people/maya",'
            '"text":"Maya\\n\\nMaya founded Acme.\\n\\n## Summary\\nMaya is recorded as Acme\'s founder.",'
            '"citations":["' + str(page_id) + '"],"content":{"title":"Maya",'
            '"body":"Maya founded Acme.\\n\\n## Summary\\nMaya is recorded as Acme\'s founder.",'
            '"type":"person","gbrain_enriched":true}}]}'
        )
    )

    result = await process_derivation_job(
        db_pool,
        claimed=claimed,
        settings=settings,
        catalog=catalog,
    )

    assert result.disposition == "done"
    assert result.output_count == 1
    ready_output = await enrich_once(db_pool, settings, catalog)
    assert ready_output.ready == 1
    async with db_pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                select content, derived_from
                from record
                where workspace = %s and collection = 'pages' and key = 'people/maya'
                  and status = 'active'
                order by seq desc
                limit 1
                """,
                (credential.workspace,),
            )
        ).fetchone()
        queued = await (
            await conn.execute(
                """
                select count(*) as count
                from job
                where workspace = %s and kind = 'derive' and derivation = 'enrich_thin'
                  and done_at is null and dead_at is null
                """,
                (credential.workspace,),
            )
        ).fetchone()
    assert row is not None
    assert row["content"] == {
        "text": "Maya\n\nMaya founded Acme.\n\n## Summary\nMaya is recorded as Acme's founder.",
        "title": "Maya",
        "body": "Maya founded Acme.\n\n## Summary\nMaya is recorded as Acme's founder.",
        "type": "person",
        "gbrain_enriched": True,
    }
    assert page_id in row["derived_from"]
    assert queued is not None
    assert queued["count"] == 0


async def test_graph_query_view_traverses_ready_edges_without_a_special_route(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    settings = gbrain_settings
    credential = await create_workspace(db_pool, "graph-view")
    catalog = load_definition_catalog(settings)
    edges = (
        ("people/maya", "companies/acme", "founded"),
        ("companies/acme", "people/nora", "advises"),
    )
    request = RecordBatchRequest(
        records=tuple(
            PublicRecordInput(
                entity="graph",
                collection="edges",
                type="edge",
                text=f"{subject} {predicate} {object}",
                content={
                    "text": f"{subject} {predicate} {object}",
                    "subject": subject,
                    "object": object,
                    "predicate": predicate,
                    "link_source": "markdown",
                    "context": "seeded graph edge",
                    "confidence": 1.0,
                },
            )
            for subject, object, predicate in edges
        )
    )
    await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=request,
        catalog=catalog,
        settings=settings,
    )
    await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="graph-view-ready",
    )

    headers = {"Authorization": f"Bearer {credential.api_key}"}
    async with _client(settings) as client:
        catalog_response = await client.get("/views", headers=headers)
        response = await client.post(
            "/views/graph_query/query",
            headers=headers,
            json={"seed": "people/maya", "depth": 2, "limit": 10},
        )
        reverse = await client.post(
            "/views/graph_query/query",
            headers=headers,
            json={"seed": "companies/acme", "direction": "in"},
        )
        invalid_direction = await client.post(
            "/views/graph_query/query",
            headers=headers,
            json={"seed": "people/maya", "direction": "sideways"},
        )
        at_cap = await client.post(
            "/views/graph_query/query",
            headers=headers,
            json={"seed": "people/maya", "depth": settings.max_graph_depth},
        )
        over_depth = await client.post(
            "/views/graph_query/query",
            headers=headers,
            json={"seed": "people/maya", "depth": settings.max_graph_depth + 1},
        )

    assert catalog_response.status_code == 200
    listed = next(
        item for item in catalog_response.json()["views"] if item["name"] == "graph_query"
    )
    assert listed["kind"] == "graph"
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["view"]["name"] == "graph_query"
    assert payload["nodes"] == ["companies/acme", "people/maya", "people/nora"]
    assert [path["nodes"] for path in payload["paths"]] == [
        ["people/maya", "companies/acme"],
        ["people/maya", "companies/acme", "people/nora"],
    ]
    assert [citation["predicate"] for citation in payload["citations"]] == ["founded", "advises"]
    assert [citation["id"] for citation in payload["hits"]] == [
        citation["id"] for citation in payload["citations"]
    ]
    assert reverse.status_code == 200, reverse.text
    assert [path["nodes"] for path in reverse.json()["paths"]] == [
        ["companies/acme", "people/maya"]
    ]
    assert invalid_direction.status_code == 422
    assert invalid_direction.json()["error"] == "view_parameter"
    # The gbrain view declares maximum: MAX_GRAPH_DEPTH, so its whole advertised
    # range is servable and the outer bound rejects the rest. A view that
    # declared more would instead surface the runtime `graph_depth` guard, which
    # no longer has coverage here -- exercising it needs a settings copy with a
    # reduced max_graph_depth.
    assert at_cap.status_code == 200, at_cap.text
    assert over_depth.status_code == 422
    assert over_depth.json()["error"] == "view_parameter"


async def test_catalog_declared_graph_supports_general_collections_and_predicates(
    gbrain_settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
) -> None:
    settings = _general_graph_settings(gbrain_settings, tmp_path)
    catalog = load_definition_catalog(settings)
    credential = await create_workspace(db_pool, "general-graph-view")
    nodes = tuple(
        PublicRecordInput(
            entity="system-map",
            collection="components",
            key=name,
            type="component",
            text=f"{name} is owned by platform",
            content={"owner": "platform"},
        )
        for name in ("api", "database", "queue", "website")
    )
    edges = tuple(
        PublicRecordInput(
            entity="system-map",
            collection="dependencies",
            type="dependency",
            text=f"{subject} {predicate} {object_}",
            content={
                "from_node": subject,
                "to_node": object_,
                "relationship": predicate,
                "metadata": {"source": "architecture"},
            },
        )
        for subject, object_, predicate in (
            ("api", "database", "depends_on"),
            ("database", "queue", "replicates_to"),
        )
    )
    await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(records=(*nodes, *edges)),
        catalog=catalog,
        settings=settings,
    )

    from memseek.graph import GraphTraversalError, GraphTraversalRequest, traverse_graph

    with pytest.raises(GraphTraversalError) as ambiguous:
        await traverse_graph(
            db_pool,
            workspace=credential.workspace,
            request=GraphTraversalRequest(seed="api", depth=2),
            catalog=catalog,
            settings=settings,
        )
    assert ambiguous.value.code == "graph_ambiguous"
    selected = await traverse_graph(
        db_pool,
        workspace=credential.workspace,
        request=GraphTraversalRequest(seed="api", graph="dependency_graph", depth=2),
        catalog=catalog,
        settings=settings,
    )
    assert selected["nodes"] == ["api", "database", "queue"]

    headers = {"Authorization": f"Bearer {credential.api_key}"}
    async with _client(settings) as client:
        views = await client.get("/views", headers=headers)
        traversal = await client.post(
            "/views/dependency_graph/query",
            headers=headers,
            json={
                "seed": "api",
                "predicates": ["depends_on", "replicates_to"],
                "depth": 2,
            },
        )
        orphans = await client.post("/views/dependency_orphans/query", headers=headers, json={})

    assert views.status_code == 200, views.text
    dependency_view = next(
        view for view in views.json()["views"] if view["name"] == "dependency_graph"
    )
    assert dependency_view["collections"] == ["dependencies"]
    assert dependency_view["graph"] == {
        "edges": "dependencies",
        "subject": "from_node",
        "object": "to_node",
        "predicate": "relationship",
        "nodes": None,
    }
    assert "enum" not in dependency_view["input_schema"]["properties"]["predicates"]["items"]
    assert traversal.status_code == 200, traversal.text
    payload = traversal.json()
    assert payload["nodes"] == ["api", "database", "queue"]
    assert [citation["predicate"] for citation in payload["citations"]] == [
        "depends_on",
        "replicates_to",
    ]
    assert payload["citations"][0]["content"]["metadata"] == {"source": "architecture"}
    assert "link_source" not in payload["citations"][0]
    assert orphans.status_code == 200, orphans.text
    assert [node["key"] for node in orphans.json()["orphans"]] == ["website"]
    assert orphans.json()["orphans"][0]["content"]["owner"] == "platform"


async def test_orphan_pages_view_uses_current_page_provenance_without_a_special_route(
    settings: Settings,
    gbrain_settings: Settings,
    db_pool: DatabasePool,
) -> None:
    """A superseded source edge cannot keep a page connected indefinitely."""

    settings = gbrain_settings
    credential = await create_workspace(db_pool, "orphan-pages-view")
    catalog = load_definition_catalog(settings)
    pages = await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="people/maya",
                    type="page",
                    text="Maya profile.",
                    content={"title": "Maya", "body": "Maya profile.", "type": "person"},
                ),
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="companies/acme",
                    type="page",
                    text="Acme profile.",
                    content={"title": "Acme", "body": "Acme profile.", "type": "company"},
                ),
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="people/nora",
                    type="page",
                    text="Nora profile.",
                    content={"title": "Nora", "body": "Nora profile.", "type": "person"},
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="orphan-pages-ready",
    )
    maya_id = next(record.id for record in pages.inserted if record.key == "people/maya")
    await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="graph",
                    collection="edges",
                    type="edge",
                    text="people/maya founded companies/acme",
                    content={
                        "text": "people/maya founded companies/acme",
                        "subject": "people/maya",
                        "object": "companies/acme",
                        "predicate": "founded",
                        "link_source": "markdown",
                        "context": "seeded graph edge",
                        "confidence": 1.0,
                    },
                    derived_from=(maya_id,),
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="orphan-edge-ready",
    )
    await enrich_once(db_pool, settings, catalog)
    from memseek.graph import GraphOrphansRequest, graph_orphans

    direct = await graph_orphans(
        db_pool,
        workspace=credential.workspace,
        request=GraphOrphansRequest(),
        catalog=catalog,
        settings=settings,
    )
    assert [item["key"] for item in direct["orphans"]] == ["people/nora"]

    headers = {"Authorization": f"Bearer {credential.api_key}"}
    async with _client(settings) as client:
        initial = await client.post("/views/orphan_pages/query", headers=headers, json={})

    assert initial.status_code == 200, initial.text
    assert [item["key"] for item in initial.json()["orphans"]] == ["people/nora"]

    await insert_public_records(
        db_pool,
        workspace=credential.workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity="graph",
                    collection="pages",
                    key="people/maya",
                    type="page",
                    text="Maya revised profile.",
                    content={
                        "title": "Maya",
                        "body": "Maya revised profile.",
                        "type": "person",
                    },
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    await run_worker_once(
        WorkerRuntime(settings=settings, catalog=catalog, pool=db_pool),
        worker_id="orphan-revision-ready",
    )
    await enrich_once(db_pool, settings, catalog)
    async with _client(settings) as client:
        stale = await client.post("/views/orphan_pages/query", headers=headers, json={})
        invalid = await client.post("/views/orphan_pages/query", headers=headers, json={"limit": 0})

    assert stale.status_code == 200, stale.text
    payload = stale.json()
    assert payload["view"]["name"] == "orphan_pages"
    assert [item["key"] for item in payload["orphans"]] == [
        "companies/acme",
        "people/maya",
        "people/nora",
    ]
    assert payload["input_record_ids"] == [item["id"] for item in payload["orphans"]]
    assert invalid.status_code == 422
    assert invalid.json()["error"] == "view_parameter"
