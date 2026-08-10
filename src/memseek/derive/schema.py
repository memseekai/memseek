"""Immutable schema for general, bounded derivation pipelines.

The authoring Interface describes data and computation: named sources feed
registered Tasks and one named Task value is emitted as canonical records.
Checkpoint, transition, and Candidate Set semantics are deliberately absent
from this module; the runtime infers them from source and emission intent.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from memseek.definitions.base import (
    DefinitionModel,
    ProcessorName,
    PublicName,
    StrictModel,
    TriggerName,
    ensure_unique,
)

type CollectionScopeName = PublicName | Literal["_system"]


class RecordScope(StrictModel):
    """A declarative scope over canonical records."""

    collections: tuple[CollectionScopeName, ...]
    collection_versions: dict[PublicName, tuple[int, ...]] = Field(default_factory=dict)
    types: tuple[PublicName, ...] = ()
    statuses: tuple[Literal["active", "draft"], ...] = ("active",)
    keyed: bool | Literal["any"] = "any"

    @model_validator(mode="after")
    def validate_scope(self) -> RecordScope:
        if not self.collections:
            raise ValueError("source collections must be non-empty")
        ensure_unique(self.collections, "collections")
        ensure_unique(self.types, "types")
        ensure_unique(self.statuses, "statuses")
        for collection, versions in self.collection_versions.items():
            if collection not in self.collections:
                raise ValueError(f"collection_versions contains out-of-scope {collection!r}")
            if not versions or any(version < 1 for version in versions):
                raise ValueError(f"collection_versions.{collection} requires positive versions")
            ensure_unique(versions, f"collection_versions.{collection}")
        return self


class SnapshotWindow(StrictModel):
    """Narrows a snapshot corpus to a recent tail or an occurred_at range.

    A plain snapshot reads every matching record through the checkpoint.  A
    window declares a smaller corpus so "complete" holds over that corpus:
    either the most recent `recent` records, or the records whose `occurred_at`
    falls in `[since, until]`.  The window is recorded in the receipt (`from_seq`)
    so the run stays reproducible and honest about what it excluded.
    """

    recent: int | None = Field(default=None, ge=1, le=500)
    since: datetime | None = None
    until: datetime | None = None

    @model_validator(mode="after")
    def validate_window(self) -> SnapshotWindow:
        tail = self.recent is not None
        ranged = self.since is not None or self.until is not None
        if tail and ranged:
            raise ValueError("snapshot window uses either recent or since/until, not both")
        if not tail and not ranged:
            raise ValueError("snapshot window requires recent or since/until")
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("snapshot window since must be earlier than until")
        return self


class StreamSource(RecordScope):
    """The one source that drives a pipeline run."""

    kind: Literal["changes", "snapshot", "stale_citations"]
    max_records: int = Field(default=200, ge=1, le=500)
    max_tokens: int = Field(default=24_000, ge=1)
    allow_empty: bool = False
    window: SnapshotWindow | None = None

    @model_validator(mode="after")
    def validate_stream(self) -> StreamSource:
        if self.window is not None and self.kind != "snapshot":
            raise ValueError("window is only valid on a snapshot source")
        if self.kind == "stale_citations" and self.keyed is not True:
            raise ValueError("a stale_citations source reads keyed records")
        return self


class CurrentSource(RecordScope):
    """A guarded read of current keyed records."""

    kind: Literal["current"]
    keys: tuple[str, ...] = ()
    max_records: int = Field(default=100, ge=1, le=500)
    max_tokens: int = Field(default=12_000, ge=1)

    @model_validator(mode="after")
    def validate_current(self) -> CurrentSource:
        ensure_unique(self.keys, "keys")
        if any(not key or len(key) > 128 for key in self.keys):
            raise ValueError("current source keys must contain 1 through 128 characters")
        if self.keyed is False:
            raise ValueError("a current source reads keyed records")
        return self


class RecordSource(StrictModel):
    """A guarded read of one current keyed slot."""

    kind: Literal["record"]
    collection: PublicName
    collection_version: int | None = Field(default=None, ge=1)
    key: str = Field(min_length=1, max_length=128)
    type: PublicName | None = None
    status: Literal["active", "draft"] = "active"
    max_tokens: int = Field(default=4_000, ge=1)


class ViewSource(StrictModel):
    """A bounded named-view query evaluated at run time."""

    kind: Literal["view"]
    view: str = Field(min_length=1)
    params: dict[PublicName, Any] = Field(default_factory=dict)
    max_tokens: int = Field(default=4_000, ge=1)

    @field_validator("view")
    @classmethod
    def validate_view(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("view source reference must be non-blank")
        return value


type PipelineSource = StreamSource | CurrentSource | RecordSource | ViewSource


type AccumulatorAggregate = Literal["sum", "count", "avg", "max", "min", "distinct_count"]


class AccumulatorMetric(StrictModel):
    annotation: ProcessorName | None = None
    path: str | None = Field(default=None, min_length=1)
    scorer: ProcessorName | None = None
    aggregate: AccumulatorAggregate = "sum"

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$", value):
            raise ValueError("annotation accumulator path must be dotted object fields")
        return value

    @model_validator(mode="after")
    def validate_metric(self) -> AccumulatorMetric:
        if self.scorer is not None:
            if self.annotation is not None or self.path is not None:
                raise ValueError("accumulator metric names a scorer or an annotation, not both")
        elif self.annotation is None or self.path is None:
            raise ValueError("annotation accumulator metric requires annotation and path")
        return self


class AccumulatorCondition(StrictModel):
    metric: ProcessorName | Literal["count"] | AccumulatorMetric
    threshold: float
    comparison: Literal["gte", "lte"] = "gte"

    @model_validator(mode="after")
    def validate_threshold(self) -> AccumulatorCondition:
        if not math.isfinite(self.threshold):
            raise ValueError("accumulator threshold must be finite")
        if self.comparison == "gte" and self.threshold <= 0:
            raise ValueError("a gte accumulator threshold must be positive")
        return self


class CronCondition(StrictModel):
    expr: str = Field(min_length=1)
    entities: Literal["dirty", "any"] = "dirty"


class WriteCondition(RecordScope):
    where: dict[PublicName, dict[str, Any]] = Field(default_factory=dict)
    ignore_own_outputs: bool = False


class QuietCondition(WriteCondition):
    """Fire once matching arrivals have settled for ``after_s`` seconds."""

    after_s: int = Field(ge=1, le=604_800)


class AtCondition(WriteCondition):
    """Fire when the wall clock passes a datetime declared in a record field."""

    field: PublicName
    offset_s: int = Field(default=0, ge=-31_536_000, le=31_536_000)


class ChangedCondition(WriteCondition):
    """Fire when a keyed head is added, changed, or removed — not on rewrites."""

    keys: tuple[str, ...] = ()
    transitions: tuple[Literal["added", "changed", "removed"], ...] = (
        "added",
        "changed",
        "removed",
    )

    @model_validator(mode="after")
    def validate_changed(self) -> ChangedCondition:
        ensure_unique(self.keys, "keys")
        ensure_unique(self.transitions, "transitions")
        if not self.transitions:
            raise ValueError("changed transitions must be non-empty")
        if any(not key or len(key) > 128 for key in self.keys):
            raise ValueError("changed keys must contain 1 through 128 characters")
        if self.keyed is False:
            raise ValueError("a changed condition watches keyed records")
        return self


class CensusCondition(WriteCondition):
    """Fire when new driver data arrives and the current census meets a floor."""

    threshold: int = Field(ge=1)


class LifecycleCondition(StrictModel):
    """Fire on an entity's first matching record or total-record growth."""

    first_record: bool = False
    total_records: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> LifecycleCondition:
        if not self.first_record and self.total_records is None:
            raise ValueError("lifecycle requires first_record or total_records")
        return self


