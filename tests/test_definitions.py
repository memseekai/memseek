from __future__ import annotations

import math
from collections.abc import MutableMapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from memseek.config import Settings
from memseek.definitions import (
    CollectionDefinitionMismatch,
    DefinitionError,
    DefinitionSources,
    canonical_json,
    compile_definition_catalog,
    load_definition_catalog,
)
from memseek.definitions.models import CollectionDefinition
from memseek.definitions.yaml import load_yaml_file
from memseek.derive.schema import CurrentSource, PipelineDefinition
from memseek.search.rank import RankValidationError, validate_rank_expression
from memseek.templates import TemplateError, render_object, resolve_value


def test_reference_catalog_loads_deterministically_and_resolves(settings: Settings) -> None:
    first = load_definition_catalog(settings)
    second = load_definition_catalog(settings)

    assert first.catalog_hash == second.catalog_hash
    assert len(first.catalog_hash) == 64
    assert len(first.collections) == 12
    assert set(first.derivations) == {
        "belief_conflict",
        "contradiction",
        "harvest",
        "profile",
        "reconcile",
        "reflection",
        "skill",
        "worldview",
    }
    assert set(first.triggers) == {
        "belief_conflict.default",
        "contradiction.default",
        "harvest.default",
        "profile.default",
        "reconcile.default",
        "reflection.default",
        "skill.default",
        "worldview.default",
    }
    assert set(first.active_views) == {
        "agent_relevant_memory",
        "open_self_contradictions",
        "upcoming_calendar",
    }
    assert set(first.active_artifacts) == {"daily_agent_prompt", "maintained_skill"}

    main = first.resolve_collection("main")
    assert main.version == 1
    assert first.resolve_collection("main@1") is main
    assert first.resolve_view("upcoming_calendar").version == 1
    assert first.resolve_artifact("maintained_skill").lifecycle == "reviewed"
    skill = first.resolve_processor("skill")
    assert isinstance(skill, PipelineDefinition)
    assert [task.use for task in skill.tasks] == ["llm", "llm"]
    assert skill.emit.complete is True
    assert skill.emit.review == "required"
    profile = first.derivations["profile"]
    assert profile.driver.collection_versions == {"main": (1,)}
    current_profile = profile.sources["current_profile"]
    assert isinstance(current_profile, CurrentSource)
    assert current_profile.collection_versions == {"profiles": (1,)}
    assert first.resolve_trigger("skill.default").processor == "skill"
    assert first.deployment_bindings["reflections"] == "pg_default"
    assert len(first.processor_config_hashes["importance"]) == 64

    assert first.resolve_stored_collection("main", main.version, main.contract_hash) is main
    with pytest.raises(CollectionDefinitionMismatch) as mismatched:
        first.resolve_stored_collection("main", main.version, "0" * 64)
    assert mismatched.value.code == "collection_definition_mismatch"
    with pytest.raises(CollectionDefinitionMismatch) as missing:
        first.resolve_stored_collection("main", 999, main.contract_hash)
    assert missing.value.code == "collection_definition_missing"

    mutable = cast(MutableMapping[tuple[str, int], CollectionDefinition], first.collections)
    with pytest.raises(TypeError):
        mutable[("new", 1)] = main


