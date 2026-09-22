"""Discover harness modules by directory: adding a harness is adding a directory."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from memseek.harnesses.contract import HarnessManifest, check_requirements, load_manifest


def load_harness(name: str, paths: Sequence[Path]) -> HarnessManifest:
    return load_manifest(HarnessManifest, name, paths, "harness.yaml")


def check_harness(manifest: HarnessManifest, *, path: str | None = None) -> None:
    check_requirements(f"harness {manifest.name!r}", manifest.requires, path=path)


__all__ = ["check_harness", "load_harness"]
