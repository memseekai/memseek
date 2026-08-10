"""Deterministic, fully asynchronous LLM provider used by offline tests."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .registry import (
    Completion,
    CompletionOutput,
    EmbeddingResult,
    LLMTransportError,
    ProviderEndpoint,
    materialize_json_schema,
)

_MARKER_RE = re.compile(r"\[([a-z][a-z0-9_]*)=(-?(?:\d+(?:\.\d*)?|\.\d+))\]")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


class FakeProviderFailure(LLMTransportError):
    """A deterministic injected fake-provider failure."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"fake provider failure: {kind}")


@dataclass(frozen=True, slots=True)
class FakeCompletionCall:
    model: str
    system: str
    prompt: str
    params: dict[str, object]
    output_mode: str
    output_schema_name: str | None
    output_schema: dict[str, object] | None
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class FakeEmbeddingCall:
    model: str
    texts: tuple[str, ...]


def estimate_tokens(text: str) -> int:
    """Return the deterministic usage estimate shared by fake calls and audit code."""

    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


class FakeLLMProvider:
    """Controllable fake with exact FIFO completions and deterministic fallbacks."""

    NAME = "fake"

    def __init__(self) -> None:
        self._completions: deque[Completion] = deque()
        self._failures: deque[str] = deque()
        self.completion_calls: list[FakeCompletionCall] = []
        self.embedding_calls: list[FakeEmbeddingCall] = []

    def reset(self) -> None:
        """Clear queued behavior and captured calls."""

        self._completions.clear()
        self._failures.clear()
        self.completion_calls.clear()
        self.embedding_calls.clear()

    def enqueue(self, *completions: Completion | str) -> None:
        """Append exact completion objects to the provider's FIFO queue."""

        for completion in completions:
            self._completions.append(
                completion if isinstance(completion, Completion) else Completion(completion)
            )

    queue_completion = enqueue

    def fail_next(self, count: int, *, kind: str = "transport") -> None:
        """Make the next ``count`` provider operations fail deterministically."""

        if count < 0:
            raise ValueError("failure count must be non-negative")
        self._failures.extend(kind for _ in range(count))

    def _maybe_fail(self) -> None:
        if self._failures:
            raise FakeProviderFailure(self._failures.popleft())

    async def complete(
        self,
        endpoint: ProviderEndpoint,
        model: str,
        system: str,
        prompt: str,
        *,
        params: dict[str, object],
        output: CompletionOutput,
        max_output_tokens: int,
    ) -> Completion:
        del endpoint
        self.completion_calls.append(
            FakeCompletionCall(
                model=model,
                system=system,
                prompt=prompt,
                params=dict(params),
                output_mode=output.mode,
                output_schema_name=output.name,
                output_schema=(
                    materialize_json_schema(output.schema) if output.schema is not None else None
                ),
                max_output_tokens=max_output_tokens,
            )
        )
        self._maybe_fail()
        if self._completions:
            completion = self._completions.popleft()
            return Completion(
                completion.text,
                completion.prompt_tokens,
                completion.completion_tokens,
                output.mode,
            )
        text = self._default_completion(prompt)
        return Completion(
            text=text,
            prompt_tokens=estimate_tokens(system) + estimate_tokens(prompt),
            completion_tokens=estimate_tokens(text),
            output_mode=output.mode,
        )

    async def embed(
        self,
        endpoint: ProviderEndpoint,
        model: str,
        texts: list[str],
        *,
        dimensions: int,
        params: Mapping[str, Any],
    ) -> EmbeddingResult:
        del endpoint, params
        self.embedding_calls.append(FakeEmbeddingCall(model=model, texts=tuple(texts)))
        self._maybe_fail()
        vectors = tuple(_normalized_embedding(text, dimensions) for text in texts)
        return EmbeddingResult(
            vectors=vectors,
            prompt_tokens=sum(estimate_tokens(text) for text in texts),
        )

    @staticmethod
    def _default_completion(prompt: str) -> str:
        scorer_match = re.search(r"SCORER:\s*([a-z][a-z0-9_]*)", prompt, re.IGNORECASE)
        if scorer_match:
            scorer = scorer_match.group(1).lower()
            default_match = re.search(
                rf"DEFAULT\s+{re.escape(scorer)}\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))",
                prompt,
                re.IGNORECASE,
            )
            default = float(default_match.group(1)) if default_match else 0.0
            rows = re.findall(r"<record\s[^>]*>(.*?)</record>", prompt, re.DOTALL)
            values: list[float] = []
            for row in rows:
                marker = next(
                    (float(value) for name, value in _MARKER_RE.findall(row) if name == scorer),
                    default,
                )
                values.append(marker)
            return json.dumps(values, separators=(",", ":"))

        if "detect direct contradictions" in prompt.casefold():
            if "[conflict]" not in prompt:
                return '{"records":[]}'
            changed, _, current = prompt.partition("CURRENT KEYS:")
            changed_ids = _UUID_RE.findall(changed)
            current_ids = _UUID_RE.findall(current)
            subject_id = changed_ids[0] if changed_ids else None
            object_id = next(
                (identifier for identifier in current_ids if identifier != subject_id),
                None,
            )
            if subject_id is None or object_id is None:
                return '{"records":[]}'
            return json.dumps(
                {
                    "records": [
                        {
                            "text": "Changed key conflicts with a current key",
                            "citations": [subject_id, object_id],
                            "content": {
                                "subject_id": subject_id,
                                "object_id": object_id,
                                "explanation": "deterministic conflict",
                                "confidence": 1.0,
                            },
                        }
                    ]
                },
                separators=(",", ":"),
            )

        if "sentiment" in prompt.casefold():
            rows = re.findall(r"<record\s[^>]*>(.*?)</record>", prompt, re.DOTALL)
            values = []
            for row in rows:
                sentiment = re.search(r"\[sentiment=(negative|neutral|positive)\]", row)
                values.append(
                    {
                        "label": sentiment.group(1) if sentiment else "neutral",
                        "confidence": 1.0 if sentiment else 0.0,
                    }
                )
            if rows:
                return json.dumps(values, separators=(",", ":"))
            return '{"label":"neutral","confidence":0.0}'

        if "questions" in prompt.casefold():
            return '{"questions":["What should be remembered?"]}'
        if "keyed" in prompt.casefold() or '"key"' in prompt:
            return '{"records":[]}'
        if "event" in prompt.casefold() or '"records"' in prompt:
            return '{"records":[]}'
        return "{}"


def _normalized_embedding(text: str, dimension: int) -> tuple[float, ...]:
    seed = text.encode("utf-8")
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        digest = hashlib.sha256(
            b"memseek.fake-embedding.v1\x00" + seed + counter.to_bytes(4, "big")
        ).digest()
        values.extend((byte - 127.5) / 127.5 for byte in digest)
        counter += 1
    values = values[:dimension]
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


fake = FakeLLMProvider()
