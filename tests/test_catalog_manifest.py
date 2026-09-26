"""The root file is a complete, validated table of contents."""

from __future__ import annotations

import json
import runpy
import shutil
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from memseek.cli import main
from memseek.config import Settings
from memseek.definitions import DefinitionError, load_definition_catalog
from memseek.definitions.manifest import (
    CatalogManifest,
    compile_catalog_files,
    discover_catalog,
    read_catalog_files,
    resolve_sources,
)
from memseek.sdk import _read_catalog_directory
from memseek.workspace_catalog import _compile_overlay, _normalize_files

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "examples/site_scrape_catalog"
CRM = ROOT / "examples/crm_profile_catalog"


@pytest.fixture
def files() -> dict[str, str]:
    return read_catalog_files(SITE)


def edit_manifest(files: dict[str, str], edit) -> None:
    manifest = yaml.safe_load(files["catalog.yaml"])
    edit(manifest)
    files["catalog.yaml"] = yaml.safe_dump(manifest, sort_keys=False)


def test_checked_in_schemas_match_the_generator() -> None:
    generator = runpy.run_path(str(ROOT / "scripts/generate_catalog_schemas.py"))
    expected = generator["schemas"]()
    on_disk = {
        path.name.removesuffix(".schema.json"): json.loads(path.read_text())
        for path in (ROOT / "schemas").glob("*.schema.json")
    }
    assert on_disk == expected


def test_all_shipped_manifests_match_the_generated_schema() -> None:
    schema = json.loads((ROOT / "schemas/catalog.schema.json").read_text())
    assert schema == {
        **CatalogManifest.model_json_schema(),
        "$schema": "https://json-schema.org/draft/2020-12/schema",
    }
    validator = Draft202012Validator(schema)
    for root in [ROOT / "resources", *sorted((ROOT / "examples").glob("*_catalog"))]:
        validator.validate(yaml.safe_load((root / "catalog.yaml").read_text()))
        catalog = compile_catalog_files(Settings(llm_fake=True), read_catalog_files(root))
        assert len(catalog.packages) == 1


def test_filesystem_sdk_and_upload_compile_the_same_release(files, bare_settings) -> None:
    disk = load_definition_catalog(
        bare_settings.model_copy(update={"catalog_file": SITE / "catalog.yaml"})
    )
    upload = _compile_overlay(bare_settings, _normalize_files(files))
    assert _read_catalog_directory(SITE) == files
    assert disk.catalog_hash == upload.catalog_hash
    assert {item["reference"] for item in disk.source_locations} >= {
        "site_scraper@1",
        "scraper_instructions@1",
    }


def test_source_move_and_root_reordering_do_not_change_hash(files, bare_settings) -> None:
    before = compile_catalog_files(bare_settings, files)
    files["feature/deep/instructions.yaml"] = files.pop("scraping/instructions.yaml")
    edit_manifest(
        files,
        lambda m: m["artifacts"].update(
            {"scraper_instructions@1": "feature/deep/instructions.yaml"}
        ),
    )
    edit_manifest(
        files, lambda m: m.update(collections=dict(reversed(list(m["collections"].items()))))
    )
    after = compile_catalog_files(bare_settings, files)
    assert after.catalog_hash == before.catalog_hash
    assert (
        next(
            item for item in after.source_locations if item["reference"] == "scraper_instructions@1"
        )["file"]
        == "feature/deep/instructions.yaml"
    )


def test_undeclared_files_are_not_read(tmp_path, bare_settings) -> None:
    root = tmp_path / "catalog"
    shutil.copytree(SITE, root)
    (root / "unrelated.yaml").write_text("broken: [yaml")
    files = read_catalog_files(root)
    assert "unrelated.yaml" not in files
    files["also_unrelated.yaml"] = "broken: [yaml"
    compile_catalog_files(bare_settings, files)


def test_grouped_file_requires_every_definition_in_the_root(files) -> None:
    edit_manifest(files, lambda m: m["views"].pop("site_learnings@1"))
    with pytest.raises(DefinitionError, match="list 'site_learnings@1'") as error:
        resolve_sources(files)
    assert error.value.file == "scraping/views.yaml"
    assert error.value.line is not None
    assert error.value.line > 1


