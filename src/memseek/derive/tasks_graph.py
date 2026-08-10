"""Deterministic structural-graph Tasks.

The graph extractor deliberately has no storage or model capabilities.  It
turns the bounded pages supplied by a derivation into edge drafts; the normal
derivation emission boundary owns validation, provenance, and the canonical
write.  Its only cross-page input is a bounded title/basename index used to
resolve bare wikilinks such as ``[[Acme]]``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memseek.derive.tasks import TaskConfigModel, TaskContext, TaskResult, register_task
from memseek.graph import GraphTraversalRequest

type GraphPredicate = Literal[
    "works_at",
    "invested_in",
    "founded",
    "advises",
    "attended",
    "mentions",
    "image_of",
    "wikilink_basename",
]
type LinkSource = Literal["markdown", "wikilink-resolved", "bare-slug"]

DEFAULT_ENTITY_DIRS = (
    "people",
    "companies",
    "meetings",
    "concepts",
    "deal",
    "civic",
    "project",
    "projects",
    "source",
    "media",
    "yc",
    "tech",
    "finance",
    "personal",
    "openclaw",
    "entities",
)
_PREDICATES = frozenset(
    {
        "works_at",
        "invested_in",
        "founded",
        "advises",
        "attended",
        "mentions",
        "image_of",
        "wikilink_basename",
    }
)
_PAGE_TYPE_ALIASES = {"people": "person", "meetings": "meeting", "images": "image"}

# These production rules deliberately use a bounded context window.  They are
# ordered to match gbrain's precedence: founded, investment, advising, work.
_DEFAULT_PREDICATE_PATTERNS: dict[str, str] = {
    "works_at": (
        r"\b(?:CEO of|CTO of|COO of|CFO of|CMO of|CRO of|VP at|VP of|works at|"
        r"worked at|working at|employed by|employed at|joined as|joined the team|"
        r"engineer at|engineer for|director at|director of|head of|currently at|"
        r"previously at|previously worked at|stint at|stint as|tenure at|tenure as|"
        r"role at|position at|(?:senior|staff|principal|lead|backend|frontend|"
        r"full-?stack|ML|data|security) engineer at|(?:his|her|their|my) time at)\b"
    ),
    "invested_in": (
        r"\b(?:invested in|invests in|investing in|invest in|investment in|"
        r"investments in|backed by|funding from|funded by|raised from|led the "
        r"(?:seed|Series|round|investment)|led .{0,30}(?:Series [A-Z]|seed|"
        r"round|investment)|participated in (?:the )?(?:seed|Series|round)|"
        r"wrote (?:a |the )?check|first check|early investor|portfolio "
        r"(?:company|includes)|board seat (?:at|in|on)|term sheet for)\b"
    ),
    "founded": (
        r"\b(?:founded|co-?founded|started the company|incorporated|founder of|"
        r"founders? (?:include|are)|the founder|is a co-?founder|"
        r"is one of the founders)\b"
    ),
    "advises": (
        r"\b(?:advises|advised|advisor (?:to|at|for|of)|advisory "
        r"(?:board|role|position|capacity|engagement|partnership|contract|"
        r"relationship|work)|board advisor|on .{0,20} advisory board|"
        r"joined .{0,20} advisory board|in an? advisory (?:capacity|role|position)|"
        r"as an? (?:advisor|security advisor|technical advisor|strategic advisor|"
        r"industry advisor|product advisor|board advisor|senior advisor)|"
        r"(?:strategic|technical|security|product|industry|senior|board) advisor "
        r"(?:to|at|for|of)|consults for|consulting role (?:at|with))\b"
    ),
}
_PARTNER_ROLE_PATTERN = (
    r"\b(?:partner at|partner of|venture partner|VC partner|invested early|investor at|"
    r"investor in|portfolio|venture capital|early-stage investor|seed investor|"
    r"fund [A-Z]|invests across|backs companies)\b"
)
_ADVISOR_ROLE_PATTERN = (
    r"\b(?:full-time advisor|professional advisor|advises (?:multiple|several|various)|"
    r"is an? (?:advisor|security advisor|technical advisor|strategic advisor|"
    r"industry advisor|product advisor|senior advisor)|took on advisory roles|"
    r"(?:her|his|their) advisory (?:work|role|engagement|portfolio)|"
    r"serves as (?:an )?advisor)\b"
)
_EMPLOYEE_ROLE_PATTERN = (
    r"\b(?:is an? (?:senior|staff|principal|lead|backend|frontend|full-?stack|ML|"
    r"data|security|DevOps|platform)? ?engineer at|is an? (?:senior|staff|principal|"
    r"lead)? ?(?:developer|designer|product manager|engineering manager|director|VP) "
    r"(?:at|of)|holds? the (?:CTO|CEO|CFO|COO|CMO|CRO|VP) "
    r"(?:role|position|seat|title) at|is the (?:CTO|CEO|CFO|COO|CMO|CRO) of|"
    r"employee at|on the team at|works on .{0,30} at)\b"
)


class ExtractRelationsConfig(TaskConfigModel):
    """Author-controlled boundaries for the deterministic extractor."""

    dir_pattern: tuple[str, ...] = DEFAULT_ENTITY_DIRS
    predicate_regex_overrides: dict[GraphPredicate, str] = Field(default_factory=dict)
    emit_mentions: bool = False
    context_chars: int = Field(default=240, ge=40, le=1_000)

    @field_validator("dir_pattern")
    @classmethod
    def validate_dirs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("dir_pattern must contain at least one entity directory")
        normalized = tuple(directory.strip().lower() for directory in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("dir_pattern must not contain duplicate directories")
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", directory) for directory in normalized):
            raise ValueError("dir_pattern entries must be slug directories")
        return normalized

    @field_validator("predicate_regex_overrides")
    @classmethod
    def validate_overrides(cls, value: dict[GraphPredicate, str]) -> dict[GraphPredicate, str]:
        unsupported = set(value) - _PREDICATES
        if unsupported:
            raise ValueError(f"unsupported predicate overrides: {sorted(unsupported)}")
        for predicate, pattern in value.items():
            if predicate not in _DEFAULT_PREDICATE_PATTERNS:
                raise ValueError(f"predicate {predicate!r} does not support a regex override")
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"invalid {predicate} regex: {exc}") from exc
        return value


class GraphTaskConfig(TaskConfigModel):
    """Traversal is supplied as the templatable Task input, not static config."""


class _PageInput(BaseModel):
    """The bounded source-record fields needed by the pure extractor."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: UUID
    key: str
    content: dict[str, Any]

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not value or len(value) > 128:
            raise ValueError("page key must contain 1 through 128 characters")
        return value


class ExtractRelationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    records: tuple[_PageInput, ...] = Field(max_length=50)
    known_pages: tuple[_PageInput, ...] = Field(default=(), max_length=64)


class _EdgeContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str
    object: str
    predicate: GraphPredicate
    link_source: LinkSource
    context: str
    confidence: float = Field(ge=0, le=1)


class _EdgeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    content: _EdgeContent
    citations: tuple[UUID, ...]


def strip_code_blocks(content: str) -> str:
    """Blank fenced and inline code while preserving character positions."""

    output: list[str] = []
    index = 0
    while index < len(content):
        if content.startswith("```", index):
            end = content.find("```", index + 3)
            if end == -1:
                output.append(" " * (len(content) - index))
                break
            output.append(" " * (end + 3 - index))
            index = end + 3
            continue
        if content[index] == "`":
            end = content.find("`", index + 1)
            if end != -1 and "\n" not in content[index + 1 : end]:
                output.append(" " * (end + 1 - index))
                index = end + 1
                continue
        output.append(content[index])
        index += 1
    return "".join(output)


def infer_predicate(
    page_type: str,
    context: str,
    global_context: str,
    target: str,
    *,
    regex_overrides: Mapping[GraphPredicate, str] | None = None,
) -> tuple[GraphPredicate, float]:
    """Classify one resolved link using gbrain's deterministic precedence."""

    normalized_type = _PAGE_TYPE_ALIASES.get(page_type.lower(), page_type.lower())
    if normalized_type == "image":
        return "image_of", 1.0
    if normalized_type == "meeting":
        return "attended", 1.0
    if normalized_type == "media":
        return "mentions", 0.6

    patterns = {**_DEFAULT_PREDICATE_PATTERNS, **(regex_overrides or {})}
    for predicate in ("founded", "invested_in", "advises", "works_at"):
        if re.search(patterns[predicate], context, re.IGNORECASE):
            return predicate, 0.95  # type: ignore[return-value]

    if normalized_type == "person" and target.startswith("companies/"):
        if re.search(_PARTNER_ROLE_PATTERN, global_context, re.IGNORECASE):
            return "invested_in", 0.8
        if re.search(_ADVISOR_ROLE_PATTERN, global_context, re.IGNORECASE):
            return "advises", 0.8
        if re.search(_EMPLOYEE_ROLE_PATTERN, global_context, re.IGNORECASE):
            return "works_at", 0.8
    return "mentions", 0.6


