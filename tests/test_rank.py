"""Rank AST validation and canonical evaluator tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memseek.search.rank import (
    RankCandidate,
    RankEvaluationError,
    RankValidationError,
    evaluate_rank,
    normalize_rank_scores,
    rank_score_bounds,
    validate_rank_expression,
)

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _candidate(
    *,
    similarity: float | None = None,
    text_match: float | None = None,
    scores: dict[str, float] | None = None,
    age_hours: float = 0.0,
) -> RankCandidate:
    stamp = _NOW - timedelta(hours=age_hours)
    return RankCandidate(
        occurred_at=stamp,
        created_at=stamp,
        last_accessed=stamp,
        similarity=similarity,
        text_match=text_match,
        scores=scores or {},
    )


def test_leaves_operators_and_missing_signals() -> None:
    candidates = [
        _candidate(similarity=0.9, scores={"importance": 8}),
        _candidate(similarity=None, scores={}),
    ]
    assert evaluate_rank(["similarity"], candidates, now=_NOW) == [0.9, 0.0]
    assert evaluate_rank(["score", "importance"], candidates, now=_NOW) == [8.0, 0.0]
    assert evaluate_rank(["const", 2.5], candidates, now=_NOW) == [2.5, 2.5]
    assert evaluate_rank(["sum", [["similarity"], ["const", 1]]], candidates, now=_NOW) == [
        1.9,
        1.0,
    ]
    assert evaluate_rank(["max", [["similarity"], ["const", 0.5]]], candidates, now=_NOW) == [
        0.9,
        0.5,
    ]
    assert evaluate_rank(["product", 2, ["score", "importance"]], candidates, now=_NOW) == [
        16.0,
        0.0,
    ]


def test_age_hours_saturate_and_decay_midpoints() -> None:
    candidates = [_candidate(age_hours=24.0), _candidate(age_hours=0.0)]
    ages = evaluate_rank(["age_hours", "occurred_at"], candidates, now=_NOW)
    assert ages == [24.0, 0.0]
    decayed = evaluate_rank(
        ["decay", ["age_hours", "occurred_at"], {"midpoint": 24, "exponent": 1}],
        candidates,
        now=_NOW,
    )
    assert decayed[0] == pytest.approx(0.5)
    assert decayed[1] == pytest.approx(1.0)
    saturated = evaluate_rank(
        ["saturate", ["score", "importance"], {"midpoint": 5, "exponent": 1}],
        [_candidate(scores={"importance": 5}), _candidate(scores={"importance": 0})],
        now=_NOW,
    )
    assert saturated[0] == pytest.approx(0.5)
    assert saturated[1] == pytest.approx(0.0)


def test_negative_operands_are_clamped_before_saturate_and_decay() -> None:
    candidates = [_candidate(scores={"delta": -3})]
    saturated = evaluate_rank(
        ["saturate", ["score", "delta"], {"midpoint": 1, "exponent": 1}],
        candidates,
        now=_NOW,
    )
    decayed = evaluate_rank(
        ["decay", ["score", "delta"], {"midpoint": 1, "exponent": 1}],
        candidates,
        now=_NOW,
    )
    assert saturated == [0.0]
    assert decayed == [1.0]


def test_normalize_is_min_max_across_the_candidate_set() -> None:
    candidates = [
        _candidate(scores={"importance": 2}),
        _candidate(scores={"importance": 6}),
        _candidate(scores={"importance": 10}),
    ]
    normalized = evaluate_rank(["normalize", ["score", "importance"]], candidates, now=_NOW)
    assert normalized == [0.0, 0.5, 1.0]
    equal = [_candidate(scores={"importance": 7}), _candidate(scores={"importance": 7})]
    assert evaluate_rank(["normalize", ["score", "importance"]], equal, now=_NOW) == [0.0, 0.0]
    assert evaluate_rank(["normalize", ["score", "importance"]], [], now=_NOW) == []


def test_response_score_normalization_is_bounded_monotonic_and_keeps_ties() -> None:
    native = [8.0, 5.0, 2.0]
    bounds = rank_score_bounds(native)

    assert bounds == (2.0, 8.0)
    assert normalize_rank_scores(native, bounds=bounds) == [1.0, 0.5, 0.0]
    assert normalize_rank_scores([8.0, 5.0], bounds=bounds) == [1.0, 0.5]
    assert normalize_rank_scores([7.0, 7.0]) == [1.0, 1.0]
    assert normalize_rank_scores([]) == []


def test_response_score_normalization_rejects_invalid_native_values_and_bounds() -> None:
    with pytest.raises(RankEvaluationError, match="non-finite"):
        rank_score_bounds([float("nan")])
    with pytest.raises(RankEvaluationError, match="outside normalization bounds"):
        normalize_rank_scores([3.0], bounds=(0.0, 2.0))


def test_validation_rejects_illegal_shapes_and_modes() -> None:
    with pytest.raises(RankValidationError, match="not legal for text mode"):
        validate_rank_expression(["similarity"], mode="text")
    with pytest.raises(RankValidationError, match="not legal for vector mode"):
        validate_rank_expression(["text_match"], mode="vector")
    with pytest.raises(RankValidationError, match="unknown rank operator"):
        validate_rank_expression(["median", [["similarity"]]])
    with pytest.raises(RankValidationError, match="unknown scorer"):
        validate_rank_expression(["score", "mystery"], scorer_names=["importance"])
    with pytest.raises(RankValidationError, match="post-fusion boost"):
        validate_rank_expression(["similarity"], boost=True)
    validate_rank_expression(["score", "importance"], boost=True, scorer_names=["importance"])
    deep = ["normalize", ["normalize", ["normalize", ["normalize", ["normalize", ["const", 1]]]]]]
    with pytest.raises(RankValidationError, match="depth"):
        validate_rank_expression(deep)
    wide = ["sum", [["const", index] for index in range(16)]]
    with pytest.raises(RankValidationError, match="nodes"):
        validate_rank_expression(wide)


def test_non_finite_scores_evaluate_to_zero() -> None:
    candidates = [_candidate(scores={"importance": float("nan")})]
    assert evaluate_rank(["score", "importance"], candidates, now=_NOW) == [0.0]