def test_wrong_reference_points_at_the_root_entry(files) -> None:
    edit_manifest(
        files, lambda m: m["views"].update({"curent_task@1": m["views"].pop("current_task@1")})
    )
    with pytest.raises(DefinitionError, match="did you mean 'current_task@1'") as error:
        resolve_sources(files)
    assert error.value.file == "catalog.yaml"
    assert error.value.path == "views.curent_task@1"
    assert error.value.line is not None
    assert error.value.line > 1


@pytest.mark.parametrize(
    "path",
    [
        "../outside.yaml",
        "/tmp/outside.yaml",
        "scraping/../../outside.yaml",
        "scraping\\agent.yaml",
        "catalog.yaml",
        "agent.txt",
    ],
)
def test_unsafe_source_paths_are_rejected(files, path) -> None:
    edit_manifest(files, lambda m: m["agents"].update({"site_scraper@1": path}))
    with pytest.raises(DefinitionError) as error:
        resolve_sources(files)
    assert error.value.code == "file_path"


def test_symlink_cannot_escape_the_root(tmp_path) -> None:
    root = tmp_path / "catalog"
    shutil.copytree(SITE, root)
    source = root / "scraping/agent.yaml"
    outside = tmp_path / "outside.yaml"
    source.rename(outside)
    source.symlink_to(outside)
    with pytest.raises(DefinitionError, match="escapes catalog root"):
        read_catalog_files(root)


def test_missing_file_reports_its_manifest_owner(files) -> None:
    del files["scraping/agent.yaml"]
    with pytest.raises(DefinitionError) as error:
        resolve_sources(files)
    assert error.value.code == "file_missing"
    assert error.value.file == "catalog.yaml"
    assert error.value.path == "agents"


def test_duplicate_yaml_keys_are_never_silently_overwritten(files) -> None:
    files["catalog.yaml"] += "\nname: another_name\n"
    with pytest.raises(DefinitionError) as error:
        resolve_sources(files)
    assert error.value.code == "yaml"


def test_derivations_own_generated_processors_and_inline_triggers(bare_settings) -> None:
    catalog = compile_catalog_files(bare_settings, read_catalog_files(CRM))
    package = next(iter(catalog.packages.values()))
    assert {"crm_profile", "crm_summary", "crm_profile_rebuild"} <= set(package.processors)
    assert {"crm_profile.default", "crm_summary.default"} <= set(package.triggers)
    assert "crm_profile_rebuild.default" not in package.triggers
    locations = {
        (item["kind"], item["reference"]): item["file"] for item in catalog.source_locations
    }
    assert locations["derivations", "crm_profile"] == locations["triggers", "crm_profile.default"]


def test_legacy_bundle_requires_republishing(files) -> None:
    del files["catalog.yaml"]
    with pytest.raises(DefinitionError, match="migrate and republish"):
        resolve_sources(files)


@pytest.mark.parametrize("field", ["collections_dir", "packages_dir", "processors_file"])
def test_directory_settings_without_a_manifest_are_refused(field, tmp_path) -> None:
    settings = Settings(llm_fake=True, **{field: tmp_path})
    with pytest.raises(DefinitionError, match=r"set catalog_file to catalog\.yaml"):
        load_definition_catalog(settings)


def test_catalog_file_must_be_named_catalog_yaml(tmp_path) -> None:
    entry = tmp_path / "memory.yaml"
    shutil.copy2(SITE / "catalog.yaml", entry)
    with pytest.raises(DefinitionError, match=r"entry point must be named catalog\.yaml"):
        load_definition_catalog(Settings(llm_fake=True, catalog_file=entry))


def test_discovery_and_cli_navigation(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "catalog"
    shutil.copytree(SITE, root)
    monkeypatch.chdir(root / "scraping")
    assert discover_catalog() == root
    assert main(["catalog-validate", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert main(["catalog-locate", "scraper_instructions@1", "--json"]) == 0
    location = json.loads(capsys.readouterr().out)
    assert location["file"] == "scraping/instructions.yaml"
    assert (
        "name: scraper_instructions"
        in (root / location["file"]).read_text().splitlines()[location["line"] - 1]
    )


def test_cli_errors_are_structured_and_nonzero(tmp_path, capsys) -> None:
    assert main(["catalog-validate", "--dir", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "catalog_file"
