"""Build the immutable context one Agent run starts from.

The Computer supplies an environment; the Agent supplies a harness. This module
resolves both into the files a run is given and says what each one is for, so
the runtime can assemble a prompt and a tool surface without inspecting bytes to
guess. It is the single implementation for both the durable-invocation and the
batch-derivation paths, which previously built the same thing twice.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from .artifacts import render_artifact
from .config import Settings
from .db import DatabasePool
from .definitions.loader import DefinitionCatalog
from .definitions.models import ToolsetDefinition, ToolSourceDefinition
from .definitions.toolsets import effective_computer, effective_toolset
from .lessons import SkillLessons, render_instructions, render_playbook, resolve_toolset
from .lessons import search_spec as lessons_search_spec

FileRole = Literal["instructions", "skill", "reference", "view", "manifest", "writeback_schemas"]

MANIFEST_PATH = "/.memseek/manifest.json"
INSTRUCTIONS_PATH = "/.memseek/instructions.md"
WRITEBACK_SCHEMAS_PATH = "/.memseek/writeback-schemas.json"
# Beside a skill's SKILL.md: what earlier runs learned, and how to record more.
PLAYBOOK_FILE = "PLAYBOOK.md"
LESSONS_FILE = "LESSONS.md"


class MaterializationError(RuntimeError):
    """Context for an Agent cannot be built from the catalog and the workspace."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class MaterializedFile:
    """One file written into ``/.memseek``, and what it is for.

    ``context_files`` still carries the bytes and is what the runtime hashes and
    diffs for tampering. This says only how to *treat* the same path, which is
    why adding it changes no existing guarantee.
    """

    path: str
    role: FileRole
    skill_name: str | None = None
    description: str | None = None
    prompt: bool = True
    tool: str | None = None

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": self.path, "role": self.role}
        if self.role == "skill":
            payload["skill"] = {"name": self.skill_name, "description": self.description}
        if self.role in {"reference", "instructions"} and not self.prompt:
            payload["prompt"] = False
        if self.tool is not None:
            payload["tool"] = self.tool
        return payload


@dataclass(frozen=True, slots=True)
class AgentMaterialization:
    context_files: Mapping[str, str]
    files: tuple[MaterializedFile, ...]
    toolset: ToolsetDefinition
    toolset_ref: str | None
    citation_ids: frozenset[UUID]
    lessons: tuple[SkillLessons, ...] = ()

    def descriptor_json(self) -> dict[str, Any]:
        return {"version": 1, "files": [item.as_json() for item in self.files]}

    def toolset_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ref": self.toolset_ref,
            "hash": self.toolset.definition_hash or None,
            "instructions": self.toolset.instructions,
            "tools": [source.model_dump(mode="json") for source in self.toolset.sources],
        }
        if self.lessons:
            payload["lessons"] = [spec.as_json() for spec in self.lessons]
        return payload


def _skill_document(name: str, description: str, body: str) -> str:
    """A skill as the Agent SDK loads one: the offer, then the procedure.

    The two values are repeated in the typed descriptor, but they live in the
    bytes as well because the bytes are what the runtime hashes — a prompt that
    described a file the tamper check covers, from a source it does not, would
    be a claim nothing verifies.
    """

    # Deliberately hand-written rather than dumped by a YAML library: both values
    # are already constrained, and a dumper would make hashed bytes depend on a
    # library version.
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n"


async def build_agent_materialization(
    pool: DatabasePool,
    *,
    workspace: str,
    entity: str,
    computer_ref: str,
    agent_ref: str,
    catalog: DefinitionCatalog,
    settings: Settings,
) -> AgentMaterialization:
    """Resolve one Agent run's immutable context and its tool surface."""

    try:
        computer = catalog.resolve_computer(computer_ref)
        agent = catalog.resolve_agent(agent_ref)
        toolset = effective_toolset(agent, computer, catalog)
        computer = effective_computer(agent, computer, catalog)
    except (KeyError, ValueError) as exc:
        raise MaterializationError("reference", str(exc)) from exc
    lessons = resolve_toolset(toolset, catalog, settings)

    builder = _Builder(
        pool=pool,
        workspace=workspace,
        entity=entity,
        catalog=catalog,
        settings=settings,
    )
    await builder.add(INSTRUCTIONS_PATH, agent.instructions, role="instructions")
    for source in toolset.sources:
        if source.kind == "skill":
            await builder.add_skill(source)
    await builder.add_lessons(lessons)
    for mount in computer.context:
        if mount.artifact in builder.rendered:
            # The Agent already declares this artifact. Rendering it again would
            # bill the retrieval twice and put the same text in the prompt twice.
            continue
        artifact = catalog.resolve_artifact(mount.artifact)
        await builder.add(
            mount.path, mount.artifact, role="reference", prompt=artifact.kind == "prompt"
        )

    builder.add_literal(
        MANIFEST_PATH,
        _canonical_json(
            {
                "entity": entity,
                "computer": computer_ref,
                "agent": agent_ref,
                "toolset": agent.toolset,
                "sources": builder.manifests,
            }
        ),
        role="manifest",
    )
    schemas = _writeback_schemas(computer, catalog)
    if schemas:
        builder.add_literal(
            WRITEBACK_SCHEMAS_PATH, _canonical_json(schemas), role="writeback_schemas"
        )
    return AgentMaterialization(
        context_files=builder.files,
        files=tuple(builder.described),
        toolset=toolset,
        toolset_ref=agent.toolset,
        citation_ids=frozenset(builder.ids),
        lessons=lessons,
    )


