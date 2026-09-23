"""The system prompt every harness receives, built once and harness-neutral.

A harness appends this to its own system prompt. Keeping it here, rather than in
each harness, is what makes a run under one harness comparable to a run under
another: they are told the same things in the same words.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from typing import Any

from memseek.skillpacks import LEARNINGS_PATH

_MAX_INLINE_SCHEMA_BYTES = 8 * 1024

_PREAMBLE = (
    "You are the exact versioned MemSeek Agent named in /.memseek/manifest.json.",
    "Your working directory is /workspace. The Computer root is its parent, so /.memseek "
    "is ../.memseek, /inputs is ../inputs, and /outbox is ../outbox.",
    "/.memseek and /inputs are read-only context. Write working files in /workspace.",
    "Read immutable context before acting. Load an installed skill before following its procedure.",
)

_ENVELOPE = (
    'End with one JSON object and nothing else: {"value": <object>, "citation_ids": '
    '[<authorized UUIDs>], "awaiting_input": <boolean>}.',
    "Set awaiting_input true only when work cannot continue without one concrete user answer.",
    "Never cite an ID that is not in the authorized list. Every citation in a file you write "
    "to /outbox must also appear in the final citation_ids.",
)


def build_system_prompt(
    *,
    context_files: Mapping[str, str],
    materialization: Mapping[str, Any] | None,
    toolset: Mapping[str, Any] | None,
    output_schema: Mapping[str, Any],
    citation_ids: Collection[str],
    writeback_paths: Collection[str],
    writeback_tools: Mapping[str, str],
    learning_packs: Collection[str],
    playbooks: Mapping[str, str],
) -> str:
    """``writeback_tools`` maps each outbox path that has a tool to the tool's name."""

    sections: list[str] = [*_PREAMBLE, _outbox_rule(writeback_paths, writeback_tools)]
    sections.extend(_playbook_section(name, text) for name, text in sorted(playbooks.items()))
    if learning_packs:
        # The recording procedure sits at the end of each pack's SKILL.md, which a
        # long upstream skill can push past where an agent stops reading.
        names = ", ".join(sorted(learning_packs))
        tool = writeback_tools.get(LEARNINGS_PATH)
        how = f"call the {tool} tool" if tool else f"append to {LEARNINGS_PATH}"
        sections.append(
            f"Before your final answer, record what you learned about this site: {how}, "
            f'following the "Recording what you learned" section of the {names} skill. If '
            "the tool rejects a call, fix what it names and call it again."
        )
    if toolset is not None and toolset.get("instructions"):
        sections.append(str(toolset["instructions"]))
    sections.extend(_context_sections(context_files, materialization))
    encoded = json.dumps(dict(output_schema), sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) <= _MAX_INLINE_SCHEMA_BYTES:
        sections.append(f'The "value" object MUST validate against this JSON Schema:\n{encoded}')
    else:
        sections.append(
            'The "value" object is validated against a JSON Schema too large to inline.'
        )
    authorized = ", ".join(sorted(citation_ids)) or "none"
    sections.append(f"Authorized citation IDs: {authorized}")
    sections.extend(_ENVELOPE)
    return "\n\n".join(sections)


def _playbook_section(skill: str, playbook: str) -> str:
    # Inlined, not pointed at: a run that only lists the file re-derives what
    # earlier runs already paid to learn.
    return (
        "## What earlier runs learned about this site\n\n"
        f"This is the {skill} skill's PLAYBOOK.md. Start from it. Try its URLs, selectors, "
        "and pitfalls before you explore, and explore only what it does not cover. When a "
        "learning turns out to be wrong, say so in a new learning.\n\n"
        f"{playbook.strip()}"
    )


def _outbox_rule(writeback_paths: Collection[str], writeback_tools: Mapping[str, str]) -> str:
    # Anything else under /outbox is discarded, so the agent is told the exact
    # list rather than left to guess a name like result.json.
    answer = "Return your answer only in the final JSON object, never as a file."
    if not writeback_paths:
        return f"Write nothing to /outbox. {answer}"
    listed = ", ".join(
        f"{path} (only through the {writeback_tools[path]} tool)"
        if path in writeback_tools
        else path
        for path in sorted(writeback_paths)
    )
    return (
        f"The only files you may write in /outbox are: {listed}. Anything else there is "
        f"discarded. {answer}"
    )


def _context_sections(
    context_files: Mapping[str, str], materialization: Mapping[str, Any] | None
) -> list[str]:
    """Inline what is meant to be read now; point at what is meant to be opened.

    Skills are left out because the provider installs them where the harness
    discovers skills, and a harness offers a skill by its description.
    """

    files = (materialization or {}).get("files") or [
        {"path": path, "role": "reference"} for path in sorted(context_files)
    ]
    inline: list[str] = []
    pointers: list[str] = []
    for file in files:
        path = str(file["path"])
        role = file.get("role")
        if role == "skill" or path not in context_files:
            continue
        if role in {"instructions", "reference", "view"} and file.get("prompt", True):
            inline.append(f"## {path}\n\n{context_files[path]}")
        else:
            pointers.append(path)
    if pointers:
        inline.append("Also available to read: " + ", ".join(pointers))
    return inline


__all__ = ["build_system_prompt"]
