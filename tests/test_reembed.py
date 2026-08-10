"""Changing the embedding model without losing vector recall.

The staged space is what makes this possible at all, so these tests check the
whole path: stage, report coverage, refuse an incomplete cutover, promote, keep
the outgoing space complete, and reverse.
"""

from __future__ import annotations

import httpx
import pytest
from evolution_catalog import build_app, catalog_files, enrich, ingest, publish

from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import load_definition_catalog
from memseek.models import WorkspaceCredential
from memseek.reembed import ReembedError, coverage, cutover_space, reembed

_NEXT = "default-v2"


@pytest.fixture
async def workspace(db_pool: DatabasePool) -> WorkspaceCredential:
    return await create_workspace(db_pool, "reembed")


async def _seed(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential, *, rows: int = 3
) -> None:
    headers = {"Authorization": f"Bearer {workspace.api_key}"}
    app = build_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            published = await publish(client, headers, catalog_files())
            assert published.status_code == 200, published.text
            for index in range(rows):
                assert (await ingest(client, headers, text=f"note {index}")).status_code == 200
    await enrich(settings, db_pool, workspace.workspace)


async def _spaces(db_pool: DatabasePool, workspace: str) -> dict[str, int]:
    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select embedding_space, count(*) as rows
            from record
            where workspace = %s and embedding is not null
            group by embedding_space
            """,
            (workspace,),
        )
        return {str(row["embedding_space"]): int(row["rows"]) for row in await result.fetchall()}


async def _staged(db_pool: DatabasePool, workspace: str) -> dict[str, int]:
    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select staged.space, count(*) as rows
            from record_embedding staged
            join record on record.id = staged.record_id
            where record.workspace = %s
            group by staged.space
            """,
            (workspace,),
        )
        return {str(row["space"]): int(row["rows"]) for row in await result.fetchall()}


async def test_staging_a_space_leaves_the_active_one_untouched(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    await _seed(settings, db_pool, workspace, rows=3)
    catalog = load_definition_catalog(settings)
    before = await _spaces(db_pool, workspace.workspace)
    assert before == {catalog.models.embedding.space: 3}

    result = await reembed(db_pool, settings, catalog, workspace=workspace.workspace, space=_NEXT)
    assert result.embedded == 3
    assert result.failed == 0
    assert result.coverage.complete is True
    assert result.coverage.remaining == 0

    # Reads still run entirely against the active space.
    assert await _spaces(db_pool, workspace.workspace) == before
    assert await _staged(db_pool, workspace.workspace) == {_NEXT: 3}


async def test_reembed_is_idempotent_and_bounded(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    await _seed(settings, db_pool, workspace, rows=4)
    catalog = load_definition_catalog(settings)

    partial = await reembed(
        db_pool, settings, catalog, workspace=workspace.workspace, space=_NEXT, max_rows=2
    )
    assert partial.embedded == 2
    assert partial.coverage.remaining == 2
    assert partial.coverage.complete is False

    rest = await reembed(db_pool, settings, catalog, workspace=workspace.workspace, space=_NEXT)
    assert rest.embedded == 2
    assert rest.coverage.complete is True

    # Nothing left to do, so a repeat pass is free.
    again = await reembed(db_pool, settings, catalog, workspace=workspace.workspace, space=_NEXT)
    assert again.embedded == 0
    assert again.coverage.complete is True


async def test_reembed_refuses_the_active_space_and_bad_input(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    catalog = load_definition_catalog(settings)
    with pytest.raises(ReembedError) as active:
        await reembed(
            db_pool,
            settings,
            catalog,
            workspace=workspace.workspace,
            space=catalog.models.embedding.space,
        )
    assert active.value.code == "space_active"

    with pytest.raises(ReembedError) as invalid:
        await reembed(
            db_pool, settings, catalog, workspace=workspace.workspace, space="Not A Space"
        )
    assert invalid.value.code == "space"

    with pytest.raises(ReembedError) as budget:
        await reembed(
            db_pool, settings, catalog, workspace=workspace.workspace, space=_NEXT, max_rows=0
        )
    assert budget.value.code == "max_rows"


async def test_cutover_refuses_an_incomplete_space(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    """Promoting a partial space would silently drop rows out of vector recall."""

    await _seed(settings, db_pool, workspace, rows=3)
    catalog = load_definition_catalog(settings)
    await reembed(
        db_pool, settings, catalog, workspace=workspace.workspace, space=_NEXT, max_rows=1
    )

    with pytest.raises(ReembedError) as refused:
        await cutover_space(db_pool, workspace=workspace.workspace, space=_NEXT)
    assert refused.value.code == "incomplete_space"
    assert refused.value.status == 409
    assert "2 record(s) have no vector staged" in refused.value.detail

    # Nothing moved.
    assert await _spaces(db_pool, workspace.workspace) == {catalog.models.embedding.space: 3}


async def test_cutover_promotes_and_stays_reversible(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    await _seed(settings, db_pool, workspace, rows=3)
    catalog = load_definition_catalog(settings)
    original = catalog.models.embedding.space
    await reembed(db_pool, settings, catalog, workspace=workspace.workspace, space=_NEXT)

    promoted = await cutover_space(db_pool, workspace=workspace.workspace, space=_NEXT)
    assert promoted.promoted == 3
    assert promoted.previous_space == original
    # The outgoing vectors were staged on the way out, so the old space is complete.
    assert promoted.staged_previous == 3
    assert _NEXT in promoted.as_json()["next_step"]

    assert await _spaces(db_pool, workspace.workspace) == {_NEXT: 3}
    staged = await _staged(db_pool, workspace.workspace)
    assert staged[original] == 3

    async with db_pool.connection() as conn:
        result = await conn.execute(
            """
            select enrichment_meta #>> '{embedding,space}' as space
            from record
            where workspace = %s and embedding is not null
            """,
            (workspace.workspace,),
        )
        assert {str(row["space"]) for row in await result.fetchall()} == {_NEXT}

    # Reversal is just a cutover back to the space that was staged on the way out.
    reversed_result = await cutover_space(db_pool, workspace=workspace.workspace, space=original)
    assert reversed_result.promoted == 3
    assert await _spaces(db_pool, workspace.workspace) == {original: 3}


async def test_coverage_reports_an_untouched_space_as_incomplete(
    settings: Settings, db_pool: DatabasePool, workspace: WorkspaceCredential
) -> None:
    await _seed(settings, db_pool, workspace, rows=2)
    report = await coverage(db_pool, workspace=workspace.workspace, space=_NEXT)
    assert report.embedded_records == 2
    assert report.staged == 0
    assert report.remaining == 2
    assert report.complete is False
    assert report.as_json()["space"] == _NEXT