def _writeback_schemas(computer: Any, catalog: DefinitionCatalog) -> list[dict[str, Any]]:
    return [
        {
            "path": item.path,
            "collection": item.collection,
            "schema": catalog.resolve_collection(item.collection).content_schema,
            "mode": catalog.resolve_collection(item.collection).mode,
        }
        for item in computer.writeback
        if item.type != "final_result" and item.collection is not None
    ]


@dataclass(slots=True)
class _Builder:
    pool: DatabasePool
    workspace: str
    entity: str
    catalog: DefinitionCatalog
    settings: Settings
    files: dict[str, str] = field(default_factory=dict)
    described: list[MaterializedFile] = field(default_factory=list)
    manifests: list[dict[str, Any]] = field(default_factory=list)
    ids: set[UUID] = field(default_factory=set)
    rendered: set[str] = field(default_factory=set)

    async def _render(self, path: str, reference: str) -> str:
        if path in self.files:
            raise MaterializationError("materialization", f"duplicate context path {path!r}")
        artifact = self.catalog.resolve_artifact(reference)
        parameters = {"entity": self.entity} if "entity" in artifact.parameters else {}
        rendered = await render_artifact(
            self.pool,
            workspace=self.workspace,
            name=reference,
            parameters=parameters,
            catalog=self.catalog,
            settings=self.settings,
        )
        manifest = dict(rendered["manifest"])
        self.manifests.append({"path": path, **manifest})
        self.ids.update(UUID(str(value)) for value in manifest["input_record_ids"])
        self.rendered.add(reference)
        return str(rendered["rendered"])

    async def add(self, path: str, reference: str, *, role: FileRole, prompt: bool = True) -> None:
        self.files[path] = await self._render(path, reference)
        self.described.append(MaterializedFile(path=path, role=role, prompt=prompt))

    async def add_skill(self, source: ToolSourceDefinition) -> None:
        assert source.artifact is not None
        artifact = self.catalog.resolve_artifact(source.artifact)
        name = source.resolved_skill_name
        if artifact.description is None:
            # Progressive disclosure needs something to offer. Without a
            # description the skill cannot be a tool, so it stays inline exactly
            # as it did before this mechanism existed, rather than being offered
            # under a name with nothing to say about when it applies.
            path = f"/.memseek/skills/{name}.md"
            self.files[path] = await self._render(path, source.artifact)
            self.described.append(MaterializedFile(path=path, role="reference"))
            return
        path = f"/.memseek/skills/{name}/SKILL.md"
        body = await self._render(path, source.artifact)
        self.files[path] = _skill_document(name, artifact.description, body)
        self.described.append(
            MaterializedFile(
                path=path, role="skill", skill_name=name, description=artifact.description
            )
        )

    async def add_lessons(self, specs: tuple[SkillLessons, ...]) -> None:
        """Each learning skill's playbook and recording instructions, beside its SKILL.md.

        A skill with no lessons yet gets no playbook. Neither file is prompt
        text here: the provider decides, from the run's learning mode, whether
        the agent sees them at all.
        """

        # Imported here: the search engine imports this package's neighbours.
        from .search.engine import execute_search
        from .search.spec import SearchSpec

        for spec in specs:
            directory = f"/.memseek/skills/{spec.skill}"
            if spec.playbook:
                path = f"{directory}/{PLAYBOOK_FILE}"
                search = SearchSpec.model_validate(lessons_search_spec(self.entity, spec.skill))
                result = await execute_search(
                    self.pool,
                    workspace=self.workspace,
                    spec=search,
                    catalog=self.catalog,
                    settings=self.settings,
                )
                hits = list(result["hits"])
                playbook = render_playbook(spec, hits)
                if playbook is not None:
                    ids = [str(hit["id"]) for hit in hits]
                    self.manifests.append(
                        {"path": path, "source": "lessons", "input_record_ids": ids}
                    )
                    self.ids.update(UUID(value) for value in ids)
                    self.files[path] = playbook
                    self.described.append(
                        MaterializedFile(path=path, role="reference", prompt=False)
                    )
            if spec.collect:
                path = f"{directory}/{LESSONS_FILE}"
                # The tool is named for the lessons collection; see writeback.tool_name.
                self.files[path] = render_instructions(spec, tool="record_lessons")
                self.described.append(MaterializedFile(path=path, role="reference", prompt=False))

    def add_literal(self, path: str, content: str, *, role: FileRole) -> None:
        self.files[path] = content
        self.described.append(MaterializedFile(path=path, role=role))


__all__ = [
    "AgentMaterialization",
    "MaterializationError",
    "MaterializedFile",
    "build_agent_materialization",
]
