#!/usr/bin/env python3
"""Preflight or publish the compatible Memseek agent-memory catalog."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from memseek_client import MemseekClient, MemseekError, load_config

PACKAGE = "agent_memory@0.3.0"
DEFAULT_CATALOG = Path(__file__).resolve().parents[3] / "examples" / "agent_memory_catalog"


def catalog_files(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise ValueError(f"catalog directory does not exist: {root}")
    paths = sorted(
        [*root.rglob("*.yaml"), *root.rglob("*.yml")],
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not paths:
        raise ValueError(f"catalog directory contains no YAML files: {root}")
    return {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8") for path in paths}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--package", default=PACKAGE)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="publish after validation; without this flag the command is a read-only preflight",
    )
    arguments = parser.parse_args()
    try:
        client = MemseekClient(load_config(Path.cwd()))
        result = client.publish_catalog(
            package=arguments.package,
            files=catalog_files(arguments.catalog.resolve()),
            dry_run=not arguments.apply,
        )
    except (MemseekError, ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
