"""What one Agent run is given, and what each file it is given is for."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import load_definition_catalog
from memseek.materialization import build_agent_materialization

RENEWAL_ROOT = Path(__file__).resolve().parents[1] / "examples" / "computer_renewal_catalog"
WORKSPACE = "materialization-demo"


@pytest.fixture
def renewal_root(tmp_path: Path) -> Path:
    destination = tmp_path / "renewal"
    shutil.copytree(RENEWAL_ROOT, destination)
    return destination


def _settings(settings: Settings, root: Path) -> Settings:
    return settings.model_copy(
        update={
            "models_file": root / "conf/models.yaml",
            "processors_file": root / "conf/processors.yaml",
            "collections_dir": root / "collections",
            "derivations_dir": root / "derivations",
            "views_dir": None,
            "triggers_dir": None,
            "artifacts_dir": root / "artifacts",
            "computers_dir": root / "computers",
            "programs_dir": root / "programs",
            "agents_dir": root / "agents",
            "context_policies_dir": root / "context_policies",
            "toolsets_dir": root / "toolsets",
            "mcp_dir": root / "mcp",
            "packages_dir": root / "packages",
            "search_profiles_file": root / "conf/search_profiles.yaml",
            "rank_default_file": root / "conf/rank_default.yaml",
        }
    )


def _replace(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    assert old in source, f"test mutation marker not found in {path}: {old!r}"
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def _make_legacy(root: Path) -> None:
    """Rewind the fixture to a catalog published before toolsets existed."""

    (root / "toolsets" / "renewal.yaml").unlink()
    agents = root / "agents/renewal_analyst.yaml"
    document = agents.read_text(encoding="utf-8")
    agents.write_text(
        document[: document.index("  # Version 2")].replace(
            "    version: 1\n    active: false", "    version: 1\n    active: true", 1
        ),
        encoding="utf-8",
    )
    _replace(
        root / "artifacts/renewal_agent.yaml",
        "    description: >-\n      Work a renewal file evidence-first: keep a risk table in the workspace,\n"
        "      recall promises that have left working context, and cite original record\n"
        "      IDs. Load this before assessing an account.\n",
        "",
    )
    _replace(
        root / "packages/renewal.yaml",
        "agents: [renewal_analyst@1, renewal_analyst@2]\ntoolsets: [renewal@1]\n",
        "agents: [renewal_analyst@1]\n",
    )
    _replace(root / "mcp/renewal.yaml", "renewal_analyst@2", "renewal_analyst@1")
    _replace(root / "derivations/renewal_assessment.yaml", "renewal_analyst@2", "renewal_analyst@1")


async def _materialize(
    pool: DatabasePool,
    settings: Settings,
    root: Path,
    *,
    agent: str = "renewal_analyst@1",
):
    async with pool.connection() as conn:
        await conn.execute(
            "insert into workspace (id, api_key_hash) values (%s, %s) on conflict do nothing",
            (WORKSPACE, "e" * 64),
        )
    demo = _settings(settings, root)
    return await build_agent_materialization(
        pool,
        workspace=WORKSPACE,
        entity="acme",
        computer_ref="research_workspace@1",
        agent_ref=agent,
        catalog=load_definition_catalog(demo),
        settings=demo,
    )


async def test_a_skill_without_a_description_stays_inline(
    settings: Settings, db_pool: DatabasePool, renewal_root: Path
) -> None:
    """Published catalogs do not change shape until they opt in.

    Progressive disclosure needs something to offer. Without a description there
    is nothing to put in the prompt, so the skill is inlined exactly as it was
    before the mechanism existed rather than offered blind.
    """

    _make_legacy(renewal_root)
    materialized = await _materialize(db_pool, settings, renewal_root)
    roles = {item.path: item.role for item in materialized.files}
    assert roles["/.memseek/skills/renewal-research-skill.md"] == "reference"
    assert not any(role == "skill" for role in roles.values())


async def test_a_described_skill_is_offered_and_held_back(
    settings: Settings, db_pool: DatabasePool, renewal_root: Path
) -> None:
    materialized = await _materialize(db_pool, settings, renewal_root, agent="renewal_analyst@2")
    path = "/.memseek/skills/renewal-research/SKILL.md"
    described = {item.path: item for item in materialized.files}[path]
    assert described.role == "skill"
    assert described.description is not None
    assert described.description.startswith("Work a renewal file evidence-first")

    body = materialized.context_files[path]
    assert body.startswith("---\nname: renewal-research\n")
    assert "description: Work a renewal file evidence-first" in body
    # The offer is in the descriptor too, so the runtime never has to parse the
    # file to know what to put in the prompt — but the bytes remain the source
    # of truth, because the bytes are what gets hashed.
    descriptor = materialized.descriptor_json()
    entry = next(item for item in descriptor["files"] if item["path"] == path)
    assert entry["skill"]["name"] == "renewal-research"
    assert entry["skill"]["description"] == described.description


async def test_an_artifact_the_agent_declares_is_not_mounted_twice(
    settings: Settings, db_pool: DatabasePool, renewal_root: Path
) -> None:
    """A Computer mount of an artifact the Agent already declares costs one file.

    The renewal catalog used to mount both of the Agent's own artifacts, which
    put the Agent's instructions in its own prompt twice.
    """

    _replace(
        renewal_root / "computers/workspaces.yaml",
        "    provider: fake\n    # No `context:`.",
        "    provider: fake\n"
        "    context:\n"
        "      - {path: /.memseek/context.md, artifact: renewal_instructions@1, mode: read_only}\n"
        "    # No `context:`.",
    )
    materialized = await _materialize(db_pool, settings, renewal_root)
    instructions = [
        path
        for path, body in materialized.context_files.items()
        if "Analyze renewal evidence" in body
    ]
    assert instructions == ["/.memseek/instructions.md"]


async def test_the_run_carries_its_tool_surface_and_its_authority(
    settings: Settings, db_pool: DatabasePool, renewal_root: Path
) -> None:
    materialized = await _materialize(db_pool, settings, renewal_root)
    toolset = materialized.toolset_json()
    # This Agent uses the legacy spelling, so it declares no toolset of its own
    # and the surface is the desugared equivalent.
    assert toolset["ref"] is None
    assert [tool["kind"] for tool in toolset["tools"]] == [
        "filesystem",
        "exec",
        "recall",
        "skill",
        "writeback",
        "writeback",
    ]

    manifest = json.loads(materialized.context_files["/.memseek/manifest.json"])
    assert manifest["entity"] == "acme"
    assert manifest["toolset"] is None
    schemas = json.loads(materialized.context_files["/.memseek/writeback-schemas.json"])
    assert {item["path"] for item in schemas} == {
        "/outbox/observations.jsonl",
        "/outbox/proposals",
    }
    # Citation authority is exactly what the rendered context retrieved.
    assert materialized.citation_ids == frozenset()
