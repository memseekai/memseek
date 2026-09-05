"""Contract checks for the Codex-first workspace-wiki example catalog."""

from __future__ import annotations

from pathlib import Path

from memseek.config import Settings
from memseek.definitions import load_definition_catalog
from memseek.derive.schema import PipelineDefinition
from memseek.tools import tool_definitions_payload

_CATALOG_ROOT = Path(__file__).parents[1] / "examples" / "workspace_wiki_catalog"


def _example_settings(tmp_path: Path) -> Settings:
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


def test_workspace_wiki_is_codex_first_and_bounded(tmp_path: Path) -> None:
    settings = _example_settings(tmp_path)
    catalog = load_definition_catalog(settings)

    reports = catalog.resolve_collection("session_reports")
    pages = catalog.resolve_collection("pages")
    assert reports.mode == "event"
    assert reports.content_schema["properties"]["text"]["maxLength"] == 12_000
    assert pages.mode == "keyed"
    assert pages.content_schema["properties"]["text"]["maxLength"] == 2_000
    assert pages.content_schema["properties"]["tags"]["maxItems"] == 8

    maintain = catalog.derivations["maintain_pages_v1"]
    assert isinstance(maintain, PipelineDefinition)
    assert maintain.driver.kind == "changes"
    assert maintain.driver.collections == ("session_reports",)
    assert maintain.driver.max_records == 4
    assert maintain.driver.max_tokens == 9_000
    assert maintain.sources["current_pages"].max_records == 48
    assert maintain.sources["current_pages"].max_tokens == 28_000
    assert maintain.tasks[0].config["max_tokens"] == 4_000
    maintain_prompt_reserve = (
        maintain.driver.max_tokens
        + maintain.sources["current_pages"].max_tokens
        + maintain.tasks[0].config["max_tokens"]
    )
    assert maintain_prompt_reserve <= settings.max_prompt_tokens - 5_000
    assert maintain.emit.dynamic_keys is True
    assert maintain.emit.max_active_keys == 48
    assert maintain.emit.max_records == 3

    audit = catalog.derivations["audit_pages_v1"]
    assert isinstance(audit, PipelineDefinition)
    assert audit.driver.kind == "snapshot"
    assert audit.driver.collections == ("session_reports",)
    assert audit.trigger is not None
    assert audit.trigger.accumulator is not None
    assert audit.trigger.accumulator.threshold == 40
    assert audit.sources["current_pages"].max_records == 48
    assert audit.sources["current_pages"].max_tokens == 28_000
    assert audit.tasks[1].config["max_tokens"] == 14_000
    audit_prompt_reserve = (
        audit.sources["current_pages"].max_tokens + audit.tasks[1].config["max_tokens"]
    )
    assert audit_prompt_reserve <= settings.max_prompt_tokens - 5_000
    assert audit.emit.max_active_keys == 48
    assert audit.emit.max_records == 4

    package = catalog.resolve_package("workspace_wiki", "0.1.0")
    assert package.collections == ("session_reports@1", "pages@1")
    assert "build_episodes_v1" not in package.processors
    assert package.mcp == "workspace_wiki@1"


def test_workspace_wiki_exposes_a_narrow_codex_mcp_interface(tmp_path: Path) -> None:
    settings = _example_settings(tmp_path)
    catalog = load_definition_catalog(settings)
    package = catalog.resolve_package("workspace_wiki", "0.1.0")
    discovery = tool_definitions_payload(settings, catalog=catalog, package=package)

    assert [tool["name"] for tool in discovery["tools"]] == [
        "wiki_context",
        "search_wiki",
        "read_record",
        "answer_from_wiki",
        "capture_session",
    ]
    search = next(tool for tool in discovery["tools"] if tool["name"] == "search_wiki")
    assert search["input_schema"]["properties"]["limit"]["maximum"] == 30

    capture = next(tool for tool in discovery["tools"] if tool["name"] == "capture_session")
    assert capture["kind"] == "ingest"
    assert capture["binding"]["reference"] == "session_reports@1"
    schema = capture["input_schema"]
    assert schema["required"] == ["entity", "type", "content"]
    assert schema["properties"]["content"]["properties"]["source"]["enum"] == ["codex"]
    assert schema["properties"]["content"]["properties"]["text"]["maxLength"] == 12_000
    assert "collection" not in schema["properties"]
