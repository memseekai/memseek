"""Publish the catalog an example needs, because nothing is loaded by default.

A workspace's catalog is the one it published. The service no longer compiles
whatever definitions happen to sit beside it, so an example that wants the
reference catalog — the memory design in ``resources/`` — has to say so. The
examples with a catalog of their own (agent memory, gbrain, CRM) publish that
one instead and never call this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

REFERENCE_CATALOG = Path(__file__).resolve().parents[1] / "resources"
REFERENCE_PACKAGE = "agentic_memory_core@2.2.0"


async def publish_reference_catalog(client: Any, *, quiet: bool = False) -> None:
    """Publish ``resources/`` into this client's workspace.

    Publishing is idempotent — the same files compile to the same catalog hash —
    so an example is safe to re-run against a workspace it already set up.
    """

    result = await client.catalog.publish(
        package=REFERENCE_PACKAGE,
        directory=str(REFERENCE_CATALOG),
    )
    if not quiet:
        package = result.get("package", {})
        print(
            f"  published {package.get('name')}@{package.get('version')} "
            f"({len(result.get('files', []))} files) from resources/"
        )
