"""Interactive dependency graph for one catalog package.

A catalog outgrows its directory listing quickly: a Computer mounts Artifacts, an
Agent selects a Computer and a Context Policy, a Pipeline runs both and emits
into a Collection that a Processor then annotates, and an MCP interface exposes
some of the same parts again under tool names.  This module projects that
already-compiled definition graph for one Package into nodes and typed edges,
which :mod:`memseek.catalog_graph_page` renders as one self-contained page.

The projection reads a compiled :class:`DefinitionCatalog`, never the YAML text.
What it draws is therefore exactly what the loader accepted — the same resolved
versions, defaults, and hashes the runtime uses — rather than a second, weaker
interpretation of the same files that could disagree with it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memseek.config import Settings, get_settings
from memseek.definitions.base import split_exact_reference
from memseek.definitions.hashing import dump_definition
from memseek.definitions.loader import DefinitionCatalog
from memseek.definitions.models import (
    AgentDefinition,
    ArtifactDefinition,
    CollectionDefinition,
    ComputerDefinition,
    ContextPolicyDefinition,
    McpDefinition,
    PackageDefinition,
    ProcessorDefinition,
    ProgramDefinition,
    ToolsetDefinition,
    ViewDefinition,
)
from memseek.derive.schema import PipelineDefinition, RecordScope

# Node kinds, in the order the legend and the detail panel present them.  The
# order is also the layout's tie-break, so a rendered graph stays stable between
# runs of the same catalog.
KINDS: tuple[str, ...] = (
    "collection",
    "derivation",
    "processor",
    "computer",
    "program",
    "agent",
    "artifact",
    "context_policy",
    "view",
    "mcp",
    "mcp_tool",
    "toolset",
    "tool",
    "trigger",
    "model",
    "search_profile",
)

# Edge relations.  Each one answers a different question about the catalog, so
# they are filterable separately in the page.
RELATIONS: tuple[str, ...] = (
    "triggers",
    "reads",
    "writes",
    "runs",
    "mounts",
    "uses",
    "annotates",
    "routes",
    "exposes",
)


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One definition in the package, with the facts worth seeing at a glance."""

    id: str
    kind: str
    name: str
    version: str | None
    summary: str
    facts: tuple[tuple[str, str], ...]
    member: bool
    definition: Mapping[str, Any] | None

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "facts": [[label, value] for label, value in self.facts],
            "member": self.member,
            "definition": self.definition,
        }


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """One declared reference, drawn in the direction its data or control moves.

    ``flow`` separates the references that order the graph from the ones that
    only decorate it.  A ``forward`` edge means the source genuinely feeds the
    target, so the layout can put the source to its left; a ``none`` edge is a
    binding — the model an Agent selects, the Processor that annotates a
    Collection — which has no place in that ordering and would otherwise drag
    unrelated parts across the canvas.
    """

    source: str
    target: str
    relation: str
    label: str
    flow: str = "forward"

    def as_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "label": self.label,
            "flow": self.flow,
        }


@dataclass(frozen=True, slots=True)
class CatalogGraph:
    """The projected graph for one package."""

    package: str
    catalog_hash: str
    package_hash: str
    overview: tuple[tuple[str, str], ...]
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "catalog_hash": self.catalog_hash,
            "package_hash": self.package_hash,
            "overview": [[label, value] for label, value in self.overview],
            "kinds": list(KINDS),
            "relations": list(RELATIONS),
            "nodes": [node.as_json() for node in self.nodes],
            "edges": [edge.as_json() for edge in self.edges],
        }


def compile_catalog_directory(
    directory: Path,
    *,
    settings: Settings | None = None,
) -> DefinitionCatalog:
    """Compile a catalog directory through the normal publishing validation."""

    from memseek.sdk import _read_catalog_directory
    from memseek.workspace_catalog import _compile_overlay

    return _compile_overlay(settings or get_settings(), _read_catalog_directory(directory))


def sole_package(catalog: DefinitionCatalog) -> str:
    """Return the one package reference in a catalog, or fail with the choices."""

    references = sorted(f"{name}@{version}" for name, version in catalog.packages)
    if len(references) == 1:
        return references[0]
    if not references:
        raise ValueError("catalog declares no package")
    raise ValueError("catalog declares several packages; select one of " + ", ".join(references))


def build_package_graph(catalog: DefinitionCatalog, package: str) -> CatalogGraph:
    """Project one package's definitions and their declared references."""

    name, version = split_exact_reference(package, semver=True)
    definition = catalog.resolve_package(name, str(version))
    builder = _GraphBuilder(catalog, definition)
    builder.build()
    nodes = tuple(builder.nodes.values())
    return CatalogGraph(
        package=f"{definition.name}@{definition.version}",
        catalog_hash=catalog.catalog_hash,
        package_hash=definition.definition_hash,
        overview=_overview(definition, nodes),
        nodes=nodes,
        edges=tuple(builder.edges),
    )


