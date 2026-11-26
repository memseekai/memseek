"""Read one run's trace from the deployment's trace store, and render it as an episode.

Fetching is the only I/O here. Normalizing the store's response and rendering
the episode are pure, so an episode is a deterministic function of the run
record and the spans the store returned.

One adapter covers stores that serve the Jaeger query API, which includes
Jaeger and VictoriaTraces (under ``/select/jaeger``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from memseek.config import Settings

# GenAI semantic convention names, mirrored from harnesses/pi/otel.mjs.
OPERATION = "gen_ai.operation.name"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
TOOL_NAME = "gen_ai.tool.name"
TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
TOOL_RESULT = "gen_ai.tool.call.result"
ERROR_TYPE = "error.type"
AGENT_RUN_ID = "memseek.agent_run.id"

_SKILL_PATH = re.compile(r"\.agents/skills/([a-z0-9][a-z0-9._-]{0,63})/")
_STEP_TEXT_CHARS = 600
_TOOL_TEXT_CHARS = 400
_FINAL_TEXT_CHARS = 2_000


class TraceStoreError(Exception):
    """The store could not answer; ``kind`` is the derivation error kind to raise."""

    def __init__(self, kind: Literal["transport", "config"], detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class Span:
    span_id: str
    parent_id: str | None
    name: str
    start_us: int
    duration_us: int
    error: str | None
    attributes: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Trace:
    spans: tuple[Span, ...]
    bytes: int
    sha256: str

    def root(self, agent_run_id: str) -> Span | None:
        """The run's root span, the one the worker exported for this run record."""

        span_ids = {span.span_id for span in self.spans}
        for span in self.spans:
            if span.attributes.get(AGENT_RUN_ID) == agent_run_id and (
                span.parent_id is None or span.parent_id not in span_ids
            ):
                return span
        return None


@dataclass(frozen=True, slots=True)
class Fetched:
    """A trace, or why there is none: ``not_found`` or ``too_large``."""

    trace: Trace | None
    missing: Literal["not_found", "too_large"] | None = None


async def fetch_trace(settings: Settings, trace_id: str) -> Fetched:
    if not settings.trace_store_url:
        raise TraceStoreError("config", "TRACE_STORE_URL is not configured")
    headers = {"Accept": "application/json"}
    if settings.trace_store_token:
        headers["Authorization"] = f"Bearer {settings.trace_store_token}"
    url = f"{settings.trace_store_url.rstrip('/')}/api/traces/{trace_id}"
    try:
        async with (
            httpx.AsyncClient(timeout=settings.trace_store_timeout_s) as client,
            client.stream("GET", url, headers=headers) as response,
        ):
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > settings.trace_store_max_bytes:
                    return Fetched(None, "too_large")
            status = response.status_code
    except httpx.HTTPError as exc:
        raise TraceStoreError(
            "transport", f"trace store unreachable: {type(exc).__name__}"
        ) from exc
    if status >= 500:
        raise TraceStoreError("transport", f"trace store answered {status}")
    payload = _json(bytes(body))
    if not isinstance(payload, Mapping) or ("data" not in payload and "errors" not in payload):
        # A route that is not a Jaeger query API, such as a wrong base URL,
        # must stop the run rather than read every trace as missing.
        raise TraceStoreError("config", f"TRACE_STORE_URL is not a Jaeger query API ({status})")
    data = payload.get("data")
    if status == 404 or not data:
        return Fetched(None, "not_found")
    if status != 200 or not isinstance(data, list):
        raise TraceStoreError("config", f"trace store answered {status}")
    return Fetched(normalize(data, bytes(body)))


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def _tags(tags: Any) -> dict[str, Any]:
    if not isinstance(tags, list):
        return {}
    return {
        str(tag["key"]): tag.get("value")
        for tag in tags
        if isinstance(tag, Mapping) and "key" in tag
    }


def normalize(data: Sequence[Any], body: bytes) -> Trace:
    """The store's Jaeger-shaped traces as one flat, start-ordered span list."""

    spans: list[Span] = []
    for trace in data:
        for raw in trace.get("spans", ()) if isinstance(trace, Mapping) else ():
            if not isinstance(raw, Mapping):
                continue
            attributes = _tags(raw.get("tags"))
            parent = next(
                (
                    str(ref.get("spanID"))
                    for ref in raw.get("references") or ()
                    if isinstance(ref, Mapping) and ref.get("refType") == "CHILD_OF"
                ),
                None,
            )
            status = attributes.get("otel.status_code")
            error = None
            if status == "ERROR" or attributes.get("error") in (True, "true"):
                error = str(
                    attributes.get(ERROR_TYPE)
                    or attributes.get("otel.status_description")
                    or "error"
                )
            spans.append(
                Span(
                    span_id=str(raw.get("spanID", "")),
                    parent_id=parent,
                    name=str(raw.get("operationName", "")),
                    start_us=int(raw.get("startTime", 0) or 0),
                    duration_us=int(raw.get("duration", 0) or 0),
                    error=error,
                    attributes=attributes,
                )
            )
    spans.sort(key=lambda span: (span.start_us, span.span_id))
    return Trace(tuple(spans), len(body), hashlib.sha256(body).hexdigest())


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _message_text(value: Any) -> str:
    """The text parts of a GenAI messages attribute, which arrives as a JSON string."""

    messages = _json(value.encode()) if isinstance(value, str) else value
    if not isinstance(messages, list):
        return ""
    return " ".join(
        str(part.get("content", ""))
        for message in messages
        if isinstance(message, Mapping)
        for part in message.get("parts", ())
        if isinstance(part, Mapping) and part.get("type") == "text"
    ).strip()


