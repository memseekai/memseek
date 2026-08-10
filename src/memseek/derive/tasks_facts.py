"""Deterministic page-fact index Task.

The task deliberately produces one complete, entity-scoped facts array rather
than one independently keyed record per fact.  The normal static keyed
emission boundary owns replacement, provenance, and concurrency protection.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memseek.derive.tasks import TaskConfigModel, TaskContext, TaskResult, register_task


class ExtractFactsConfig(TaskConfigModel):
    """Bounds and heading name for the Markdown facts parser."""

    heading: str = Field(default="Facts", min_length=1, max_length=80)
    max_facts: int = Field(default=100, ge=1, le=100)
    max_fact_chars: int = Field(default=80, ge=1, le=80)

    @field_validator("heading")
    @classmethod
    def normalize_heading(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("heading must not be blank")
        return normalized


class _PageInput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: UUID
    key: str = Field(min_length=1, max_length=128)
    content: dict[str, Any]


class ExtractFactsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    records: tuple[_PageInput, ...] = Field(max_length=64)
    changed_records: tuple[_PageInput, ...] = Field(max_length=50)


class _FactEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_key: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=80)


class _FactIndexContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: tuple[_FactEntry, ...] = Field(max_length=100)
    page_keys: tuple[str, ...] = Field(max_length=64)
    truncated: bool
    omitted_facts: int = Field(ge=0)


class _FactIndexDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = "page_facts"
    text: str = Field(min_length=1)
    content: _FactIndexContent
    citations: tuple[UUID, ...] = Field(min_length=1, max_length=64)


_HEADING_RE = re.compile(r"^\s*(?P<marks>#{2,6})\s+.+?\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(?P<text>\S.*)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _page_body(page: _PageInput) -> str:
    body = page.content.get("body", page.content.get("text", ""))
    return body if isinstance(body, str) else ""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def _finish_fact(parts: list[str], *, max_chars: int) -> str | None:
    if not parts:
        return None
    normalized = " ".join(" ".join(parts).split())
    return _truncate(normalized, max_chars) if normalized else None


def extract_declared_facts(body: str, config: ExtractFactsConfig) -> tuple[str, ...]:
    """Return bullet facts from a ``## Facts`` Markdown section.

    The parser ignores fenced code, accepts ordered and unordered list items,
    joins their indented continuation lines, and ends the section at the next
    Markdown heading. It intentionally does not infer facts from prose.
    """

    target = config.heading.casefold()
    in_fence = False
    in_facts = False
    parts: list[str] = []
    result: list[str] = []

    def flush() -> None:
        fact = _finish_fact(parts, max_chars=config.max_fact_chars)
        parts.clear()
        if fact is not None:
            result.append(fact)

    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            heading_text = line.lstrip()[len(heading.group("marks")) :].strip().strip("#").strip()
            if heading_text.casefold() == target:
                if in_facts:
                    flush()
                in_facts = True
                continue
            if in_facts:
                flush()
                break
            continue
        if not in_facts:
            continue
        bullet = _BULLET_RE.match(line)
        if bullet is not None:
            flush()
            parts.append(bullet.group("text"))
        elif parts and line[:1].isspace() and line.strip():
            parts.append(line.strip())
        elif not line.strip():
            flush()
        else:
            flush()
    if in_facts:
        flush()
    return tuple(result)


def _index_text(entries: Iterable[_FactEntry], *, truncated: bool, omitted_facts: int) -> str:
    lines = ["Page facts index:"]
    lines.extend(f"{entry.page_key}: {entry.text}" for entry in entries)
    if truncated:
        lines.append(f"Index truncated: {omitted_facts} declared facts omitted.")
    if len(lines) == 1:
        lines.append("No declared page facts.")
    return "\n".join(lines)


def extract_page_fact_index(
    pages: tuple[_PageInput, ...], config: ExtractFactsConfig
) -> _FactIndexDraft:
    """Build one deterministic, bounded facts snapshot from current pages."""

    entries: list[_FactEntry] = []
    cited_pages: list[UUID] = []
    all_page_ids: list[UUID] = []
    for page in sorted(pages, key=lambda item: (item.key, str(item.id))):
        all_page_ids.append(page.id)
        facts = extract_declared_facts(_page_body(page), config)
        if facts:
            cited_pages.append(page.id)
        seen: set[str] = set()
        for fact in facts:
            dedupe_key = fact.casefold()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            entries.append(_FactEntry(page_key=page.key, text=fact))

    omitted_facts = max(0, len(entries) - config.max_facts)
    retained = tuple(entries[: config.max_facts])
    page_keys = tuple(dict.fromkeys(entry.page_key for entry in retained))
    citations = tuple(dict.fromkeys(cited_pages or all_page_ids))
    return _FactIndexDraft(
        text=_index_text(retained, truncated=omitted_facts > 0, omitted_facts=omitted_facts),
        content=_FactIndexContent(
            facts=retained,
            page_keys=page_keys,
            truncated=omitted_facts > 0,
            omitted_facts=omitted_facts,
        ),
        citations=citations,
    )


async def extract_facts(
    context: TaskContext, value: ExtractFactsInput, config: TaskConfigModel
) -> TaskResult[list[_FactIndexDraft]]:
    """Task adapter entry point with precise page-derived citation authority."""

    del context
    assert isinstance(config, ExtractFactsConfig)
    draft = extract_page_fact_index(value.records, config)
    source_ids = frozenset(page.id for page in (*value.records, *value.changed_records))
    citation_ids = frozenset(draft.citations)
    return TaskResult([draft], source_ids=source_ids, citation_ids=citation_ids)


register_task(
    "extract_facts",
    implementation_hash=hashlib.sha256(b"memseek.extract_facts.v1").hexdigest(),
    config_model=ExtractFactsConfig,
    input_type=ExtractFactsInput,
    output_type=list[_FactIndexDraft],
    handler=extract_facts,
)


__all__ = [
    "ExtractFactsConfig",
    "ExtractFactsInput",
    "extract_declared_facts",
    "extract_facts",
    "extract_page_fact_index",
]
