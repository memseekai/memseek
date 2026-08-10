"""Provenance-carrying values and citation-visible prompt rendering.

M4 derivation runners cannot rely on plain strings for their prompt data:
every variable must carry the set of canonical record IDs it transitively
represents. This module implements the primitive from Section 10.3 of the
v3.2 specification. It is intentionally pure and side-effect free so that
the runner, retrieve step, output validator, and unit tests can all share
one canonical implementation.

Two shapes are provided:

- :class:`ProvenanceValue` wraps an arbitrary JSON-like payload with the
  set of canonical UUID sources it transitively represents. Values may
  additionally be marked *trusted* (operator-authored literals are
  interpolated verbatim) or *pre-escaped* (retrieval renders escaped
  their own rows as they were built).

- :func:`render_prompt` renders a template string against a mapping of
  variables and returns the fully rendered prompt together with the exact
  transitive source union that reached the prompt and the strictly
  smaller ``citation_visible_ids`` subset whose full UUID handles are
  literally present in the rendered text.

The renderer preserves the two documented reference behaviours:

- An exact reference such as ``{{qs.questions}}`` returns the original
  typed value plus its source set, so :func:`resolve_typed_reference` can
  power ``foreach`` iteration without collapsing lists to text.
- An embedded reference is stringified. Untrusted lists and mappings are
  serialised as compact JSON, scalars use their canonical string form,
  and every untrusted value is escaped so it cannot close or forge an
  element in the surrounding prompt.

The renderer adds no element and no sentence of its own. Marking retrieved
data as untrusted is prompt composition, and prompt composition belongs to
the derivation author: the element goes in the task template, beside the
instructions it qualifies, where an author can read and change it. Escaping
is the invariant that makes that authored element trustworthy.

The module does not touch the database, the LLM registry, or the search
engine. It is safe to import from any layer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from memseek.render import escape_untrusted
from memseek.templates import TemplateError

__all__ = [
    "MAX_FOREACH_ITEMS",
    "ProvenanceValue",
    "RenderedPrompt",
    "extract_uuid_handles",
    "render_prompt",
    "resolve_typed_reference",
]


MAX_FOREACH_ITEMS = 5
"""``foreach`` iteration is capped by Section 6.5 of the v3.2 specification."""


_REFERENCE_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*}}")
_EXACT_REFERENCE_RE = re.compile(
    r"^{{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*}}$"
)
# UUID handles are canonical 8-4-4-4-12 hex, matched case-insensitively so a
# rendered lower-case handle from a model prompt still matches its canonical
# hyphenated source UUID.
_UUID_HANDLE_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


@dataclass(frozen=True, slots=True)
class ProvenanceValue:
    """A value that carries its complete transitive record-source set.

    ``value`` is the actual payload (a scalar, list, or mapping).
    ``source_ids`` is the deduplicated union of canonical record IDs this
    value represents. ``trusted`` marks operator-authored template
    literals that are interpolated verbatim; every value produced from a
    record, a retrieval, or a model call is untrusted by default and is
    escaped on interpolation. ``pre_escaped`` records that the value is a
    row rendering from :mod:`memseek.render`, which escaped each row as it
    was built, so escaping it again would double the sequences.
    """

    value: Any
    source_ids: frozenset[UUID] = field(default_factory=frozenset)
    trusted: bool = False
    pre_escaped: bool = False

    def with_value(self, value: Any) -> ProvenanceValue:
        """Return a copy that preserves provenance and flags."""

        return ProvenanceValue(
            value=value,
            source_ids=self.source_ids,
            trusted=self.trusted,
            pre_escaped=self.pre_escaped,
        )


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """One rendered LLM prompt together with its provenance projections.

    ``text`` is the fully substituted prompt string. ``transitive_source_ids``
    is the union of source sets of every reference actually interpolated
    into the prompt (references that never appear contribute nothing).
    ``citation_visible_ids`` intersects the full UUID handles literally
    present in ``text`` with ``transitive_source_ids``, so a hidden
    transitive source cannot be cited and an invented handle cannot pass
    validation.
    """

    text: str
    transitive_source_ids: frozenset[UUID]
    citation_visible_ids: frozenset[UUID]


def _lookup(path: str, variables: Mapping[str, Any]) -> Any:
    """Walk a dotted path across mappings and :class:`ProvenanceValue` payloads."""

    current: Any = variables
    source_ids: frozenset[UUID] = frozenset()
    trusted = True
    pre_escaped = False
    wrapped = False
    for part in path.split("."):
        while isinstance(current, ProvenanceValue):
            wrapped = True
            source_ids = source_ids | current.source_ids
            trusted = trusted and current.trusted
            pre_escaped = pre_escaped or current.pre_escaped
            current = current.value
        if not isinstance(current, Mapping) or part not in current:
            raise TemplateError(
                f"missing template value: {path}", path=path, code="template_missing"
            )
        current = current[part]
    while isinstance(current, ProvenanceValue):
        wrapped = True
        source_ids = source_ids | current.source_ids
        trusted = trusted and current.trusted
        pre_escaped = pre_escaped or current.pre_escaped
        current = current.value
    if not wrapped:
        return current
    return ProvenanceValue(
        value=current,
        source_ids=source_ids,
        trusted=trusted,
        pre_escaped=pre_escaped,
    )


def _resolved_source_ids(node: Any) -> frozenset[UUID]:
    """Collect provenance from a value and every nested :class:`ProvenanceValue`."""

    if isinstance(node, ProvenanceValue):
        inner = _resolved_source_ids(node.value)
        return node.source_ids | inner
    if isinstance(node, Mapping):
        combined: frozenset[UUID] = frozenset()
        for item in node.values():
            combined = combined | _resolved_source_ids(item)
        return combined
    if isinstance(node, (list, tuple)):
        combined = frozenset()
        for item in node:
            combined = combined | _resolved_source_ids(item)
        return combined
    return frozenset()


def _strip_provenance(node: Any) -> Any:
    """Recursively unwrap :class:`ProvenanceValue` for JSON serialisation."""

    if isinstance(node, ProvenanceValue):
        return _strip_provenance(node.value)
    if isinstance(node, Mapping):
        return {key: _strip_provenance(item) for key, item in node.items()}
    if isinstance(node, (list, tuple)):
        return [_strip_provenance(item) for item in node]
    return node


def _stringify(value: Any, *, trusted: bool, pre_escaped: bool) -> str:
    """Render a JSON-like payload for embedded prompt interpolation.

    Untrusted text is escaped and returned otherwise unadorned.  The escape
    form is a JSON unicode escape, so an escaped value interpolated inside a
    JSON literal still parses to the original characters.
    """

    payload = _strip_provenance(value)
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    elif payload is None:
        text = "null"
    elif isinstance(payload, bool):
        text = "true" if payload else "false"
    else:
        text = str(payload)
    if trusted or pre_escaped:
        return text
    return escape_untrusted(text)


def _reference_shape(value: str) -> re.Match[str] | None:
    """Return the exact-reference match, rejecting stray template delimiters."""

    for match in _REFERENCE_RE.finditer(value):
        del match
    remainder = _REFERENCE_RE.sub("", value)
    if "{{" in remainder:
        raise TemplateError("unsupported or unbalanced template expression", code="template_syntax")
    return _EXACT_REFERENCE_RE.fullmatch(value)


def resolve_typed_reference(
    value: Any, variables: Mapping[str, Any]
) -> tuple[Any, frozenset[UUID]]:
    """Resolve an exact reference and return its typed value plus provenance.

    ``foreach`` and other typed positions require the caller to preserve
    the original list/mapping structure. Any other input, including a
    template with surrounding characters, raises :class:`TemplateError`.
    """

    if not isinstance(value, str):
        raise TemplateError("typed reference must be a template string", code="template_syntax")
    match = _reference_shape(value)
    if match is None:
        raise TemplateError(
            "typed reference must be exactly one {{name}} expression",
            code="template_syntax",
        )
    resolved = _lookup(match.group(1), variables)
    if isinstance(resolved, ProvenanceValue):
        return resolved.value, resolved.source_ids | _resolved_source_ids(resolved.value)
    return resolved, _resolved_source_ids(resolved)


def extract_uuid_handles(text: str) -> frozenset[UUID]:
    """Return the deduplicated set of canonical UUID handles literally present."""

    handles: set[UUID] = set()
    for match in _UUID_HANDLE_RE.finditer(text):
        try:
            handles.add(UUID(match.group(0)))
        except ValueError:  # pragma: no cover - regex guarantees canonical form
            continue
    return frozenset(handles)


def render_prompt(template: str, variables: Mapping[str, Any]) -> RenderedPrompt:
    """Render one LLM prompt template and compute its provenance projections."""

    if not isinstance(template, str):
        raise TemplateError("prompt template must be a string", code="template_syntax")
    transitive: set[UUID] = set()

    def replace(match: re.Match[str]) -> str:
        resolved = _lookup(match.group(1), variables)
        if isinstance(resolved, ProvenanceValue):
            transitive.update(resolved.source_ids)
            transitive.update(_resolved_source_ids(resolved.value))
            return _stringify(
                resolved.value,
                trusted=resolved.trusted,
                pre_escaped=resolved.pre_escaped,
            )
        transitive.update(_resolved_source_ids(resolved))
        return _stringify(resolved, trusted=True, pre_escaped=False)

    # ``_reference_shape`` runs the same syntax check used by the exact form.
    _reference_shape(template)
    rendered = _REFERENCE_RE.sub(replace, template)
    handles = extract_uuid_handles(rendered)
    transitive_frozen = frozenset(transitive)
    return RenderedPrompt(
        text=rendered,
        transitive_source_ids=transitive_frozen,
        citation_visible_ids=handles & transitive_frozen,
    )


def foreach_items(
    value: Any, variables: Mapping[str, Any]
) -> tuple[Sequence[Any], frozenset[UUID]]:
    """Resolve a ``foreach`` template and enforce its typed contract.

    The value must be an exact template reference resolving to a list of at
    most :data:`MAX_FOREACH_ITEMS` items. The returned provenance is the
    union of the outer reference's source set and every nested
    :class:`ProvenanceValue` payload; the runner charges those IDs against
    ``limits.max_visible_records`` before rendering any child prompt.
    """

    payload, provenance = resolve_typed_reference(value, variables)
    if not isinstance(payload, (list, tuple)):
        raise TemplateError("foreach requires a typed list reference", code="foreach_type")
    if len(payload) > MAX_FOREACH_ITEMS:
        raise TemplateError(
            f"foreach lists are capped at {MAX_FOREACH_ITEMS} items",
            code="foreach_cap",
        )
    return list(payload), provenance


def union_source_ids(values: Iterable[Any]) -> frozenset[UUID]:
    """Return the deduplicated union of provenance across arbitrary values."""

    combined: frozenset[UUID] = frozenset()
    for value in values:
        if isinstance(value, ProvenanceValue):
            combined = combined | value.source_ids | _resolved_source_ids(value.value)
        else:
            combined = combined | _resolved_source_ids(value)
    return combined
