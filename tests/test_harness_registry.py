"""Harness modules are found by directory and parsed at the boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from memseek.config import Settings
from memseek.harnesses.contract import ManifestError, MissingRequirementError
from memseek.harnesses.registry import check_harness, load_harness

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_HARNESSES = REPOSITORY_ROOT / "tests" / "fixtures" / "harnesses"


def test_the_shipped_pi_manifest_parses() -> None:
    manifest = load_harness("pi", Settings().harness_paths)

    assert manifest.model_dump(mode="json") == {
        "name": "pi",
        "version": 1,
        "entry": ["node", "run.mjs"],
        "requires": [
            {"bin": "node", "install": "https://nodejs.org (Node.js 20 or newer)"},
            {"bin": "pi", "install": "npm i -g --ignore-scripts @earendil-works/pi-coding-agent"},
        ],
        "skills_dir": ".agents/skills",
        "model_env": {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        },
    }
    assert manifest.command() == ["node", str(REPOSITORY_ROOT / "harnesses" / "pi" / "run.mjs")]


def test_first_path_holding_the_module_wins(tmp_path: Path) -> None:
    manifest = load_harness("echo", (tmp_path, FIXTURE_HARNESSES))

    assert (manifest.name, manifest.version) == ("echo", 3)
    assert manifest.root == FIXTURE_HARNESSES / "echo"


def test_a_missing_binary_is_reported_with_its_install_hint(tmp_path: Path) -> None:
    manifest = load_harness("pi", Settings().harness_paths)

    with pytest.raises(MissingRequirementError) as caught:
        check_harness(manifest, path=str(tmp_path))

    assert str(caught.value) == (
        "harness 'pi' requires 'node' (install: https://nodejs.org (Node.js 20 or newer)); "
        "'pi' (install: npm i -g --ignore-scripts @earendil-works/pi-coding-agent)"
    )
    assert [item.bin for item in caught.value.missing] == ["node", "pi"]


def test_an_unknown_harness_names_where_it_looked(tmp_path: Path) -> None:
    with pytest.raises(ManifestError) as caught:
        load_harness("codex", (tmp_path,))

    assert str(caught.value) == f"no harness.yaml for 'codex' in {tmp_path}"


def test_a_manifest_must_name_its_own_directory(tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "harness.yaml").write_text(
        "name: pi\nversion: 1\nentry: [node]\nskills_dir: skills\n", encoding="utf-8"
    )

    with pytest.raises(ManifestError, match="declares name 'pi'"):
        load_harness("other", (tmp_path,))


def test_skills_dir_cannot_escape_the_root(tmp_path: Path) -> None:
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "harness.yaml").write_text(
        "name: bad\nversion: 1\nentry: [node]\nskills_dir: ../skills\n", encoding="utf-8"
    )

    with pytest.raises(ManifestError, match="skills_dir must be a safe relative path"):
        load_harness("bad", (tmp_path,))
