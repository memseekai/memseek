"""Artifact uses, learning targets, and feedback ingestion.

This file is the executable form of the correctness list in the artifact-use
specification: a bind returns a stable handle and exact identity, the stored row
holds no content, telemetry stays scalar, delayed feedback needs only the use ID,
and a signal is an ordinary record whose provenance depends on whether an exact
snapshot exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from memseek.api import create_app
from memseek.artifact_uses import (
    ArtifactUseRequest,
    bind_artifact_use,
    purge_expired_artifact_uses,
)
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog
from memseek.sdk import MemseekClient, MemseekHTTPError

_ENTITY = "agent:ada"
_PARAMETERS = {
    "entity": _ENTITY,
    "task": "Where is my refund?",
    "start": "2026-07-26T00:00:00Z",
    "end": "2026-07-27T00:00:00Z",
}


async def _seed_agent_state(
    pool: DatabasePool,
    settings: Settings,
    *,
    workspace: str,
    skill_run_id: UUID | None = None,
) -> dict[str, str]:
    """Write one active profile plus a complete promoted skill for the prompt.

    The skill rows are inserted already-ready and behind one shared run so the
    fixture stands in for a promotion without running the candidate pipeline.
    """

    catalog = load_definition_catalog(settings)
    profiles = catalog.resolve_collection("profiles")
    skills = catalog.resolve_collection("skills")
    run_id = skill_run_id or uuid4()
    written: dict[str, str] = {}
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, key, type, content, enriched_at
            ) values (%s, %s, %s, %s, %s, 'role', 'fact', %s, now())
            returning id
            """,
            (
                workspace,
                profiles.name,
                profiles.version,
                profiles.definition_hash,
                _ENTITY,
                Jsonb({"text": "Ada handles refund questions."}),
            ),
        )
        row = await result.fetchone()
        assert row is not None
        written["profile"] = str(row["id"])
        for key, text in (
            ("steps", "Check the authoritative payment status first."),
            ("pitfalls", "Never call a refund complete from the case status alone."),
            ("examples", "Pending in payments, closed in CRM: report pending."),
        ):
            result = await conn.execute(
                """
                insert into record (
                  workspace, collection, collection_version, collection_hash,
                  entity, key, type, content, enriched_at, run_id
                ) values (%s, %s, %s, %s, %s, %s, 'skill', %s, now(), %s)
                returning id
                """,
                (
                    workspace,
                    skills.name,
                    skills.version,
                    skills.definition_hash,
                    _ENTITY,
                    key,
                    Jsonb({"text": text}),
                    run_id,
                ),
            )
            row = await result.fetchone()
            assert row is not None
            written[key] = str(row["id"])
    written["skill_run_id"] = str(run_id)
    return written


def _app(settings: Settings) -> Any:
    return create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
    )