class RetractionCondition(WriteCondition):
    """Fire when a ready tombstone lands above the watermark."""

    @model_validator(mode="after")
    def validate_retraction(self) -> RetractionCondition:
        if self.keyed is False:
            raise ValueError("a retraction condition watches keyed records")
        return self


class TriggerConditions(StrictModel):
    read: bool = False
    accumulator: AccumulatorCondition | None = None
    cron: CronCondition | None = None
    write: WriteCondition | None = None
    quiet: QuietCondition | None = None
    at: AtCondition | None = None
    changed: ChangedCondition | None = None
    census: CensusCondition | None = None
    lifecycle: LifecycleCondition | None = None
    retraction: RetractionCondition | None = None
    cooldown_s: int = Field(default=0, ge=0)
    debounce_s: int = Field(default=0, ge=0, le=604_800)

    @property
    def automatic(self) -> bool:
        return bool(
            self.read
            or self.accumulator
            or self.cron
            or self.write
            or self.quiet
            or self.at
            or self.changed
            or self.census
            or self.lifecycle
            or self.retraction
        )

    @property
    def arrival_scopes(self) -> dict[str, WriteCondition]:
        """The consumable record scopes that react to matching arrivals."""

        scopes: dict[str, WriteCondition] = {}
        for condition in ("write", "quiet", "changed", "retraction"):
            scope = getattr(self, condition)
            if scope is not None:
                scopes[condition] = scope
        return scopes

    @property
    def observed_scopes(self) -> dict[str, WriteCondition]:
        """Every declared record scope, consumable or observational."""

        scopes = self.arrival_scopes
        for condition in ("at", "census"):
            scope = getattr(self, condition)
            if scope is not None:
                scopes[condition] = scope
        return scopes


