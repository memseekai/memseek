"""Small, expression-free template helpers used by definitions.

These helpers substitute values and nothing else.  They add no fence, no
attribute, and no prose of their own: a caller that interpolates untrusted text
escapes it first, and the author's template supplies whatever wrapper that text
should appear inside.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_REFERENCE_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*}}")
_EXACT_REFERENCE_RE = re.compile(
    r"^{{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*}}$"
)


class TemplateError(ValueError):
    """Raised for unsupported syntax or a missing reference."""

    def __init__(self, message: str, *, path: str | None = None, code: str = "template") -> None:
        self.path = path
        self.code = code
        super().__init__(message)


def template_references(value: str) -> frozenset[str]:
    """Return every dotted reference and reject stray template delimiters."""

    refs = frozenset(match.group(1) for match in _REFERENCE_RE.finditer(value))
    remainder = _REFERENCE_RE.sub("", value)
    # A literal JSON example commonly contains adjacent closing braces, so a
    # stray ``}}`` is not by itself template syntax. Every expression starts
    # with ``{{`` and those must all have been consumed by the reference regex.
    if "{{" in remainder:
        raise TemplateError("unsupported or unbalanced template expression", code="template_syntax")
    return refs


def _lookup(path: str, variables: Mapping[str, Any]) -> Any:
    current: Any = variables
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise TemplateError(
                f"missing template value: {path}", path=path, code="template_missing"
            )
        current = current[part]
    return current


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def resolve_value(value: Any, variables: Mapping[str, Any]) -> Any:
    """Resolve one scalar, preserving the type of an exact reference."""

    if not isinstance(value, str):
        return value
    template_references(value)
    exact = _EXACT_REFERENCE_RE.fullmatch(value)
    if exact:
        return _lookup(exact.group(1), variables)

    return _REFERENCE_RE.sub(lambda match: _stringify(_lookup(match.group(1), variables)), value)


def render_template(template: str, variables: Mapping[str, Any]) -> str:
    """Render a string template without evaluating arbitrary expressions."""

    rendered = resolve_value(template, variables)
    if not isinstance(rendered, str):
        return _stringify(rendered)
    return rendered


def render_object(value: Any, variables: Mapping[str, Any]) -> Any:
    """Recursively resolve template strings in a JSON-like object."""

    if isinstance(value, str):
        return resolve_value(value, variables)
    if isinstance(value, list):
        return [render_object(item, variables) for item in value]
    if isinstance(value, tuple):
        return tuple(render_object(item, variables) for item in value)
    if isinstance(value, Mapping):
        return {key: render_object(item, variables) for key, item in value.items()}
    return value


def require_known_references(
    value: Any,
    allowed_roots: set[str] | frozenset[str],
    *,
    context: str = "template",
) -> frozenset[str]:
    """Validate that all templates in a JSON-like object use known roots."""

    refs: set[str] = set()
    if isinstance(value, str):
        refs.update(template_references(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            refs.update(require_known_references(item, allowed_roots, context=context))
    elif isinstance(value, (list, tuple)):
        for item in value:
            refs.update(require_known_references(item, allowed_roots, context=context))
    unknown = sorted(ref for ref in refs if ref.split(".", 1)[0] not in allowed_roots)
    if unknown:
        raise TemplateError(
            f"{context} references unavailable variable(s): {', '.join(unknown)}",
            path=unknown[0],
            code="template_reference",
        )
    return frozenset(refs)
