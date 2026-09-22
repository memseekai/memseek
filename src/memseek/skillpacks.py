"""Skill pack modules: what a harness can do, and how it records what it learned.

A pack knows nothing about which harness mounts it. Its only link to memory is
``learns: true``, which opts into one convention owned by the Computer: the agent
appends learnings to ``/outbox/learnings.jsonl``, the Computer ingests that file
as an ordinary observations writeback, and the next run reads them back from the
playbook the Computer mounts at ``/.memseek/playbook.md``.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from memseek.definitions.base import EnvVarName, NonBlank, PublicName, StrictModel
from memseek.definitions.models import ComputerCapabilityName
from memseek.harnesses.contract import (
    LearningMode,
    Requirement,
    check_requirements,
    load_manifest,
)

LEARNINGS_PATH = "/outbox/learnings.jsonl"
PLAYBOOK_PATH = "/.memseek/playbook.md"

_SKILL_COMMAND_TIMEOUT_S = 60
# One rendered playbook row: `... | [<pack>/<kind>] <text>`. The prefix is the
# collection's text contract, which is what lets one playbook serve many packs.
_PLAYBOOK_ROW = re.compile(
    r"^\[id=(?P<id>[0-9a-f-]{36})\].*?\| \[(?P<pack>[a-z][a-z0-9._-]{0,63})/"
    r"(?P<kind>[a-z_]+)\] (?P<text>.+)$"
)


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
    # the run asks.
    set: dict[EnvVarName, str] = Field(default_factory=dict)


class SkillPackManifest(StrictModel):
    name: PublicName
    version: int = Field(ge=1)
    requires: tuple[Requirement, ...] = ()
    skill: SkillSource
    env: SkillPackEnv = Field(default_factory=SkillPackEnv)
    capabilities: tuple[ComputerCapabilityName, ...] = ()
    learns: bool = False
    root: Path = Field(exclude=True)


@dataclass(frozen=True, slots=True)
class SkillMount:
    name: str
    dir: Path


def load_skillpack(name: str, paths: Sequence[Path]) -> SkillPackManifest:
    return load_manifest(SkillPackManifest, name, paths, "skillpack.yaml")


def check_skillpack(pack: SkillPackManifest, *, path: str | None = None) -> None:
    check_requirements(f"skill pack {pack.name!r}", pack.requires, path=path)


def pack_environment(
    pack: SkillPackManifest, *, state_dir: Path, parent: Mapping[str, str]
) -> dict[str, str]:
    """The variables this pack declares, and nothing else from ``parent``."""

    passed = {name: parent[name] for name in pack.env.passthrough if name in parent}
    declared = {
        name: value.replace("{state}", str(state_dir)) for name, value in pack.env.set.items()
    }
    return {**passed, **declared}


def playbook_section(playbook_md: str, pack: str) -> str | None:
    """This pack's learnings from the rendered playbook, grouped by kind.

    The playbook renders newest first, so within a kind the first row is the
    most recent. Rows keep their record ID, which the agent may cite.
    """

    grouped: dict[str, list[str]] = {}
    for line in playbook_md.splitlines():
        match = _PLAYBOOK_ROW.match(line.strip())
        if match is None or match["pack"] != pack:
            continue
        grouped.setdefault(match["kind"], []).append(f"- {match['text']} (id {match['id']})")
    if not grouped:
        return None
    parts = [
        f"# Playbook: {pack}",
        "Learned on earlier runs against this site, newest first within each kind. "
        "Start from these instead of rediscovering them.",
    ]
    for kind in sorted(grouped):
        parts.append(f"## {kind}\n\n" + "\n".join(grouped[kind]))
    return "\n\n".join(parts) + "\n"


def materialize_skillpack(
    pack: SkillPackManifest,
    skills_root: Path,
    *,
    playbook_md: str | None,
    learning: LearningMode,
    env: Mapping[str, str],
) -> SkillMount:
    """Write ``<skills_root>/<pack>/SKILL.md``, plus the pack's playbook when it has one.

    ``learning`` decides what the run is told: ``off`` mounts the bare skill,
    ``read`` adds the playbook, and ``read_write`` also asks for new learnings.
    """

    directory = skills_root / pack.name
    directory.mkdir(parents=True, exist_ok=True)
    parts = [_skill_document(pack, env).rstrip()]
    if pack.learns and learning == "read_write":
        parts.append((pack.root / "LEARNING.md").read_text(encoding="utf-8").strip())
    section = (
        playbook_section(playbook_md, pack.name)
        if playbook_md is not None and learning != "off"
        else None
    )
    if section is not None:
        (directory / "PLAYBOOK.md").write_text(section, encoding="utf-8")
        parts.append(
            "## Playbook\n\nRead PLAYBOOK.md in this directory before you start: it holds "
            "what earlier runs learned about this site."
        )
    (directory / "SKILL.md").write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return SkillMount(name=pack.name, dir=directory)


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
    "LEARNINGS_PATH",
    "PLAYBOOK_PATH",
    "SkillMount",
    "SkillPackError",
    "SkillPackManifest",
    "check_skillpack",
    "load_skillpack",
    "materialize_skillpack",
    "pack_environment",
    "playbook_section",
]
