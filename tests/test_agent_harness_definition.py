"""An Agent's harness and a toolset's skill packs, as the catalog compiles them."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from memseek.config import Settings
from memseek.definitions import DefinitionError, load_definition_catalog

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RENEWAL_ROOT = REPOSITORY_ROOT / "examples" / "computer_renewal_catalog"


@pytest.fixture
def renewal_root(tmp_path: Path) -> Path:
    destination = tmp_path / "renewal"
    shutil.copytree(RENEWAL_ROOT, destination)
    return destination


def _settings(root: Path) -> Settings:
    return Settings(
        models_file=root / "conf/models.yaml",
        processors_file=root / "conf/processors.yaml",
        rank_default_file=root / "conf/rank_default.yaml",
        search_profiles_file=root / "conf/search_profiles.yaml",
        collections_dir=root / "collections",
        derivations_dir=root / "derivations",
        views_dir=None,
        triggers_dir=None,
        artifacts_dir=root / "artifacts",
        computers_dir=root / "computers",
        programs_dir=root / "programs",
        agents_dir=root / "agents",
        context_policies_dir=root / "context_policies",
        toolsets_dir=root / "toolsets",
        mcp_dir=root / "mcp",
        packages_dir=root / "packages",
        llm_fake=True,
    )


def _replace(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    assert old in source, f"test mutation marker not found in {path}: {old!r}"
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def test_definitions_without_a_harness_keep_their_published_hashes(renewal_root: Path) -> None:
    catalog = load_definition_catalog(_settings(renewal_root))

    assert {key: agent.definition_hash for key, agent in catalog.agents.items()} == {
        ("renewal_analyst", 1): "3bc007cad94722bb3264144c91c6d1485535040338b22518c3034e4f3ac52892",
        ("renewal_analyst", 2): "d96248fd39fe68ffb912832b8112f324dfcc4a89a02ed9a1234137dd411549e8",
    }
    assert catalog.toolsets[("renewal", 1)].definition_hash == (
        "234f33dbf0f04a0d8cb54153a48606db97475aa5f420ac37800d27d614bd7d57"
    )
    assert "harness" not in catalog.agents[("renewal_analyst", 2)].model_dump(mode="json")


def test_a_declared_harness_is_part_of_the_agent_identity(renewal_root: Path) -> None:
    agents = renewal_root / "agents" / "renewal_analyst.yaml"
    _replace(agents, "    toolset: renewal@1\n", "    toolset: renewal@1\n    harness: pi\n")

    agent = load_definition_catalog(_settings(renewal_root)).agents[("renewal_analyst", 2)]

    assert agent.model_dump(mode="json")["harness"] == "pi"
    assert agent.definition_hash != (
        "d96248fd39fe68ffb912832b8112f324dfcc4a89a02ed9a1234137dd411549e8"
    )


def test_skillpack_source_forbids_a_description(renewal_root: Path) -> None:
    _replace(
        renewal_root / "toolsets" / "renewal.yaml",
        "      - name: shell\n",
        "      - {name: browser, kind: skillpack, pack: browser-harness, description: d}\n"
        "      - name: shell\n",
    )

    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(_settings(renewal_root))

    assert caught.value.code == "schema"
    assert "skillpack tool source forbids description" in str(caught.value)


def _grant_browser_pack(root: Path) -> None:
    _replace(
        root / "toolsets" / "renewal.yaml",
        "      - name: shell\n",
        "      - {name: browser, kind: skillpack, pack: browser-harness}\n      - name: shell\n",
    )


def test_an_unknown_harness_is_a_reference_error(renewal_root: Path) -> None:
    agents = renewal_root / "agents" / "renewal_analyst.yaml"
    _replace(agents, "    toolset: renewal@1\n", "    toolset: renewal@1\n    harness: codex\n")

    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(_settings(renewal_root))

    assert caught.value.code == "reference"
    assert caught.value.path == "agents[1].harness"
    assert "agent names harness 'codex': no harness.yaml for 'codex'" in str(caught.value)


def test_an_unknown_skill_pack_is_a_reference_error(renewal_root: Path) -> None:
    _replace(
        renewal_root / "toolsets" / "renewal.yaml",
        "      - name: shell\n",
        "      - {name: browser, kind: skillpack, pack: nope}\n      - name: shell\n",
    )

    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(_settings(renewal_root))

    assert caught.value.code == "reference"
    assert caught.value.path == "sources[1].pack"


def test_a_pack_cannot_need_more_than_the_computer_allows(renewal_root: Path) -> None:
    _grant_browser_pack(renewal_root)

    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(_settings(renewal_root))

    assert caught.value.code == "computer_capability"
    assert str(caught.value).endswith(
        "skill pack 'browser-harness' needs ['network'], which computer "
        "'research_workspace@1' denies"
    )


def test_a_learning_pack_requires_the_learnings_writeback(renewal_root: Path) -> None:
    _grant_browser_pack(renewal_root)
    computers = renewal_root / "computers" / "workspaces.yaml"
    _replace(
        computers,
        "      fallback_requires: explicit_policy\n"
        "    capabilities: {filesystem: true, exec: true, network: false}",
        "      fallback_requires: explicit_policy\n"
        "    capabilities: {filesystem: true, exec: true, network: true}",
    )

    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(_settings(renewal_root))

    assert caught.value.code == "computer_capability"
    assert str(caught.value).endswith(
        "skill pack 'browser-harness' learns, so computer 'research_workspace@1' must declare "
        "/outbox/learnings.jsonl as an observations writeback"
    )

    _replace(
        computers,
        "      - {path: /outbox/observations.jsonl,",
        "      - {path: /outbox/learnings.jsonl, type: observations, review: false, "
        "collection: task_observations@1, record_type: observation}\n"
        "      - {path: /outbox/observations.jsonl,",
    )
    toolset = load_definition_catalog(_settings(renewal_root)).toolsets[("renewal", 1)]
    assert toolset.sources[1].model_dump(mode="json") == {
        "name": "browser",
        "kind": "skillpack",
        "pack": "browser-harness",
    }
