"""Skill lessons: what a loaded skill records while in use, and reads back next run.

A toolset turns learning on for a skill with ``learning: true``. What is worth
learning is declared beside the skill (``lessons:`` on its pack or artifact)
and may be overridden on the source. Everything else is built in and lives
here: the one ``lessons`` collection every learning catalog shares, the
recording instructions and the playbook, both generated from the skill's
declared kinds, and the tool schema that holds each lesson to its skill's
kinds. Nothing here knows any one skill's domain.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from memseek.definitions.models import LessonSpec, ToolsetDefinition, ToolSourceDefinition

if TYPE_CHECKING:
    from memseek.config import Settings
    from memseek.definitions.loader import DefinitionCatalog

LESSONS_COLLECTION = "lessons"
LESSONS_REF = f"{LESSONS_COLLECTION}@1"
LESSONS_RECORD_TYPE = "lesson"
LESSONS_PATH = "/outbox/lessons.jsonl"
# The one search profile a synthesized collection can count on by name.
LESSONS_SEARCH_PROFILE = "pg_default"

# What a skill that declares nothing gets.
DEFAULT_KINDS: Mapping[str, str] = {
    "helper": "Code that did the job, so the next run can run it instead of writing it again.",
    "tip": "Something that worked and is worth doing again.",
    "pitfall": "Something that went wrong or wasted time, or an earlier lesson that proved "
    "false, and what is true now.",
}
DEFAULT_MAX_PER_RUN = 8
# How many of a skill's newest lessons one playbook shows.
PLAYBOOK_LESSONS = 40

_TEXT_MAX = 480


@dataclass(frozen=True, slots=True)
class SkillLessons:
    """One skill's merged lesson spec, as a run uses it."""

    skill: str
    kinds: Mapping[str, str]
    require: tuple[str, ...]
    code: tuple[str, ...]
    guidance: str | None
    max_per_run: int
    collect: bool
    playbook: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "kinds": dict(self.kinds),
            "require": list(self.require),
            "code": list(self.code),
            "guidance": self.guidance,
            "max_per_run": self.max_per_run,
            "collect": self.collect,
            "playbook": self.playbook,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> SkillLessons:
        return cls(
            skill=str(value["skill"]),
            kinds={str(key): str(text) for key, text in dict(value["kinds"]).items()},
            require=tuple(value.get("require") or ()),
            code=tuple(value.get("code") or ()),
            guidance=value.get("guidance"),
            max_per_run=int(value.get("max_per_run") or DEFAULT_MAX_PER_RUN),
            collect=bool(value.get("collect", True)),
            playbook=bool(value.get("playbook", True)),
        )


def resolve(source: ToolSourceDefinition, declared: LessonSpec | None) -> SkillLessons:
    """Merge the defaults, the skill's own ``lessons:``, then the source's overrides.

    Kinds merge (a later layer adds kinds or re-describes one); every other
    field is replaced by the last layer that sets it.
    """

    learning = source.learning
    assert learning is not None
    layers = [layer for layer in (declared, learning) if layer is not None]
    kinds: dict[str, str] = {}
    declares_kinds = any(layer.kinds is not None for layer in layers)
    if not declares_kinds:
        kinds.update(DEFAULT_KINDS)
    for layer in layers:
        kinds.update(layer.kinds or {})
    require: tuple[str, ...] = ()
    code: tuple[str, ...] = ("helper",) if not declares_kinds else ()
    guidance: str | None = None
    max_per_run = DEFAULT_MAX_PER_RUN
    for layer in layers:
        require = layer.require if layer.require is not None else require
        code = layer.code if layer.code is not None else code
        guidance = layer.guidance if layer.guidance is not None else guidance
        max_per_run = layer.max_per_run if layer.max_per_run is not None else max_per_run
    return SkillLessons(
        skill=source.installed_skill_name,
        kinds=kinds,
        require=require,
        code=code,
        guidance=guidance,
        max_per_run=max_per_run,
        collect=learning.collect,
        playbook=learning.playbook,
    )


def resolve_toolset(
    toolset: ToolsetDefinition, catalog: DefinitionCatalog, settings: Settings
) -> tuple[SkillLessons, ...]:
    """Every learning skill's merged spec, in toolset order."""

    from memseek.skillpacks import load_skillpack

    specs: list[SkillLessons] = []
    for source in toolset.learning_sources:
        if source.kind == "skillpack":
            assert source.pack is not None
            declared = load_skillpack(source.pack, settings.skillpack_paths).lessons
        else:
            assert source.artifact is not None
            declared = catalog.resolve_artifact(source.artifact).lessons
        specs.append(resolve(source, declared))
    return tuple(specs)


