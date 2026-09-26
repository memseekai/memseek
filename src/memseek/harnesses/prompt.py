"""The system prompt every harness receives, built once and harness-neutral.

A harness appends this to its own system prompt. Keeping it here, rather than in
each harness, is what makes a run under one harness comparable to a run under
another: they are told the same things in the same words.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from typing import Any

_MAX_INLINE_SCHEMA_BYTES = 8 * 1024

_PREAMBLE = (
    "You are the exact versioned MemSeek Agent named in /.memseek/manifest.json.",
    "/.memseek and /inputs are read-only context. Write working files in /workspace.",
    "Read immutable context before acting. Load an installed skill before following its procedure.",
)


def _paths_rule(root: str | None) -> str:
    if root is None:
        return (
            "Your working directory is /workspace. The Computer root is its parent, so "
            "/.memseek is ../.memseek, /inputs is ../inputs, and /outbox is ../outbox."
        )
    return (
        f"The Computer root is {root}, and every path in these instructions is already "
        f"absolute on this machine. Your working directory is {root}/workspace, so use "
        "relative paths for your working files."
    )


# A Computer path, where it starts a token: not the tail of a URL or of a
# longer path.
_COMPUTER_PATH = re.compile(
    r"(?<![\w.:/-])/(\.memseek|outbox|workspace|inputs)(?=[/\s.,;:)`'\"]|$)"
)


def _localize(text: str, root: str) -> str:
    # Live runs took "/workspace" and then "/.memseek/playbook.md" literally,
    # hit the read-only filesystem root, and spent turns on it. A mapping
    # sentence did not stop them, so the prompt names the real paths.
    return _COMPUTER_PATH.sub(lambda match: f"{root}/{match[1]}", text)


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
    learning_skills: Collection[str],
    learning_writeback: str | None,
    playbooks: Mapping[str, str],
    root: str | None = None,
) -> str:
    """``writeback_tools`` maps each outbox path that has a tool to the tool's name."""

    sections: list[str] = [
        _PREAMBLE[0],
        _paths_rule(root),
        *_PREAMBLE[1:],
        _outbox_rule(writeback_paths, writeback_tools),
    ]
    sections.extend(_playbook_section(name, text) for name, text in sorted(playbooks.items()))
    if learning_skills and learning_writeback is not None:
        # The recording procedure sits at the end of each skill's SKILL.md, which a
        # long upstream skill can push past where an agent stops reading.
        names = ", ".join(sorted(learning_skills))
        tool = writeback_tools.get(learning_writeback)
        how = f"call the {tool} tool" if tool else f"append to {learning_writeback}"
        sections.append(
            f"Before your final answer, record what you learned using the {names} "
            f"skill{'s' if len(learning_skills) > 1 else ''}: {how}, following the recording "
            "instructions at the end of each skill's SKILL.md. If the tool rejects a call, "
            "fix what it names and call it again."
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
    prompt = "\n\n".join(sections)
    return _localize(prompt, root) if root is not None else prompt


def _playbook_section(skill: str, playbook: str) -> str:
    # Inlined, not pointed at: a run that only lists the file re-derives what
    # earlier runs already paid to learn. What to do with it is the playbook's
    # own text, which the catalog renders.
    return f"## The {skill} skill's PLAYBOOK.md\n\n{playbook.strip()}"


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
