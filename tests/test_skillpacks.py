"""Materializing a skill pack for one run, in each learning mode."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memseek.config import Settings
from memseek.harnesses.contract import ManifestError
from memseek.skillpacks import (
    SkillPackError,
    load_skillpack,
    materialize_skillpack,
    pack_environment,
    playbook_section,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PACKS = (REPOSITORY_ROOT / "tests" / "fixtures" / "skillpacks",)

FRONTMATTER = "---\nname: echo-pack\ndescription: Scrape a page.\n---"
BODY = "Scrape the page."
SKILL = f"{FRONTMATTER}\n\n{BODY}"
LEARNING = (
    "## Recording what you learned\n\nAppend one line per learning to ../outbox/learnings.jsonl."
)
POINTER = (
    "## Start from the playbook\n\n"
    "Earlier runs on this site left PLAYBOOK.md in this directory. Read it before you open "
    "the browser or write any code. Try what it says first, and explore only what it does "
    "not cover."
)
PLAYBOOK = """Learned site knowledge, newest first.

[id=00000000-0000-4000-8000-000000000003] 2026-09-22T10:00:00Z | skill_learnings/learning | [echo-pack/pitfall] The login wall appears after 3 pages.
[id=00000000-0000-4000-8000-000000000002] 2026-09-21T10:00:00Z | skill_learnings/learning | [other-pack/navigation] Not this pack.
[id=00000000-0000-4000-8000-000000000001] 2026-09-20T10:00:00Z | skill_learnings/learning | [echo-pack/extraction] Stories are tr.athing rows.
[id=00000000-0000-4000-8000-000000000000] 2026-09-19T10:00:00Z | skill_learnings/learning | [echo-pack/pitfall] Older pitfall.
"""
SECTION = """# Playbook: echo-pack

Learned on earlier runs against this site, newest first within each kind. Start from these instead of rediscovering them.

## extraction

- Stories are tr.athing rows. (id 00000000-0000-4000-8000-000000000001)

## pitfall

- The login wall appears after 3 pages. (id 00000000-0000-4000-8000-000000000003)
- Older pitfall. (id 00000000-0000-4000-8000-000000000000)
"""


def _env() -> dict[str, str]:
    return {"PATH": os.environ["PATH"]}


def test_playbook_section_keeps_only_this_pack_grouped_by_kind() -> None:
    assert playbook_section(PLAYBOOK, "echo-pack") == SECTION
    assert playbook_section(PLAYBOOK, "missing-pack") is None


def test_read_write_mounts_skill_learning_and_playbook(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)

    mount = materialize_skillpack(
        pack, tmp_path, playbook_md=PLAYBOOK, learning="read_write", env=_env()
    )

    assert mount.dir == tmp_path / "echo-pack"
    assert (mount.dir / "SKILL.md").read_text() == (
        f"{FRONTMATTER}\n\n{POINTER}\n\n{BODY}\n\n{LEARNING}\n"
    )
    assert (mount.dir / "PLAYBOOK.md").read_text() == SECTION
    assert mount.playbook == SECTION


def test_read_mounts_the_playbook_without_asking_for_learnings(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)

    mount = materialize_skillpack(pack, tmp_path, playbook_md=PLAYBOOK, learning="read", env=_env())

    assert (mount.dir / "SKILL.md").read_text() == f"{FRONTMATTER}\n\n{POINTER}\n\n{BODY}\n"
    assert (mount.dir / "PLAYBOOK.md").read_text() == SECTION
    assert mount.playbook == SECTION


def test_off_mounts_the_bare_skill(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)

    mount = materialize_skillpack(pack, tmp_path, playbook_md=PLAYBOOK, learning="off", env=_env())

    assert (mount.dir / "SKILL.md").read_text() == f"{SKILL}\n"
    assert not (mount.dir / "PLAYBOOK.md").exists()


def test_without_a_playbook_section_no_playbook_is_mounted(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)

    mount = materialize_skillpack(
        pack, tmp_path, playbook_md=None, learning="read_write", env=_env()
    )

    assert (mount.dir / "SKILL.md").read_text() == f"{SKILL}\n\n{LEARNING}\n"
    assert not (mount.dir / "PLAYBOOK.md").exists()


def test_pack_environment_passes_only_what_the_pack_declares(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)

    environment = pack_environment(
        pack, state_dir=tmp_path / "state", parent={"ECHO_PASS": "yes", "SECRET": "no"}
    )

    assert environment == {"ECHO_PASS": "yes", "ECHO_STATE": str(tmp_path / "state")}


def test_the_shipped_browser_harness_pack_parses() -> None:
    pack = load_skillpack("browser-harness", Settings().skillpack_paths)

    assert pack.model_dump(mode="json", by_alias=True) == {
        "name": "browser-harness",
        "version": 1,
        "requires": [
            {"bin": "browser-harness", "install": "uv tool install --python 3.12 browser-harness"}
        ],
        "skill": {"command": ["browser-harness", "skill"], "file": None},
        "env": {"pass": ["BU_CDP_URL", "BU_NAME"], "set": {"BH_HOME": "{state}"}},
        "capabilities": ["exec", "network"],
        "learns": True,
    }


def test_a_failing_skill_command_names_the_pack(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)
    broken = pack.model_copy(
        update={"skill": pack.skill.model_copy(update={"command": ("false",)})}
    )

    with pytest.raises(SkillPackError, match="skill pack 'echo-pack' skill command exited 1"):
        materialize_skillpack(broken, tmp_path, playbook_md=None, learning="off", env=_env())


def test_an_unknown_pack_is_a_manifest_error() -> None:
    with pytest.raises(ManifestError, match=r"no skillpack\.yaml for 'nope'"):
        load_skillpack("nope", FIXTURE_PACKS)
