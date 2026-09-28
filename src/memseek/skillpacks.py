"""Skill pack modules: what a harness can do, and how a skill is installed for it.

A pack knows nothing about which harness mounts it, or about memory. Whether a
loaded skill records lessons and reads a playbook is the toolset's choice
(``learning:`` on its source); this module only installs what materialization
rendered for it beside the skill.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from memseek.definitions.base import EnvVarName, NonBlank, PublicName, StrictModel
from memseek.definitions.models import ComputerCapabilityName, LessonSpec
from memseek.harnesses.contract import (
    Requirement,
    check_requirements,
    load_manifest,
)

_SKILL_COMMAND_TIMEOUT_S = 60
_FETCH_TIMEOUT_S = 300


class SkillPackError(RuntimeError):
    """A pack's skill could not be materialized."""


class SkillSource(StrictModel):
    """Where SKILL.md comes from: a command's stdout, or a file in the pack."""

    command: tuple[NonBlank, ...] | None = Field(default=None, min_length=1)
    file: NonBlank | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if (self.command is None) == (self.file is None):
            raise ValueError("skill requires exactly one of command or file")
        return self


class SkillPackEnv(StrictModel):
    passthrough: tuple[EnvVarName, ...] = Field(default=(), alias="pass")
    # `{state}` expands to the pack's state directory, fresh or persisted as
    # the run asks. `{runtime}` expands to a short private directory that lives
    # for one run, for sockets and pid files.
    set: dict[EnvVarName, str] = Field(default_factory=dict)


class WorkspaceSource(StrictModel):
    repo: NonBlank
    # A commit, so a cached copy is never stale.
    ref: NonBlank
    path: NonBlank


class SkillPackWorkspace(StrictModel):
    """Files the pack's skill expects to find, copied from ``from`` into ``dir``."""

    dir: NonBlank
    source: WorkspaceSource = Field(alias="from")


class SkillPackManifest(StrictModel):
    name: PublicName
    version: int = Field(ge=1)
    requires: tuple[Requirement, ...] = ()
    skill: SkillSource
    env: SkillPackEnv = Field(default_factory=SkillPackEnv)
    capabilities: tuple[ComputerCapabilityName, ...] = ()
    workspace: SkillPackWorkspace | None = None
    # What is worth learning while using this pack, for toolsets that turn
    # learning on for it.
    lessons: LessonSpec | None = None
    # Run after the harness exits, with the pack's environment, so nothing the
    # pack started (a browser daemon) outlives the run.
    stop: tuple[NonBlank, ...] | None = Field(default=None, min_length=1)
    root: Path = Field(exclude=True)


@dataclass(frozen=True, slots=True)
class SkillMount:
    name: str
    dir: Path
    playbook: str | None = None


def load_skillpack(name: str, paths: Sequence[Path]) -> SkillPackManifest:
    return load_manifest(SkillPackManifest, name, paths, "skillpack.yaml")


def check_skillpack(pack: SkillPackManifest, *, path: str | None = None) -> None:
    check_requirements(f"skill pack {pack.name!r}", pack.requires, path=path)


def pack_environment(
    pack: SkillPackManifest, *, state_dir: Path, runtime_dir: Path, parent: Mapping[str, str]
) -> dict[str, str]:
    """The variables this pack declares, and nothing else from ``parent``."""

    passed = {name: parent[name] for name in pack.env.passthrough if name in parent}
    declared = {
        name: _expand_dirs(value, state_dir, runtime_dir) for name, value in pack.env.set.items()
    }
    return {**passed, **declared}


def _expand_dirs(value: str, state_dir: Path, runtime_dir: Path) -> str:
    return value.replace("{state}", str(state_dir)).replace("{runtime}", str(runtime_dir))


def seed_workspace(
    pack: SkillPackManifest, *, state_dir: Path, runtime_dir: Path, cache_root: Path
) -> None:
    """Copy the pack's workspace files into place, keeping every file already there.

    A kept state directory holds what earlier runs changed, so a file that
    exists is never replaced.
    """

    if pack.workspace is None:
        return
    source = _fetch(pack.workspace.source, cache_root / pack.name)
    target = Path(_expand_dirs(pack.workspace.dir, state_dir, runtime_dir))
    for file in sorted(source.rglob("*")):
        destination = target / file.relative_to(source)
        if file.is_dir() or destination.exists() or destination.is_symlink():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, destination)


