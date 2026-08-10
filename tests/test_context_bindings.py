"""Named Pipeline sources: compile-time validation and bounded runtime."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from reference_catalog import materialize_reference_catalog

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import DefinitionError, load_definition_catalog
from memseek.derive.schema import RecordSource, ViewSource
from memseek.llm.fake import fake
from memseek.llm.registry import Completion
from memseek.worker import WorkerRuntime, run_worker_once, worker_lifespan

_PROFILE_WITH_CONTEXT = """name: profile
trigger:
  read: true
  accumulator: {metric: importance, threshold: 100}
  cooldown_s: 60
sources:
  new_events:
    kind: changes
    collections: [main]
    types: [event, chat, observation]
    statuses: [active]
    keyed: false
    max_records: 200
    max_tokens: 24000
    allow_empty: false
  current_profile:
    kind: current
    collections: [profiles]
    types: [fact]
    statuses: [active]
    keys: [role, preferences, commitments, open_threads, timeline]
    max_records: 100
    max_tokens: 12000
  todays_plan:
    kind: record
    collection: plans
    key: today
    type: plan
    max_tokens: 1000
  related_memory:
    kind: view
    view: agent_relevant_memory
    params: {entity: "{{entity}}", task: "profile refresh"}
    max_tokens: 2000
model: strong
limits:
  max_tasks: 1
  max_llm_calls: 2
  max_retrieved_records: 0
  max_visible_records: 255
  max_total_tokens: 50000
  max_wall_s: 120
tasks:
  - id: result
    use: llm
    with:
      output_schema:
        type: object
        required: [records]
        properties:
          records:
            type: array
            items:
              type: object
              required: [citations]
              properties:
                key: {type: string}
                text: {type: string}
                content: {type: object}
                citations:
                  type: array
                  items: {type: string, format: uuid}
                retract: {type: boolean}
              additionalProperties: false
        additionalProperties: false
      prompt: |
        Maintain the current cited profile of {{entity}}.

        CURRENT PROFILE STATE:
        {{current_profile.rendered}}

        TODAY'S PLAN:
        {{todays_plan.rendered}}

        RELATED MEMORY:
        {{related_memory.rendered}}

        NEW EVIDENCE:
        {{new_events.rendered}}

        Return only JSON: {"records":[{"key":"role","text":"...","citations":["uuid"]}]}
emit:
  from: "{{result.records}}"
  collection: profiles
  type: fact
  keys: [role, preferences, commitments, open_threads, timeline]