def _tool_calls(value: Any) -> list[str]:
    messages = _json(value.encode()) if isinstance(value, str) else value
    if not isinstance(messages, list):
        return []
    return [
        str(part.get("name"))
        for message in messages
        if isinstance(message, Mapping)
        for part in message.get("parts", ())
        if isinstance(part, Mapping) and part.get("type") == "tool_call"
    ]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


def render_episode(record: Mapping[str, Any], trace: Trace, max_chars: int) -> str:
    """One run as numbered steps, bounded to ``max_chars`` by keeping head and tail."""

    content = record.get("content", {})
    agent = content.get("agent", {})
    origin = content.get("origin", {})
    outcome = content.get("status", "unknown")
    if content.get("error_kind"):
        outcome = f"{outcome} ({content['error_kind']})"
    where = origin.get("derivation") or origin.get("invocation_id") or origin.get("kind", "")
    metrics = content.get("metrics") or {}
    header = [
        f"Agent {agent.get('ref')} (harness {agent.get('harness') or 'built-in'}, "
        f"model {agent.get('model')}) ended {outcome} in {origin.get('kind', 'run')} {where}.",
    ]
    if metrics:
        header.append(
            "Metrics: " + ", ".join(f"{key} {value}" for key, value in sorted(metrics.items()))
        )
    if content.get("error_kind") in {"timeout", "cancelled"}:
        header.append("The run was cut off, so its last steps may be missing.")

    steps: list[str] = []
    task = ""
    final = ""
    skills: set[str] = set()
    for span in trace.spans:
        operation = span.attributes.get(OPERATION)
        if operation == "chat":
            asked = _message_text(span.attributes.get(INPUT_MESSAGES))
            if asked and not task:
                task = asked
            said = _message_text(span.attributes.get(OUTPUT_MESSAGES))
            if said:
                final = said
            calls = _tool_calls(span.attributes.get(OUTPUT_MESSAGES))
            tokens = (
                f"in {span.attributes.get(INPUT_TOKENS, '?')}, "
                f"out {span.attributes.get(OUTPUT_TOKENS, '?')} tokens"
            )
            line = f"model ({tokens})"
            if said:
                line += f": {_clip(said, _STEP_TEXT_CHARS)}"
            if calls:
                line += f" -> calls {', '.join(calls)}"
            if span.error:
                line += f" [error: {span.error}]"
            steps.append(line)
        elif operation == "execute_tool":
            arguments = span.attributes.get(TOOL_ARGUMENTS)
            result = span.attributes.get(TOOL_RESULT)
            if arguments is not None:
                skills.update(_SKILL_PATH.findall(_text(arguments)))
            outcome_text = "failed" if span.error else "ok"
            line = f"tool {span.attributes.get(TOOL_NAME, span.name)} {outcome_text}"
            if arguments is not None:
                line += f": {_clip(_text(arguments), _TOOL_TEXT_CHARS)}"
            if result is not None:
                line += f" => {_clip(_text(result), _TOOL_TEXT_CHARS)}"
            steps.append(line)

    head = [*header, f"Task: {_clip(task, _FINAL_TEXT_CHARS) if task else 'not recorded'}"]
    tail = [f"Final: {_clip(final, _FINAL_TEXT_CHARS) if final else 'no final message'}"]
    if skills:
        tail.append(f"Skills read: {', '.join(sorted(skills))}")
    numbered = [f"{index}. {line}" for index, line in enumerate(steps, start=1)]
    return _fit(head, numbered, tail, max_chars)


def _fit(head: list[str], steps: list[str], tail: list[str], max_chars: int) -> str:
    def joined(kept: list[str]) -> str:
        return "\n".join([*head, "Steps:", *kept, *tail])

    text = joined(steps)
    if len(text) <= max_chars:
        return text
    # Keep the opening and the end of the run, which say what was asked and
    # how it went wrong; drop steps from the middle.
    first, last = 0, len(steps)
    kept_head: list[str] = []
    kept_tail: list[str] = []
    while first < last:
        candidate_head = [*kept_head, steps[first]]
        marker = [f"... {last - first - 1} steps omitted ..."] if last - first > 1 else []
        if len(joined([*candidate_head, *marker, *kept_tail])) > max_chars:
            break
        kept_head = candidate_head
        first += 1
        if first >= last:
            break
        candidate_tail = [steps[last - 1], *kept_tail]
        marker = [f"... {last - first - 1} steps omitted ..."] if last - first > 1 else []
        if len(joined([*kept_head, *marker, *candidate_tail])) > max_chars:
            break
        kept_tail = candidate_tail
        last -= 1
    omitted = [f"... {last - first} steps omitted ..."] if last > first else []
    text = joined([*kept_head, *omitted, *kept_tail])
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


__all__ = [
    "Fetched",
    "Span",
    "Trace",
    "TraceStoreError",
    "fetch_trace",
    "normalize",
    "render_episode",
]
