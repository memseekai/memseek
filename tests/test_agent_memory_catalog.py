"""Contract checks for the standalone L0-L3 agent-memory example catalog."""

from __future__ import annotations

from pathlib import Path

from memseek.config import Settings
from memseek.definitions import load_definition_catalog
from memseek.derive.schema import PipelineDefinition

_CATALOG_ROOT = Path(__file__).parents[1] / "examples" / "agent_memory_catalog"


def _example_settings(tmp_path: Path) -> Settings:
    """Load only the example catalog, with an explicitly empty trigger directory."""

    triggers = tmp_path / "triggers"
    triggers.mkdir()
    return Settings(
        llm_fake=True,
        models_file=_CATALOG_ROOT / "conf/models.yaml",
        processors_file=_CATALOG_ROOT / "conf/processors.yaml",
        rank_default_file=_CATALOG_ROOT / "conf/rank_default.yaml",
        search_profiles_file=_CATALOG_ROOT / "conf/search_profiles.yaml",
        collections_dir=_CATALOG_ROOT / "collections",
        derivations_dir=_CATALOG_ROOT / "derivations",
        triggers_dir=triggers,
        views_dir=_CATALOG_ROOT / "views",
        artifacts_dir=_CATALOG_ROOT / "artifacts",
        mcp_dir=_CATALOG_ROOT / "mcp",
        packages_dir=_CATALOG_ROOT / "packages",
    )


def test_agent_memory_example_uses_independent_scene_blocks_and_l2_l3_promotion(
    tmp_path: Path,
) -> None:
    catalog = load_definition_catalog(_example_settings(tmp_path))

    scenes = catalog.resolve_collection("scenes")
    assert scenes.version == 3
    assert catalog.resolve_collection("scenes@1").active is False
    assert catalog.resolve_collection("scenes@2").active is False
    assert set(scenes.content_schema["required"]) == {
        "text",
        "created",
        "updated",
        "summary",
        "heat",
    }

    synthesis = catalog.derivations["scene_synthesis"]
    assert isinstance(synthesis, PipelineDefinition)
    assert synthesis.trigger is not None
    assert synthesis.trigger.write is not None
    assert synthesis.trigger.write.collections == ("memories",)
    assert synthesis.emit.dynamic_keys is True
    assert synthesis.emit.max_active_keys == 15
    assert synthesis.emit.keys == ()
    assert synthesis.sources["current_scenes"].max_records == 15

    persona = catalog.derivations["persona"]
    assert isinstance(persona, PipelineDefinition)
    assert persona.trigger is not None
    assert persona.trigger.write is None
    assert persona.trigger.changed is not None
    assert persona.trigger.changed.collections == ("scenes",)
    assert persona.sources["changed_scenes"].kind == "changes"
    assert persona.sources["current_scenarios"].kind == "current"

    package = catalog.resolve_package("agent_memory", "0.3.0")
    assert package.collections.count("scenes@1") == 1
    assert package.collections.count("scenes@2") == 1
    assert package.collections.count("scenes@3") == 1
