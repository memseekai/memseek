"""Private emission normalization, validation, and keyed divergence."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.protocols import Validator
from pydantic import ValidationError

from memseek.config import Settings
from memseek.definitions import DefinitionCatalog
from memseek.derive.basis import EvaluationBasis, ExpectedHead
from memseek.derive.emission import EmissionEffect, emission_effect, emission_status
from memseek.derive.errors import DerivationError
from memseek.derive.schema import EmitDefinition, RecordDraft

type CandidateEffect = EmissionEffect
type DivergenceKind = Literal["added", "changed", "removed", "unchanged"]


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    id: UUID
    key: str | None
    content: dict[str, Any]
    citations: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """A private bounded write proposal derived from author emission intent."""

    basis: EvaluationBasis
    effect: CandidateEffect
    status: Literal["active", "draft"]
    records: tuple[CandidateRecord, ...]
    divergence: tuple[dict[str, Any], ...]
    # Keyed records equal to their active head: covered, but not written again.
    unchanged: tuple[CandidateRecord, ...] = ()

    @property
    def coverage(self) -> Literal["partial", "complete"]:
        return "complete" if self.effect == "replace" else "partial"

    def manifest(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "coverage": self.coverage,
            "status": self.status,
            "covered_keys": [
                record.key for record in (*self.records, *self.unchanged) if record.key is not None
            ],
            "divergence": list(self.divergence),
        }


_OUTPUT_SYSTEM_FIELDS = frozenset(
    {
        "text",
        "tombstone",
        "citations",
        "run_id",
        "derived_from",
        "workspace",
        "collection",
        "collection_version",
        "collection_hash",
        "entity",
        "status",
        "key",
        "ready",
        "depth",
        "seq",
        "enriched_at",
        "occurred_at",
        "created_at",
        "scores",
        "annotations",
        "annotation_meta",
        "enrichment_meta",
        "enrichment_error",
        "embedding",
    }
)


def _validate_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise DerivationError("validation", "emission contains a non-finite number")
    if isinstance(value, Mapping):
        for item in value.values():
            _validate_finite_json(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _validate_finite_json(item)


@dataclass(frozen=True, slots=True)
class _ContentValidator:
    name: str
    version: int
    validator: Validator

    @classmethod
    def for_emit(cls, emit: EmitDefinition, catalog: DefinitionCatalog) -> _ContentValidator:
        collection = catalog.resolve_collection(emit.collection, emit.collection_version)
        return cls(
            name=collection.name,
            version=collection.version,
            validator=Draft202012Validator(
                collection.content_schema, format_checker=FormatChecker()
            ),
        )

    def check(self, content: dict[str, Any]) -> None:
        errors = sorted(
            self.validator.iter_errors(content),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if not errors:
            return
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path)
        location = f"content.{path}" if path else "content"
        raise DerivationError(
            "validation",
            f"emitted record does not match {self.name}@{self.version}: "
            f"{location}: {error.message}",
        )


def validate_collection_content(
    content: dict[str, Any],
    *,
    emit: EmitDefinition,
    catalog: DefinitionCatalog,
) -> None:
    _ContentValidator.for_emit(emit, catalog).check(content)


def _citations(
    values: Any,
    *,
    visible: frozenset[UUID],
    settings: Settings,
    allow_empty: bool = False,
) -> tuple[UUID, ...]:
    if allow_empty and (values is None or values == [] or values == ()):
        return ()
    if not isinstance(values, list) or not 1 <= len(values) <= settings.max_citations_per_output:
        raise DerivationError(
            "validation", "each emitted record requires a bounded non-empty citations list"
        )
    result: list[UUID] = []
    seen: set[UUID] = set()
    for value in values:
        try:
            citation = UUID(str(value))
        except (ValueError, AttributeError, TypeError) as exc:
            raise DerivationError("validation", "emission citation must be a UUID") from exc
        if citation not in visible:
            raise DerivationError(
                "validation", "emission citation was not available to its producing Task"
            )
        if citation in seen:
            raise DerivationError("validation", "emission citations must be unique")
        seen.add(citation)
        result.append(citation)
    return tuple(result)


def _content(item: Mapping[str, Any], *, settings: Settings) -> dict[str, Any]:
    raw_content = item.get("content", {})
    if not isinstance(raw_content, Mapping):
        raise DerivationError("validation", "emission content must be a JSON object")
    content = dict(raw_content)
    if _OUTPUT_SYSTEM_FIELDS & (content.keys() - {"text"}):
        raise DerivationError("validation", "emission content contains reserved system fields")
    text = item.get("text")
    if text is not None:
        if not isinstance(text, str) or not text or len(text) > settings.max_text_chars:
            raise DerivationError("validation", "emission text exceeds MAX_TEXT_CHARS")
        if "text" in content and content["text"] != text:
            raise DerivationError("validation", "emission text conflicts with content.text")
        content["text"] = text
    return content


def _equals_head(
    record: CandidateRecord, head: ExpectedHead | None, *, emit: EmitDefinition, version: int
) -> bool:
    return (
        head is not None
        and head.record_id is not None
        and head.type == emit.type
        and head.collection_version == version
        and head.content == record.content
        and head.citations == frozenset(record.citations)
    )


def _divergence(
    records: tuple[CandidateRecord, ...],
    *,
    emit: EmitDefinition,
    basis: EvaluationBasis,
    unchanged: frozenset[UUID],
) -> tuple[dict[str, Any], ...]:
    heads = {(head.collection, head.key): head for head in basis.expected_heads}
    report: list[dict[str, Any]] = []
    for record in records:
        if record.key is None:
            continue
        head = heads.get((emit.collection, record.key))
        active_id = head.record_id if head is not None else None
        active_content = head.content if head is not None else None
        tombstone = record.content.get("tombstone") is True
        if active_id is None:
            change: DivergenceKind = "unchanged" if tombstone else "added"
        elif active_content == record.content:
            change = "unchanged"
        elif tombstone:
            change = "removed"
        else:
            change = "changed"
        report.append(
            {
                "collection": emit.collection,
                "key": record.key,
                "change": change,
                "active_record_id": str(active_id) if active_id is not None else None,
                "candidate_record_id": str(active_id if record.id in unchanged else record.id),
            }
        )
    return tuple(report)


def _dynamic_candidate_basis(
    basis: EvaluationBasis,
    *,
    emit: EmitDefinition,
    records: tuple[CandidateRecord, ...],
) -> EvaluationBasis:
    """Give newly named dynamic blocks an explicit empty-head precondition.

    Existing heads were captured before Tasks ran.  A key first proposed by a
    Task did not exist at that point, but it still needs an auditable ``None``
    precondition for commit and (where relevant) later promotion.
    """

    known = {(head.collection, head.key) for head in basis.expected_heads}
    additions = tuple(
        ExpectedHead(collection=emit.collection, key=record.key, record_id=None)
        for record in records
        if record.key is not None and (emit.collection, record.key) not in known
    )
    return replace(basis, expected_heads=(*basis.expected_heads, *additions))


def _validate_dynamic_capacity(
    *,
    basis: EvaluationBasis,
    emit: EmitDefinition,
    records: tuple[CandidateRecord, ...],
) -> None:
    """Apply the declared live-block bound after this partial update."""

    assert emit.max_active_keys is not None
    active = {
        head.key
        for head in basis.expected_heads
        if head.collection == emit.collection
        and head.record_id is not None
        and not bool((head.content or {}).get("tombstone", False))
    }
    for record in records:
        if record.key is None:
            continue
        if record.content.get("tombstone") is True:
            active.discard(record.key)
        else:
            active.add(record.key)
    if len(active) > emit.max_active_keys:
        raise DerivationError(
            "validation",
            "dynamic keyed emission exceeds emit.max_active_keys",
        )


def compile_candidate_set(
    value: Any,
    *,
    emit: EmitDefinition,
    basis: EvaluationBasis,
    visible: frozenset[UUID],
    settings: Settings,
    catalog: DefinitionCatalog,
) -> CandidateSet:
    """Compile a generic list of record drafts into the private write proposal."""

    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise DerivationError("validation", "emit.from must resolve to a record list")
    if len(value) > emit.max_records:
        raise DerivationError("validation", "emission exceeds emit.max_records")
    _validate_finite_json(value)
    effect = emission_effect(emit)
    driver_keys = (
        tuple(head.key for head in basis.expected_heads if head.collection == emit.collection)
        if emit.driver_key
        else ()
    )
    if emit.driver_key and len(driver_keys) != 1:
        raise DerivationError("validation", "driver_key emission requires one captured target key")
    allowed_keys = None if emit.dynamic_keys else emit.keys or driver_keys
    content_validator = _ContentValidator.for_emit(emit, catalog)
    records: list[CandidateRecord] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise DerivationError("validation", "every emitted record must be an object")
        try:
            draft = RecordDraft.model_validate(item)
        except ValidationError as exc:
            error = exc.errors(include_url=False)[0]
            raise DerivationError("validation", f"invalid emitted record: {error['msg']}") from exc
        normalized = draft.model_dump(mode="python")
        raw_key = draft.key
        if effect == "append":
            if raw_key is not None:
                raise DerivationError("validation", "event emissions must omit key")
            key = None
        else:
            if (
                not isinstance(raw_key, str)
                or not raw_key
                or len(raw_key) > 128
                or (allowed_keys is not None and raw_key not in allowed_keys)
                or raw_key in seen
            ):
                raise DerivationError(
                    "validation", f"invalid or duplicate emission key {raw_key!r}"
                )
            key = raw_key
            seen.add(key)
        retract = draft.retract
        if retract:
            if key is None:
                raise DerivationError("validation", "event emissions cannot retract")
            if draft.content or draft.text is not None:
                raise DerivationError("validation", "a retraction cannot also carry content")
            content = {"text": "", "tombstone": True}
        else:
            content = _content(normalized, settings=settings)
            if not content:
                raise DerivationError("validation", "an emitted record requires content or text")
            if effect == "append" and not isinstance(content.get("text"), str):
                raise DerivationError("validation", "event emissions require text")
        content_validator.check(content)
        records.append(
            CandidateRecord(
                id=uuid4(),
                key=key,
                content=content,
                citations=_citations(
                    list(draft.citations),
                    visible=visible,
                    settings=settings,
                    allow_empty=(
                        (retract and effect == "replace" and basis.mode == "corpus")
                        or basis.mode == "citation_repair"
                    ),
                ),
            )
        )
    if effect == "replace" and seen != set(allowed_keys or ()):
        raise DerivationError("validation", "complete emission must cover every declared key")
    result = tuple(records)
    if emit.dynamic_keys:
        basis = _dynamic_candidate_basis(basis, emit=emit, records=result)
        _validate_dynamic_capacity(basis=basis, emit=emit, records=result)
    status = emission_status(emit)
    unchanged: tuple[CandidateRecord, ...] = ()
    if status == "active":
        # Drafts stay whole: promotion checks every draft row against
        # covered_keys, and a reviewer promotes the set, not single keys.
        version = content_validator.version
        heads = {
            head.key: head for head in basis.expected_heads if head.collection == emit.collection
        }
        unchanged = tuple(
            record
            for record in result
            if record.key is not None
            and _equals_head(record, heads.get(record.key), emit=emit, version=version)
        )
    unchanged_ids = frozenset(record.id for record in unchanged)
    return CandidateSet(
        basis=basis,
        effect=effect,
        status=status,
        records=tuple(record for record in result if record.id not in unchanged_ids),
        divergence=_divergence(result, emit=emit, basis=basis, unchanged=unchanged_ids),
        unchanged=unchanged,
    )


__all__ = [
    "CandidateEffect",
    "CandidateRecord",
    "CandidateSet",
    "compile_candidate_set",
    "validate_collection_content",
]
