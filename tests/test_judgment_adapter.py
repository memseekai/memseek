from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter

from memseek.config import Settings
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.definitions.judgment import QuestionMap, Questions, answers_json
from memseek.definitions.models import ModelAlias, ProviderConnection
from memseek.llm import registry
from memseek.llm.fake import fake
from memseek.llm.registry import (
    JudgmentRequest,
    LLMTransportError,
    ProviderEndpoint,
    RateLimited,
)
from memseek.llm.runtime import ModelAttemptsExhausted, judge
from memseek.llm.typesafe import TypeSafeProvider

QUESTIONS: Questions = TypeAdapter(QuestionMap).validate_python(
    {
        "is_spam": {
            "type": "noul",
            "instructions": "Is `email` spam?",
            "criteria": {"true": "Unsolicited bulk mail", "false": "Legitimate mail"},
        },
        "department": {
            "type": "choice",
            "instructions": "Which department should handle `email`?",
            "criteria": {"billing": "Payments", "support": None, "sales": None},
        },
        "urgency": {
            "type": "score",
            "instructions": "How urgent is `email`?",
            "criteria": ["Low", "Medium", "High"],
        },
    }
)

# The response shape documented at docs.typesafe.ai, including a score whose
# probabilities arrive keyed by level index.
DOCUMENTED_RESPONSE: dict[str, Any] = {
    "model": "jev-1.13.0",
    "answers": {
        "is_spam": {"type": "noul", "noul": 0.95},
        "department": {
            "type": "choice",
            "choice": "billing",
            "probabilities": {"billing": 0.82, "support": 0.12, "sales": 0.06},
            "confidence": 0.71,
        },
        "urgency": {
            "type": "score",
            "score": 1.4,
            "legend": "Medium",
            "probabilities": {"0": 0.2, "1": 0.2, "2": 0.6},
            "confidence": 0.4,
        },
    },
    "usage": {"input_tokens": 296, "output_tokens": 20},
}


def _endpoint(adapter: str = "typesafe") -> ProviderEndpoint:
    return ProviderEndpoint(
        provider="typesafe",
        adapter=adapter,
        base_url="https://api.typesafe.ai",
        api_key="ts-key",
        json_capability="none",
        json_schema_strict=False,
        token_limit_field="max_completion_tokens",
        max_concurrency=4,
    )


