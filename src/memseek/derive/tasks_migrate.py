"""Deterministic record-to-record mapping, for moving a corpus to a new contract.

Records are immutable, so a collection version cannot be edited into another one.
What a migration does instead is *copy forward with lineage*: read the old
version, emit the new one, and let ``derived_from`` record where each new row came
from.  That is exactly what a derivation already does, so a migration needs no new
persistence concept — only a Task that maps fields without asking a model to do
it.

Everything a migration inherits from being an ordinary derivation is the point:

* bounded, resumable, and claim-fenced execution;
* provenance from every emitted row back to its original;
* reviewed emission, so a corpus is never rewritten unseen;
* erasability through the existing ``derived_from`` closure.

The mapping is intentionally not a language.  It can copy a value, read a nested
path, supply a default, and coerce a scalar type.  Anything a real migration needs
beyond that is a job for an ``llm`` Task in the same pipeline, where the output is
still schema-validated and still reviewed before promotion.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memseek.derive.tasks import TaskConfigModel, TaskContext, TaskResult, register_task

_MAX_RECORDS = 100


class FieldMapping(BaseModel):
    """How one property of the migrated record is produced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # A dotted read against the source record, rooted at content or annotations.
    from_: str | None = Field(default=None, alias="from", min_length=1, max_length=128)
    value: Any | None = None
    default: Any | None = None
    cast: Literal["string", "number", "integer", "boolean"] | None = None

    @field_validator("from_")
    @classmethod
    def rooted_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        root = value.split(".", 1)[0]
        if root not in {"content", "annotations", "scores"}:
            raise ValueError("from must be rooted at content, annotations, or scores")
        return value

    @model_validator(mode="after")
    def one_source(self) -> FieldMapping:
        if (self.from_ is None) == (self.value is None):
            raise ValueError("each mapping needs exactly one of from or value")
        if self.value is not None and self.default is not None:
            raise ValueError("a constant mapping cannot also declare a default")
        return self


class MapRecordsConfig(TaskConfigModel):
    """A closed, deterministic description of the migrated record."""

    # Properties carried over unchanged from the source content.  Named ``keep``
    # rather than ``copy`` so it cannot shadow a Pydantic model attribute.
    keep: tuple[str, ...] = Field(default=("text",), max_length=32)
    # Properties computed from a path, a constant, or a default.
    set: dict[str, FieldMapping] = Field(default_factory=dict, max_length=32)
    # Drop properties the new contract no longer declares.
    drop: tuple[str, ...] = Field(default=(), max_length=32)
    # Keyed collections carry the source key forward unless told otherwise.
    carry_key: bool = True

    @model_validator(mode="after")
    def coherent_mapping(self) -> MapRecordsConfig:
        if "text" in self.drop:
            raise ValueError("text cannot be dropped")
        if "text" not in self.keep and "text" not in self.set:
            raise ValueError("every record requires text, so keep or set must produce it")
        overlap = set(self.keep) & set(self.set)
        if overlap:
            raise ValueError(f"keep and set cannot both produce {sorted(overlap)}")
        dropped = set(self.drop) & (set(self.keep) | set(self.set))
        if dropped:
            raise ValueError(f"drop cannot name mapped properties: {sorted(dropped)}")
        return self


class _SourceRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: UUID
    key: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    scores: dict[str, Any] = Field(default_factory=dict)


class MapRecordsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    records: tuple[_SourceRecord, ...] = Field(max_length=_MAX_RECORDS)


class _MigratedDraft(BaseModel):
    """One emitted record, cited back to the source row it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str | None = None
    text: str = Field(min_length=1)
    content: dict[str, Any]
    citations: tuple[UUID, ...] = Field(min_length=1, max_length=8)


def _read(record: _SourceRecord, dotted: str) -> Any:
    root, *parts = dotted.split(".")
    value: Any = {
        "content": record.content,
        "annotations": record.annotations,
        "scores": record.scores,
    }[root]
    for part in parts:
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _coerce(value: Any, cast: str | None) -> Any:
    if cast is None or value is None:
        return value
    try:
        if cast == "string":
            return value if isinstance(value, str) else str(value)
        if cast == "number":
            return float(value)
        if cast == "integer":
            return int(value)
        if cast == "boolean":
            if isinstance(value, str):
                lowered = value.strip().casefold()
                if lowered in {"true", "1", "yes"}:
                    return True
                if lowered in {"false", "0", "no"}:
                    return False
                raise ValueError(f"cannot read {value!r} as a boolean")
            return bool(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cannot cast {value!r} to {cast}: {exc}") from exc
    raise ValueError(f"unsupported cast {cast!r}")


async def map_records(
    context: TaskContext,
    value: MapRecordsInput,
    config: TaskConfigModel,
) -> TaskResult[list[_MigratedDraft]]:
    """Map every source record onto the shape the new contract declares."""

    del context
    assert isinstance(config, MapRecordsConfig)
    drafts: list[_MigratedDraft] = []
    source_ids: list[UUID] = []
    for record in value.records:
        source_ids.append(record.id)
        content: dict[str, Any] = {
            name: record.content[name]
            for name in config.keep
            if name in record.content and name not in config.drop
        }
        for name, mapping in config.set.items():
            if mapping.value is not None:
                content[name] = mapping.value
                continue
            assert mapping.from_ is not None
            read = _coerce(_read(record, mapping.from_), mapping.cast)
            if read is None:
                if mapping.default is None:
                    # Leaving the property absent is correct: the new contract
                    # declares it optional, or the publish would have been refused.
                    continue
                read = mapping.default
            content[name] = read
        text = content.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"record {record.id} produced no text under this mapping")
        drafts.append(
            _MigratedDraft(
                key=record.key if config.carry_key else None,
                text=text,
                content=content,
                citations=(record.id,),
            )
        )
    return TaskResult(
        drafts,
        source_ids=frozenset(source_ids),
        citation_ids=frozenset(source_ids),
    )


register_task(
    "map_records",
    implementation_hash=hashlib.sha256(b"memseek.map_records.v1").hexdigest(),
    config_model=MapRecordsConfig,
    input_type=MapRecordsInput,
    output_type=list[_MigratedDraft],
    handler=map_records,
)


__all__ = [
    "FieldMapping",
    "MapRecordsConfig",
    "MapRecordsInput",
    "map_records",
]