async def test_bind_returns_exact_identity_and_resolved_learning_target(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-bind")
    seeded = await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    catalog = load_definition_catalog(settings)
    artifact = catalog.resolve_artifact("daily_agent_prompt")

    bound = await bind_artifact_use(
        db_pool,
        workspace=credential.workspace,
        name="daily_agent_prompt",
        request=ArtifactUseRequest(parameters=_PARAMETERS),
        catalog=catalog,
        settings=settings,
    )

    assert UUID(bound["id"])
    assert bound["artifact"] == {
        "name": "daily_agent_prompt",
        "version": artifact.version,
        "definition_hash": artifact.definition_hash,
    }
    assert bound["snapshot_id"] is None
    assert bound["expired"] is False
    assert "Ada handles refund questions." in bound["content"]

    # The learning target names the reviewed artifact that owns the skill's
    # promotion lifecycle, plus the exact heads that were in force.
    target = bound["learning_target"]
    assert target["artifact"]["name"] == "maintained_skill"
    assert target["block"] == "skill"
    assert target["entity"] == _ENTITY
    assert target["base_run_id"] == seeded["skill_run_id"]
    assert {head["key"] for head in target["heads"]} == {"steps", "pitfalls", "examples"}
    assert {head["record_id"] for head in target["heads"]} == {
        seeded["steps"],
        seeded["pitfalls"],
        seeded["examples"],
    }


async def test_use_row_stores_no_content_and_telemetry_is_scalar(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-no-content")
    await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    catalog = load_definition_catalog(settings)

    bound = await bind_artifact_use(
        db_pool,
        workspace=credential.workspace,
        name="daily_agent_prompt",
        request=ArtifactUseRequest(parameters=_PARAMETERS),
        catalog=catalog,
        settings=settings,
    )

    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select to_jsonb(artifact_use) as row from artifact_use where id = %s",
            (UUID(bound["id"]),),
        )
        row = await result.fetchone()
    assert row is not None
    stored = row["row"]
    assert set(stored) == {
        "id",
        "workspace",
        "artifact_name",
        "artifact_version",
        "definition_hash",
        "render_sha256",
        "learning_target",
        "snapshot_id",
        "created_at",
        "expires_at",
    }
    # Neither the render nor the request parameters may reach the row: the task
    # text is untrusted user content and the render is the prompt itself.
    serialized = str(stored)
    assert "Where is my refund?" not in serialized
    assert "Ada handles refund questions." not in serialized

    telemetry = bound["telemetry"]
    assert set(telemetry) == {
        "memseek.use.id",
        "memseek.artifact.name",
        "memseek.artifact.version",
        "memseek.artifact.definition_hash",
        "memseek.artifact.render_sha256",
    }
    assert all(isinstance(value, (str, int)) for value in telemetry.values())
    assert telemetry["memseek.artifact.render_sha256"] == bound["render_sha256"]


async def test_snapshot_bind_shares_one_render_hash_and_cites_provenance(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-snapshot")
    await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    catalog = load_definition_catalog(settings)

    bound = await bind_artifact_use(
        db_pool,
        workspace=credential.workspace,
        name="daily_agent_prompt",
        request=ArtifactUseRequest(parameters=_PARAMETERS, snapshot=True),
        catalog=catalog,
        settings=settings,
    )

    assert bound["snapshot_id"] is not None
    assert bound["telemetry"]["memseek.artifact.snapshot_id"] == bound["snapshot_id"]
    async with db_pool.connection() as conn:
        result = await conn.execute(
            "select content, collection from record where id = %s",
            (UUID(bound["snapshot_id"]),),
        )
        row = await result.fetchone()
    assert row is not None
    assert row["collection"] == "prompt_snapshots"
    # One resolution produced both, so the persisted render and the handle name
    # the same bytes.
    assert row["content"]["rendered_sha256"] == bound["render_sha256"]
    assert row["content"]["text"] == bound["content"]


async def test_feedback_creates_learning_signal_and_is_idempotent(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-feedback")
    await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
            MemseekClient("http://test", credential.api_key, client=http_client) as client,
        ):
            bound = await client.artifact("daily_agent_prompt").bind(_PARAMETERS)
            # The application keeps exactly one short field beside its result.
            stored_use_id = bound.id

            first = await client.feedback.submit(
                use_id=stored_use_id,
                kind="thumbs_down",
                source="end_user",
                comment="It said the refund was complete.",
                label="incorrect_status",
                dedupe_key="message:msg_123:thumbs_down",
            )
            assert first["duplicate"] is False
            assert first["collection"] == "learning_signals"
            # Routed to the reviewed artifact that should improve, not the prompt.
            assert first["entity"] == "artifact:maintained_skill"
            assert first["type"] == "thumbs_down"

            repeat = await client.feedback.submit(
                use_id=stored_use_id,
                kind="thumbs_down",
                source="end_user",
                comment="It said the refund was complete.",
                label="incorrect_status",
                dedupe_key="message:msg_123:thumbs_down",
            )
            assert repeat["duplicate"] is True
            assert repeat["record_id"] == first["record_id"]

            record = await client.record(first["record_id"])

    content = record["content"]
    assert content["signal"] == {
        "kind": "thumbs_down",
        "source": "end_user",
        "label": "incorrect_status",
    }
    assert content["evidence"] == {"comment": "It said the refund was complete."}
    assert content["artifact_use"]["id"] == stored_use_id
    assert content["artifact_use"]["render_sha256"] == bound.render_sha256
    assert content["artifact_use"]["snapshot_id"] is None
    assert content["artifact_use"]["learning_target"]["artifact"]["name"] == "maintained_skill"
    # Without a snapshot the signal claims no provenance edge to source records.
    assert record["derived_from"] == []


async def test_snapshot_feedback_cites_the_snapshot_as_provenance(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-feedback-snapshot")
    await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
            MemseekClient("http://test", credential.api_key, client=http_client) as client,
        ):
            bound = await client.artifact("daily_agent_prompt").bind(_PARAMETERS, snapshot=True)
            signal = await client.feedback.for_use(bound.id).correction(
                expected="Tell the customer the refund is pending.",
                comment="The authoritative system still showed pending.",
            )
            record = await client.record(signal["record_id"])

    assert signal["type"] == "correction"
    assert record["derived_from"] == [bound.snapshot_id]
    assert record["content"]["evidence"]["expected"] == ("Tell the customer the refund is pending.")


async def test_expired_use_rejects_feedback_and_is_purged(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-expired")
    await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
            MemseekClient("http://test", credential.api_key, client=http_client) as client,
        ):
            bound = await client.artifact("daily_agent_prompt").bind(_PARAMETERS)
            async with db_pool.connection() as conn:
                await conn.execute(
                    "update artifact_use set expires_at = %s where id = %s",
                    (datetime.now(UTC) - timedelta(seconds=1), UUID(bound.id)),
                )
            metadata = await client.artifact_use(bound.id)
            assert metadata["expired"] is True

            with pytest.raises(MemseekHTTPError) as expired:
                await client.feedback.submit(use_id=bound.id, kind="thumbs_down", source="end_user")
    assert expired.value.status_code == 410
    assert cast(dict[str, Any], expired.value.payload)["error"] == "artifact_use_expired"

    assert await purge_expired_artifact_uses(db_pool, settings) == 1
    assert await purge_expired_artifact_uses(db_pool, settings) == 0
    async with db_pool.connection() as conn:
        result = await conn.execute("select count(*) as count from artifact_use")
        row = await result.fetchone()
    assert row is not None
    assert row["count"] == 0


async def test_unknown_use_and_foreign_workspace_are_not_found(
    settings: Settings, db_pool: DatabasePool
) -> None:
    owner = await create_workspace(db_pool, "uses-owner")
    stranger = await create_workspace(db_pool, "uses-stranger")
    await _seed_agent_state(db_pool, settings, workspace=owner.workspace)
    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            async with MemseekClient(
                "http://test", owner.api_key, client=http_client
            ) as owner_client:
                bound = await owner_client.artifact("daily_agent_prompt").bind(_PARAMETERS)
            async with MemseekClient(
                "http://test", stranger.api_key, client=http_client
            ) as stranger_client:
                # A use ID is not a credential and grants no cross-workspace read.
                with pytest.raises(MemseekHTTPError) as foreign:
                    await stranger_client.artifact_use(bound.id)
                with pytest.raises(MemseekHTTPError) as missing:
                    await stranger_client.artifact_use(str(uuid4()))
    assert foreign.value.status_code == 404
    assert missing.value.status_code == 404


async def test_changed_target_is_not_silently_treated_as_the_original_base(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-rebase")
    first = await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    catalog = load_definition_catalog(settings)

    before = await bind_artifact_use(
        db_pool,
        workspace=credential.workspace,
        name="daily_agent_prompt",
        request=ArtifactUseRequest(parameters=_PARAMETERS),
        catalog=catalog,
        settings=settings,
    )

    # A later promotion writes new heads behind a different run.
    second = await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    after = await bind_artifact_use(
        db_pool,
        workspace=credential.workspace,
        name="daily_agent_prompt",
        request=ArtifactUseRequest(parameters=_PARAMETERS),
        catalog=catalog,
        settings=settings,
    )

    assert first["skill_run_id"] != second["skill_run_id"]
    assert before["learning_target"]["base_run_id"] == first["skill_run_id"]
    assert after["learning_target"]["base_run_id"] == second["skill_run_id"]
    # The earlier use keeps naming the version that actually influenced its run.
    assert before["learning_target"]["heads"] != after["learning_target"]["heads"]


async def test_mixed_head_runs_resolve_to_no_single_base(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-mixed-base")
    seeded = await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    catalog = load_definition_catalog(settings)
    async with db_pool.connection() as conn:
        await conn.execute(
            "update record set run_id = %s where id = %s",
            (uuid4(), UUID(seeded["steps"])),
        )

    bound = await bind_artifact_use(
        db_pool,
        workspace=credential.workspace,
        name="daily_agent_prompt",
        request=ArtifactUseRequest(parameters=_PARAMETERS),
        catalog=catalog,
        settings=settings,
    )

    # Heads that were not promoted together have no one base version to name.
    assert bound["learning_target"]["base_run_id"] is None
    assert len(bound["learning_target"]["heads"]) == 3


async def test_absent_target_heads_resolve_to_no_learning_target(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-no-target")
    catalog = load_definition_catalog(settings)
    profiles = catalog.resolve_collection("profiles")
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            insert into record (
              workspace, collection, collection_version, collection_hash,
              entity, key, type, content, enriched_at
            ) values (%s, %s, %s, %s, %s, 'role', 'fact', %s, now())
            """,
            (
                credential.workspace,
                profiles.name,
                profiles.version,
                profiles.definition_hash,
                _ENTITY,
                Jsonb({"text": "Ada handles refund questions."}),
            ),
        )

    bound = await bind_artifact_use(
        db_pool,
        workspace=credential.workspace,
        name="daily_agent_prompt",
        request=ArtifactUseRequest(parameters=_PARAMETERS),
        catalog=catalog,
        settings=settings,
    )

    # No skill was active, so no signal may be attributed to a skill version.
    assert bound["learning_target"] is None


async def test_generic_context_manager_carries_the_handle_for_any_sdk(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-context-manager")
    await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    app = _app(settings)
    calls: list[dict[str, Any]] = []

    async def arbitrary_sdk(*, instructions: str, input: str) -> str:
        calls.append({"instructions": instructions, "input": input})
        return "The refund is pending."

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
            MemseekClient("http://test", credential.api_key, client=http_client) as client,
        ):
            handle = client.artifact("daily_agent_prompt")
            async with handle.use(_PARAMETERS) as use:
                answer = await arbitrary_sdk(instructions=use.content, input="Where is my refund?")
                retained = use.id
            assert answer == "The refund is pending."
            assert calls[0]["instructions"] == use.content
            assert use.truncated is False

            # Delayed feedback needs only the retained ID plus the new outcome.
            evaluation = await client.feedback.for_use(retained).evaluation(
                score=0.2, label="incorrect_status"
            )
    assert evaluation["type"] == "evaluation"
    assert evaluation["artifact_use"]["id"] == retained


async def test_execution_refs_are_optional_and_informational(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-execution-refs")
    await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
            MemseekClient("http://test", credential.api_key, client=http_client) as client,
        ):
            bound = await client.artifact("daily_agent_prompt").bind(_PARAMETERS)
            signal = await client.feedback.submit(
                use_id=bound.id,
                kind="task_failure",
                source="application",
                execution_refs=[{"system": "logfire", "id": "trace-abc"}],
            )
            record = await client.record(signal["record_id"])

    assert record["content"]["execution_refs"] == [{"system": "logfire", "id": "trace-abc"}]
    # A reference is metadata; it never becomes a provenance edge.
    assert record["derived_from"] == []


async def test_feedback_rejects_unknown_kind_and_out_of_range_score(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-feedback-schema")
    await _seed_agent_state(db_pool, settings, workspace=credential.workspace)
    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {credential.api_key}"}
            bound = await client.post(
                "/artifacts/daily_agent_prompt/uses",
                headers=headers,
                json={"parameters": _PARAMETERS},
            )
            assert bound.status_code == 200, bound.text
            use_id = bound.json()["id"]
            unknown = await client.post(
                f"/artifact-uses/{use_id}/feedback",
                headers=headers,
                json={"kind": "vibes", "source": "end_user"},
            )
            out_of_range = await client.post(
                f"/artifact-uses/{use_id}/feedback",
                headers=headers,
                json={"kind": "evaluation", "source": "evaluator", "score": 4},
            )
            malformed = await client.get("/artifact-uses/not-a-uuid", headers=headers)
    assert unknown.status_code == 422
    assert out_of_range.status_code == 422
    assert malformed.status_code == 422


async def test_binding_an_unknown_artifact_is_not_found(
    settings: Settings, db_pool: DatabasePool
) -> None:
    credential = await create_workspace(db_pool, "uses-unknown-artifact")
    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/artifacts/no_such_artifact/uses",
                headers={"Authorization": f"Bearer {credential.api_key}"},
                json={"parameters": {}},
            )
    assert response.status_code == 404
    assert response.json()["error"] == "artifact_not_found"


async def test_artifact_use_routes_require_authentication(
    settings: Settings, db_pool: DatabasePool
) -> None:
    app = _app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            identity = str(uuid4())
            bind = await client.post(
                "/artifacts/daily_agent_prompt/uses", json={"parameters": _PARAMETERS}
            )
            read = await client.get(f"/artifact-uses/{identity}")
            feedback = await client.post(
                f"/artifact-uses/{identity}/feedback",
                json={"kind": "thumbs_up", "source": "end_user"},
            )
    assert {bind.status_code, read.status_code, feedback.status_code} == {401}
