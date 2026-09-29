from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError
from reference_catalog import materialize_reference_catalog

from memseek.config import Settings
from memseek.definitions import DefinitionError, load_definition_catalog
from memseek.definitions.judgment import (
    QuestionMap,
    Questions,
    answer_schema,
    answers_json,
    compile_keep,
    kept,
    parse_answers,
    questions_json,
)

QUESTIONS = TypeAdapter(QuestionMap)


def _questions() -> Questions:
    return QUESTIONS.validate_python(
        {
            "actionable": {
                "type": "noul",
                "instructions": "Does `record.text` ask someone to do something?",
                # The YAML loader hands these keys over as booleans.
                "criteria": {True: "An explicit request", False: "Chatter"},
            },
            "topic": {
                "type": "choice",
                "instructions": {"task": "classify", "field": "`record.text`"},
                "criteria": {"bug": "Something is broken", "feature": None, "other": None},
            },
            "urgency": {
                "type": "score",
                "instructions": "How time-sensitive is `record.text`?",
                "criteria": ["Can wait", "This week", "Today"],
            },
        }
    )


RAW_ANSWERS: dict[str, Any] = {
    "actionable": {"type": "noul", "noul": 0.91},
    "topic": {
        "type": "choice",
        "choice": "bug",
        "probabilities": {"bug": 0.8, "feature": 0.15, "other": 0.05},
        "confidence": 0.73,
    },
    "urgency": {
        "type": "score",
        "score": 1.6,
        "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7},
        "confidence": 0.55,
    },
}


def test_questions_serialize_in_the_api_vocabulary() -> None:
    assert questions_json(_questions()) == {
        "actionable": {
            "type": "noul",
            "instructions": "Does `record.text` ask someone to do something?",
            "criteria": {"true": "An explicit request", "false": "Chatter"},
        },
        "topic": {
            "type": "choice",
            "instructions": {"task": "classify", "field": "`record.text`"},
            "criteria": {"bug": "Something is broken", "feature": None, "other": None},
        },
        "urgency": {
            "type": "score",
            "instructions": "How time-sensitive is `record.text`?",
            "criteria": ["Can wait", "This week", "Today"],
        },
    }


def test_parsed_answers_normalize_score_probabilities_and_match_the_schema() -> None:
    questions = _questions()

    stored = answers_json(parse_answers(questions, RAW_ANSWERS))

    assert stored == {
        "actionable": {"noul": 0.91},
        "topic": {
            "choice": "bug",
            "probabilities": {"bug": 0.8, "feature": 0.15, "other": 0.05},
            "confidence": 0.73,
        },
        "urgency": {"score": 1.6, "probabilities": [0.1, 0.2, 0.7], "confidence": 0.55},
    }
    assert list(Draft202012Validator(answer_schema(questions)).iter_errors(stored)) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"actionable": None}, "answers missing for ['actionable']"),
        ({"extra": {"noul": 0.5}}, "answers returned for undeclared questions ['extra']"),
        ({"actionable": {"noul": 1.2}}, "actionable.noul must be finite and within [0, 1]"),
        (
            {"actionable": {"type": "score", "noul": 0.5}},
            "actionable was answered as 'score', not 'noul'",
        ),
        (
            {"topic": {"choice": "sales", "probabilities": {}, "confidence": 0.5}},
            "topic.choice 'sales' is not a declared option",
        ),
        (
            {"urgency": {"score": 3, "probabilities": [0.1, 0.2, 0.7], "confidence": 0.5}},
            "urgency.score must be within [0, 2]",
        ),
        (
            {"urgency": {"score": 1, "probabilities": [0.5, 0.5], "confidence": 0.5}},
            "urgency.probabilities must list 3 level probabilities",
        ),
    ],
)
def test_malformed_answers_are_rejected(change: dict[str, Any], message: str) -> None:
    raw = {**RAW_ANSWERS, **change}
    raw = {name: value for name, value in raw.items() if value is not None}

    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        parse_answers(_questions(), raw)