def _overview(
    package: PackageDefinition, nodes: Sequence[GraphNode]
) -> tuple[tuple[str, str], ...]:
    """What the package declares, before any one part is selected."""

    counts = Counter(node.kind for node in nodes)
    facts: list[tuple[str, str]] = [
        (
            "parts",
            _joined(
                (_plural(counts[kind], kind.replace("_", " ")) for kind in KINDS if counts[kind]),
                limit=len(KINDS),
            ),
        ),
        ("collections", _joined(package.collections)),
        ("processors", _joined(package.processors)),
    ]
    if package.mcp is not None:
        facts.append(("mcp interface", package.mcp))
    if package.optional_search_profiles:
        facts.append(("optional profiles", _joined(package.optional_search_profiles)))
    if package.retentions:
        facts.append(
            (
                "retention",
                _joined(
                    f"{item.name}: {item.collection} after "
                    f"{_plural(item.after_days, 'day')} ({item.cron})"
                    for item in package.retentions
                ),
            )
        )
    return tuple(facts)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _joined(values: Iterable[str], *, limit: int = 6) -> str:
    items = list(values)
    if not items:
        return "—"
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", +{len(items) - limit} more"


@dataclass
class _GraphBuilder:
    catalog: DefinitionCatalog
    package: PackageDefinition
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)
    _seen_edges: set[tuple[str, str, str, str]] = field(default_factory=set)

    # ---- graph assembly ----------------------------------------------------

    def build(self) -> None:
        # Members first, so a node the package declares is never displaced by the
        # placeholder a reference to it would otherwise create.
        for reference in self.package.collections:
            self._collection(reference, member=True)
        for reference in self.package.views:
            self._view(reference, member=True)
        for reference in self.package.artifacts:
            self._artifact(reference, member=True)
        for reference in self.package.computers:
            self._computer(reference, member=True)
        for reference in self.package.programs:
            self._program(reference, member=True)
        for reference in self.package.agents:
            self._agent(reference, member=True)
        for reference in self.package.context_policies:
            self._context_policy(reference, member=True)
        for reference in self.package.toolsets:
            self._toolset(reference, member=True)
        for name in self.package.processors:
            # A package's `processors` list names annotation Processors and
            # Pipelines alike; the catalog decides which one a name is.
            if name in self.catalog.derivations:
                self._derivation(name, member=True)
            else:
                self._processor(name, member=True)
        for name in (*self.package.search_profiles, *self.package.optional_search_profiles):
            self._search_profile(name, member=True)
        for name in self.package.triggers:
            self._trigger(name)
        if self.package.mcp is not None:
            self._mcp(self.package.mcp)

    def _add(self, node: GraphNode) -> str:
        existing = self.nodes.get(node.id)
        if existing is None or (node.member and not existing.member):
            self.nodes[node.id] = node
        return node.id

    def _edge(
        self,
        source: str,
        target: str,
        relation: str,
        label: str = "",
        *,
        flow: str = "forward",
    ) -> None:
        key = (source, target, relation, label)
        if key in self._seen_edges:
            return
        self._seen_edges.add(key)
        self.edges.append(
            GraphEdge(source=source, target=target, relation=relation, label=label, flow=flow)
        )

    # ---- reference resolution ---------------------------------------------

    def _exact(self, reference: str, active: Mapping[str, int]) -> tuple[str, int]:
        """Split ``name@version``, falling back to the active version."""

        if "@" in reference:
            name, version = split_exact_reference(reference)
            return name, int(version)
        return reference, active.get(reference, 1)

    # ---- node loaders ------------------------------------------------------

    def _collection(self, reference: str, *, member: bool = False) -> str:
        name, version = self._exact(reference, self.catalog.active_collections)
        definition = self.catalog.collections.get((name, version))
        node_id = f"collection:{name}@{version}"
        if definition is None:
            return self._add(self._missing(node_id, "collection", name, str(version)))
        if node_id in self.nodes and not member:
            return node_id
        self._add(self._collection_node(node_id, definition, member=member))
        for processor in definition.required_processors:
            self._edge(self._processor(processor), node_id, "annotates", "required", flow="none")
        for processor in definition.optional_processors:
            self._edge(self._processor(processor), node_id, "annotates", "optional", flow="none")
        for profile in (definition.search_profile, *definition.allowed_search_profiles):
            self._edge(node_id, self._search_profile(profile), "routes", "", flow="none")
        return node_id

    def _collection_node(
        self,
        node_id: str,
        definition: CollectionDefinition,
        *,
        member: bool,
    ) -> GraphNode:
        schema = definition.content_schema
        required = tuple(str(value) for value in schema.get("required", ()))
        properties = tuple(str(value) for value in schema.get("properties", {}))
        facts: list[tuple[str, str]] = [
            ("mode", definition.mode),
            ("answerable", "yes" if definition.answerable else "no"),
            ("search profile", definition.search_profile),
            ("required content", _joined(required)),
            ("content properties", _joined(properties)),
        ]
        if definition.fields:
            facts.append(("declared fields", _joined(definition.fields)))
        if definition.required_processors or definition.optional_processors:
            facts.append(
                (
                    "processors",
                    _joined(
                        (
                            *definition.required_processors,
                            *(f"{name} (optional)" for name in definition.optional_processors),
                        )
                    ),
                )
            )
        return GraphNode(
            id=node_id,
            kind="collection",
            name=definition.name,
            version=str(definition.version),
            summary=f"{definition.mode} collection"
            + (", answerable" if definition.answerable else ""),
            facts=tuple(facts),
            member=member,
            definition=dump_definition(definition),
        )

    def _derivation(self, name: str, *, member: bool = False) -> str:
        definition = self.catalog.derivations.get(name)
        node_id = f"derivation:{name}"
        if definition is None:
            return self._add(self._missing(node_id, "derivation", name, None))
        if node_id in self.nodes and not member:
            return node_id
        self._add(self._derivation_node(node_id, definition, member=member))
        for label, collection in self._trigger_scopes(definition):
            self._edge(self._collection(collection), node_id, "triggers", label)
        for source_name, source in definition.sources.items():
            for collection in _source_collections(source):
                self._edge(self._collection(collection), node_id, "reads", source_name)
            view = getattr(source, "view", None)
            if view is not None:
                self._edge(self._view(view), node_id, "reads", source_name)
        for task in definition.tasks:
            for relation, target in self._task_targets(task.config):
                # A task's Computer, Program, and Agent are what the Pipeline
                # runs; its Context Policy and model are settings it is held to.
                flow = "none" if relation == "uses" else "forward"
                self._edge(node_id, target, relation, task.id, flow=flow)
        if definition.model is not None:
            self._edge(node_id, self._model(definition.model), "uses", "model", flow="none")
        emit = definition.emit
        emit_version = emit.collection_version or self.catalog.active_collections.get(
            emit.collection, 1
        )
        self._edge(
            node_id,
            self._collection(f"{emit.collection}@{emit_version}"),
            "writes",
            emit.type,
        )
        return node_id

    def _derivation_node(
        self,
        node_id: str,
        definition: PipelineDefinition,
        *,
        member: bool,
    ) -> GraphNode:
        limits = definition.limits
        facts: list[tuple[str, str]] = [
            ("trigger", _trigger_summary(definition)),
            (
                "sources",
                _joined(f"{name} ({source.kind})" for name, source in definition.sources.items()),
            ),
            ("tasks", _joined(f"{task.id} → {task.use}" for task in definition.tasks)),
            (
                "emits",
                f"{definition.emit.type} into {definition.emit.collection}"
                + (" (review required)" if definition.emit.review == "required" else ""),
            ),
            (
                "budgets",
                f"{limits.max_tasks} tasks · {limits.max_llm_calls} model calls · "
                f"{limits.max_total_tokens} tokens · {limits.max_wall_s}s",
            ),
        ]
        if limits.max_computer_runs:
            facts.append(
                (
                    "computer budget",
                    f"{_plural(limits.max_computer_runs, 'run')} · "
                    f"{limits.max_agent_steps} agent steps · "
                    f"{limits.max_computer_output_bytes} output bytes",
                )
            )
        return GraphNode(
            id=node_id,
            kind="derivation",
            name=definition.name,
            version=None,
            summary=f"{_trigger_summary(definition)} → {definition.emit.collection}",
            facts=tuple(facts),
            member=member,
            definition=dump_definition(definition),
        )

    def _trigger_scopes(self, definition: PipelineDefinition) -> tuple[tuple[str, str], ...]:
        trigger = definition.trigger
        if trigger is None:
            return ()
        scopes: list[tuple[str, str]] = []
        for condition_name in ("write", "quiet", "at", "changed", "census", "retraction"):
            condition = getattr(trigger, condition_name, None)
            if isinstance(condition, RecordScope):
                scopes.extend((condition_name, collection) for collection in condition.collections)
        return tuple(scopes)

    def _task_targets(self, config: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
        """Map one task's validated ``with`` block onto the parts it runs."""

        targets: list[tuple[str, str]] = []
        computer = config.get("computer")
        if isinstance(computer, str):
            targets.append(("runs", self._computer(computer)))
        program = config.get("program")
        if isinstance(program, str):
            targets.append(("runs", self._program(program)))
        agent = config.get("agent")
        if isinstance(agent, str):
            targets.append(("runs", self._agent(agent)))
        policy = config.get("context_policy")
        if isinstance(policy, str):
            targets.append(("uses", self._context_policy(policy)))
        model = config.get("model")
        if isinstance(model, str):
            targets.append(("uses", self._model(model)))
        return tuple(targets)

    def _processor(self, name: str, *, member: bool = False) -> str:
        # A `candidate_processor` or trigger target may name a Pipeline rather
        # than an annotation Processor; the two share one name space.
        if name in self.catalog.derivations:
            return self._derivation(name, member=member)
        node_id = f"processor:{name}"
        definition = self.catalog.processors.get(name)
        if definition is None:
            return self._add(self._missing(node_id, "processor", name, None))
        if node_id in self.nodes and not member:
            return node_id
        self._add(self._processor_node(node_id, definition, member=member))
        if definition.model is not None:
            self._edge(node_id, self._model(definition.model), "uses", "model", flow="none")
        return node_id

    def _processor_node(
        self,
        node_id: str,
        definition: ProcessorDefinition,
        *,
        member: bool,
    ) -> GraphNode:
        facts: list[tuple[str, str]] = [
            ("kind", definition.kind),
            ("scope", _joined(definition.input.collections)),
        ]
        if definition.source is not None:
            facts.append(("source", definition.source))
        if definition.scale is not None:
            low, high = definition.scale
            facts.append(("scale", f"{low} to {high}"))
        if definition.model is not None:
            facts.append(("model", definition.model))
        if definition.supersedes is not None:
            facts.append(("supersedes", definition.supersedes))
        return GraphNode(
            id=node_id,
            kind="processor",
            name=definition.name,
            version=None,
            summary=f"{definition.kind} annotation",
            facts=tuple(facts),
            member=member,
            definition=dump_definition(definition),
        )

    def _computer(self, reference: str, *, member: bool = False) -> str:
        name, version = self._exact(reference, self.catalog.active_computers)
        node_id = f"computer:{name}@{version}"
        definition = self.catalog.computers.get((name, version))
        if definition is None:
            return self._add(self._missing(node_id, "computer", name, str(version)))
        if node_id in self.nodes and not member:
            return node_id
        self._add(self._computer_node(node_id, definition, member=member))
        for mount in definition.context:
            # The mounted text flows into the workspace, so the Artifact leads.
            self._edge(self._artifact(mount.artifact), node_id, "mounts", mount.path)
        for writeback in definition.writeback:
            if writeback.collection is None:
                continue
            self._edge(
                node_id,
                self._collection(writeback.collection),
                "writes",
                writeback.record_type or writeback.type,
            )
        return node_id

    def _computer_node(
        self,
        node_id: str,
        definition: ComputerDefinition,
        *,
        member: bool,
    ) -> GraphNode:
        capabilities = definition.capabilities
        enabled = [
            label
            for label, value in (
                ("filesystem", capabilities.filesystem),
                ("exec", capabilities.exec),
                ("network", capabilities.network),
            )
            if value
        ]
        runtime = definition.runtime.default
        if definition.runtime.fallback is not None:
            runtime += f" → {definition.runtime.fallback}"
            if definition.runtime.fallback_requires is not None:
                runtime += f" ({definition.runtime.fallback_requires})"
        facts: list[tuple[str, str]] = [
            ("provider", definition.provider),
            ("runtime", runtime),
            ("capabilities", _joined(enabled)),
            ("writable", _joined(definition.writable)),
            (
                "mounted context",
                _joined(f"{mount.path} ← {mount.artifact}" for mount in definition.context),
            ),
            (
                "writeback",
                _joined(
                    f"{item.path} → {item.type}"
                    + (f" ({item.collection})" if item.collection else "")
                    + (" [review]" if item.review else "")
                    for item in definition.writeback
                ),
            ),
            (
                "retention",
                f"{_plural(definition.retention.workspace_days, 'day')}; "
                f"preserve {_joined(definition.retention.preserve)}",
            ),
        ]
        reviewed = any(item.review for item in definition.writeback)
        return GraphNode(
            id=node_id,
            kind="computer",
            name=definition.name,
            version=str(definition.version),
            summary=f"{definition.provider} · {definition.runtime.default}"
            + (" · review gate" if reviewed else ""),
            facts=tuple(facts),
            member=member,
            definition=dump_definition(definition),
        )

    def _program(self, reference: str, *, member: bool = False) -> str:
        name, version = self._exact(reference, self.catalog.active_programs)
        node_id = f"program:{name}@{version}"
        definition = self.catalog.programs.get((name, version))
        if definition is None:
            return self._add(self._missing(node_id, "program", name, str(version)))
        if node_id in self.nodes and not member:
            return node_id
        return self._add(self._program_node(node_id, definition, member=member))

    def _program_node(
        self,
        node_id: str,
        definition: ProgramDefinition,
        *,
        member: bool,
    ) -> GraphNode:
        source = (
            f"bundle {definition.bundle.uri}"
            if definition.bundle is not None
            else _joined(definition.files)
        )
        facts: list[tuple[str, str]] = [
            ("runtime", definition.runtime),
            ("entrypoint", definition.entrypoint),
            ("capabilities", _joined(definition.capabilities)),
            ("code", source),
            ("input", _joined(str(key) for key in definition.input_schema.get("properties", {}))),
            ("output", _joined(str(key) for key in definition.output_schema.get("properties", {}))),
        ]
        if definition.command:
            facts.append(("command", " ".join(definition.command)))
        return GraphNode(
            id=node_id,
            kind="program",
            name=definition.name,
            version=str(definition.version),
            summary=f"deterministic {definition.runtime}",
            facts=tuple(facts),
            member=member,
            definition=dump_definition(definition),
        )

    def _agent(self, reference: str, *, member: bool = False) -> str:
        name, version = self._exact(reference, self.catalog.active_agents)
        node_id = f"agent:{name}@{version}"
        definition = self.catalog.agents.get((name, version))
        if definition is None:
            return self._add(self._missing(node_id, "agent", name, str(version)))
        if node_id in self.nodes and not member:
            return node_id
        self._add(self._agent_node(node_id, definition, member=member))
        self._edge(node_id, self._model(definition.model), "uses", "model", flow="none")
        self._edge(
            node_id, self._artifact(definition.instructions), "uses", "instructions", flow="none"
        )
        for skill in definition.skills:
            self._edge(node_id, self._artifact(skill), "uses", "skill", flow="none")
        for computer in definition.computers:
            self._edge(node_id, self._computer(computer), "runs", "")
        self._edge(
            node_id,
            self._context_policy(definition.context_policy),
            "uses",
            "policy",
            flow="none",
        )
        if definition.toolset is not None:
            self._edge(node_id, self._toolset(definition.toolset), "uses", "toolset", flow="none")
        return node_id

    def _agent_node(
        self,
        node_id: str,
        definition: AgentDefinition,
        *,
        member: bool,
    ) -> GraphNode:
        limits = definition.limits
        facts: tuple[tuple[str, str], ...] = (
            ("model", definition.model),
            ("instructions", definition.instructions),
            ("skills", _joined(definition.skills)),
            ("tools", definition.toolset or _joined(definition.tools)),
            ("computers", _joined(definition.computers)),
            ("context policy", definition.context_policy),
            (
                "limits",
                f"{limits.max_steps} steps · {limits.max_wall_s}s · "
                f"{limits.max_output_bytes} output bytes",
            ),
        )
        return GraphNode(
            id=node_id,
            kind="agent",
            name=definition.name,
            version=str(definition.version),
            summary=f"{definition.model} · {_plural(limits.max_steps, 'step')} max",
            facts=facts,
            member=member,
            definition=dump_definition(definition),
        )

    def _artifact(self, reference: str, *, member: bool = False) -> str:
        name, version = self._exact(reference, self.catalog.active_artifacts)
        node_id = f"artifact:{name}@{version}"
        definition = self.catalog.artifacts.get((name, version))
        if definition is None:
            return self._add(self._missing(node_id, "artifact", name, str(version)))
        if node_id in self.nodes and not member:
            return node_id
        self._add(self._artifact_node(node_id, definition, member=member))
        for block_name, block in definition.blocks.items():
            if block.document is not None:
                for collection in block.document.collections:
                    self._edge(self._collection(collection), node_id, "reads", block_name)
            if block.view is not None:
                self._edge(self._view(block.view), node_id, "reads", block_name)
        if definition.snapshot is not None:
            self._edge(
                node_id,
                self._collection(definition.snapshot.collection),
                "writes",
                definition.snapshot.type,
            )
        if definition.candidate_processor is not None:
            self._edge(
                node_id,
                self._processor(definition.candidate_processor),
                "uses",
                "candidate",
                flow="none",
            )
        if definition.learning is not None:
            self._edge(
                node_id,
                self._artifact(definition.learning.artifact),
                "uses",
                "learning",
                flow="none",
            )
        return node_id

    def _artifact_node(
        self,
        node_id: str,
        definition: ArtifactDefinition,
        *,
        member: bool,
    ) -> GraphNode:
        facts: list[tuple[str, str]] = [
            ("kind", definition.kind),
            ("lifecycle", definition.lifecycle),
            ("parameters", _joined(definition.parameters)),
            (
                "blocks",
                _joined(
                    f"{name} ({_block_source(block)}, {block.max_tokens} tokens)"
                    for name, block in definition.blocks.items()
                ),
            ),
            ("template", f"{len(definition.template)} characters"),
        ]
        if definition.snapshot is not None:
            facts.append(
                (
                    "snapshot",
                    f"{definition.snapshot.type} into {definition.snapshot.collection}",
                )
            )
        if definition.learning is not None:
            facts.append(
                (
                    "learning",
                    f"{definition.learning.artifact} → {definition.learning.target_block}",
                )
            )
        return GraphNode(
            id=node_id,
            kind="artifact",
            name=definition.name,
            version=str(definition.version),
            summary=f"{definition.kind} · {definition.lifecycle}",
            facts=tuple(facts),
            member=member,
            definition=dump_definition(definition),
        )

    def _context_policy(self, reference: str, *, member: bool = False) -> str:
        name, version = self._exact(reference, self.catalog.active_context_policies)
        node_id = f"context_policy:{name}@{version}"
        definition = self.catalog.context_policies.get((name, version))
        if definition is None:
            return self._add(self._missing(node_id, "context_policy", name, str(version)))
        if node_id in self.nodes and not member:
            return node_id
        return self._add(self._context_policy_node(node_id, definition, member=member))

    def _context_policy_node(
        self,
        node_id: str,
        definition: ContextPolicyDefinition,
        *,
        member: bool,
    ) -> GraphNode:
        thresholds = definition.thresholds
        facts: tuple[tuple[str, str], ...] = (
            ("input budget", f"{definition.max_input_tokens} tokens"),
            ("reserved output", f"{definition.reserve_output_tokens} tokens"),
            (
                "thresholds",
                f"pointerize {thresholds.pointerize} · compact {thresholds.compact} · "
                f"pause {thresholds.pause}",
            ),
            (
                "recall",
                f"{_plural(definition.max_recall_pages, 'page')} · "
                f"{_plural(definition.max_recall_hits, 'hit')}",
            ),
            ("exposed bytes", str(definition.max_exposed_bytes)),
            ("receipt fanout", str(definition.receipt_fanout)),
        )
        return GraphNode(
            id=node_id,
            kind="context_policy",
            name=definition.name,
            version=str(definition.version),
            summary=f"{definition.max_input_tokens} token window",
            facts=facts,
            member=member,
            definition=dump_definition(definition),
        )

    def _view(self, reference: str, *, member: bool = False) -> str:
        name, version = self._exact(reference, self.catalog.active_views)
        node_id = f"view:{name}@{version}"
        definition = self.catalog.views.get((name, version))
        if definition is None:
            return self._add(self._missing(node_id, "view", name, str(version)))
        if node_id in self.nodes and not member:
            return node_id
        self._add(self._view_node(node_id, definition, member=member))
        for collection in _view_collections(definition):
            self._edge(self._collection(collection), node_id, "reads", "")
        return node_id

    def _view_node(
        self,
        node_id: str,
        definition: ViewDefinition,
        *,
        member: bool,
    ) -> GraphNode:
        facts: list[tuple[str, str]] = [
            ("kind", definition.kind),
            ("parameters", _joined(definition.parameters)),
            ("collections", _joined(_view_collections(definition))),
        ]
        if definition.required_capabilities:
            facts.append(("requires", _joined(definition.required_capabilities)))
        if definition.graph is not None:
            facts.append(("edge collection", definition.graph.edges))
        return GraphNode(
            id=node_id,
            kind="view",
            name=definition.name,
            version=str(definition.version),
            summary=f"{definition.kind} view",
            facts=tuple(facts),
            member=member,
            definition=dump_definition(definition),
        )

    def _toolset(self, reference: str, *, member: bool = False) -> str:
        """A declared inbound tool surface, and one node per tool it grants."""

        name, version = split_exact_reference(reference)
        node_id = f"toolset:{name}@{version}"
        definition = self.catalog.toolsets.get((name, int(version)))
        if definition is None:
            return self._add(self._missing(node_id, "toolset", name, str(version)))
        self._add(self._toolset_node(node_id, definition, member=member))
        for source in definition.sources:
            tool_id = self._add(
                GraphNode(
                    id=f"tool:{name}@{version}:{source.name}",
                    kind="tool",
                    name=source.name,
                    version=None,
                    summary=f"{source.kind} tool",
                    facts=(
                        ("kind", source.kind),
                        ("description", source.description or "—"),
                        (
                            "binds",
                            _joined(
                                f"{label} {value}"
                                for label, value in (
                                    ("view", source.view),
                                    ("artifact", source.artifact),
                                    ("path", source.path),
                                    ("root", source.root),
                                    ("url", source.url),
                                    ("modes", _joined(source.modes or ())),
                                )
                                if value
                            ),
                        ),
                    ),
                    member=True,
                    definition=source.model_dump(mode="json"),
                )
            )
            self._edge(node_id, tool_id, "exposes", source.kind)
            # What a tool reads reaches it from the left, as for MCP tools.
            for target in (
                source.view and self._view(source.view),
                source.artifact and self._artifact(source.artifact),
            ):
                if target:
                    self._edge(target, tool_id, "reads", "", flow="forward")
        return node_id

    def _toolset_node(
        self, node_id: str, definition: ToolsetDefinition, *, member: bool
    ) -> GraphNode:
        return GraphNode(
            id=node_id,
            kind="toolset",
            name=definition.name,
            version=str(definition.version),
            summary=f"{_plural(len(definition.sources), 'tool')} granted",
            facts=(
                ("title", definition.title or "—"),
                ("instructions", definition.instructions or "—"),
                ("tools", _joined(source.name for source in definition.sources)),
            ),
            member=member,
            definition=dump_definition(definition),
        )

    def _mcp(self, reference: str) -> str:
        name, version = split_exact_reference(reference)
        node_id = f"mcp:{name}@{version}"
        definition = self.catalog.mcps.get((name, int(version)))
        if definition is None:
            return self._add(self._missing(node_id, "mcp", name, str(version)))
        self._add(self._mcp_node(node_id, definition))
        for tool in definition.tools:
            tool_id = self._add(
                GraphNode(
                    id=f"mcp_tool:{name}@{version}:{tool.name}",
                    kind="mcp_tool",
                    name=tool.name,
                    version=None,
                    summary=f"{tool.kind} tool"
                    + (f" · {tool.invocation_task}" if tool.invocation_task else ""),
                    facts=(
                        ("kind", tool.kind),
                        ("description", tool.description),
                        ("invocation task", tool.invocation_task or "—"),
                        (
                            "binds",
                            _joined(
                                f"{label} {value}"
                                for label, value in (
                                    ("view", tool.view),
                                    ("artifact", tool.artifact),
                                    ("collection", tool.collection),
                                    ("computer", tool.computer),
                                    ("agent", tool.agent),
                                    ("program", tool.program),
                                    ("context policy", tool.context_policy),
                                )
                                if value
                            ),
                        ),
                    ),
                    member=True,
                    definition=tool.model_dump(mode="json", exclude_none=True),
                )
            )
            self._edge(node_id, tool_id, "exposes", tool.kind)
            # What a tool renders reaches it from the left; what it starts or
            # writes sits to its right.
            for relation, target, inbound, flow in (
                ("reads", tool.view and self._view(tool.view), True, "forward"),
                ("reads", tool.artifact and self._artifact(tool.artifact), True, "forward"),
                ("writes", tool.collection and self._collection(tool.collection), False, "forward"),
                ("runs", tool.computer and self._computer(tool.computer), False, "forward"),
                ("runs", tool.agent and self._agent(tool.agent), False, "forward"),
                ("runs", tool.program and self._program(tool.program), False, "forward"),
                (
                    "uses",
                    tool.context_policy and self._context_policy(tool.context_policy),
                    False,
                    "none",
                ),
            ):
                if not target:
                    continue
                source, destination = (target, tool_id) if inbound else (tool_id, target)
                self._edge(source, destination, relation, "", flow=flow)
        return node_id

    def _mcp_node(self, node_id: str, definition: McpDefinition) -> GraphNode:
        return GraphNode(
            id=node_id,
            kind="mcp",
            name=definition.name,
            version=str(definition.version),
            summary=f"{_plural(len(definition.tools), 'tool')} exposed",
            facts=(
                ("title", definition.title or "—"),
                ("instructions", definition.instructions or "—"),
                ("tools", _joined(tool.name for tool in definition.tools)),
            ),
            member=True,
            definition=dump_definition(definition),
        )

    def _trigger(self, name: str) -> str | None:
        """A standalone trigger; a Pipeline's own inline one is already drawn."""

        definition = self.catalog.triggers.get(name)
        if definition is None or name == f"{definition.processor}.default":
            return None
        node_id = f"trigger:{name}"
        scopes: list[tuple[str, str]] = []
        for label in ("write", "quiet", "at", "changed", "census", "retraction"):
            condition = getattr(definition, label, None)
            if isinstance(condition, RecordScope):
                scopes.extend((label, collection) for collection in condition.collections)
        facts: list[tuple[str, str]] = [
            ("starts", definition.processor),
            (
                "condition",
                _joined(sorted({label for label, _ in scopes})) if scopes else "schedule",
            ),
            ("scope", _joined(sorted({collection for _, collection in scopes}))),
        ]
        if definition.cron is not None:
            facts.append(("cron", f"{definition.cron.expr} ({definition.cron.entities})"))
        if definition.cooldown_s or definition.debounce_s:
            facts.append(
                ("pacing", f"cooldown {definition.cooldown_s}s · debounce {definition.debounce_s}s")
            )
        self._add(
            GraphNode(
                id=node_id,
                kind="trigger",
                name=name,
                version=None,
                summary=f"starts {definition.processor}",
                facts=tuple(facts),
                member=True,
                definition=dump_definition(definition),
            )
        )
        for label, collection in scopes:
            self._edge(self._collection(collection), node_id, "triggers", label)
        self._edge(node_id, self._processor(definition.processor), "runs", "")
        return node_id

    def _model(self, name: str) -> str:
        node_id = f"model:{name}"
        alias = self.catalog.models.aliases.get(name)
        if alias is None:
            return self._add(self._missing(node_id, "model", name, None))
        if node_id in self.nodes:
            return node_id
        providers = sorted({target.split(":", 1)[0] for target in alias.targets})
        return self._add(
            GraphNode(
                id=node_id,
                kind="model",
                name=name,
                version=None,
                summary=_joined(alias.targets, limit=2),
                facts=(
                    ("targets", _joined(alias.targets)),
                    ("providers", _joined(providers)),
                    (
                        "adapters",
                        _joined(
                            f"{provider} ({self.catalog.models.providers[provider].adapter})"
                            for provider in providers
                        ),
                    ),
                    ("context", f"{alias.context_tokens} tokens" if alias.context_tokens else "—"),
                    (
                        "params",
                        _joined(f"{key}={value}" for key, value in sorted(alias.params.items())),
                    ),
                ),
                member=True,
                definition=alias.model_dump(mode="json", exclude_none=True),
            )
        )

    def _search_profile(self, name: str, *, member: bool = False) -> str:
        node_id = f"search_profile:{name}"
        definition = self.catalog.search_profiles.get(name)
        if definition is None:
            return self._add(self._missing(node_id, "search_profile", name, None))
        if node_id in self.nodes and not member:
            return node_id
        return self._add(
            GraphNode(
                id=node_id,
                kind="search_profile",
                name=name,
                version=None,
                summary=f"{definition.backend} backend",
                facts=(
                    ("backend", definition.backend),
                    ("layout", definition.layout or "—"),
                    ("consistency", definition.consistency or "—"),
                    (
                        "credential gated",
                        "yes" if definition.enabled_if_credentials else "no",
                    ),
                ),
                member=member,
                definition=dump_definition(definition),
            )
        )

    def _missing(self, node_id: str, kind: str, name: str, version: str | None) -> GraphNode:
        """A reference the compiled catalog resolved outside this package.

        Package closure validation means this is normally unreachable; drawing it
        rather than dropping it keeps a partial catalog inspectable.
        """

        return GraphNode(
            id=node_id,
            kind=kind,
            name=name,
            version=version,
            summary="declared outside this package",
            facts=(("status", "not declared by this package"),),
            member=False,
            definition=None,
        )


def _block_source(block: Any) -> str:
    if block.document is not None:
        return f"document: {_joined(block.document.collections, limit=3)}"
    if block.view is not None:
        return f"view: {block.view}"
    return "static"


def _source_collections(source: Any) -> tuple[str, ...]:
    """Every collection one Pipeline Source reads, however it is scoped."""

    collections = getattr(source, "collections", None)
    if collections is not None:
        return tuple(str(value) for value in collections)
    collection = getattr(source, "collection", None)
    return (str(collection),) if collection is not None else ()


def _view_collections(definition: ViewDefinition) -> tuple[str, ...]:
    """Collections a view reads, from its scopes or its graph projection."""

    names: list[str] = []
    if definition.graph is not None:
        names.extend(name for name in (definition.graph.edges, definition.graph.nodes) if name)
    if definition.query is not None:
        names.extend(_scoped_collections(definition.query))
    seen: dict[str, None] = {}
    for name in names:
        # A template reference is not a collection name; the loader has already
        # validated the real ones, so anything unresolved is dropped.
        if "{{" not in name:
            seen.setdefault(name, None)
    return tuple(seen)


def _scoped_collections(value: Any) -> tuple[str, ...]:
    """Collect every ``scope.collections`` entry anywhere in a query mapping."""

    if isinstance(value, Mapping):
        found: list[str] = []
        for key, item in value.items():
            if key == "collections" and isinstance(item, Sequence) and not isinstance(item, str):
                found.extend(str(entry) for entry in item)
            else:
                found.extend(_scoped_collections(item))
        return tuple(found)
    if isinstance(value, Sequence) and not isinstance(value, str):
        return tuple(name for item in value for name in _scoped_collections(item))
    return ()


def _trigger_summary(definition: PipelineDefinition) -> str:
    """One phrase for what starts this Pipeline."""

    trigger = definition.trigger
    if trigger is None:
        return "called directly"
    if trigger.cron is not None:
        return f"cron {trigger.cron.expr} ({trigger.cron.entities})"
    for label in ("write", "quiet", "at", "changed", "census", "retraction"):
        condition = getattr(trigger, label, None)
        if isinstance(condition, RecordScope):
            return f"on {label}: {_joined(condition.collections, limit=3)}"
    if trigger.accumulator is not None:
        return "on accumulator threshold"
    if trigger.lifecycle is not None:
        return "on entity lifecycle"
    if trigger.read:
        return "on read"
    return "called directly"
