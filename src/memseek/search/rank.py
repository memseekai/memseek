"""Validation, normalization, and evaluation for the portable rank AST."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from memseek.definitions.base import finite_number

type RankMode = Literal["vector", "text", "hybrid", "recent", "structured"]
type RankExpression = tuple[Any, ...]

MAX_RANK_DEPTH = 5
MAX_RANK_NODES = 16
_AGE_FIELDS = {"created_at", "occurred_at", "last_accessed"}


class RankValidationError(ValueError):
    def __init__(self, message: str, *, path: str = "rank", code: str = "rank") -> None:
        self.path = path
        self.code = code
        super().__init__(message)


def validate_rank_expression(
    expression: Any,
    *,
    mode: RankMode | None = None,
    scorer_names: Iterable[str] | None = None,
    boost: bool = False,
) -> RankExpression:
    """Validate a rank expression and return a recursively immutable form."""

    scorers = frozenset(scorer_names) if scorer_names is not None else None
    node_count = 0

    def visit(node: Any, depth: int, path: str) -> RankExpression:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_RANK_NODES:
            raise RankValidationError(
                f"rank expression exceeds {MAX_RANK_NODES} nodes",
                path=path,
                code="rank_nodes",
            )
        if depth > MAX_RANK_DEPTH:
            raise RankValidationError(
                f"rank expression exceeds depth {MAX_RANK_DEPTH}",
                path=path,
                code="rank_depth",
            )
        if not isinstance(node, (list, tuple)) or not node or not isinstance(node[0], str):
            raise RankValidationError("rank node must be a non-empty array", path=path)

        operator = node[0]
        if operator in {"similarity", "text_match"}:
            if len(node) != 1:
                raise RankValidationError(f"{operator} takes no arguments", path=path)
            if boost:
                raise RankValidationError(
                    f"{operator} is not legal in a post-fusion boost",
                    path=path,
                    code="rank_boost_leaf",
                )
            legal_modes = {"vector", "hybrid"} if operator == "similarity" else {"text", "hybrid"}
            if mode is not None and mode not in legal_modes:
                raise RankValidationError(
                    f"{operator} is not legal for {mode} mode",
                    path=path,
                    code="rank_mode",
                )
            return (operator,)

        if operator == "score":
            if len(node) != 2 or not isinstance(node[1], str):
                raise RankValidationError("score requires one scorer name", path=path)
            if scorers is not None and node[1] not in scorers:
                raise RankValidationError(
                    f"unknown scorer {node[1]!r}", path=f"{path}[1]", code="reference"
                )
            return (operator, node[1])

        if operator == "age_hours":
            if len(node) != 2 or node[1] not in _AGE_FIELDS:
                raise RankValidationError(
                    "age_hours field must be created_at, occurred_at, or last_accessed",
                    path=path,
                )
            return (operator, node[1])

        if operator == "const":
            if len(node) != 2:
                raise RankValidationError("const requires one finite number", path=path)
            return (operator, finite_number(node[1], "const"))

        if operator in {"sum", "max"}:
            if len(node) != 2 or not isinstance(node[1], (list, tuple)) or not node[1]:
                raise RankValidationError(
                    f"{operator} requires a non-empty expression list", path=path
                )
            children = tuple(
                visit(child, depth + 1, f"{path}[1][{index}]")
                for index, child in enumerate(node[1])
            )
            return (operator, children)

        if operator == "product":
            if len(node) != 3:
                raise RankValidationError("product requires a factor and expression", path=path)
            factor = finite_number(node[1], "product factor")
            return (operator, factor, visit(node[2], depth + 1, f"{path}[2]"))

        if operator in {"saturate", "decay"}:
            if len(node) != 3 or not isinstance(node[2], dict):
                raise RankValidationError(f"{operator} requires expression and options", path=path)
            if set(node[2]) != {"midpoint", "exponent"}:
                raise RankValidationError(
                    f"{operator} options must be exactly midpoint and exponent", path=f"{path}[2]"
                )
            midpoint = finite_number(node[2]["midpoint"], "midpoint")
            exponent = finite_number(node[2]["exponent"], "exponent")
            if midpoint <= 0 or exponent <= 0:
                raise RankValidationError(
                    f"{operator} midpoint and exponent must be positive", path=f"{path}[2]"
                )
            return (
                operator,
                visit(node[1], depth + 1, f"{path}[1]"),
                {"midpoint": midpoint, "exponent": exponent},
            )

        if operator == "normalize":
            if len(node) != 2:
                raise RankValidationError("normalize requires one expression", path=path)
            return (operator, visit(node[1], depth + 1, f"{path}[1]"))

        raise RankValidationError(
            f"unknown rank operator {operator!r}", path=path, code="rank_operator"
        )

    return visit(expression, 1, "rank")


@dataclass(frozen=True, slots=True)
class RankCandidate:
    """Canonical per-row signals consumed by the rank evaluator.

    Backend scores never appear here: similarity and text match are
    recomputed from canonical PostgreSQL data before evaluation.
    """

    occurred_at: datetime
    created_at: datetime
    last_accessed: datetime
    similarity: float | None = None
    text_match: float | None = None
    scores: Mapping[str, Any] = field(default_factory=dict)


class RankEvaluationError(ValueError):
    def __init__(self, message: str) -> None:
        self.code = "rank_evaluation"
        super().__init__(message)


def _score_signal(candidate: RankCandidate, name: str) -> float:
    value = candidate.scores.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) else 0.0


def _age_hours(candidate: RankCandidate, field_name: str, now: datetime) -> float:
    value: datetime = getattr(candidate, field_name)
    return max(0.0, (now - value).total_seconds() / 3_600.0)


def evaluate_rank(
    expression: Any,
    candidates: Sequence[RankCandidate],
    *,
    now: datetime,
) -> list[float]:
    """Evaluate one validated rank expression across the whole candidate set.

    Evaluation is vectorized so ``normalize`` can min-max its child across the
    current candidates in the same recursive pass; equal child values
    normalize to zero for every candidate. A missing signal evaluates to
    zero, and every produced value must be finite.
    """

    if not candidates:
        return []

    def visit(node: Any) -> list[float]:
        operator = node[0]
        if operator == "similarity":
            return [
                candidate.similarity if candidate.similarity is not None else 0.0
                for candidate in candidates
            ]
        if operator == "text_match":
            return [
                candidate.text_match if candidate.text_match is not None else 0.0
                for candidate in candidates
            ]
        if operator == "score":
            return [_score_signal(candidate, node[1]) for candidate in candidates]
        if operator == "age_hours":
            return [_age_hours(candidate, node[1], now) for candidate in candidates]
        if operator == "const":
            return [float(node[1])] * len(candidates)
        if operator in {"sum", "max"}:
            children = [visit(child) for child in node[1]]
            combine = sum if operator == "sum" else max
            return [combine(values) for values in zip(*children, strict=True)]
        if operator == "product":
            factor = float(node[1])
            return [factor * value for value in visit(node[2])]
        if operator in {"saturate", "decay"}:
            midpoint = float(node[2]["midpoint"])
            exponent = float(node[2]["exponent"])
            results = []
            for value in visit(node[1]):
                operand = max(0.0, value)
                powered = operand**exponent
                scale = midpoint**exponent
                if operator == "saturate":
                    results.append(powered / (powered + scale))
                else:
                    results.append(1.0 / (1.0 + powered / scale))
            return results
        if operator == "normalize":
            values = visit(node[1])
            low = min(values)
            high = max(values)
            if high == low:
                return [0.0] * len(values)
            return [(value - low) / (high - low) for value in values]
        raise RankEvaluationError(f"unknown rank operator {operator!r}")

    results = visit(validate_rank_expression(expression))
    bad = next((value for value in results if not math.isfinite(value)), None)
    if bad is not None:
        raise RankEvaluationError("rank expression produced a non-finite value")
    return results


def rank_score_bounds(scores: Sequence[float]) -> tuple[float, float] | None:
    """Return finite native-score bounds for one ranked candidate pool."""

    if not scores:
        return None
    values = [float(score) for score in scores]
    if any(not math.isfinite(value) for value in values):
        raise RankEvaluationError("cannot normalize a non-finite rank score")
    return min(values), max(values)


def normalize_rank_scores(
    scores: Sequence[float], *, bounds: tuple[float, float] | None = None
) -> list[float]:
    """Project native ranking utilities onto a query-relative 0--1 scale.

    Search scores are not probabilities: their native scale depends on the
    selected rank expression, fusion method, and optional boosts.  This final
    monotonic projection gives response consumers one display scale without
    changing order or hiding the native utility.  The supplied bounds normally
    cover the complete ranked candidate pool: its highest score maps to 1 and
    its lowest to 0, even when only a leading slice is projected here.  When
    every score is tied, every result maps to 1 because all results share the
    highest rank utility.
    """

    score_bounds = bounds if bounds is not None else rank_score_bounds(scores)
    if score_bounds is None:
        return []
    low, high = score_bounds
    if not math.isfinite(low) or not math.isfinite(high) or high < low:
        raise RankEvaluationError("invalid rank score normalization bounds")
    values = [float(score) for score in scores]
    if any(not math.isfinite(value) or value < low or value > high for value in values):
        raise RankEvaluationError("rank score falls outside normalization bounds")
    if high == low:
        return [1.0] * len(values)
    scale = high - low
    return [min(1.0, max(0.0, (value - low) / scale)) for value in values]


RANK_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Memseek rank expression",
    "type": "array",
    "minItems": 1,
    "maxItems": 3,
    "description": "Validated recursively by memseek.search.rank (depth 5, 16 nodes).",
}
