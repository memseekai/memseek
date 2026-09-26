"""The reference catalog, materialized where a test can edit it.

Nothing is loaded from the repository root any more: `resources/` holds the
reference catalog and a process compiles it only when it is pointed there. The
tests that need a *writable* copy — they add a trigger file, or corrupt one
definition to prove validation catches it — go through here rather than each
naming the directories themselves.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_CATALOG = REPOSITORY_ROOT / "resources"


def materialize_reference_catalog(destination: Path) -> Path:
    """Copy the reference catalog into ``destination`` and return it.

    ``resources/`` is self-contained: ``catalog.yaml`` and every file it lists,
    including the ``conf/`` files its ``config:`` block names.
    """

    shutil.copytree(REFERENCE_CATALOG, destination, dirs_exist_ok=True)
    return destination


def declare_test_sources(root: Path, *paths: str) -> None:
    """Explicitly add a test's new or changed source files to its root index."""
    import yaml

    from memseek.definitions.manifest import SINGLE, UNVERSIONED

    manifest_path = root / "catalog.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    for path in paths:
        family = "processors" if path.startswith("conf/processors") else path.split("/")[0]
        raw = yaml.safe_load((root / path).read_text())
        values = [raw] if family in SINGLE else raw[family]
        entries = manifest.setdefault(family, {})
        for ref in [ref for ref, source in entries.items() if source == path]:
            del entries[ref]
        for value in values:
            ref = value["name"] if family in UNVERSIONED else f"{value['name']}@{value['version']}"
            entries[ref] = path
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))


def indexed_test_bundle(name: str, version: str, files: dict[str, str]) -> dict[str, str]:
    """Build an explicit root for generated in-memory test definitions."""
    import yaml

    from memseek.definitions.manifest import FAMILIES, SINGLE, UNVERSIONED

    files = dict(files)
    files.setdefault("conf/models.yaml", (REPOSITORY_ROOT / "conf/models.yaml").read_text())
    files.setdefault("conf/search_profiles.yaml", "profiles: {pg_default: {backend: pg}}\n")
    files.setdefault(
        "conf/rank_default.yaml", (REPOSITORY_ROOT / "conf/rank_default.yaml").read_text()
    )
    manifest: dict[str, Any] = {
        "name": name,
        "version": version,
        "config": {
            "models": "conf/models.yaml",
            "ranking": "conf/rank_default.yaml",
            "search_profiles": "conf/search_profiles.yaml",
        },
    }
    for path, text in files.items():
        family = "processors" if path == "conf/processors.yaml" else path.split("/")[0]
        if family not in FAMILIES:
            continue
        raw = yaml.safe_load(text)
        for value in [raw] if family in SINGLE else raw[family]:
            ref = value["name"] if family in UNVERSIONED else f"{value['name']}@{value['version']}"
            manifest.setdefault(family, {})[ref] = path
    files["catalog.yaml"] = yaml.safe_dump(manifest, sort_keys=False)
    return files
