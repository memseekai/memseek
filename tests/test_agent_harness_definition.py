"""An Agent's harness and a toolset's skill packs, as the catalog compiles them."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from reference_catalog import declare_test_sources
from site_scrape_fixture import site_scrape_settings

from memseek.config import Settings
from memseek.definitions import DefinitionError, load_definition_catalog
from memseek.definitions.loader import DefinitionSources

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RENEWAL_ROOT = REPOSITORY_ROOT / "examples" / "computer_renewal_catalog"


@pytest.fixture
def renewal_root(tmp_path: Path) -> Path:
    destination = tmp_path / "renewal"
    shutil.copytree(RENEWAL_ROOT, destination)
    return destination


def _settings(root: Path) -> Settings:
    return Settings(catalog_file=root / "catalog.yaml", llm_fake=True)


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


def _opt_into_learning(root: Path, research_learning: str = "true") -> None:
    """A pack and a catalog skill both learn; nothing else in the catalog changes."""

    toolsets = root / "toolsets" / "renewal.yaml"
    _replace(
        toolsets,
        "      - name: shell\n",
        "      - {name: browser, kind: skillpack, pack: browser-harness, learning: true}\n"
        "      - name: shell\n",
    )
    _replace(
        toolsets,
        "        skill_name: renewal-research\n",
        f"        skill_name: renewal-research\n        learning: {research_learning}\n",
    )
    _replace(
        root / "computers" / "workspaces.yaml",
        "      fallback_requires: explicit_policy\n"
        "    capabilities: {filesystem: true, exec: true, network: false}",
        "      fallback_requires: explicit_policy\n"
        "    capabilities: {filesystem: true, exec: true, network: true}",
    )


def test_a_skill_that_learns_brings_the_lessons_collection_with_it(renewal_root: Path) -> None:
    without = load_definition_catalog(_settings(renewal_root))
    _opt_into_learning(renewal_root)

    catalog = load_definition_catalog(_settings(renewal_root))

    assert ("lessons", 1) not in without.collections
    lessons = catalog.collections[("lessons", 1)]
    assert set(lessons.content_schema["properties"]) == {"text", "skill", "kind", "detail", "code"}
    assert "lessons@1" in catalog.packages[("computer_renewal_demo", "1.0.0")].collections


def test_the_built_in_lessons_collection_is_not_carried_as_an_input(tmp_path: Path) -> None:
    catalog = load_definition_catalog(site_scrape_settings(Settings(llm_fake=True), tmp_path))

    sources = DefinitionSources.from_catalog(catalog)

    # Compiling these sources synthesizes it again; carrying it would duplicate it.
    assert ("lessons", 1) in catalog.collections
    assert {getattr(collection, "name", None) for collection in sources.collections} == {
        "scrape_tasks",
        "skill_eval_runs",
    }


def test_require_must_name_a_kind_the_skill_declares(renewal_root: Path) -> None:
    _opt_into_learning(renewal_root, research_learning="{require: [quote]}")

    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(_settings(renewal_root))

    assert caught.value.code == "reference"
    assert str(caught.value).endswith(
        "toolset source 'renewal_research' learns with require or code naming ['quote'], "
        "which its lessons do not declare as kinds"
    )


def test_a_catalog_cannot_shadow_the_built_in_lessons_collection(renewal_root: Path) -> None:
    _opt_into_learning(renewal_root)
    collections = next((renewal_root / "collections").glob("*.yaml"))
    collections.write_text(
        collections.read_text()
        + "\n  - {name: lessons, version: 1, mode: event, schema: {type: object}, "
        "search_profile: pg_default}\n"
    )

    declare_test_sources(renewal_root, collections.relative_to(renewal_root).as_posix())
    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(_settings(renewal_root))

    assert caught.value.code == "duplicate"
    assert "collection 'lessons' is built in when a skill learns; rename this one" in str(
        caught.value
    )