async def test_adapter_posts_the_documented_request_and_parses_the_answers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DOCUMENTED_RESPONSE)

    provider = TypeSafeProvider(transport=httpx.MockTransport(handler))
    judgment = await provider.judge(
        _endpoint(), "jev-1.13.0", JudgmentRequest({"email": "Refund please"}, QUESTIONS)
    )
    await provider.aclose()

    assert str(seen[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert seen[0].headers["authorization"] == "Bearer ts-key"
    assert json.loads(seen[0].content) == {
        "state": {"email": "Refund please"},
        "model": "jev-1.13.0",
        "questions": {
            "is_spam": {
                "type": "noul",
                "instructions": "Is `email` spam?",
                "criteria": {"true": "Unsolicited bulk mail", "false": "Legitimate mail"},
            },
            "department": {
                "type": "choice",
                "instructions": "Which department should handle `email`?",
                "criteria": {"billing": "Payments", "support": None, "sales": None},
            },
            "urgency": {
                "type": "score",
                "instructions": "How urgent is `email`?",
                "criteria": ["Low", "Medium", "High"],
            },
        },
    }
    assert judgment.model == "jev-1.13.0"
    assert (judgment.input_tokens, judgment.output_tokens) == (296, 20)
    assert answers_json(judgment.answers) == {
        "is_spam": {"noul": 0.95},
        "department": {
            "choice": "billing",
            "probabilities": {"billing": 0.82, "support": 0.12, "sales": 0.06},
            "confidence": 0.71,
        },
        "urgency": {
            "score": 1.4,
            "probabilities": [0.2, 0.2, 0.6],
            "confidence": 0.4,
            "legend": "Medium",
        },
    }


@pytest.mark.parametrize(
    ("response", "error", "kind"),
    [
        (httpx.Response(429, headers={"retry-after": "3"}), RateLimited, "rate_limited"),
        (httpx.Response(503), LLMTransportError, "transport"),
        (
            httpx.Response(
                200,
                json={**DOCUMENTED_RESPONSE, "answers": {"is_spam": {"noul": 0.5}}},
            ),
            LLMTransportError,
            "response",
        ),
    ],
)
async def test_adapter_maps_retryable_failures(
    response: httpx.Response, error: type[LLMTransportError], kind: str
) -> None:
    provider = TypeSafeProvider(transport=httpx.MockTransport(lambda _request: response))

    with pytest.raises(error) as caught:
        await provider.judge(_endpoint(), "jev-1.13.0", JudgmentRequest("state", QUESTIONS))

    assert getattr(caught.value, "kind", "transport") == kind
    if isinstance(caught.value, RateLimited):
        assert caught.value.retry_after_s == 3.0


async def test_adapter_rejects_a_bad_request_without_retrying() -> None:
    provider = TypeSafeProvider(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(400, json={"error": {"message": "bad criteria"}})
        )
    )

    with pytest.raises(ValueError, match=r"HTTP 400: \{'message': 'bad criteria'\}"):
        await provider.judge(_endpoint(), "jev-1.13.0", JudgmentRequest("state", QUESTIONS))


async def test_fake_answers_deterministically_and_records_calls() -> None:
    fake.reset()

    judgment = await fake.judge(_endpoint("fake"), "jev-1.13.0", JudgmentRequest("s", QUESTIONS))

    assert answers_json(judgment.answers) == {
        "is_spam": {"noul": 0.5},
        "department": {
            "choice": "billing",
            "probabilities": {"billing": 1 / 3, "support": 1 / 3, "sales": 1 / 3},
            "confidence": 0.5,
        },
        "urgency": {"score": 1.0, "probabilities": [1 / 3, 1 / 3, 1 / 3], "confidence": 0.5},
    }
    assert [(call.model, call.state) for call in fake.judgment_calls] == [("jev-1.13.0", "s")]


def _judgment_catalog(settings: Settings) -> DefinitionCatalog:
    catalog = load_definition_catalog(settings)
    models = catalog.models.model_copy(
        update={
            "providers": {
                **catalog.models.providers,
                "typesafe": ProviderConnection(
                    adapter="typesafe",
                    base_url="https://api.typesafe.ai",
                    api_key_env="TYPESAFE_API_KEY",
                    json_capability="none",
                ),
            },
            "aliases": {
                **catalog.models.aliases,
                "judge_test": ModelAlias(targets=("typesafe:jev-latest",)),
            },
        }
    )
    return replace(catalog, models=models)


async def test_runtime_honours_retry_after_and_resolves_the_reported_version(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = [
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(200, json=DOCUMENTED_RESPONSE),
    ]
    provider = TypeSafeProvider(transport=httpx.MockTransport(lambda _request: responses.pop(0)))
    monkeypatch.setitem(registry.JUDGMENT_PROVIDERS, "typesafe", provider)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    resolved = await judge(
        settings.model_copy(update={"llm_fake": False}),
        _judgment_catalog(settings),
        "judge_test",
        {"email": "hi"},
        QUESTIONS,
        _sleep=sleep,
    )

    assert slept == [7.0]
    assert resolved.resolved == "typesafe:jev-1.13.0"
    assert [
        (attempt.outcome, attempt.resolved, attempt.error_kind) for attempt in resolved.attempts
    ] == [
        ("error", "typesafe:jev-latest", "rate_limited"),
        ("ok", "typesafe:jev-1.13.0", None),
    ]
    usage = resolved.attempts[-1].usage
    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.estimated) == (296, 20, False)


async def test_runtime_gives_up_after_two_attempts_per_target(settings: Settings) -> None:
    fake.reset()
    fake.enqueue_judgment({"is_spam": {"noul": 2.0}}, {"is_spam": {"noul": 2.0}})

    async def sleep(_seconds: float) -> None:
        return None

    with pytest.raises(ModelAttemptsExhausted) as caught:
        await judge(
            settings, _judgment_catalog(settings), "judge_test", "s", QUESTIONS, _sleep=sleep
        )

    assert [(a.outcome, a.error_kind) for a in caught.value.attempts] == [
        ("error", "response"),
        ("error", "response"),
    ]