def test_keep_clauses_are_anded_over_parsed_answers() -> None:
    questions = _questions()
    answers = parse_answers(questions, RAW_ANSWERS)

    passing = compile_keep(
        questions,
        {
            "actionable": {"noul_gte": 0.65},
            "topic": {"choice_in": ["bug", "feature"], "confidence_gte": 0.5},
            "urgency": {"score_gte": 1.5, "score_lte": 2},
        },
    )
    failing = compile_keep(
        questions,
        {"actionable": {"noul_gte": 0.65}, "topic": {"prob_gte": {"feature": 0.5}}},
    )

    assert kept(passing, answers) is True
    assert kept(failing, answers) is False
    assert kept(compile_keep(questions, {"actionable": {"noul_lte": 0.35}}), answers) is False


@pytest.mark.parametrize(
    ("keep", "message"),
    [
        ({"missing": {"noul_gte": 0.5}}, "keep.missing names an undeclared question"),
        (
            {"actionable": {"choice_in": ["bug"]}},
            "keep.actionable.choice_in does not apply to a noul question",
        ),
        (
            {"topic": {"between": [0, 1]}},
            "keep.topic.between is not one of ['choice_in', 'confidence_gte', 'noul_gte', "
            "'noul_lte', 'prob_gte', 'score_gte', 'score_lte']",
        ),
        (
            {"topic": {"choice_in": ["sales"]}},
            "keep.topic.choice_in: choice_in names undeclared options ['sales']",
        ),
    ],
)
def test_keep_rejects_operators_outside_the_closed_set(keep: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        compile_keep(_questions(), keep)


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"q": {"type": "choice", "instructions": "x", "criteria": {"only": None}}},
        {"q": {"type": "score", "instructions": "x", "criteria": ["one"]}},
        {"q": {"type": "score", "instructions": "x", "criteria": [str(n) for n in range(11)]}},
        {"q": {"type": "noul", "instructions": "  "}},
        {"Bad": {"type": "noul", "instructions": "x"}},
    ],
)
def test_question_limits_are_enforced(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        QUESTIONS.validate_python(raw)


def _catalog_error(tmp_path: Path, file: str, old: str, new: str) -> DefinitionError:
    root = materialize_reference_catalog(tmp_path / "catalog")
    path = root / file
    source = path.read_text(encoding="utf-8")
    assert old in source
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(Settings(catalog_file=root / "catalog.yaml", llm_fake=True))
    return caught.value


@pytest.mark.parametrize(
    ("file", "old", "new", "path", "message"),
    [
        (
            "conf/models.yaml",
            'targets: ["typesafe:jev-1.13.0"]',
            'targets: ["typesafe:jev-1.13.0", "openai:gpt-5.4-2026-03-05"]',
            "aliases.jev.targets",
            "alias targets must all support completion or all support judgment",
        ),
        (
            "conf/models.yaml",
            'targets: ["typesafe:jev-1.13.0"]',
            'targets: ["typesafe:jev-1.13.0"]\n    params: {temperature: 0}',
            "aliases.jev",
            "a judgment alias forbids params and context_tokens",
        ),
        (
            "conf/models.yaml",
            "  derivation: strong",
            "  derivation: jev",
            "defaults.derivation",
            "model alias 'jev' cannot be used for completion",
        ),
        (
            "conf/processors.yaml",
            "model: importance_scorer",
            "model: jev",
            "processors.importance.model",
            "model alias 'jev' cannot be used for completion",
        ),
        (
            "derivations/reflection.yaml",
            "model: strong",
            "model: jev",
            "tasks[2].with.model",
            "model alias 'jev' cannot be used for completion",
        ),
    ],
)
def test_alias_kind_must_match_its_use(
    tmp_path: Path, file: str, old: str, new: str, path: str, message: str
) -> None:
    error = _catalog_error(tmp_path, file, old, new)

    assert (error.code, error.path, error.message) == ("model_kind", path, message)