def _mask_ranges(content: str, ranges: list[tuple[int, int]]) -> str:
    characters = list(content)
    for start, end in ranges:
        for index in range(start, min(end, len(characters))):
            characters[index] = " "
    return "".join(characters)


def _excerpt(content: str, index: int, width: int) -> str:
    half = width // 2
    return " ".join(content[max(0, index - half) : index + half].split())


def _page_body(page: _PageInput) -> str:
    body = page.content.get("body", page.content.get("text", ""))
    return body if isinstance(body, str) else ""


def _page_type(page: _PageInput) -> str:
    declared = page.content.get("type")
    if isinstance(declared, str) and declared:
        return declared
    return page.key.split("/", 1)[0]


def _reference_patterns(
    directories: tuple[str, ...],
) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    directory_pattern = "(?:" + "|".join(re.escape(directory) for directory in directories) + ")"
    markdown = re.compile(
        rf"\[([^\]\n]+)\]\((?:\.\./)*(?P<target>{directory_pattern}/[^)\s#]+)"
        r"(?:#[^)]*)?\)",
        re.IGNORECASE,
    )
    wikilink = re.compile(
        r"\[\[(?P<target>[^|\]#\n]+)(?:#[^|\]]*)?(?:\|[^\]]+)?\]\]",
        re.IGNORECASE,
    )
    bare_slug = re.compile(
        rf"\b(?P<target>{directory_pattern}/[a-z0-9][a-z0-9/_-]*[a-z0-9])\b",
        re.IGNORECASE,
    )
    return markdown, wikilink, bare_slug


def _normalize_wikilink_basename(value: str) -> str:
    """Normalize a title or final slug component for deterministic matching."""

    without_extension = value.strip().removesuffix(".md")
    return re.sub(r"[\s_-]+", "-", without_extension.casefold()).strip("-")


def _is_direct_page_key(target: str, directories: tuple[str, ...]) -> bool:
    """Whether a wikilink already names an allowed directory/slug directly."""

    if "/" not in target:
        return False
    directory, _, remainder = target.partition("/")
    return directory.casefold() in directories and bool(remainder.strip("/"))


def _wikilink_basename_index(pages: tuple[_PageInput, ...]) -> dict[str, tuple[str, ...]]:
    """Map normalized titles and terminal page slugs to every matching page key.

    The mapping intentionally retains ambiguity.  A bare wikilink that matches
    several pages emits one cited edge to each target rather than guessing a
    winner from insertion order or creating a hidden resolver state.
    """

    matches: dict[str, set[str]] = {}
    for page in pages:
        aliases = {page.key.rsplit("/", 1)[-1]}
        title = page.content.get("title")
        if isinstance(title, str) and title.strip():
            aliases.add(title)
        for alias in aliases:
            normalized = _normalize_wikilink_basename(alias)
            if normalized:
                matches.setdefault(normalized, set()).add(page.key)
    return {name: tuple(sorted(keys)) for name, keys in matches.items()}


