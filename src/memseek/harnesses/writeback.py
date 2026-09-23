"""Writeback tools for harnesses: validate a record when it is written, not after.

A harness exposes each tool natively (pi registers one per spec). Calling a
tool runs this module's command, which checks the records against the
destination collection's schema and the run's authorized citations, then
appends them to the declared outbox file. A rejection comes back to the agent
as a tool error that names the fix, so it can correct the record and call
again within the same run.

The same ``check_candidate`` also filters the outbox after the run, so a file
the agent wrote by hand is held to identical rules.

Run as ``python -m memseek.harnesses.writeback <root> <tool>`` with the tool's
JSON arguments on stdin.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import Field

from memseek.definitions.base import StrictModel

WRITEBACK_SCHEMAS_PATH = "/.memseek/writeback-schemas.json"
_CANDIDATE_KEYS = frozenset({"text", "content", "citations", "key"})
_MAX_RECORDS_PER_CALL = 100
_MAX_FILE_BYTES = 1024 * 1024


class WritebackTool(StrictModel):
    """What a harness needs to offer one writeback destination as a tool."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str
    path: str
    input_schema: dict[str, Any]


class _Destination(StrictModel):
    path: str
    collection: str
    mode: str | None = None
    schema_: dict[str, Any] = Field(alias="schema")


def writeback_tools(
    writeback: Iterable[Mapping[str, Any]],
    context_files: Mapping[str, str],
    citation_ids: Collection[str],
) -> list[WritebackTool]:
    """One tool per declared ``observations`` writeback that has a schema."""

    destinations = _destinations(context_files)
    citations = sorted(citation_ids)
    tools: list[WritebackTool] = []
    for declaration in writeback:
        if declaration.get("type") != "observations":
            continue
        destination = destinations.get(str(declaration.get("path")))
        if destination is None:
            continue
        tools.append(
            WritebackTool(
                name=tool_name(destination.collection),
                description=(
                    f"Record entries in {destination.collection}. Each record is validated now; "
                    "a rejected call says what to fix, so fix it and call again. Use this tool "
                    f"instead of writing {destination.path} yourself."
                ),
                path=destination.path,
                input_schema=_input_schema(destination.schema_, citations),
            )
        )
    return tools


def tool_name(collection: str) -> str:
    base = collection.split("@", 1)[0]
    return "record_" + re.sub(r"[^a-z0-9_]", "_", base.lower())[:56]


def check_candidate(
    candidate: Any, schema: Mapping[str, Any], citation_ids: Collection[str]
) -> list[str]:
    """Every reason ``candidate`` would fail ingestion; empty when it would pass.

    The rules are ``_ingest_outbox_tx``'s and the record validator's: the known
    keys only, at least one authorized UUID citation, and ``{text, **content}``
    valid against the collection's schema.
    """

    if not isinstance(candidate, dict):
        return ["each entry must be a JSON object"]
    problems: list[str] = []
    extra = sorted(set(candidate) - _CANDIDATE_KEYS)
    if extra:
        problems.append(f"unknown keys {extra}; an entry has only text, content, and citations")
    citations = candidate.get("citations")
    if not isinstance(citations, list) or not citations:
        problems.append("citations must be a non-empty list of authorized UUIDs")
    else:
        allowed = set(citation_ids)
        for value in citations:
            if not _is_uuid(value) or str(value) not in allowed:
                problems.append(
                    f"citation {value!r} is not authorized; use one of {sorted(allowed)}"
                )
    content = candidate.get("content", {})
    text = candidate.get("text")
    if not isinstance(content, dict):
        problems.append("content must be a JSON object")
        return problems
    if not isinstance(text, str):
        problems.append("text must be a string")
        return problems
    record = {**{key: value for key, value in content.items() if value is not None}, "text": text}
    validator = Draft202012Validator(dict(schema), format_checker=FormatChecker())
    for error in sorted(validator.iter_errors(record), key=lambda item: list(item.path)):
        where = ".".join(str(part) for part in error.path) or "record"
        problems.append(f"{where}: {error.message}")
    return problems


