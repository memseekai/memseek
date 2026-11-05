"""Question and answer vocabulary for System One judgment models.

The question shapes mirror the TypeSafe Jev HTTP API so authors can copy
examples verbatim.  Each question type owns its answer schema and its answer
parser; ``KEEP_OPERATORS`` is the closed set of threshold tests a derivation may
apply to parsed answers.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, field_validator, model_validator

from .base import ProcessorName, StrictModel

MAX_QUESTIONS = 64
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10

type Instruction = str | dict[str, Any] | list[Any]


def _require_instruction(value: Instruction, label: str) -> Instruction:
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{label} must be non-blank")
    if not isinstance(value, str) and not value:
        raise ValueError(f"{label} must be non-empty")
    return value


def _probability(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{label} must be finite and within [0, 1]")
    return number


def _answer_object(raw: Any, label: str, question_type: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"answer {label} must be an object")
    answered = raw.get("type", question_type)
    if answered != question_type:
        raise ValueError(f"{label} was answered as {answered!r}, not {question_type!r}")
    return raw


_UNIT = {"type": "number", "minimum": 0, "maximum": 1}


@dataclass(frozen=True, slots=True)
class NoulAnswer:
    noul: float

    @property
    def unit(self) -> float:
        return self.noul

    def as_json(self) -> dict[str, Any]:
        return {"noul": self.noul}


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float

    def as_json(self) -> dict[str, Any]:
        return {
            "choice": self.choice,
            "probabilities": dict(self.probabilities),
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    score: float
    probabilities: tuple[float, ...]
    confidence: float
    legend: Any = None

    @property
    def unit(self) -> float:
        return self.score / (len(self.probabilities) - 1)

    def as_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "score": self.score,
            "probabilities": list(self.probabilities),
            "confidence": self.confidence,
        }
        if self.legend is not None:
            value["legend"] = self.legend
        return value


type Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


class NoulCriteria(StrictModel):
    true: Instruction | None = None
    false: Instruction | None = None

    @model_validator(mode="before")
    @classmethod
    def _yaml_boolean_keys(cls, value: Any) -> Any:
        # YAML 1.1 reads the bare keys `true:` and `false:` as booleans.
        if isinstance(value, Mapping):
            return {
                ("true" if key is True else "false" if key is False else key): item
                for key, item in value.items()
            }
        return value


class NoulQuestion(StrictModel):
    """A yes-or-no question answered with a probability in [0, 1]."""

    type: Literal["noul"]
    instructions: Instruction
    criteria: NoulCriteria | None = None

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: Instruction) -> Instruction:
        return _require_instruction(value, "instructions")

    def answer_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["noul"],
            "properties": {"noul": _UNIT},
            "additionalProperties": False,
        }

    def parse_answer(self, raw: Any, label: str) -> NoulAnswer:
        value = _answer_object(raw, label, self.type)
        return NoulAnswer(noul=_probability(value.get("noul"), f"{label}.noul"))


class ChoiceQuestion(StrictModel):
    """A pick-one question over named options."""

    type: Literal["choice"]
    instructions: Instruction
    criteria: dict[str, Instruction | None]

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: Instruction) -> Instruction:
        return _require_instruction(value, "instructions")

    @field_validator("criteria")
    @classmethod
    def _criteria(cls, value: dict[str, Instruction | None]) -> dict[str, Instruction | None]:
        if not 2 <= len(value) <= MAX_CHOICE_OPTIONS:
            raise ValueError(f"choice criteria must declare 2 to {MAX_CHOICE_OPTIONS} options")
        for option, description in value.items():
            if not option.strip():
                raise ValueError("choice options must be non-blank")
            if description is not None:
                _require_instruction(description, f"criteria.{option}")
        return value

    def answer_schema(self) -> dict[str, Any]:
        options = list(self.criteria)
        return {
            "type": "object",
            "required": ["choice", "probabilities", "confidence"],
            "properties": {
                "choice": {"type": "string", "enum": options},
                "probabilities": {
                    "type": "object",
                    "properties": dict.fromkeys(options, _UNIT),
                    "additionalProperties": False,
                },
                "confidence": _UNIT,
            },
            "additionalProperties": False,
        }

    def parse_answer(self, raw: Any, label: str) -> ChoiceAnswer:
        value = _answer_object(raw, label, self.type)
        choice = value.get("choice")
        if choice not in self.criteria:
            raise ValueError(f"{label}.choice {choice!r} is not a declared option")
        probabilities = value.get("probabilities")
        if not isinstance(probabilities, Mapping) or not set(probabilities) <= set(self.criteria):
            raise ValueError(f"{label}.probabilities must map declared options to numbers")
        return ChoiceAnswer(
            choice=str(choice),
            probabilities={
                str(option): _probability(p, f"{label}.probabilities.{option}")
                for option, p in probabilities.items()
            },
            confidence=_probability(value.get("confidence"), f"{label}.confidence"),
        )


class ScoreQuestion(StrictModel):
    """An ordinal question over 2 to 10 ordered levels."""

    type: Literal["score"]
    instructions: Instruction
    criteria: tuple[Instruction, ...]

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: Instruction) -> Instruction:
        return _require_instruction(value, "instructions")

    @field_validator("criteria")
    @classmethod
    def _criteria(cls, value: tuple[Instruction, ...]) -> tuple[Instruction, ...]:
        if not MIN_SCORE_LEVELS <= len(value) <= MAX_SCORE_LEVELS:
            raise ValueError(
                f"score criteria must declare {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS} levels"
            )
        for index, level in enumerate(value):
            _require_instruction(level, f"criteria[{index}]")
        return value

    def answer_schema(self) -> dict[str, Any]:
        levels = len(self.criteria)
        return {
            "type": "object",
            "required": ["score", "probabilities", "confidence"],
            "properties": {
                "score": {"type": "number", "minimum": 0, "maximum": levels - 1},
                "probabilities": {
                    "type": "array",
                    "items": _UNIT,
                    "minItems": levels,
                    "maxItems": levels,
                },
                "confidence": _UNIT,
                "legend": {},
            },
            "additionalProperties": False,
        }

    def parse_answer(self, raw: Any, label: str) -> ScoreAnswer:
        value = _answer_object(raw, label, self.type)
        levels = len(self.criteria)
        score = value.get("score")
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise ValueError(f"{label}.score must be a number")
        if not math.isfinite(score) or not 0 <= score <= levels - 1:
            raise ValueError(f"{label}.score must be within [0, {levels - 1}]")
        return ScoreAnswer(
            score=float(score),
            probabilities=_level_probabilities(value.get("probabilities"), levels, label),
            confidence=_probability(value.get("confidence"), f"{label}.confidence"),
            legend=value.get("legend"),
        )


def _level_probabilities(raw: Any, levels: int, label: str) -> tuple[float, ...]:
    """Normalize a list or a level-index map into one probability per level."""

    if isinstance(raw, Mapping):
        try:
            indexed = {int(str(key)): item for key, item in raw.items()}
        except ValueError as exc:
            raise ValueError(f"{label}.probabilities keys must be level indexes") from exc
        if set(indexed) != set(range(levels)):
            raise ValueError(f"{label}.probabilities must cover levels 0 to {levels - 1}")
        raw = [indexed[index] for index in range(levels)]
    if not isinstance(raw, list | tuple) or len(raw) != levels:
        raise ValueError(f"{label}.probabilities must list {levels} level probabilities")
    return tuple(
        _probability(item, f"{label}.probabilities[{index}]") for index, item in enumerate(raw)
    )


type Question = Annotated[
    NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")
]
type Questions = Mapping[str, Question]


def _question_count(questions: dict[str, Question]) -> dict[str, Question]:
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError(f"questions must declare 1 to {MAX_QUESTIONS} questions")
    return questions


type QuestionMap = Annotated[dict[ProcessorName, Question], AfterValidator(_question_count)]
type KeepSpec = dict[ProcessorName, dict[str, Any]]


def answer_schema(questions: Questions) -> dict[str, Any]:
    """The JSON Schema of the answer map returned for ``questions``."""

    return {
        "type": "object",
        "required": list(questions),
        "properties": {name: question.answer_schema() for name, question in questions.items()},
        "additionalProperties": False,
    }


def parse_answers(questions: Questions, raw: Any) -> dict[str, Answer]:
    """Parse one external answer map, rejecting missing, extra, or out-of-range answers."""

    if not isinstance(raw, Mapping):
        raise ValueError("answers must be an object")
    missing = sorted(set(questions) - set(raw))
    if missing:
        raise ValueError(f"answers missing for {missing}")
    extra = sorted(set(raw) - set(questions))
    if extra:
        raise ValueError(f"answers returned for undeclared questions {extra}")
    return {name: question.parse_answer(raw[name], name) for name, question in questions.items()}


def answers_json(answers: Mapping[str, Answer]) -> dict[str, Any]:
    return {name: answer.as_json() for name, answer in answers.items()}


def questions_json(questions: Questions) -> dict[str, Any]:
    """The request form of ``questions``, as the judgment API expects it."""

    return {
        name: question.model_dump(mode="json", exclude_none=True)
        for name, question in questions.items()
    }


@dataclass(frozen=True, slots=True)
class KeepOperator:
    """One threshold test: which question types it reads and how it decides."""

    question_types: frozenset[str]
    operand: Callable[[Any, Question], Any]
    test: Callable[[Answer, Any], bool]


def _threshold(value: Any, question: Question) -> float:
    del question
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("keep thresholds must be finite numbers")
    return float(value)


def _options(value: Any, question: Question) -> frozenset[str]:
    assert isinstance(question, ChoiceQuestion)
    if not isinstance(value, list | tuple) or not value:
        raise ValueError("choice_in requires a non-empty list of options")
    unknown = sorted(str(option) for option in value if option not in question.criteria)
    if unknown:
        raise ValueError(f"choice_in names undeclared options {unknown}")
    return frozenset(str(option) for option in value)


def _option_thresholds(value: Any, question: Question) -> dict[str, float]:
    assert isinstance(question, ChoiceQuestion)
    if not isinstance(value, Mapping) or not value:
        raise ValueError("prob_gte requires a non-empty map of option to threshold")
    unknown = sorted(str(option) for option in value if option not in question.criteria)
    if unknown:
        raise ValueError(f"prob_gte names undeclared options {unknown}")
    return {str(option): _threshold(item, question) for option, item in value.items()}


def _noul(answer: Answer) -> float:
    assert isinstance(answer, NoulAnswer)
    return answer.noul


def _score(answer: Answer) -> float:
    assert isinstance(answer, ScoreAnswer)
    return answer.score


def _confidence(answer: Answer) -> float:
    assert isinstance(answer, ChoiceAnswer | ScoreAnswer)
    return answer.confidence


def _choice(answer: Answer) -> ChoiceAnswer:
    assert isinstance(answer, ChoiceAnswer)
    return answer


KEEP_OPERATORS: Mapping[str, KeepOperator] = {
    "noul_gte": KeepOperator(frozenset({"noul"}), _threshold, lambda a, x: _noul(a) >= x),
    "noul_lte": KeepOperator(frozenset({"noul"}), _threshold, lambda a, x: _noul(a) <= x),
    "choice_in": KeepOperator(frozenset({"choice"}), _options, lambda a, x: _choice(a).choice in x),
    "score_gte": KeepOperator(frozenset({"score"}), _threshold, lambda a, x: _score(a) >= x),
    "score_lte": KeepOperator(frozenset({"score"}), _threshold, lambda a, x: _score(a) <= x),
    "confidence_gte": KeepOperator(
        frozenset({"choice", "score"}), _threshold, lambda a, x: _confidence(a) >= x
    ),
    "prob_gte": KeepOperator(
        frozenset({"choice"}),
        _option_thresholds,
        lambda a, x: all(_choice(a).probabilities.get(o, 0.0) >= t for o, t in x.items()),
    ),
}


@dataclass(frozen=True, slots=True)
class KeepClause:
    question: str
    operator: str
    operand: Any

    def passes(self, answers: Mapping[str, Answer]) -> bool:
        return KEEP_OPERATORS[self.operator].test(answers[self.question], self.operand)


def compile_keep(
    questions: Questions, keep: Mapping[str, Mapping[str, Any]]
) -> tuple[KeepClause, ...]:
    """Resolve a declared ``keep`` block into clauses that are AND-ed together."""

    clauses: list[KeepClause] = []
    for name, tests in keep.items():
        question = questions.get(name)
        if question is None:
            raise ValueError(f"keep.{name} names an undeclared question")
        if not tests:
            raise ValueError(f"keep.{name} must declare at least one operator")
        for operator_name, raw in tests.items():
            operator = KEEP_OPERATORS.get(operator_name)
            if operator is None:
                raise ValueError(
                    f"keep.{name}.{operator_name} is not one of {sorted(KEEP_OPERATORS)}"
                )
            if question.type not in operator.question_types:
                raise ValueError(
                    f"keep.{name}.{operator_name} does not apply to a {question.type} question"
                )
            try:
                operand = operator.operand(raw, question)
            except ValueError as exc:
                raise ValueError(f"keep.{name}.{operator_name}: {exc}") from exc
            clauses.append(KeepClause(name, operator_name, operand))
    return tuple(clauses)


def kept(clauses: tuple[KeepClause, ...], answers: Mapping[str, Answer]) -> bool:
    return all(clause.passes(answers) for clause in clauses)


__all__ = [
    "KEEP_OPERATORS",
    "Answer",
    "ChoiceAnswer",
    "ChoiceQuestion",
    "KeepClause",
    "KeepSpec",
    "NoulAnswer",
    "NoulQuestion",
    "Question",
    "QuestionMap",
    "Questions",
    "ScoreAnswer",
    "ScoreQuestion",
    "answer_schema",
    "answers_json",
    "compile_keep",
    "kept",
    "parse_answers",
    "questions_json",
]
