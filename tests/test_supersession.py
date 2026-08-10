"""Annotation supersession: preferring a newer annotation without rewriting an old one.

A backfill leaves rows carrying the old name, rows carrying the new one, and rows
carrying both.  Immutability is right; making every reader handle that boundary is
not.  ``supersedes`` is the read-time preference that closes the gap, so these
tests check both halves of the resolution — the SQL expression and the canonical
Python recheck — plus the loader rules that keep a chain well formed.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from evolution_catalog import (
    ARCHIVE,
    ARTIFACT,
    COLLECTION,
    MIGRATION,
    PROCESSORS,
    RANK_DEFAULT,
    VIEW,
    build_app,
    catalog_files,
    enrich,
    ingest,
    publish,
)

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionError, load_definition_catalog
from memseek.definitions.loader import DefinitionSources, compile_definition_catalog
from memseek.models import WorkspaceCredential
from memseek.search.engine import _field_value
from memseek.search.scope import declared_field_paths, field_value_expression


@pytest.fixture
async def workspace(db_pool: DatabasePool) -> WorkspaceCredential:
    return await create_workspace(db_pool, "supersession")


def _tone_v2(**overrides: Any) -> dict[str, Any]:
    tone = copy.deepcopy(next(item for item in PROCESSORS if item["name"] == "tone_v1"))
    tone["name"] = "tone_v2"
    tone["prompt"] = "Classify the tone precisely."
    tone["supersedes"] = "tone_v1"
    tone.update(overrides)
    return tone


def _sources(
    processors: list[dict[str, Any]],
    *,
    collections: tuple[dict[str, Any], ...] | None = None,
) -> DefinitionSources:
    """Compile a catalog from in-memory definitions, as the loader would from YAML."""

    return DefinitionSources(
        models=yaml.safe_load(Path("conf/models.yaml").read_text(encoding="utf-8")),
        processors=tuple(processors),
        rank_defaults=yaml.safe_load(RANK_DEFAULT),
        search_profiles={"pg_default": {"backend": "pg"}},
        collections=collections or (copy.deepcopy(COLLECTION), copy.deepcopy(ARCHIVE)),
        derivations=(copy.deepcopy(MIGRATION),),
        views=(copy.deepcopy(VIEW),),
        artifacts=(copy.deepcopy(ARTIFACT),),
        packages=(),
    )


def test_supersession_chain_is_validated(settings: Settings) -> None:
    base = copy.deepcopy(PROCESSORS)

    unknown = [*copy.deepcopy(base), _tone_v2(supersedes="absent")]
    with pytest.raises(DefinitionError, match="supersedes unknown processor"):
        compile_definition_catalog(settings, _sources(unknown))

    itself = [*copy.deepcopy(base), _tone_v2(name="tone_v2", supersedes="tone_v2")]
    with pytest.raises(DefinitionError, match="cannot supersede itself"):
        compile_definition_catalog(settings, _sources(itself))

    forked = [*copy.deepcopy(base), _tone_v2(), _tone_v2(name="tone_v3")]
    with pytest.raises(DefinitionError, match="both supersede"):
        compile_definition_catalog(settings, _sources(forked))

    mismatched = [
        *copy.deepcopy(base),
        {
            "name": "tone_score",
            "kind": "score",
            "source": "constant",
            "input": {"collections": ["notes"]},
            "scale": [1, 10],
            "value": 3,
            "supersedes": "tone_v1",
        },
    ]
    with pytest.raises(DefinitionError, match="different kind"):
        compile_definition_catalog(settings, _sources(mismatched))


def test_a_valid_chain_compiles_and_is_reported(settings: Settings) -> None:
    processors = [*copy.deepcopy(PROCESSORS), _tone_v2()]
    catalog = compile_definition_catalog(settings, _sources(processors))
    assert catalog.processors["tone_v2"].supersedes == "tone_v1"
    assert catalog.processors["tone_v1"].supersedes is None


def test_supersession_does_not_change_the_record_contract(settings: Settings) -> None:
    """Preferring a newer annotation is a read preference, never a reinterpretation."""

    plain = compile_definition_catalog(settings, _sources(copy.deepcopy(PROCESSORS)))
    chained = compile_definition_catalog(
        settings, _sources([*copy.deepcopy(PROCESSORS), _tone_v2()])
    )
    # The collection is byte-identical in both catalogs, so its contract must be too:
    # declaring a supersession never restates what a stored row means.
    assert (
        plain.collections[("notes", 1)].contract_hash
        == chained.collections[("notes", 1)].contract_hash
    )


def test_declared_field_gains_fallback_paths_for_a_chain(settings: Settings) -> None:
    declared = copy.deepcopy(COLLECTION)
    declared["fields"] = {
        "tone": {"path": "annotations.tone_v2.label", "type": "string", "filter": True}
    }
    catalog = compile_definition_catalog(
        settings,
        _sources(
            [*copy.deepcopy(PROCESSORS), _tone_v2()],
            collections=(declared, copy.deepcopy(ARCHIVE)),
        ),
    )
    field = catalog.collections[("notes", 1)].fields["tone"]
    assert field.path == "annotations.tone_v2.label"
    assert field.fallback_paths == ("annotations.tone_v1.label",)

    # Both read paths, newest first.
    assert declared_field_paths(field) == (
        ("annotations", ["tone_v2", "label"]),
        ("annotations", ["tone_v1", "label"]),
    )

    # The SQL expression coalesces them in the same order.
    sql, params = field_value_expression({("notes", 1): field})
    assert "coalesce(" in sql
    assert params == ["notes", 1, ["tone_v2", "label"], ["tone_v1", "label"]]


def test_canonical_recheck_prefers_the_newest_annotation(settings: Settings) -> None:
    """The authoritative Python evaluation must match the SQL preference order."""

    declared = copy.deepcopy(COLLECTION)
    declared["fields"] = {
        "tone": {"path": "annotations.tone_v2.label", "type": "string", "filter": True}
    }
    catalog = compile_definition_catalog(
        settings,
        _sources(
            [*copy.deepcopy(PROCESSORS), _tone_v2()],
            collections=(declared, copy.deepcopy(ARCHIVE)),
        ),
    )
    field = catalog.collections[("notes", 1)].fields["tone"]

    both: dict[str, Any] = {
        "content": {},
        "annotations": {"tone_v1": {"label": "old"}, "tone_v2": {"label": "new"}},
    }
    only_old = {"content": {}, "annotations": {"tone_v1": {"label": "old"}}}
    only_new = {"content": {}, "annotations": {"tone_v2": {"label": "new"}}}
    neither: dict[str, Any] = {"content": {}, "annotations": {}}

    assert _field_value(both, field) == "new"
    assert _field_value(only_old, field) == "old"
    assert _field_value(only_new, field) == "new"
    assert _field_value(neither, field) is None


def test_a_field_without_a_chain_reads_exactly_one_path(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    relations = catalog.collections[("relations", 1)]
    field = relations.fields["confidence"]
    assert field.fallback_paths == ()
    sql, params = field_value_expression({("relations", 1): field})
    assert "coalesce(" not in sql
    assert params == ["relations", 1, ["confidence"]]


async def test_superseded_annotations_answer_a_live_query(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """End to end: old rows keep the old name and still satisfy the new field."""

    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    bound_v1 = copy.deepcopy(COLLECTION)
    # tone_v1 is required, which is what makes a filter over the field legal: every
    # row is guaranteed to hold at least the oldest annotation in the chain.
    bound_v1["required_processors"] = ["embedding_v1", "tone_v1"]
    bound_v1["fields"] = {
        "tone": {
            "path": "annotations.tone_v1.label",
            "type": "string",
            "filter": True,
            "sort": True,
        }
    }

    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await publish(client, headers, catalog_files(collections=[bound_v1]))
            assert first.status_code == 200, first.text
            assert (await ingest(client, headers, text="an older note")).status_code == 200
            await enrich(settings, db_pool, workspace.workspace)

    # Write the old annotation directly: this stands in for a row annotated before
    # tone_v2 existed, which is exactly the state supersession has to read.
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            update record
            set annotations = annotations || '{"tone_v1": {"label": "warm"}}'::jsonb
            where workspace = %s and collection = 'notes'
            """,
            (workspace.workspace,),
        )

    superseding = copy.deepcopy(COLLECTION)
    # required_processors is unchanged (that would reinterpret readiness); tone_v2
    # arrives as a binding and the field now prefers it.
    superseding["required_processors"] = ["embedding_v1", "tone_v1"]
    superseding["optional_processors"] = ["tone_v2"]
    superseding["fields"] = {
        "tone": {
            "path": "annotations.tone_v2.label",
            "type": "string",
            "filter": True,
            "sort": True,
        }
    }
    processors = [*copy.deepcopy(PROCESSORS), _tone_v2()]

    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            second = await publish(
                client,
                headers,
                catalog_files(
                    collections=[superseding],
                    processors=processors,
                    package_processors=[
                        *(item["name"] for item in processors),
                        "archive_notes",
                    ],
                ),
            )
            assert second.status_code == 200, second.text

            found = await client.post(
                "/search",
                headers=headers,
                json={
                    "mode": "structured",
                    "k": 10,
                    "scope": {"collections": ["notes"]},
                    "where": {"tone": {"eq": "warm"}},
                    "order_by": [{"field": "tone", "direction": "asc"}],
                },
            )

    assert found.status_code == 200, found.text
    # The row only has tone_v1, yet the tone_v2-rooted field found it.
    assert len(found.json()["hits"]) == 1


def test_migration_derivation_is_part_of_the_shipped_fixture() -> None:
    """The fixture's migration derivation keeps map_records exercised."""

    assert MIGRATION["tasks"][0]["use"] == "map_records"