def undeclared_kinds(spec: SkillLessons) -> list[str]:
    """Kinds that ``require`` or ``code`` name but the spec does not declare."""

    return sorted({*spec.require, *spec.code} - set(spec.kinds))


def collection_document(search_profile: str = LESSONS_SEARCH_PROFILE) -> dict[str, Any]:
    """The ``lessons`` collection every catalog with a learning skill gets.

    Its shape never depends on any skill's kinds, which are checked per run, so
    changing what a skill learns never changes the catalog's hash.
    """

    return {
        "name": LESSONS_COLLECTION,
        "version": 1,
        "active": True,
        "mode": "event",
        "schema": {
            "type": "object",
            "required": ["text", "skill", "kind"],
            "properties": {
                "text": {"type": "string", "pattern": f"^[^\\n]{{1,{_TEXT_MAX}}}$"},
                "skill": {"type": "string", "pattern": "^[a-z][a-z0-9._-]{0,63}$"},
                "kind": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"},
                "detail": {"type": "string", "minLength": 1, "maxLength": 4000},
                "code": {"type": "string", "minLength": 1, "maxLength": 8000},
            },
            "additionalProperties": False,
        },
        "fields": {
            "skill": {"path": "content.skill", "type": "string", "filter": True, "project": True},
            "kind": {"path": "content.kind", "type": "string", "filter": True, "project": True},
            "code": {"path": "content.code", "type": "string", "project": True},
        },
        "required_processors": [],
        "search_profile": search_profile,
    }


def writeback_document() -> dict[str, Any]:
    """The Computer writeback a run with a collecting skill gets."""

    return {
        "path": LESSONS_PATH,
        "type": "observations",
        "review": False,
        "collection": LESSONS_REF,
        "record_type": LESSONS_RECORD_TYPE,
    }


def bind_schema(schema: Mapping[str, Any], specs: Iterable[SkillLessons]) -> dict[str, Any]:
    """The collection schema narrowed to this run's collecting skills and their kinds.

    A lesson filed under a skill the run did not load, or under a kind its
    skill does not declare, would be stored and never shown. It is refused
    while the agent can still fix it.
    """

    collecting = sorted((spec for spec in specs if spec.collect), key=lambda spec: spec.skill)
    properties = dict(schema.get("properties") or {})
    properties["skill"] = {**properties.get("skill", {}), "enum": [s.skill for s in collecting]}
    # The tool sees an entry before null fields are dropped, and models send
    # null for an optional field they leave empty.
    for optional in ("detail", "code"):
        if optional in properties:
            properties[optional] = {**properties[optional], "type": ["string", "null"]}
    rules: list[dict[str, Any]] = list(schema.get("allOf") or [])
    for spec in collecting:
        when = {"properties": {"skill": {"const": spec.skill}}, "required": ["skill"]}
        rules.append({"if": when, "then": {"properties": {"kind": {"enum": list(spec.kinds)}}}})
        for kind in spec.code:
            rules.append(
                {
                    "if": {
                        "properties": {"skill": {"const": spec.skill}, "kind": {"const": kind}},
                        "required": ["skill", "kind"],
                    },
                    "then": {"required": ["code"], "properties": {"code": {"type": "string"}}},
                }
            )
    return {**schema, "properties": properties, "allOf": rules}


def require_calls(input_schema: Mapping[str, Any], specs: Iterable[SkillLessons]) -> dict[str, Any]:
    """A tool input schema that refuses a call missing a skill's required kinds.

    A call that records anything for a skill must include one lesson of each
    kind that skill requires.
    """

    schema = json.loads(json.dumps(input_schema))
    records = schema["properties"]["records"]
    rules: list[dict[str, Any]] = []
    for spec in sorted(specs, key=lambda spec: spec.skill):
        if not spec.collect:
            continue
        for kind in spec.require:
            rules.append(
                {
                    "if": {"contains": _entry(spec.skill)},
                    "then": {"contains": _entry(spec.skill, kind)},
                }
            )
    if rules:
        records["allOf"] = [*records.get("allOf", []), *rules]
    return schema


def _entry(skill: str, kind: str | None = None) -> dict[str, Any]:
    wanted: dict[str, Any] = {"skill": {"const": skill}}
    if kind is not None:
        wanted["kind"] = {"const": kind}
    return {
        "type": "object",
        "properties": {
            "content": {"type": "object", "properties": wanted, "required": sorted(wanted)}
        },
        "required": ["content"],
    }


