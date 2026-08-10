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

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_CATALOG = REPOSITORY_ROOT / "resources"

#: The definition families a catalog directory may contain.
CATALOG_DIRECTORIES = (
    "collections",
    "derivations",
    "triggers",
    "views",
    "artifacts",
    "mcp",
    "packages",
)


def materialize_reference_catalog(destination: Path) -> Path:
    """Copy the reference catalog into ``destination`` and return it.

    ``conf/`` is assembled from two places on purpose: models, ranking and
    search profiles are deployment configuration that lives at the repository
    root, while processors belong to the catalog and travel with it.
    """

    destination.mkdir(parents=True, exist_ok=True)
    for name in CATALOG_DIRECTORIES:
        shutil.copytree(REFERENCE_CATALOG / name, destination / name, dirs_exist_ok=True)

    conf = destination / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    for source in (REPOSITORY_ROOT / "conf", REFERENCE_CATALOG / "conf"):
        for path in sorted(source.glob("*.yaml")):
            shutil.copy2(path, conf / path.name)
    return destination
