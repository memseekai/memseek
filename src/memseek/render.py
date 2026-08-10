"""Deterministic record rendering and prompt-fencing primitives.

Collection projections are resolved before records reach this module.  A
renderer therefore uses only persisted canonical values and never reinterprets
old content through the active collection definition.

Two responsibilities are deliberately separated.  Escaping untrusted text is
the engine's unconditional guarantee: record content can never close or forge
an element, whatever wrapper surrounds it.  Fencing that text — the element and
any sentence introducing it to a model — is the author's decision, declared as
a :class:`FenceDeclaration` in the YAML that composes the prompt.  A renderer
that receives no declaration emits bare escaped rows and lets its caller's
template own every character of agent-facing text.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from memseek.definitions import DefinitionCatalog
from memseek.definitions.base import FenceDeclaration

TRUNCATION_SENTINEL = "\n[...] truncated [...]\n"
COMPACT_CONTENT_CHARS = 500
type RenderProfile = Literal["compact", "derivation_input"]


@dataclass(frozen=True, slots=True)
class RenderableRecord:
    """The canonical fields shared by compact and derivation-input rendering."""

    id: UUID
    occurred_at: datetime
    collection: str
    type: str
    content: Mapping[str, Any]
    key: str | None = None
    scores: Mapping[str, Any] = field(default_factory=dict)


def truncate_middle(text: str, limit: int) -> str:
    """Keep both ends of ``text`` in exactly the specification's proportions."""

    if limit < 0:
        raise ValueError("truncate limit must be non-negative")
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATION_SENTINEL):
        return TRUNCATION_SENTINEL[:limit]
    remaining = limit - len(TRUNCATION_SENTINEL)
    head = remaining // 2
    tail = remaining - head
    return f"{text[:head]}{TRUNCATION_SENTINEL}{text[-tail:]}"


def escape_untrusted(text: str) -> str:
    """Escape the three characters that can forge or close a prompt fence."""

    return text.replace("&", r"\u0026").replace("<", r"\u003c").replace(">", r"\u003e")


def _domain_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("record occurred_at must include a timezone")
    normalized = value.astimezone(UTC)
    if normalized.microsecond:
        timespec = "microseconds"
    elif normalized.second:
        timespec = "seconds"
    else:
        timespec = "minutes"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _score(value: Any, name: str) -> str:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"rendered scorer {name!r} is not finite")
    return format(float(value), "g")


def render_record(
    record: RenderableRecord,
    *,
    profile: RenderProfile,
    catalog: DefinitionCatalog,
) -> str:
    """Render one complete canonical row under an explicit profile."""

    text = record.content.get("text")
    if not isinstance(text, str):
        raise ValueError("record content.text must be a string")
    if record.content.get("tombstone") is True:
        rendered_text = "retracted"
    elif profile == "compact":
        rendered_text = truncate_middle(text, COMPACT_CONTENT_CHARS)
    elif profile == "derivation_input":
        rendered_text = text
    else:
        raise ValueError(f"unknown render profile: {profile}")

    metadata = [
        f"[id={record.id}] {_domain_time(record.occurred_at)}",
        f"{record.collection}/{record.type}",
    ]
    if record.key is not None:
        metadata.append(f"key {escape_untrusted(record.key)}")
    for name in sorted(catalog.processors):
        definition = catalog.processors[name]
        if definition.kind == "score" and definition.render and name in record.scores:
            metadata.append(f"{name} {_score(record.scores[name], name)}")
    metadata.append(escape_untrusted(rendered_text))
    return " | ".join(metadata)


def render_rows(rows: Sequence[str], *, fence: FenceDeclaration | None) -> str:
    """Join complete rendered rows under the author's fence declaration.

    Without a declaration the rows are returned bare, so the caller's own
    template is the only source of agent-facing framing.  The rows are already
    escaped, so they cannot close whatever element the template puts around
    them.
    """

    body = "\n".join(rows)
    if fence is None:
        return body
    preamble = "" if fence.preamble is None else f"{fence.preamble}\n"
    return f'{preamble}<{fence.tag} untrusted="true">\n{body}\n</{fence.tag}>'


def fence_overhead_tokens(fence: FenceDeclaration | None, estimate: Callable[[str], int]) -> int:
    """The token cost `fence` adds to any row set, under `estimate`."""

    if fence is None:
        return 0
    return estimate(render_rows((), fence=fence))


def render_records(
    records: Sequence[RenderableRecord],
    *,
    profile: RenderProfile,
    catalog: DefinitionCatalog,
    fence: FenceDeclaration | None,
) -> str:
    """Render a complete deterministic record sequence under one fence choice.

    `fence` has no default.  Every caller states whether it is producing bare
    rows for a template to frame or a self-contained fenced block, so the
    choice is always visible at the call site.
    """

    return render_rows(
        tuple(render_record(record, profile=profile, catalog=catalog) for record in records),
        fence=fence,
    )


__all__ = [
    "COMPACT_CONTENT_CHARS",
    "TRUNCATION_SENTINEL",
    "FenceDeclaration",
    "RenderProfile",
    "RenderableRecord",
    "escape_untrusted",
    "fence_overhead_tokens",
    "render_record",
    "render_records",
    "render_rows",
    "truncate_middle",
]
