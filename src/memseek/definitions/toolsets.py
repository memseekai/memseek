"""The one tool surface an Agent actually gets.

An Agent may declare its tools in either of two spellings: the original
``tools:``/``skills:`` literals, or a ``toolset:`` reference.  Everything
downstream — materialization, the request body, the runtime registry — reads the
resolved surface, so the two spellings are reconciled exactly once, here, and no
consumer branches on which one an author used.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .base import split_exact_reference
from .models import (
    AgentDefinition,
    ComputerDefinition,
    ToolsetDefinition,
    ToolSourceDefinition,
)

if TYPE_CHECKING:
    from .loader import DefinitionCatalog

# The desugared surface is a request-time value: never hashed, never stored,
# never registered in a catalog, never resolved by name.  The name below is
# descriptive only — `agent.toolset is None` is the authoritative signal that a
# surface was desugared, so an authored toolset that happened to share this name
# would still be reported as itself.
LEGACY_TOOLSET = "legacy"

# What the `computer` literal has always meant in practice: the whole filesystem
# tool set `createAITools` installs.  Naming it here rather than in the runtime
# is what lets a declared toolset grant less.
LEGACY_FILESYSTEM_MODES = ("read", "ls", "find", "grep", "write", "edit", "delete")


def _writeback_source_name(path: str) -> str:
    """A stable tool-source name for a declared writeback path."""

    stem = PurePosixPath(path).name.split(".", 1)[0]
    return stem.replace("-", "_") or "writeback"


def effective_toolset(
    agent: AgentDefinition,
    computer: ComputerDefinition,
    catalog: DefinitionCatalog,
) -> ToolsetDefinition:
    """Resolve an Agent's tool surface against the Computer that will run it.

    A declared toolset is returned as authored.  The legacy spelling is
    desugared into the equivalent surface, including the writeback tools the
    Computer's declarations have always generated implicitly.
    """

    if agent.toolset is not None:
        return catalog.resolve_toolset(agent.toolset)
    return ToolsetDefinition(
        name=LEGACY_TOOLSET, version=1, sources=tuple(_legacy_sources(agent, computer))
    )


def _legacy_sources(
    agent: AgentDefinition, computer: ComputerDefinition
) -> list[ToolSourceDefinition]:
    sources: list[ToolSourceDefinition] = []
    if "computer" in agent.tools:
        sources.append(
            ToolSourceDefinition(
                name="filesystem",
                kind="filesystem",
                description="Read and write files in the Computer workspace.",
                modes=LEGACY_FILESYSTEM_MODES,
            )
        )
        # `computer` has always meant "filesystem, plus a shell if the Computer
        # allows one".  The capability stays the ceiling either way.
        if computer.capabilities.exec:
            sources.append(
                ToolSourceDefinition(
                    name="shell",
                    kind="exec",
                    description="Run a command inside the Computer.",
                )
            )
    if "recall" in agent.tools:
        sources.append(
            ToolSourceDefinition(
                name="recall",
                kind="recall",
                description=(
                    "Search already-authorized immutable context and durable workspace "
                    "files, then open the materialized receipt before citing anything."
                ),
            )
        )
    for reference in agent.skills:
        # The artifact's own name, which `split_exact_reference` has already
        # validated as a public name, keeps each source distinct.
        artifact_name, _ = split_exact_reference(reference)
        sources.append(ToolSourceDefinition(name=artifact_name, kind="skill", artifact=reference))
    for item in computer.writeback:
        if item.type == "final_result":
            # The invocation's output path, not something the Agent calls.
            continue
        sources.append(
            ToolSourceDefinition(
                name=_writeback_source_name(item.path),
                kind="writeback",
                description=f"Write {item.type.replace('_', ' ')} to {item.path}.",
                path=item.path,
            )
        )
    return sources


__all__ = ["LEGACY_FILESYSTEM_MODES", "LEGACY_TOOLSET", "effective_toolset"]
