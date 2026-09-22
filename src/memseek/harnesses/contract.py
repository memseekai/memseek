"""The contract between memseek and a harness module.

A harness is a directory holding a manifest and an entry command. The provider
prepares a root, writes a :class:`HarnessInput` to ``<root>/.harness/input.json``,
runs the entry, and parses one :class:`HarnessOutput` line from its stdout. These
models are the boundary: nothing past them re-checks what a module declared or
what a harness printed.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from memseek.definitions.base import EnvVarName, NonBlank, ProviderName, PublicName, StrictModel

HARNESS_INPUT_PATH = ".harness/input.json"

LearningMode = Literal["off", "read", "read_write"]


class ManifestError(ValueError):
    """A module is missing, or its manifest does not parse."""


class Requirement(StrictModel):
    bin: NonBlank
    install: NonBlank


class MissingRequirementError(RuntimeError):
    def __init__(self, module: str, missing: Sequence[Requirement]) -> None:
        self.missing = tuple(missing)
        hints = "; ".join(f"{item.bin!r} (install: {item.install})" for item in self.missing)
        super().__init__(f"{module} requires {hints}")


def check_requirements(
    module: str, requires: Sequence[Requirement], *, path: str | None = None
) -> None:
    """Fail with every missing binary and its install hint, not just the first."""

    missing = [item for item in requires if shutil.which(item.bin, path=path) is None]
    if missing:
        raise MissingRequirementError(module, missing)


def _relative_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
        raise ValueError(f"{label} must be a safe relative path")
    return str(path)


class HarnessManifest(StrictModel):
    name: PublicName
    version: int = Field(ge=1)
    entry: tuple[NonBlank, ...] = Field(min_length=1)
    requires: tuple[Requirement, ...] = ()
    # Where this harness discovers skills, relative to the Computer root.
    skills_dir: str
    # Which environment variable carries the key for each model provider.
    model_env: dict[ProviderName, EnvVarName] = Field(default_factory=dict)
    root: Path = Field(exclude=True)

    @field_validator("skills_dir")
    @classmethod
    def validate_skills_dir(cls, value: str) -> str:
        return _relative_path(value, "skills_dir")

    def command(self) -> list[str]:
        """The entry argv, with arguments naming a module file made absolute."""

        return [
            str(self.root / part) if (self.root / part).is_file() else part for part in self.entry
        ]


def load_manifest[TManifest: BaseModel](
    model: type[TManifest], name: str, paths: Sequence[Path], filename: str
) -> TManifest:
    """Find ``<path>/<name>/<filename>`` on the first path that has it, and parse it."""

    for base in paths:
        directory = base.expanduser() / name
        manifest = directory / filename
        if not manifest.is_file():
            continue
        try:
            raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ManifestError(f"{manifest}: manifest must be a mapping")
            parsed = model.model_validate({**raw, "root": directory.resolve()})
        except (yaml.YAMLError, ValidationError) as exc:
            raise ManifestError(f"{manifest}: {exc}") from exc
        if getattr(parsed, "name", None) != name:
            raise ManifestError(f"{manifest}: declares name {getattr(parsed, 'name', None)!r}")
        return parsed
    searched = ", ".join(str(path) for path in paths) or "no paths"
    raise ManifestError(f"no {filename} for {name!r} in {searched}")


class HarnessModel(StrictModel):
    provider: ProviderName
    model: NonBlank
    params: dict[str, Any] = Field(default_factory=dict)


class HarnessLimits(StrictModel):
    max_steps: int = Field(ge=1)
    max_wall_s: int = Field(ge=1)


class HarnessSkill(StrictModel):
    name: PublicName
    dir: str


class HarnessInput(StrictModel):
    task: str
    system_prompt: str
    output_schema: dict[str, Any]
    model: HarnessModel
    limits: HarnessLimits
    tools: tuple[dict[str, Any], ...]
    skills: tuple[HarnessSkill, ...]
    learning: LearningMode
    citation_ids: tuple[str, ...]


class HarnessMetrics(StrictModel):
    """Normalized by each harness, so runs compare across harnesses.

    Token and cost fields are None when the model provider does not report them,
    which is different from a reported zero.
    """

    wall_s: float = Field(ge=0)
    steps: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tool_errors: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class HarnessEvent(StrictModel):
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    payload: dict[str, Any]


class HarnessOutput(StrictModel):
    value: Any
    citation_ids: tuple[UUID, ...]
    steps: int = Field(ge=0)
    awaiting_input: bool = False
    events: tuple[HarnessEvent, ...] = Field(default=(), max_length=256)
    metrics: HarnessMetrics


__all__ = [
    "HARNESS_INPUT_PATH",
    "HarnessEvent",
    "HarnessInput",
    "HarnessLimits",
    "HarnessManifest",
    "HarnessMetrics",
    "HarnessModel",
    "HarnessOutput",
    "HarnessSkill",
    "LearningMode",
    "ManifestError",
    "MissingRequirementError",
    "Requirement",
    "check_requirements",
    "load_manifest",
]
