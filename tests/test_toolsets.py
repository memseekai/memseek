"""Validation matrix for declared Agent tool surfaces.

Every case enters through ``load_definition_catalog`` so it exercises the same
compilation a service performs at startup, not the models in isolation.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from reference_catalog import materialize_reference_catalog

from memseek.config import Settings
from memseek.definitions import DefinitionError, load_definition_catalog
from memseek.definitions.toolsets import effective_toolset

RENEWAL_ROOT = Path(__file__).resolve().parents[1] / "examples" / "computer_renewal_catalog"


@pytest.fixture
def reference_root(tmp_path: Path) -> Path:
    return materialize_reference_catalog(tmp_path / "catalog")


@pytest.fixture
def renewal_root(tmp_path: Path) -> Path:
    destination = tmp_path / "renewal"
    shutil.copytree(RENEWAL_ROOT, destination)
    return destination


def _reference_settings(root: Path) -> Settings:
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
        toolsets_dir=root / "toolsets",
        llm_fake=True,
    )


def _renewal_settings(root: Path) -> Settings:
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


def _write_toolset(root: Path, document: str) -> None:
    (root / "toolsets").mkdir(exist_ok=True)
    (root / "toolsets" / "test.yaml").write_text(document, encoding="utf-8")


def _replace(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    assert old in source, f"test mutation marker not found in {path}: {old!r}"
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def _error(settings: Settings, *, code: str) -> DefinitionError:
    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(settings)
    assert caught.value.code == code, caught.value
    return caught.value


# --- source shape ---------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "detail"),
    [
        pytest.param(
            "{name: s, kind: skill, artifact: maintained_skill@1, view: upcoming_calendar@1}",
            "skill tool source forbids view",
            id="skill_forbids_view",
        ),
        pytest.param(
            "{name: v, kind: view, description: d, view: upcoming_calendar@1, path: /outbox/x}",
            "view tool source forbids path",
            id="view_forbids_path",
        ),
        pytest.param(
            "{name: w, kind: writeback, description: d, path: /workspace/notes.jsonl}",
            "writeback tool sources must name a path below /outbox",
            id="writeback_outside_outbox",
        ),
        pytest.param(
            "{name: m, kind: mcp_server, description: d, url: 'http://evil.example/mcp'}",
            "base_url must use HTTPS except on localhost",
            id="mcp_requires_https",
        ),
        pytest.param(
            "{name: f, kind: filesystem, description: d}",
            "filesystem tool source requires modes",
            id="filesystem_requires_modes",
        ),
        pytest.param(
            "{name: r, kind: recall}",
            "recall tool source requires description",
            id="description_required",
        ),
    ],
)
def test_source_shape_is_enforced_per_kind(reference_root: Path, source: str, detail: str) -> None:
    _write_toolset(
        reference_root, f"toolsets:\n  - name: t\n    version: 1\n    sources: [{source}]\n"
    )
    error = _error(_reference_settings(reference_root), code="schema")
    assert detail in str(error)


def test_forward_compatible_modes_are_declared_but_refused(reference_root: Path) -> None:
    """The knobs the host tool channel will use are not silently accepted."""

    _write_toolset(
        reference_root,
        "toolsets:\n  - name: t\n    version: 1\n    sources:\n"
        "      - {name: v, kind: view, description: d, view: upcoming_calendar@1, mode: live}\n",
    )
    assert "requires the host tool channel" in str(
        _error(_reference_settings(reference_root), code="schema")
    )


# --- catalog targets ------------------------------------------------------


def test_view_source_must_name_the_active_version(reference_root: Path) -> None:
    _write_toolset(
        reference_root,
        "toolsets:\n  - name: t\n    version: 1\n    sources:\n"
        "      - {name: v, kind: view, description: d, view: upcoming_calendar@7}\n",
    )
    assert "unknown view" in str(_error(_reference_settings(reference_root), code="reference"))


def test_view_source_arguments_must_satisfy_the_view(reference_root: Path) -> None:
    _write_toolset(
        reference_root,
        "toolsets:\n  - name: t\n    version: 1\n    sources:\n"
        "      - name: v\n        kind: view\n        description: d\n"
        "        view: upcoming_calendar@1\n        arguments: {start: 'not-a-time'}\n",
    )
    # `end` is required and unsupplied, so the surface is under-specified before
    # the type of `start` is even reached.
    assert "missing=['end']" in str(_error(_reference_settings(reference_root), code="view_args"))


def test_skill_source_requires_a_description_on_the_artifact(reference_root: Path) -> None:
    _write_toolset(
        reference_root,
        "toolsets:\n  - name: t\n    version: 1\n    sources:\n"
        "      - {name: s, kind: skill, artifact: maintained_skill@1}\n",
    )
    error = _error(_reference_settings(reference_root), code="skill_description")
    assert "before its body is loaded" in str(error)


def test_skill_source_rejects_a_prompt_artifact(reference_root: Path) -> None:
    _write_toolset(
        reference_root,
        "toolsets:\n  - name: t\n    version: 1\n    sources:\n"
        "      - {name: s, kind: skill, artifact: daily_agent_prompt@1}\n",
    )
    assert "is not a skill artifact" in str(
        _error(_reference_settings(reference_root), code="reference")
    )


def test_toolset_source_names_must_be_unique(reference_root: Path) -> None:
    _write_toolset(
        reference_root,
        "toolsets:\n  - name: t\n    version: 1\n    sources:\n"
        "      - {name: dup, kind: recall, description: d}\n"
        "      - {name: dup, kind: filesystem, description: d, modes: [read]}\n",
    )
    assert "toolset source names" in str(_error(_reference_settings(reference_root), code="schema"))


# --- agent binding --------------------------------------------------------


def _add_source(root: Path, source: str) -> None:
    """Append one more source to the fixture's own toolset."""

    path = root / "toolsets" / "renewal.yaml"
    path.write_text(path.read_text(encoding="utf-8") + source, encoding="utf-8")


