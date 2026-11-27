"""Run records of traced Agents: written by the runtime on every exit, at depth 0."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from memseek.agent_runs import collection_document
from memseek.computers import FAKE_COMPUTER_PROVIDER, ComputerExecutionError, ComputerResult
from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionError, load_definition_catalog
from memseek.derive.errors import DerivationError
from memseek.derive.runner import enqueue_derivation_job, process_derivation_job
from memseek.jobs import claim_job
from memseek.records import (
    PublicRecordInput,
    RecordBatchRequest,
    RecordValidationError,
    insert_public_records,
)
from memseek.triggers import evaluate_entity_triggers_tx

RENEWAL = Path(__file__).resolve().parents[1] / "examples" / "computer_renewal_catalog"
ENTITY = "account:acme"
SCHEMA = Draft202012Validator(collection_document()["schema"], format_checker=FormatChecker())


def _traced_catalog(
    settings: Settings, tmp_path: Path, *, max_wall_s: int = 180, on_write: bool = False
) -> Settings:
    root = tmp_path / "renewal"
    shutil.copytree(RENEWAL, root)
    agents = root / "agents" / "agents.yaml"
    if not agents.exists():
        agents = next((root / "agents").glob("*.yaml"))
    agents.write_text(
        agents.read_text().replace(
            "    toolset: renewal@1\n", "    toolset: renewal@1\n    trace: {content: metadata}\n"
        )
    )
    if max_wall_s != 180:
        for path in (root / "derivations").glob("*.yaml"):
            path.write_text(
                path.read_text().replace("max_wall_s: 180", f"max_wall_s: {max_wall_s}")
            )
    if on_write:
        assessment = root / "derivations" / "renewal_assessment.yaml"
        assessment.write_text(
            assessment.read_text().replace(
                '  cron: {expr: "0 2 * * *", entities: dirty}\n',
                "  write:\n    collections: [renewal_evidence]\n",
            )
        )
    return settings.model_copy(update={"catalog_file": root / "catalog.yaml"})


async def _run_assessment(
    settings: Settings, pool: DatabasePool, handler: Any
) -> tuple[str, list[UUID], Exception | None]:
    catalog = load_definition_catalog(settings)
    workspace = f"runs-{uuid4().hex[:8]}"
    async with pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values (%s, %s)", (workspace, "d" * 64)
        )
    written = await insert_public_records(
        pool,
        workspace=workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity=ENTITY,
                    collection="renewal_evidence",
                    type="evidence",
                    text="We promise a 12% discount below 99.95% uptime.",
                    content={"kind": "email"},
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )
    evidence = [row.id for row in written.inserted]
    await enqueue_derivation_job(
        pool, workspace=workspace, derivation="renewal_assessment", entity=ENTITY
    )
    claimed = await claim_job(
        pool,
        worker_id="runs-worker",
        kinds=("derive",),
        derivations=("renewal_assessment",),
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    assert claimed is not None
    FAKE_COMPUTER_PROVIDER.clear()
    FAKE_COMPUTER_PROVIDER.register_agent("renewal_analyst@2", handler)
    failure: Exception | None = None
    try:
        await process_derivation_job(pool, claimed=claimed, settings=settings, catalog=catalog)
    except DerivationError as exc:
        failure = exc
    finally:
        FAKE_COMPUTER_PROVIDER.clear()
    return workspace, evidence, failure


async def _rows(pool: DatabasePool, workspace: str, collection: str) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        result = await conn.execute(
            "select id, type, depth, run_id, derived_from, content, enriched_at is not null as ready from record "
            "where workspace = %s and collection = %s order by seq",
            (workspace, collection),
        )
        return [dict(row) for row in await result.fetchall()]


def _assess(evidence_ids: list[UUID]) -> Any:
    async def assess(request: Any) -> ComputerResult:
        return ComputerResult(
            value={
                "records": [
                    {
                        "text": "High renewal risk.",
                        "citations": [str(evidence_ids[0])],
                        "content": {"kind": "renewal_risk", "severity": "high"},
                    }
                ]
            },
            citation_ids=frozenset(evidence_ids),
            receipt={"provider": "fake", "metrics": {"steps": 3, "tool_calls": 2}},
            steps=3,
        )

    return assess


async def test_a_successful_derivation_records_its_traced_agent_run_at_depth_zero(
    settings: Settings, db_pool: DatabasePool, tmp_path: Path
) -> None:
    traced = _traced_catalog(settings, tmp_path)

    async def handler(request: Any) -> ComputerResult:
        evidence = [UUID(str(record["id"])) for record in request.input["evidence"]]
        return await _assess(evidence)(request)

    workspace, _, failure = await _run_assessment(traced, db_pool, handler)

    assert failure is None
    (run_audit,) = await _rows(db_pool, workspace, "_system")
    (record,) = await _rows(db_pool, workspace, "agent_runs")
    assert (record["type"], record["depth"], record["ready"]) == ("run_ok", 0, True)
    assert record["run_id"] == run_audit["id"]
    assert record["derived_from"] == [run_audit["id"]]
    SCHEMA.validate(record["content"])
    content = record["content"]
    assert content["status"] == "ok"
    assert content["origin"]["derivation"] == "renewal_assessment"
    assert content["agent"]["ref"] == "renewal_analyst@2"
    assert content["metrics"] == {"steps": 3, "tool_calls": 2}
    assert len(content["trace_id"]) == 32
    assert "error" not in content
    async with db_pool.connection() as conn:
        audit = await (
            await conn.execute("select content from record where id = %s", (run_audit["id"],))
        ).fetchone()
    assert audit is not None
    assert [entry["trace_id"] for entry in audit["content"]["computer_trace"]] == [
        content["trace_id"]
    ]


async def test_a_provider_failure_records_a_failed_run(
    settings: Settings, db_pool: DatabasePool, tmp_path: Path
) -> None:
    traced = _traced_catalog(settings, tmp_path)

    async def broken(request: Any) -> ComputerResult:
        raise ComputerExecutionError("provider", "secret stderr that must stay private")

    workspace, _, failure = await _run_assessment(traced, db_pool, broken)

    assert isinstance(failure, DerivationError)
    (record,) = await _rows(db_pool, workspace, "agent_runs")
    assert (record["type"], record["depth"]) == ("run_failed", 0)
    assert record["content"]["error_kind"] == "provider"
    assert "secret stderr" not in str(record["content"])
    SCHEMA.validate(record["content"])


async def test_a_failed_run_does_not_retrigger_its_own_derivation(
    settings: Settings, db_pool: DatabasePool, tmp_path: Path
) -> None:
    traced = _traced_catalog(settings, tmp_path, on_write=True)

    async def broken(request: Any) -> ComputerResult:
        raise ComputerExecutionError("provider", "the model answered without an envelope")

    workspace, _, failure = await _run_assessment(traced, db_pool, broken)

    assert isinstance(failure, DerivationError)
    async with db_pool.connection() as conn:
        jobs = await (
            await conn.execute(
                "select attempts, run_after - now() > interval '4 seconds' as backed_off "
                "from job where workspace = %s and derivation = 'renewal_assessment' "
                "and done_at is null and dead_at is null",
                (workspace,),
            )
        ).fetchall()
    assert [(job["attempts"], job["backed_off"]) for job in jobs] == [(1, True)]


async def _write_evidence(
    pool: DatabasePool, catalog: Any, settings: Settings, workspace: str, text: str
) -> None:
    await insert_public_records(
        pool,
        workspace=workspace,
        request=RecordBatchRequest(
            records=(
                PublicRecordInput(
                    entity=ENTITY,
                    collection="renewal_evidence",
                    type="evidence",
                    text=text,
                    content={"kind": "incident"},
                ),
            )
        ),
        catalog=catalog,
        settings=settings,
    )


async def _active_assessment_jobs(pool: DatabasePool, workspace: str) -> list[bool]:
    async with pool.connection() as conn:
        jobs = await (
            await conn.execute(
                "select run_after - now() > interval '4 seconds' as backed_off from job "
                "where workspace = %s and derivation = 'renewal_assessment' "
                "and done_at is null and dead_at is null",
                (workspace,),
            )
        ).fetchall()
    return [job["backed_off"] for job in jobs]


async def test_inputs_a_failed_run_saw_rearm_it_only_when_new_input_arrives(
    settings: Settings, db_pool: DatabasePool, tmp_path: Path
) -> None:
    traced = _traced_catalog(settings, tmp_path, on_write=True)
    catalog = load_definition_catalog(traced)

    async def broken(request: Any) -> ComputerResult:
        # Arrives after the run read its inputs, as a second queued task does
        # behind one the run keeps failing on.
        await _write_evidence(db_pool, catalog, traced, request.workspace, "Q1 credits were late.")
        raise ComputerExecutionError("provider", "the model key was rejected")

    workspace, _, _ = await _run_assessment(traced, db_pool, broken)

    # Another derivation on the entity succeeding re-evaluates every trigger.
    async with db_pool.connection() as conn:
        await evaluate_entity_triggers_tx(conn, workspace=workspace, entity=ENTITY, catalog=catalog)
    assert await _active_assessment_jobs(db_pool, workspace) == [True]

    async with db_pool.connection() as conn:
        await conn.execute(
            "update job set dead_at = now() where workspace = %s and dead_at is null",
            (workspace,),
        )
        await evaluate_entity_triggers_tx(conn, workspace=workspace, entity=ENTITY, catalog=catalog)
    assert await _active_assessment_jobs(db_pool, workspace) == []

    await _write_evidence(db_pool, catalog, traced, workspace, "Uptime fell to 99.9% in March.")
    assert await _active_assessment_jobs(db_pool, workspace) == [False]


async def test_a_run_cut_off_by_the_derivation_wall_limit_is_recorded_as_a_timeout(
    settings: Settings, db_pool: DatabasePool, tmp_path: Path
) -> None:
    traced = _traced_catalog(settings, tmp_path, max_wall_s=1)

    async def stuck(request: Any) -> ComputerResult:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    workspace, _, failure = await _run_assessment(traced, db_pool, stuck)

    assert isinstance(failure, DerivationError)
    assert failure.kind == "timeout"
    (record,) = await _rows(db_pool, workspace, "agent_runs")
    assert (record["type"], record["content"]["error_kind"]) == ("run_failed", "timeout")


async def test_the_public_record_path_refuses_agent_runs(
    settings: Settings, db_pool: DatabasePool, tmp_path: Path
) -> None:
    traced = _traced_catalog(settings, tmp_path)
    catalog = load_definition_catalog(traced)
    async with db_pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values (%s, %s)", ("forger", "f" * 64)
        )

    with pytest.raises(RecordValidationError) as refused:
        await insert_public_records(
            db_pool,
            workspace="forger",
            request=RecordBatchRequest(
                records=(
                    PublicRecordInput(
                        entity=ENTITY,
                        collection="agent_runs",
                        type="run_ok",
                        text="A forged run.",
                        content={"trace_id": "a" * 32},
                    ),
                )
            ),
            catalog=catalog,
            settings=traced,
        )

    assert refused.value.code == "reserved_collection"


def test_only_a_catalog_with_a_traced_agent_gets_the_built_in_collection(
    settings: Settings, tmp_path: Path
) -> None:
    plain = load_definition_catalog(
        settings.model_copy(update={"catalog_file": RENEWAL / "catalog.yaml"})
    )
    traced = load_definition_catalog(_traced_catalog(settings, tmp_path))

    assert ("agent_runs", 1) not in plain.collections
    assert ("agent_runs", 1) in traced.collections


def test_a_derivation_that_reads_run_records_cannot_run_a_traced_agent(
    settings: Settings, tmp_path: Path
) -> None:
    traced = _traced_catalog(settings, tmp_path)
    assert traced.catalog_file is not None
    path = traced.catalog_file.parent / "derivations"
    assessment = next(p for p in path.glob("*.yaml") if "renewal_assessment" in p.read_text())
    assessment.write_text(
        assessment.read_text()
        .replace(
            'trigger:\n  cron: {expr: "0 2 * * *", entities: dirty}\n',
            "trigger:\n  write:\n    collections: [agent_runs]\n",
        )
        .replace(
            "    collections: [renewal_evidence, contract_terms]\n",
            "    collections: [renewal_evidence, contract_terms, agent_runs]\n",
        )
    )

    with pytest.raises(DefinitionError, match="automatic derivation dependency cycle"):
        load_definition_catalog(traced)


def test_a_catalog_may_not_declare_its_own_agent_runs(settings: Settings, tmp_path: Path) -> None:
    traced = _traced_catalog(settings, tmp_path)
    assert traced.catalog_file is not None
    (traced.catalog_file.parent / "collections" / "zz_runs.yaml").write_text(
        "collections:\n"
        "  - name: agent_runs\n    version: 1\n    active: true\n    mode: event\n"
        "    schema: {type: object, required: [text], properties: {text: {type: string}}}\n"
        "    required_processors: []\n    search_profile: pg_default\n"
    )
    catalog = traced.catalog_file.read_text()
    traced.catalog_file.write_text(
        catalog.replace(
            "collections:\n", "collections:\n  agent_runs@1: collections/zz_runs.yaml\n", 1
        )
    )

    with pytest.raises(DefinitionError, match="built in when an Agent is traced"):
        load_definition_catalog(traced)