class StandaloneTrigger(TriggerConditions):
    name: TriggerName
    processor: ProcessorName
    definition_hash: str = Field(default="", exclude=True, repr=False)


class PipelineLimits(StrictModel):
    """Run-wide limits shared by every built-in or installed Task."""

    max_tasks: int = Field(default=10, ge=1, le=20)
    max_llm_calls: int = Field(default=4, ge=0)
    max_retrieved_records: int = Field(default=100, ge=0)
    max_visible_records: int = Field(default=250, ge=1)
    max_total_tokens: int = Field(default=50_000, ge=1)
    max_wall_s: int = Field(default=120, ge=1)


class TaskCall(StrictModel):
    """One call to a process-installed Task Adapter."""

    id: PublicName
    use: ProcessorName
    input: Any | None = None
    config: dict[str, Any] = Field(default_factory=dict, alias="with", serialization_alias="with")


class EmitDefinition(StrictModel):
    """Canonical-write boundary; transition details are inferred.

    ``driver_key`` carries one captured driver key into one output.  The
    separately opt-in ``dynamic_keys`` mode supports a *bounded* set of named
    keyed blocks, such as independently maintained scene documents.  It must
    declare ``max_active_keys`` so the engine can snapshot every current head
    and enforce the collection-wide bound before committing a change.
    """

    from_: str = Field(alias="from", serialization_alias="from", min_length=1)
    collection: PublicName
    collection_version: int | None = Field(default=None, ge=1)
    type: PublicName
    keys: tuple[str, ...] = ()
    driver_key: bool = False
    dynamic_keys: bool = False
    max_active_keys: int | None = Field(default=None, ge=1, le=100)
    complete: bool = False
    review: Literal["required"] | None = None
    max_records: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def validate_emit(self) -> EmitDefinition:
        if self.collection.startswith("_"):
            raise ValueError("pipelines cannot emit to a reserved collection")
        ensure_unique(self.keys, "keys")
        if self.driver_key and self.keys:
            raise ValueError("driver_key emission cannot also declare static keys")
        if self.dynamic_keys and self.keys:
            raise ValueError("dynamic_keys emission cannot also declare static keys")
        if self.dynamic_keys and self.driver_key:
            raise ValueError("dynamic_keys emission cannot also use driver_key")
        if self.driver_key and self.complete:
            raise ValueError("driver_key emission cannot be complete")
        if self.dynamic_keys and self.complete:
            raise ValueError("dynamic_keys emission cannot be complete")
        if self.driver_key and self.max_records != 1:
            raise ValueError("driver_key emission requires max_records: 1")
        if self.dynamic_keys and self.max_active_keys is None:
            raise ValueError("dynamic_keys emission requires max_active_keys")
        if not self.dynamic_keys and self.max_active_keys is not None:
            raise ValueError("max_active_keys requires dynamic_keys")
        if self.dynamic_keys and self.review is not None:
            raise ValueError("dynamic_keys emission cannot require review")
        if len(self.keys) > 50:
            raise ValueError("keys is capped at 50 output records")
        if any(not key or len(key) > 128 for key in self.keys):
            raise ValueError("emit keys must contain 1 through 128 characters")
        if self.complete and not self.keys:
            raise ValueError("complete emission requires keys")
        if self.keys and self.max_records < len(self.keys) and self.complete:
            raise ValueError("max_records must cover every complete emission key")
        if not re.fullmatch(
            r"{{\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\s*}}",
            self.from_,
        ):
            raise ValueError("emit.from must be one exact typed reference")
        return self