def test_agent_binds_a_declared_toolset(renewal_root: Path) -> None:
    catalog = load_definition_catalog(_renewal_settings(renewal_root))
    agent = catalog.resolve_agent("renewal_analyst@2")
    assert agent.toolset == "renewal@1"
    toolset = effective_toolset(agent, catalog.resolve_computer("research_workspace@1"), catalog)
    assert [source.kind for source in toolset.sources] == [
        "filesystem",
        "exec",
        "recall",
        "skill",
        "writeback",
        "writeback",
    ]
    # A declared surface may grant less than the Computer allows: no `delete`.
    assert toolset.sources[0].modes == ("read", "ls", "find", "grep", "write", "edit")


def test_toolset_writeback_must_be_declared_by_every_computer(renewal_root: Path) -> None:
    _add_source(
        renewal_root,
        "      - {name: notes, kind: writeback, description: d, path: /outbox/notes.jsonl}\n",
    )
    assert "is not declared by computer" in str(
        _error(_renewal_settings(renewal_root), code="computer_capability")
    )


def test_toolset_cannot_widen_computer_capabilities(renewal_root: Path) -> None:
    _add_source(
        renewal_root,
        "      - {name: mcp, kind: mcp_server, description: d, url: 'https://tools.example/mcp'}\n",
    )
    assert "needs the network capability" in str(
        _error(_renewal_settings(renewal_root), code="computer_capability")
    )


def test_package_must_declare_what_a_bound_toolset_reaches(renewal_root: Path) -> None:
    _replace(
        renewal_root / "packages/renewal.yaml",
        "artifacts: [renewal_instructions@1, renewal_research_skill@1]\n",
        "artifacts: [renewal_instructions@1]\n",
    )
    assert "omitted from its package" in str(
        _error(_renewal_settings(renewal_root), code="package_dependency")
    )


# --- the legacy spelling --------------------------------------------------


def test_legacy_tools_desugar_to_the_same_shape(renewal_root: Path) -> None:
    """An Agent that never heard of toolsets still resolves to one surface."""

    catalog = load_definition_catalog(_renewal_settings(renewal_root))
    agent = catalog.resolve_agent("renewal_analyst@1")
    assert agent.toolset is None
    toolset = effective_toolset(agent, catalog.resolve_computer("research_workspace@1"), catalog)
    kinds = [(source.kind, source.name) for source in toolset.sources]
    assert kinds == [
        ("filesystem", "filesystem"),
        ("exec", "shell"),
        ("recall", "recall"),
        ("skill", "renewal_research_skill"),
        ("writeback", "observations"),
        ("writeback", "proposals"),
    ]
    # `final_result` is the invocation's output path, not a tool the Agent calls.
    assert all(source.path != "/outbox/final-result.json" for source in toolset.sources)


def test_declaring_both_spellings_is_refused(renewal_root: Path) -> None:
    _replace(
        renewal_root / "agents/renewal_analyst.yaml",
        "    tools: [computer, recall]\n",
        "    tools: [computer, recall]\n    toolset: renewal@1\n",
    )
    assert "exclusive with" in str(_error(_renewal_settings(renewal_root), code="schema"))