def test_gbrain_catalog_is_a_separate_self_contained_package(gbrain_settings: Settings) -> None:
    catalog = load_definition_catalog(gbrain_settings)

    assert set(catalog.active_collections) == {
        "atoms",
        "concepts",
        "edges",
        "facts",
        "pages",
        "patterns",
        "syntheses",
        "takes",
        "transcripts",
    }
    assert set(catalog.derivations) == {
        "atom_extraction",
        "concept_synthesis",
        "consolidate",
        "enrich_thin",
        "fact_extraction",
        "link_extraction",
        "pattern_detection",
        "repair_synthesis",
    }
    graph_view = catalog.resolve_view("graph_query")
    assert graph_view.kind == "graph"
    assert graph_view.query is None
    assert graph_view.graph is not None
    assert graph_view.graph.edges == "edges"
    orphan_view = catalog.resolve_view("orphan_pages")
    assert orphan_view.kind == "graph_orphans"
    assert orphan_view.query is None
    assert orphan_view.graph is not None
    assert orphan_view.graph.nodes == "pages"
    assert catalog.resolve_artifact("gbrain_context").lifecycle == "live"
    package = catalog.resolve_package("gbrain", "0.13.0")
    assert package.collections == (
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
    assert package.mcp == "gbrain@1"
    interface = catalog.resolve_mcp(package.mcp)
    assert [tool.name for tool in interface.tools] == [
        "answer",
        "search_memory",
        "explore_graph",
        "find_orphan_pages",
        "context",
        "record",
    ]
    search = catalog.resolve_view("gbrain_search@1")
    assert search.parameters["query"].max_length == 8_192
    assert search.parameters["limit"].maximum == 50


def test_operational_override_changes_catalog_hash_not_collection_hash(
    tmp_path: Path, settings: Settings
) -> None:
    override = tmp_path / "profiles.yaml"
    override.write_text("collection_profiles:\n  reflections: memory_tpuf\n", encoding="utf-8")
    base = load_definition_catalog(settings)
    changed = load_definition_catalog(
        settings.model_copy(
            update={
                "search_profile_overrides_file": override,
                "turbopuffer_api_key": "test-key",
            }
        )
    )

    assert changed.deployment_bindings["reflections"] == "memory_tpuf"
    assert changed.catalog_hash != base.catalog_hash
    assert (
        changed.resolve_collection("reflections").definition_hash
        == base.resolve_collection("reflections").definition_hash
    )


def test_python_authored_sources_use_the_same_catalog_compiler(settings: Settings) -> None:
    base = load_definition_catalog(settings)
    source = DefinitionSources.from_catalog(base)
    profile = base.derivations["profile"]
    assert profile.trigger is not None
    assert profile.trigger.accumulator is not None
    trigger = profile.trigger.model_copy(
        update={"accumulator": profile.trigger.accumulator.model_copy(update={"threshold": 20})}
    )
    replacement = profile.model_copy(update={"trigger": trigger})
    source = replace(
        source,
        derivations=tuple(
            replacement if getattr(item, "name", None) == "profile" else item
            for item in source.derivations
        ),
    )

    compiled = compile_definition_catalog(settings, source)

    assert compiled.derivations["profile"].trigger is not None
    assert compiled.derivations["profile"].trigger.accumulator is not None
    assert compiled.derivations["profile"].trigger.accumulator.threshold == 20
    assert compiled.catalog_hash != base.catalog_hash


def test_duplicate_yaml_key_reports_file_and_code(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yaml"
    path.write_text("name: one\nname: two\n", encoding="utf-8")

    with pytest.raises(DefinitionError) as caught:
        load_yaml_file(path)

    assert caught.value.code == "yaml"
    assert caught.value.file == str(path)
    assert "duplicate key" in str(caught.value)


def test_unavailable_deployment_profile_is_rejected(tmp_path: Path, settings: Settings) -> None:
    override = tmp_path / "profiles.yaml"
    override.write_text("collection_profiles:\n  reflections: memory_tpuf\n", encoding="utf-8")

    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(
            settings.model_copy(update={"search_profile_overrides_file": override})
        )

    assert caught.value.code == "profile_unavailable"


def test_canonical_json_is_compact_sorted_utf8_and_finite() -> None:
    assert canonical_json({"z": "é", "a": [1, True]}) == b'{"a":[1,true],"z":"\xc3\xa9"}'

    with pytest.raises(DefinitionError, match="canonical_json"):
        canonical_json({"bad": math.nan})


def test_rank_grammar_checks_mode_depth_nodes_and_scorers() -> None:
    expression = ["sum", [["normalize", ["similarity"]], ["score", "importance"]]]
    assert (
        validate_rank_expression(expression, mode="vector", scorer_names={"importance"})[0] == "sum"
    )

    with pytest.raises(RankValidationError, match="not legal"):
        validate_rank_expression(["similarity"], mode="text")
    with pytest.raises(RankValidationError, match="unknown scorer"):
        validate_rank_expression(["score", "missing"], scorer_names={"importance"})
    with pytest.raises(RankValidationError, match="post-fusion"):
        validate_rank_expression(["text_match"], boost=True)


def test_template_renderer_preserves_exact_typed_values_and_rejects_missing() -> None:
    variables = {"qs": {"questions": ["one", "two"]}, "entity": "maria"}
    assert resolve_value("{{qs.questions}}", variables) == ["one", "two"]
    assert resolve_value("Questions: {{qs.questions}}", variables) == ('Questions: ["one","two"]')
    assert render_object({"entities": ["{{entity}}"]}, variables) == {"entities": ["maria"]}
    with pytest.raises(TemplateError, match="missing"):
        resolve_value("{{missing.path}}", variables)