class RecordDraft(StrictModel):
    """The one strict, typed record vocabulary accepted by ``emit.from``."""

    key: str | None = None
    text: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    citations: tuple[str, ...]
    retract: bool = False


RESERVED_VALUE_NAMES = frozenset({"entity", "run", "item", "index"})
RUN_TEMPLATE_KEYS = frozenset({"now", "checkpoint", "source_ids"})


class PipelineDefinition(DefinitionModel):
    """A bounded dataflow whose Tasks cannot write canonical state directly."""

    name: ProcessorName
    trigger: TriggerConditions | None = None
    sources: dict[PublicName, PipelineSource]
    model: ProcessorName | None = None
    limits: PipelineLimits = Field(default_factory=PipelineLimits)
    tasks: tuple[TaskCall, ...]
    emit: EmitDefinition

    @property
    def driver_name(self) -> str:
        return next(
            name for name, source in self.sources.items() if isinstance(source, StreamSource)
        )

    @property
    def driver(self) -> StreamSource:
        source = self.sources[self.driver_name]
        assert isinstance(source, StreamSource)
        return source

    @model_validator(mode="after")
    def validate_pipeline(self) -> PipelineDefinition:
        if not self.sources:
            raise ValueError("sources must be non-empty")
        if any(
            isinstance(source, StreamSource | CurrentSource) and "_system" in source.collections
            for source in self.sources.values()
        ):
            raise ValueError("Pipeline sources cannot read the reserved _system collection")
        drivers = [
            name for name, source in self.sources.items() if isinstance(source, StreamSource)
        ]
        if len(drivers) != 1:
            raise ValueError(
                "sources must contain exactly one changes, snapshot, or stale_citations source"
            )
        if not self.tasks:
            raise ValueError("tasks must be non-empty")
        if len(self.tasks) > self.limits.max_tasks:
            raise ValueError("tasks exceed limits.max_tasks")
        names = [*self.sources, *(task.id for task in self.tasks)]
        ensure_unique(names, "source and task names")
        invalid_names = [
            name for name in names if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        ]
        if invalid_names:
            raise ValueError(f"source and task names must be template identifiers: {invalid_names}")
        reserved = RESERVED_VALUE_NAMES & set(names)
        if reserved:
            raise ValueError(
                f"source and task names cannot shadow reserved names: {sorted(reserved)}"
            )
        emit_root = re.search(r"[A-Za-z_][A-Za-z0-9_]*", self.emit.from_)
        assert emit_root is not None
        if emit_root.group(0) not in {task.id for task in self.tasks}:
            raise ValueError("emit.from must reference a Task result")
        return self


__all__ = [
    "RUN_TEMPLATE_KEYS",
    "AccumulatorCondition",
    "AccumulatorMetric",
    "AtCondition",
    "CensusCondition",
    "ChangedCondition",
    "CronCondition",
    "CurrentSource",
    "EmitDefinition",
    "LifecycleCondition",
    "PipelineDefinition",
    "PipelineLimits",
    "PipelineSource",
    "QuietCondition",
    "RecordDraft",
    "RecordScope",
    "RecordSource",
    "RetractionCondition",
    "StandaloneTrigger",
    "StreamSource",
    "TaskCall",
    "TriggerConditions",
    "ViewSource",
    "WriteCondition",
]