"""


def _copy_catalog(destination: Path) -> Path:
    return materialize_reference_catalog(destination)


def _settings(root: Path, base: Settings | None = None) -> Settings:
    if base is not None:
        return base.model_copy(
            update={
                "models_file": root / "conf/models.yaml",
                "processors_file": root / "conf/processors.yaml",
                "rank_default_file": root / "conf/rank_default.yaml",
                "search_profiles_file": root / "conf/search_profiles.yaml",
                "collections_dir": root / "collections",
                "derivations_dir": root / "derivations",
                "triggers_dir": root / "triggers",
                "views_dir": root / "views",
                "artifacts_dir": root / "artifacts",
                "mcp_dir": root / "mcp",
                "packages_dir": root / "packages",
                "llm_fake": True,
            }
        )
    return Settings(
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


def _write_profile(root: Path, document: str) -> None:
    (root / "derivations/profile.yaml").write_text(document, encoding="utf-8")


def _load_error(root: Path, *, code: str | None = None) -> DefinitionError:
    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(_settings(root))
    if code is not None:
        assert caught.value.code == code
    return caught.value


def test_named_sources_compile_and_are_exposed(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path / "catalog")
    _write_profile(root, _PROFILE_WITH_CONTEXT)

    catalog = load_definition_catalog(_settings(root))

    definition = catalog.derivations["profile"]
    assert set(definition.sources) == {
        "new_events",
        "current_profile",
        "todays_plan",
        "related_memory",
    }
    plan = definition.sources["todays_plan"]
    memory = definition.sources["related_memory"]
    assert isinstance(plan, RecordSource)
    assert isinstance(memory, ViewSource)
    assert plan.collection == "plans"
    assert memory.view == "agent_relevant_memory"


def test_view_source_rejects_unknown_view(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path / "catalog")
    _write_profile(
        root,
        _PROFILE_WITH_CONTEXT.replace("view: agent_relevant_memory", "view: missing_view"),
    )

    _load_error(root, code="reference")


def test_view_source_rejects_unknown_view_parameter(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path / "catalog")
    _write_profile(
        root,
        _PROFILE_WITH_CONTEXT.replace(
            'task: "profile refresh"', 'unknown_param: "profile refresh"'
        ),
    )

    _load_error(root, code="view_parameter")


@pytest.mark.parametrize("reference", ["entity.id", "entity.unknown"])
def test_pipeline_entity_is_a_scalar(tmp_path: Path, reference: str) -> None:
    root = _copy_catalog(tmp_path / "catalog")
    _write_profile(
        root,
        _PROFILE_WITH_CONTEXT.replace("{{entity}}", f"{{{{{reference}}}}}", 1),
    )

    error = _load_error(root, code="template_reference")
    assert "entity is a scalar" in str(error)


def test_unreferenced_source_is_rejected(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path / "catalog")
    _write_profile(
        root,
        _PROFILE_WITH_CONTEXT.replace(
            "        TODAY'S PLAN:\n        {{todays_plan.rendered}}\n\n", ""
        ),
    )

    _load_error(root, code="unused_source")


def test_record_source_requires_a_keyed_collection(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path / "catalog")
    _write_profile(
        root,
        _PROFILE_WITH_CONTEXT.replace("collection: plans", "collection: reflections"),
    )

    _load_error(root, code="source_kind")


def test_source_names_cannot_shadow_core_names(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path / "catalog")
    _write_profile(
        root,
        _PROFILE_WITH_CONTEXT.replace("todays_plan", "run"),
    )

    _load_error(root, code="schema")


def test_source_token_budget_is_bounded(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path / "catalog")
    _write_profile(
        root,
        _PROFILE_WITH_CONTEXT.replace("max_tokens: 2000", "max_tokens: 50001"),
    )

    _load_error(root, code="budget")


async def test_run_reads_record_and_view_sources_and_audits_the_trace(
    settings: Settings,
    db_pool: DatabasePool,
    tmp_path: Path,
) -> None:
    root = _copy_catalog(tmp_path / "catalog")
    _write_profile(root, _PROFILE_WITH_CONTEXT)
    run_settings = _settings(root, base=settings)
    catalog = load_definition_catalog(run_settings)
    credential = await create_workspace(db_pool, "context-bindings")
    app_pool = create_pool(run_settings)
    app = create_app(run_settings, catalog=catalog, pool=app_pool, verify_storage=False)
    headers = {"Authorization": f"Bearer {credential.api_key}"}

    fake.reset()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            plan = await client.post(
                "/records",
                headers=headers,
                json={
                    "records": [
                        {
                            "collection": "plans",
                            "entity": "maria",
                            "type": "plan",
                            "key": "today",
                            "text": "Plan: finish the quarterly report.",
                        }
                    ]
                },
            )
            assert plan.status_code == 200, plan.text
            plan_id = plan.json()["inserted"][0]["id"]
            events = await client.post(
                "/records",
                headers=headers,
                json={
                    "records": [
                        {
                            "entity": "maria",
                            "type": "event",
                            "text": "Maria was promoted to platform lead. [importance=10]",
                        }
                    ]
                },
            )
            assert events.status_code == 200, events.text
            event_id = events.json()["inserted"][0]["id"]

            worker_pool = create_pool(run_settings)
            async with worker_lifespan(run_settings, catalog=catalog, pool=worker_pool) as runtime:
                enriched = await run_worker_once(
                    WorkerRuntime(settings=run_settings, catalog=catalog, pool=runtime.pool),
                    worker_id="context-enricher",
                )
                assert enriched.enrichment_ready == 2

                fake.enqueue(
                    Completion(
                        text=(
                            '{"records":[{"key":"role",'
                            f'"text":"Platform lead.","citations":["{event_id}"]}}]}}'
                        )
                    )
                )
                queued = await client.post(
                    "/processors/profile/run",
                    headers=headers,
                    json={"entity": "maria"},
                )
                assert queued.status_code == 200, queued.text
                derived = await run_worker_once(
                    WorkerRuntime(settings=run_settings, catalog=catalog, pool=runtime.pool),
                    worker_id="context-deriver",
                )
                assert derived.not_ready_jobs == 0

            runs = await client.get(
                "/runs",
                headers=headers,
                params={"entity": "maria", "processor": "profile", "operation": "derive"},
            )
            assert runs.status_code == 200
            run_rows = runs.json()["runs"]
            assert run_rows
            run = await client.get(f"/runs/{run_rows[0]['id']}", headers=headers)
            assert run.status_code == 200
            content = run.json()["run"]["content"]

    assert content["status"] == "ok"
    assert content["basis"]["reads"]["todays_plan"] == [plan_id]
    trace = {item["name"]: item for item in content["context_trace"]}
    assert set(trace) == {"related_memory"}
    assert trace["related_memory"]["source"] == "view:agent_relevant_memory"
    assert content["task_trace"][0]["task"] == "result"
    assert content["task_trace"][0]["use"] == "llm"
    assert event_id in content["task_trace"][0]["citation_ids"]
    final_prompt = fake.completion_calls[-1].prompt
    assert "Maintain the current cited profile of maria." in final_prompt
    assert "Plan: finish the quarterly report." in final_prompt
    assert str(plan_id) in final_prompt