def render_instructions(spec: SkillLessons, *, tool: str | None) -> str:
    """How to record lessons for one skill, generated from its declared kinds."""

    how = f"with the `{tool}` tool" if tool else f"by appending them to `{LESSONS_PATH}`"
    kinds = "\n".join(f"- `{kind}`: {text}" for kind, text in spec.kinds.items())
    rules = [
        f"- `text` is one line under {_TEXT_MAX} characters, readable on its own. It is "
        "the part the next run reads first.",
        f"- `content.skill` is `{spec.skill}`, and `content.kind` is one of the kinds above.",
        "- `content.detail` is optional: the same lesson at more length.",
        "- `content.code` is optional: code that worked, exactly as you ran it.",
        "- `citations` holds the UUID of the record from your context that the lesson "
        "rests on. Put the same UUID in your final `citation_ids`.",
    ]
    for kind in spec.code:
        rules.append(f"- A `{kind}` lesson must carry its code in `content.code`.")
    if spec.require:
        needed = ", ".join(f"`{kind}`" for kind in spec.require)
        rules.append(f"- Every call that records for {spec.skill} must include a {needed} lesson.")
    example = json.dumps(
        {
            "text": "<one line>",
            "content": {"skill": spec.skill, "kind": next(iter(spec.kinds))},
            "citations": ["<record UUID>"],
        }
    )
    parts = [
        f"## Recording lessons for {spec.skill}",
        "Before your final answer, record what you learned while using "
        f"{spec.skill} that a later run would otherwise have to rediscover, {how}. Record "
        f"at most {spec.max_per_run}. Skip anything specific to today's data and anything "
        "the playbook already says. If a playbook lesson proved wrong, record one that "
        "corrects it.",
        f"Kinds of lesson:\n\n{kinds}",
        "Each entry looks like this:\n\n```json\n" + example + "\n```\n\n" + "\n".join(rules),
    ]
    if spec.guidance:
        parts.append(spec.guidance.strip())
    return "\n\n".join(parts) + "\n"


def search_spec(entity: str, skill: str) -> dict[str, Any]:
    """The search that reads one skill's lessons about one entity, newest first."""

    return {
        "mode": "recent",
        "scope": {"entities": [entity], "collections": [LESSONS_COLLECTION], "status": "active"},
        "where": {"skill": {"eq": skill}},
        "k": PLAYBOOK_LESSONS,
        "include": ["text"],
        "fields": ["kind", "code"],
    }


def render_playbook(spec: SkillLessons, hits: Sequence[Mapping[str, Any]]) -> str | None:
    """One skill's playbook: its lessons grouped in the order the skill declares its kinds.

    A kind with no lessons is left out, and so is the whole playbook when there
    are none. Lessons under a kind the skill no longer declares still show, last.
    """

    grouped: dict[str, list[str]] = {}
    for hit in hits:
        fields = hit.get("fields") or {}
        kind = str(fields.get("kind") or "")
        line = f"- {hit.get('text', '').strip()} (id {hit['id']})"
        code = fields.get("code")
        if code:
            fence = "~~~~" if "```" in str(code) else "```"
            body = "\n".join(f"  {row}" for row in str(code).splitlines())
            line += f"\n\n  {fence}\n{body}\n  {fence}"
        grouped.setdefault(kind, []).append(line)
    if not grouped:
        return None
    order = [kind for kind in spec.kinds if kind in grouped]
    order += sorted(kind for kind in grouped if kind not in spec.kinds)
    parts = [
        f"# Playbook: {spec.skill}",
        "What earlier runs learned using this skill here, newest first within each kind. "
        "Start from these instead of rediscovering them, and explore only what they do not "
        "cover.",
    ]
    for kind in order:
        heading = f"## {kind}"
        if kind in spec.kinds:
            heading += f"\n\n{spec.kinds[kind]}"
        parts.append(heading + "\n\n" + "\n".join(grouped[kind]))
    return "\n\n".join(parts) + "\n"


__all__ = [
    "DEFAULT_KINDS",
    "LESSONS_COLLECTION",
    "LESSONS_PATH",
    "LESSONS_REF",
    "SkillLessons",
    "bind_schema",
    "collection_document",
    "render_instructions",
    "render_playbook",
    "require_calls",
    "resolve",
    "resolve_toolset",
    "search_spec",
    "undeclared_kinds",
    "writeback_document",
]