def extract_page_edges(
    pages: tuple[_PageInput, ...],
    config: ExtractRelationsConfig,
    *,
    known_pages: tuple[_PageInput, ...] = (),
) -> list[_EdgeDraft]:
    """Extract deterministic edge drafts from a bounded collection of pages."""

    markdown, wikilink, bare_slug = _reference_patterns(config.dir_pattern)
    basename_index = _wikilink_basename_index(known_pages)
    drafts: list[_EdgeDraft] = []
    seen: set[tuple[str, str, GraphPredicate, LinkSource]] = set()
    for page in sorted(pages, key=lambda item: (item.key, str(item.id))):
        subject = page.key
        body = _page_body(page)
        if not body:
            continue
        scanned = strip_code_blocks(body)
        page_type = _page_type(page)
        masked_ranges: list[tuple[int, int]] = []
        references: list[tuple[str, int, LinkSource, GraphPredicate | None]] = []
        for match in markdown.finditer(scanned):
            target = match.group("target").removesuffix(".md")
            references.append((target, match.start("target"), "markdown", None))
            masked_ranges.append((match.start(), match.end()))
        for match in wikilink.finditer(scanned):
            raw_target = match.group("target").strip()
            target = raw_target.removesuffix(".md")
            if _is_direct_page_key(target, config.dir_pattern):
                references.append((target, match.start("target"), "markdown", None))
            else:
                for resolved in basename_index.get(_normalize_wikilink_basename(raw_target), ()):
                    references.append(
                        (
                            resolved,
                            match.start("target"),
                            "wikilink-resolved",
                            "wikilink_basename",
                        )
                    )
            masked_ranges.append((match.start(), match.end()))
        bare_scan = _mask_ranges(scanned, masked_ranges)
        for match in bare_slug.finditer(bare_scan):
            references.append((match.group("target"), match.start("target"), "bare-slug", None))

        for target, index, link_source, resolved_predicate in sorted(
            references, key=lambda item: (item[1], item[0], item[2])
        ):
            if target == subject:
                continue
            context = _excerpt(scanned, index, config.context_chars)
            if resolved_predicate is not None:
                predicate, confidence = resolved_predicate, 1.0
            else:
                predicate, confidence = infer_predicate(
                    page_type,
                    context,
                    scanned,
                    target,
                    regex_overrides=config.predicate_regex_overrides,
                )
            if predicate == "mentions" and not config.emit_mentions:
                continue
            dedupe_key = (subject, target, predicate, link_source)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            content = _EdgeContent(
                subject=subject,
                object=target,
                predicate=predicate,
                link_source=link_source,
                context=context,
                confidence=confidence,
            )
            drafts.append(
                _EdgeDraft(
                    text=f"{subject} {predicate} {target}",
                    content=content,
                    citations=(page.id,),
                )
            )
    return drafts


async def extract_relations(
    context: TaskContext, value: ExtractRelationsInput, config: TaskConfigModel
) -> list[_EdgeDraft]:
    """Task adapter entry point; all provenance stays with the source input."""

    del context
    assert isinstance(config, ExtractRelationsConfig)
    return extract_page_edges(value.records, config, known_pages=value.known_pages)


async def graph(
    context: TaskContext, value: GraphTraversalRequest, config: TaskConfigModel
) -> TaskResult[dict[str, Any]]:
    """Expose the shared bounded traversal to derivations such as ``answer``."""

    assert isinstance(config, GraphTaskConfig)
    return await context.traverse(value)


register_task(
    "extract_relations",
    implementation_hash=hashlib.sha256(b"memseek.extract_relations.v2").hexdigest(),
    config_model=ExtractRelationsConfig,
    input_type=ExtractRelationsInput,
    output_type=list[_EdgeDraft],
    handler=extract_relations,
)
register_task(
    "graph",
    implementation_hash=hashlib.sha256(b"memseek.graph_task.v1").hexdigest(),
    config_model=GraphTaskConfig,
    input_type=GraphTraversalRequest,
    output_type=dict[str, Any],
    handler=graph,
)


__all__ = [
    "DEFAULT_ENTITY_DIRS",
    "ExtractRelationsConfig",
    "ExtractRelationsInput",
    "GraphTaskConfig",
    "extract_page_edges",
    "extract_relations",
    "graph",
    "infer_predicate",
    "strip_code_blocks",
]