def normalize_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Drop null content fields, which models send for 'none' and schemas reject."""

    content = candidate.get("content") or {}
    return {
        **{key: value for key, value in candidate.items() if key != "content"},
        "content": {key: value for key, value in content.items() if value is not None},
    }


def destination_schema(context_files: Mapping[str, str], path: str) -> dict[str, Any] | None:
    destination = _destinations(context_files).get(path)
    return destination.schema_ if destination is not None else None


def run_tool(root: Path, name: str, arguments: Any) -> tuple[bool, str]:
    """Validate one tool call and append what passes. Returns (ok, message for the agent)."""

    harness_input = json.loads((root / ".harness" / "input.json").read_text(encoding="utf-8"))
    tool = next(
        (
            WritebackTool.model_validate(item)
            for item in harness_input.get("writeback_tools", [])
            if item.get("name") == name
        ),
        None,
    )
    if tool is None:
        return False, f"unknown writeback tool {name!r}"
    schema = destination_schema(_context_files(root), tool.path)
    if schema is None:
        return False, f"no schema is declared for {tool.path}"
    records = arguments.get("records") if isinstance(arguments, dict) else None
    if isinstance(records, str):
        # Some models send a nested array as a JSON string.
        with contextlib.suppress(json.JSONDecodeError):
            records = json.loads(records)
    if not isinstance(records, list) or not records:
        return False, 'call with {"records": [<entry>, ...]}'
    if len(records) > _MAX_RECORDS_PER_CALL:
        return False, f"at most {_MAX_RECORDS_PER_CALL} records per call"
    citations = harness_input.get("citation_ids", [])
    problems = [
        f"records[{index}] {problem}"
        for index, record in enumerate(records)
        for problem in check_candidate(record, schema, citations)
    ]
    if problems:
        return False, "Nothing was written. Fix these and call again:\n" + "\n".join(
            f"- {problem}" for problem in problems
        )
    target = root / tool.path.lstrip("/")
    lines = "".join(
        json.dumps(normalize_candidate(record), ensure_ascii=False) + "\n" for record in records
    )
    existing = target.stat().st_size if target.is_file() else 0
    if existing + len(lines.encode()) > _MAX_FILE_BYTES:
        return False, f"{tool.path} would exceed {_MAX_FILE_BYTES} bytes; record fewer entries"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(lines)
    return True, f"Recorded {len(records)} entr{'y' if len(records) == 1 else 'ies'}."


def _input_schema(record_schema: Mapping[str, Any], citations: Sequence[str]) -> dict[str, Any]:
    # The destination schema describes {text, **content}; a tool call carries the
    # same shape split the way the outbox line is, so the agent sees the rules.
    properties = dict(record_schema.get("properties") or {})
    text_schema = properties.pop("text", {"type": "string"})
    required = [name for name in record_schema.get("required", []) if name != "text"]
    content_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        content_schema["required"] = required
    return {
        "type": "object",
        "required": ["records"],
        "properties": {
            "records": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_RECORDS_PER_CALL,
                "items": {
                    "type": "object",
                    "required": ["text", "content", "citations"],
                    "additionalProperties": False,
                    "properties": {
                        "text": text_schema,
                        "content": content_schema,
                        "citations": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "enum": list(citations)}
                            if citations
                            else {"type": "string"},
                        },
                    },
                },
            }
        },
    }


def _destinations(context_files: Mapping[str, str]) -> dict[str, _Destination]:
    encoded = context_files.get(WRITEBACK_SCHEMAS_PATH)
    if not encoded:
        return {}
    try:
        items = json.loads(encoded)
    except json.JSONDecodeError:
        return {}
    destinations: dict[str, _Destination] = {}
    for item in items if isinstance(items, list) else []:
        try:
            destination = _Destination.model_validate(item)
        except ValueError:
            continue
        destinations[destination.path] = destination
    return destinations


def _context_files(root: Path) -> dict[str, str]:
    path = root / WRITEBACK_SCHEMAS_PATH.lstrip("/")
    return {WRITEBACK_SCHEMAS_PATH: path.read_text(encoding="utf-8")} if path.is_file() else {}


def _is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m memseek.harnesses.writeback <root> <tool>", file=sys.stderr)
        return 2
    try:
        arguments = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(f"arguments are not JSON: {exc}")
        return 1
    ok, message = run_tool(Path(argv[0]), argv[1], arguments)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