def _fetch(source: WorkspaceSource, cache: Path) -> Path:
    tree = cache / source.ref / source.path
    if tree.is_dir():
        return tree
    cache.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".fetch-", dir=cache))
    try:
        for command in (
            ["git", "init", "--quiet"],
            ["git", "fetch", "--quiet", "--depth", "1", source.repo, source.ref],
            ["git", "checkout", "--quiet", "FETCH_HEAD", "--", source.path],
        ):
            try:
                completed = subprocess.run(
                    command,
                    cwd=staging,
                    capture_output=True,
                    text=True,
                    timeout=_FETCH_TIMEOUT_S,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SkillPackError(f"fetching {source.repo}@{source.ref} failed: {exc}") from exc
            if completed.returncode != 0:
                detail = " ".join(completed.stderr.split())[:200]
                raise SkillPackError(f"fetching {source.repo}@{source.ref} failed: {detail}")
        tree.parent.mkdir(parents=True, exist_ok=True)
        # Another run may have finished the same fetch first; its copy is identical.
        with contextlib.suppress(OSError):
            (staging / source.path).replace(tree)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if not tree.is_dir():
        raise SkillPackError(f"{source.repo}@{source.ref} has no directory {source.path!r}")
    return tree


def stop_skillpack(pack: SkillPackManifest, env: Mapping[str, str]) -> None:
    """Best effort: a pack that fails to stop must not change the run's outcome."""

    if pack.stop is None:
        return
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            list(pack.stop),
            cwd=pack.root,
            env=dict(env),
            capture_output=True,
            timeout=_SKILL_COMMAND_TIMEOUT_S,
            check=False,
        )


def materialize_skillpack(
    pack: SkillPackManifest,
    skills_root: Path,
    *,
    playbook: str | None,
    lessons: str | None,
    env: Mapping[str, str],
) -> SkillMount:
    """Write ``<skills_root>/<pack>/SKILL.md`` from the pack's skill command or file."""

    document = _expand_declared(_skill_document(pack, env), pack, env)
    return install_skill(
        skills_root / pack.name, document, name=pack.name, playbook=playbook, lessons=lessons
    )


def install_skill(
    directory: Path,
    document: str,
    *,
    name: str,
    playbook: str | None,
    lessons: str | None,
) -> SkillMount:
    """Install one skill where the harness discovers it, with what it learned.

    ``playbook`` is written beside ``SKILL.md`` and pointed at first, because an
    upstream skill runs to hundreds of lines and a pointer at the end is one the
    agent never reaches. ``lessons`` (how to record new ones) goes last.
    """

    directory.mkdir(parents=True, exist_ok=True)
    frontmatter, body = _split_frontmatter(document)
    parts = [frontmatter]
    if playbook is not None:
        (directory / "PLAYBOOK.md").write_text(playbook, encoding="utf-8")
        parts.append(PLAYBOOK_POINTER)
    parts.append(body)
    if lessons is not None:
        parts.append(lessons.strip())
    (directory / "SKILL.md").write_text(
        "\n\n".join(part for part in parts if part) + "\n", encoding="utf-8"
    )
    return SkillMount(name=name, dir=directory, playbook=playbook)


PLAYBOOK_POINTER = (
    "## Start from the playbook\n\n"
    "Earlier runs left PLAYBOOK.md in this directory: what they learned using this skill. "
    "Read it before you start, try what it says first, and explore only what it does not cover."
)


def _expand_declared(document: str, pack: SkillPackManifest, env: Mapping[str, str]) -> str:
    # The agent reads SKILL.md as text, so `$BH_AGENT_WORKSPACE/agent_helpers.py`
    # named no path it could find, and no run ever saved a helper there. Only
    # the pack's own variables expand, so a skill's other `$NAME`s stay as written.
    names = [name for name in pack.env.set if name in env]
    if not names:
        return document
    pattern = re.compile(r"\$(?:\{(" + "|".join(names) + r")\}|(" + "|".join(names) + r")\b)")
    return pattern.sub(lambda match: env[match[1] or match[2]], document)


def _split_frontmatter(document: str) -> tuple[str, str]:
    text = document.strip()
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[: end + 4], text[end + 4 :].strip()
    return "", text


def _skill_document(pack: SkillPackManifest, env: Mapping[str, str]) -> str:
    if pack.skill.file is not None:
        return (pack.root / pack.skill.file).read_text(encoding="utf-8")
    assert pack.skill.command is not None
    try:
        completed = subprocess.run(
            list(pack.skill.command),
            cwd=pack.root,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=_SKILL_COMMAND_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SkillPackError(f"skill pack {pack.name!r} skill command failed: {exc}") from exc
    if completed.returncode != 0 or not completed.stdout.strip():
        detail = " ".join(completed.stderr.split())[:200]
        raise SkillPackError(
            f"skill pack {pack.name!r} skill command exited {completed.returncode}: {detail}"
        )
    return completed.stdout


__all__ = [
    "SkillMount",
    "SkillPackError",
    "SkillPackManifest",
    "check_skillpack",
    "install_skill",
    "load_skillpack",
    "materialize_skillpack",
    "pack_environment",
    "seed_workspace",
    "stop_skillpack",
]
