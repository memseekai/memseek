"""Installing a skill pack, and what it learned, for one run."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from memseek.config import Settings
from memseek.harnesses.contract import ManifestError
from memseek.skillpacks import (
    SkillPackError,
    SkillPackManifest,
    install_skill,
    load_skillpack,
    materialize_skillpack,
    pack_environment,
    seed_workspace,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PACKS = (REPOSITORY_ROOT / "tests" / "fixtures" / "skillpacks",)

FRONTMATTER = "---\nname: echo-pack\ndescription: Scrape a page.\n---"
BODY = "Scrape the page."
SKILL = f"{FRONTMATTER}\n\n{BODY}"
POINTER = (
    "## Start from the playbook\n\n"
    "Earlier runs left PLAYBOOK.md in this directory: what they learned using this skill. "
    "Read it before you start, try what it says first, and explore only what it does not cover."
)
PLAYBOOK = "# Playbook: echo-pack\n\n## pitfall\n\nThe login wall appears after 3 pages.\n"
LESSONS = "## Recording what you learned\n\nRecord lessons with the tool."


def _env() -> dict[str, str]:
    return {"PATH": os.environ["PATH"]}


def test_a_pack_with_a_playbook_and_lessons_gets_both(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)

    mount = materialize_skillpack(pack, tmp_path, playbook=PLAYBOOK, lessons=LESSONS, env=_env())

    assert mount.dir == tmp_path / "echo-pack"
    assert (mount.dir / "SKILL.md").read_text() == (
        f"{FRONTMATTER}\n\n{POINTER}\n\n{BODY}\n\n{LESSONS}\n"
    )
    assert (mount.dir / "PLAYBOOK.md").read_text() == PLAYBOOK
    assert mount.playbook == PLAYBOOK


def test_a_playbook_without_lessons_adds_no_recording_instructions(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)

    mount = materialize_skillpack(pack, tmp_path, playbook=PLAYBOOK, lessons=None, env=_env())

    assert (mount.dir / "SKILL.md").read_text() == f"{FRONTMATTER}\n\n{POINTER}\n\n{BODY}\n"
    assert (mount.dir / "PLAYBOOK.md").read_text() == PLAYBOOK


def test_without_learning_the_bare_skill_is_installed(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)

    mount = materialize_skillpack(pack, tmp_path, playbook=None, lessons=None, env=_env())

    assert (mount.dir / "SKILL.md").read_text() == f"{SKILL}\n"
    assert not (mount.dir / "PLAYBOOK.md").exists()
    assert mount.playbook is None


def test_any_skill_installs_with_its_playbook_not_only_packs(tmp_path: Path) -> None:
    document = "---\nname: sql\ndescription: Query the warehouse.\n---\n\nWrite SQL."

    mount = install_skill(
        tmp_path / "sql", document, name="sql", playbook=PLAYBOOK, lessons=LESSONS
    )

    assert (mount.dir / "SKILL.md").read_text() == (
        "---\nname: sql\ndescription: Query the warehouse.\n---\n\n"
        f"{POINTER}\n\nWrite SQL.\n\n{LESSONS}\n"
    )
    assert (mount.dir / "PLAYBOOK.md").read_text() == PLAYBOOK


def test_the_skill_names_the_pack_variables_where_they_really_are(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)
    text = "Save helpers in $ECHO_STATE/helpers.py, sockets in ${ECHO_RUNTIME}, not $HOME."
    pack = pack.model_copy(
        update={"skill": pack.skill.model_copy(update={"command": ("echo", text)})}
    )
    env = {**_env(), "ECHO_STATE": "/root/state", "ECHO_RUNTIME": "/tmp/msk-1"}

    mount = materialize_skillpack(pack, tmp_path, playbook=None, lessons=None, env=env)

    assert (mount.dir / "SKILL.md").read_text() == (
        "Save helpers in /root/state/helpers.py, sockets in /tmp/msk-1, not $HOME.\n"
    )


def test_pack_environment_passes_only_what_the_pack_declares(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)

    environment = pack_environment(
        pack,
        state_dir=tmp_path / "state",
        runtime_dir=Path("/tmp/msk-1"),
        parent={"ECHO_PASS": "yes", "SECRET": "no"},
    )

    assert environment == {
        "ECHO_PASS": "yes",
        "ECHO_STATE": str(tmp_path / "state"),
        "ECHO_RUNTIME": "/tmp/msk-1",
    }


def test_the_shipped_browser_harness_pack_parses() -> None:
    pack = load_skillpack("browser-harness", Settings().skillpack_paths)

    assert pack.model_dump(mode="json", by_alias=True) == {
        "name": "browser-harness",
        "version": 1,
        "requires": [
            {"bin": "browser-harness", "install": "uv tool install --python 3.12 browser-harness"}
        ],
        "skill": {
            "command": [
                "sh",
                "-c",
                "browser-harness skill | sed 's#`agent-workspace/#`$BH_AGENT_WORKSPACE/#g'",
            ],
            "file": None,
        },
        "env": {
            "pass": ["BU_CDP_URL", "BU_NAME"],
            "set": {
                "BH_HOME": "{state}",
                "BH_RUNTIME_DIR": "{runtime}",
                "BH_AGENT_WORKSPACE": "{state}/agent-workspace",
                "BH_DOMAIN_SKILLS": "1",
            },
        },
        "capabilities": ["exec", "network"],
        "workspace": {
            "dir": "{state}/agent-workspace",
            "from": {
                "repo": "https://github.com/browser-use/browser-harness",
                "ref": "c24e5072ee66f8499bacd663f4f4bcb089bc4492",
                "path": "agent-workspace",
            },
        },
        "lessons": {
            "kinds": {
                "helper": "The Python you piped into browser-harness that produced your final "
                "rows, in `code` exactly as you ran it, so the next run can pipe it in again. "
                "The next run tries the newest helper first.",
                "navigation": "A URL or JSON endpoint worth calling directly instead of "
                "clicking through the page.",
                "extraction": "Where each field lives on the page, and the selector that finds it.",
                "pitfall": "What went wrong or wasted steps, or a playbook lesson that proved "
                "false, and what is true now.",
            },
            "require": ["helper"],
            "code": ["helper"],
            "guidance": "In a helper, use only browser-harness functions you actually called, "
            "such as new_tab, wait_for_load, and js.",
        },
        "stop": ["browser-harness", "--reload"],
    }


def test_a_failing_skill_command_names_the_pack(tmp_path: Path) -> None:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)
    broken = pack.model_copy(
        update={"skill": pack.skill.model_copy(update={"command": ("false",)})}
    )

    with pytest.raises(SkillPackError, match="skill pack 'echo-pack' skill command exited 1"):
        materialize_skillpack(broken, tmp_path, playbook=None, lessons=None, env=_env())


def test_an_unknown_pack_is_a_manifest_error() -> None:
    with pytest.raises(ManifestError, match=r"no skillpack\.yaml for 'nope'"):
        load_skillpack("nope", FIXTURE_PACKS)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _upstream(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "upstream"
    (repo / "agent-workspace/domain-skills/github").mkdir(parents=True)
    (repo / "agent-workspace/agent_helpers.py").write_text('"""Upstream helpers."""\n')
    (repo / "agent-workspace/domain-skills/github/repos.md").write_text("Use the API.\n")
    (repo / "README.md").write_text("Not part of the workspace.\n")
    _git(repo, "init", "--quiet")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "seed")
    return repo, _git(repo, "rev-parse", "HEAD")


def _pack_with_workspace(repo: Path, ref: str) -> SkillPackManifest:
    pack = load_skillpack("echo-pack", FIXTURE_PACKS)
    workspace = {
        "dir": "{state}/agent-workspace",
        "from": {"repo": str(repo), "ref": ref, "path": "agent-workspace"},
    }
    return SkillPackManifest.model_validate(
        {**pack.model_dump(by_alias=True), "workspace": workspace, "root": pack.root}
    )


def test_a_fresh_state_gets_the_pinned_workspace_files(tmp_path: Path) -> None:
    repo, ref = _upstream(tmp_path)
    pack = _pack_with_workspace(repo, ref)
    state = tmp_path / "state"

    seed_workspace(pack, state_dir=state, runtime_dir=tmp_path, cache_root=tmp_path / "cache")

    workspace = state / "agent-workspace"
    assert sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*")) == [
        "agent_helpers.py",
        "domain-skills",
        "domain-skills/github",
        "domain-skills/github/repos.md",
    ]
    assert (workspace / "agent_helpers.py").read_text() == '"""Upstream helpers."""\n'


def test_seeding_keeps_files_an_earlier_run_changed(tmp_path: Path) -> None:
    repo, ref = _upstream(tmp_path)
    pack = _pack_with_workspace(repo, ref)
    state = tmp_path / "state"
    helpers = state / "agent-workspace/agent_helpers.py"
    helpers.parent.mkdir(parents=True)
    helpers.write_text("def rows():\n    return []\n")

    seed_workspace(pack, state_dir=state, runtime_dir=tmp_path, cache_root=tmp_path / "cache")

    assert helpers.read_text() == "def rows():\n    return []\n"
    assert (state / "agent-workspace/domain-skills/github/repos.md").read_text() == "Use the API.\n"


def test_the_workspace_is_fetched_once_per_ref(tmp_path: Path) -> None:
    repo, ref = _upstream(tmp_path)
    pack = _pack_with_workspace(repo, ref)
    cache = tmp_path / "cache"
    seed_workspace(pack, state_dir=tmp_path / "a", runtime_dir=tmp_path, cache_root=cache)
    (repo / ".git").rename(tmp_path / "gone")

    seed_workspace(pack, state_dir=tmp_path / "b", runtime_dir=tmp_path, cache_root=cache)

    assert (tmp_path / "b/agent-workspace/agent_helpers.py").is_file()
    assert [p.name for p in (cache / "echo-pack").iterdir()] == [ref]


def test_an_unfetchable_workspace_names_the_source(tmp_path: Path) -> None:
    repo, _ = _upstream(tmp_path)
    pack = _pack_with_workspace(repo, "0" * 40)

    with pytest.raises(SkillPackError, match=f"fetching {repo}@0000"):
        seed_workspace(
            pack, state_dir=tmp_path / "s", runtime_dir=tmp_path, cache_root=tmp_path / "c"
        )
