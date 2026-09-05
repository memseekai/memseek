"""Provider-neutral Computer execution boundary.

The module deliberately owns no canonical writer. It resolves exact catalog
definitions, validates capabilities and schemas, delegates to one installed
provider, and returns a bounded value plus an auditable receipt. Derivations
remain responsible for deciding whether that value is emitted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx
from jsonschema import Draft202012Validator, FormatChecker

from memseek.config import Settings
from memseek.definitions import DefinitionCatalog
from memseek.definitions.models import (
    AgentDefinition,
    ComputerDefinition,
    ContextPolicyDefinition,
    ProgramDefinition,
)
from memseek.evidence_spine import ContextPressure


class ComputerExecutionError(RuntimeError):
    """A Computer request is invalid, unavailable, or returned invalid data."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ComputerRequest:
    mode: Literal["derivation", "invocation"]
    workspace: str
    entity: str
    session_key: str
    task_id: str
    computer_ref: str
    computer: ComputerDefinition
    input: Any
    source_ids: frozenset[UUID]
    citation_ids: frozenset[UUID]
    output_path: str
    parent_session_key: str | None = None
    program_ref: str | None = None
    program: ProgramDefinition | None = None
    agent_ref: str | None = None
    agent: AgentDefinition | None = None
    context_policy_ref: str | None = None
    context_policy: ContextPolicyDefinition | None = None
    context_files: Mapping[str, str] | None = None
    model: Mapping[str, Any] | None = None
    # The shape `value` must satisfy. It is validated on the way back either
    # way; sending it means the executor is told the target rather than left to
    # infer it, which a model cannot do.
    output_schema: Mapping[str, Any] | None = None

    def as_json(self) -> dict[str, Any]:
        executor: dict[str, Any]
        if self.program is not None:
            executor = {
                "kind": "program",
                "ref": self.program_ref,
                "definition": self.program.model_dump(mode="json", exclude_none=True),
            }
        else:
            assert self.agent is not None
            assert self.context_policy is not None
            executor = {
                "kind": "agent",
                "ref": self.agent_ref,
                "definition": self.agent.model_dump(mode="json", exclude_none=True),
                "context_policy_ref": self.context_policy_ref,
                "context_policy": self.context_policy.model_dump(mode="json", exclude_none=True),
                "model": dict(self.model or {}),
            }
        payload = {
            "mode": self.mode,
            "workspace": self.workspace,
            "entity": self.entity,
            "session_key": self.session_key,
            "task_id": self.task_id,
            "computer_ref": self.computer_ref,
            "computer": self.computer.model_dump(mode="json", exclude_none=True),
            "executor": executor,
            "input": self.input,
            "source_ids": [str(value) for value in sorted(self.source_ids, key=str)],
            "citation_ids": [str(value) for value in sorted(self.citation_ids, key=str)],
            "output_path": self.output_path,
            "context_files": dict(self.context_files or {}),
        }
        if self.output_schema is not None:
            payload["output_schema"] = dict(self.output_schema)
        if self.parent_session_key is not None:
            payload["parent_session_key"] = self.parent_session_key
        return payload


@dataclass(frozen=True, slots=True)
class ComputerResult:
    value: Any
    citation_ids: frozenset[UUID]
    receipt: dict[str, Any]
    steps: int = 1
    awaiting_input: bool = False


class ComputerProvider(Protocol):
    async def execute(self, request: ComputerRequest) -> ComputerResult: ...


_PROVIDERS: dict[str, ComputerProvider] = {}


def register_computer_provider(
    name: str, provider: ComputerProvider, *, replace: bool = False
) -> None:
    if not name or not name.replace("_", "a").isalnum():
        raise ValueError("computer provider name must be lower-case alphanumeric/underscore")
    if name in _PROVIDERS and not replace:
        raise ValueError(f"computer provider already registered: {name}")
    _PROVIDERS[name] = provider


def computer_provider(name: str) -> ComputerProvider:
    try:
        return _PROVIDERS[name]
    except KeyError as exc:
        raise ComputerExecutionError("provider", f"unknown Computer provider {name!r}") from exc


FakeHandler = Callable[[ComputerRequest], Awaitable[ComputerResult | Any]]


class FakeComputerProvider:
    """Deterministic provider used by contract tests and local examples."""

    def __init__(self) -> None:
        self._programs: dict[str, FakeHandler] = {}
        self._agents: dict[str, FakeHandler] = {}
        self.requests: list[ComputerRequest] = []

    def register_program(self, reference: str, handler: FakeHandler) -> None:
        self._programs[reference] = handler

    def register_agent(self, reference: str, handler: FakeHandler) -> None:
        self._agents[reference] = handler

    def clear(self) -> None:
        self._programs.clear()
        self._agents.clear()
        self.requests.clear()

    async def execute(self, request: ComputerRequest) -> ComputerResult:
        self.requests.append(request)
        reference = request.program_ref or request.agent_ref
        handlers = self._programs if request.program_ref is not None else self._agents
        handler = handlers.get(str(reference))
        if handler is None:
            raise ComputerExecutionError("provider", f"no fake executor registered for {reference}")
        result = await handler(request)
        if isinstance(result, ComputerResult):
            return result
        encoded = _canonical_bytes(result)
        return ComputerResult(
            value=result,
            citation_ids=request.citation_ids,
            receipt={
                "provider": "fake",
                "backend": request.computer.runtime.default,
                "session_key": request.session_key,
                "output_path": request.output_path,
                "output_sha256": hashlib.sha256(encoded).hexdigest(),
                "bytes": len(encoded),
            },
        )


class RemoteComputerProvider:
    """HTTP adapter for the Cloudflare runtime service."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.computer_runtime_url.rstrip("/")
        self._token = settings.computer_runtime_token
        self._timeout = settings.computer_request_timeout_s
        self._max_response_bytes = settings.computer_response_max_bytes

    async def execute(self, request: ComputerRequest) -> ComputerResult:
        if not self._url or not self._token:
            raise ComputerExecutionError(
                "provider", "Cloudflare Computer runtime URL/token is not configured"
            )
        try:
            body = _canonical_bytes(request.as_json())
            timestamp = str(int(time.time()))
            signature = hmac.new(
                self._token.encode(), timestamp.encode() + b"." + body, hashlib.sha256
            ).hexdigest()
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._url}/v1/execute",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Memseek-Timestamp": timestamp,
                        "X-Memseek-Signature": signature,
                    },
                )
        except httpx.HTTPError as exc:
            raise ComputerExecutionError("transport", type(exc).__name__) from exc
        if response.status_code != 200:
            raise ComputerExecutionError(
                "transport",
                f"Computer runtime returned HTTP {response.status_code}"
                f"{_runtime_error_detail(response)}",
            )
        if len(response.content) > self._max_response_bytes:
            raise ComputerExecutionError("budget", "Computer runtime response exceeds byte limit")
        try:
            payload = response.json()
            value = payload["value"]
            citations = frozenset(UUID(str(item)) for item in payload.get("citation_ids", ()))
            receipt = dict(payload["receipt"])
            steps = int(payload.get("steps", 1))
            awaiting_input = bool(payload.get("awaiting_input", False))
        except (KeyError, TypeError, ValueError) as exc:
            raise ComputerExecutionError("validation", "invalid Computer runtime response") from exc
        return ComputerResult(
            value=value,
            citation_ids=citations,
            receipt=receipt,
            steps=steps,
            awaiting_input=awaiting_input,
        )


FAKE_COMPUTER_PROVIDER = FakeComputerProvider()
register_computer_provider("fake", FAKE_COMPUTER_PROVIDER)


def configure_remote_computer_provider(settings: Settings) -> None:
    """Install or refresh the Cloudflare adapter for one application instance."""

    register_computer_provider("cloudflare", RemoteComputerProvider(settings), replace=True)


def _runtime_error_detail(response: httpx.Response) -> str:
    """Render the runtime's own `error` field so a failure names itself.

    The runtime answers every rejection with `{"error": "<reason>"}`. Without
    this the caller sees only a status code and has to reach for wrangler tail.
    """

    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    error = payload.get("error")
    if not isinstance(error, str) or not error:
        return ""
    detail = " ".join(error.split())
    if len(detail) > 200:
        detail = f"{detail[:199]}\u2026"
    return f": {detail}"


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ComputerExecutionError("validation", "Computer output is not finite JSON") from exc


def _validate_json(schema: Mapping[str, Any], value: Any, label: str) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path)
        suffix = f" at {path}" if path else ""
        raise ComputerExecutionError("validation", f"{label}{suffix}: {error.message}")


def _session_key(workspace: str, run_key: str, computer_ref: str) -> str:
    return hashlib.sha256(f"{workspace}\0{run_key}\0{computer_ref}".encode()).hexdigest()


def _validate_computer_program(computer: ComputerDefinition, program: ProgramDefinition) -> None:
    allowed_runtimes = {computer.runtime.default, computer.runtime.fallback}
    if program.runtime not in allowed_runtimes:
        raise ComputerExecutionError(
            "capability", f"Computer does not allow Program runtime {program.runtime!r}"
        )
    missing = set(program.capabilities) - set(computer.capabilities.enabled)
    if missing:
        raise ComputerExecutionError(
            "capability", f"Computer lacks Program capabilities: {sorted(missing)}"
        )


async def execute_program(
    *,
    settings: Settings,
    catalog: DefinitionCatalog,
    workspace: str,
    entity: str,
    run_key: str,
    task_id: str,
    computer_ref: str,
    program_ref: str,
    input_value: Any,
    source_ids: frozenset[UUID],
    citation_ids: frozenset[UUID],
    output_path: str,
    max_output_bytes: int,
    mode: Literal["derivation", "invocation"] = "derivation",
    parent_run_key: str | None = None,
) -> ComputerResult:
    try:
        computer = catalog.resolve_computer(computer_ref)
        program = catalog.resolve_program(program_ref)
    except (KeyError, ValueError) as exc:
        raise ComputerExecutionError("reference", str(exc)) from exc
    _validate_computer_program(computer, program)
    _validate_json(program.input_schema, input_value, "Program input")
    request = ComputerRequest(
        mode=mode,
        workspace=workspace,
        entity=entity,
        session_key=_session_key(workspace, run_key, computer_ref),
        task_id=task_id,
        computer_ref=computer_ref,
        computer=computer,
        program_ref=program_ref,
        program=program,
        input=input_value,
        source_ids=source_ids,
        citation_ids=citation_ids,
        output_path=output_path,
        output_schema=program.output_schema,
        parent_session_key=(
            _session_key(workspace, parent_run_key, computer_ref)
            if parent_run_key is not None
            else None
        ),
    )
    configure_remote_computer_provider(settings)
    result = await computer_provider(computer.provider).execute(request)
    if result.awaiting_input:
        raise ComputerExecutionError("validation", "Programs cannot await user input")
    _validate_json(program.output_schema, result.value, "Program output")
    encoded = _canonical_bytes(result.value)
    if len(encoded) > max_output_bytes:
        raise ComputerExecutionError("budget", "Computer output exceeds declared byte limit")
    if not result.citation_ids <= citation_ids:
        raise ComputerExecutionError("provenance", "Computer widened citation authority")
    return ComputerResult(
        value=result.value,
        citation_ids=result.citation_ids,
        receipt={
            **result.receipt,
            "definition_refs": {
                "computer": {"ref": computer_ref, "hash": computer.definition_hash},
                "program": {"ref": program_ref, "hash": program.definition_hash},
            },
        },
        steps=result.steps,
        awaiting_input=result.awaiting_input,
    )


async def execute_agent(
    *,
    settings: Settings,
    catalog: DefinitionCatalog,
    workspace: str,
    entity: str,
    run_key: str,
    task_id: str,
    computer_ref: str,
    agent_ref: str,
    context_policy_ref: str,
    input_value: Any,
    source_ids: frozenset[UUID],
    citation_ids: frozenset[UUID],
    output_path: str,
    output_schema: Mapping[str, Any],
    context_files: Mapping[str, str],
    max_output_bytes: int,
    max_steps: int,
    mode: Literal["derivation", "invocation"] = "derivation",
    parent_run_key: str | None = None,
) -> ComputerResult:
    try:
        computer = catalog.resolve_computer(computer_ref)
        agent = catalog.resolve_agent(agent_ref)
        policy = catalog.resolve_context_policy(context_policy_ref)
    except (KeyError, ValueError) as exc:
        raise ComputerExecutionError("reference", str(exc)) from exc
    if computer_ref not in agent.computers:
        raise ComputerExecutionError("capability", "Agent is not allowed to use this Computer")
    model_alias = catalog.models.aliases[agent.model]
    context_bytes = sum(len(value.encode("utf-8")) for value in context_files.values())
    input_bytes = len(_canonical_bytes(input_value))
    used_tokens = (context_bytes + input_bytes + 3) // 4
    pressure = ContextPressure(
        used_tokens=used_tokens,
        max_input_tokens=policy.max_input_tokens,
        reserve_output_tokens=policy.reserve_output_tokens,
        pointerize=policy.thresholds.pointerize,
        compact=policy.thresholds.compact,
        pause=policy.thresholds.pause,
    )
    if pressure.action == "pause":
        raise ComputerExecutionError(
            "context_exhausted",
            "protected Agent context cannot fit the declared context policy",
        )
    request = ComputerRequest(
        mode=mode,
        workspace=workspace,
        entity=entity,
        session_key=_session_key(workspace, run_key, computer_ref),
        task_id=task_id,
        computer_ref=computer_ref,
        computer=computer,
        agent_ref=agent_ref,
        agent=agent,
        context_policy_ref=context_policy_ref,
        context_policy=policy,
        input=input_value,
        source_ids=source_ids,
        citation_ids=citation_ids,
        output_path=output_path,
        output_schema=output_schema,
        parent_session_key=(
            _session_key(workspace, parent_run_key, computer_ref)
            if parent_run_key is not None
            else None
        ),
        context_files=context_files,
        model={
            "alias": agent.model,
            "targets": list(model_alias.targets),
            "params": model_alias.params,
            "context_pressure": {
                "used_tokens": used_tokens,
                "ratio": pressure.ratio,
                "action": pressure.action,
            },
        },
    )
    configure_remote_computer_provider(settings)
    result = await computer_provider(computer.provider).execute(request)
    _validate_json(output_schema, result.value, "Agent output")
    encoded = _canonical_bytes(result.value)
    if len(encoded) > max_output_bytes:
        raise ComputerExecutionError("budget", "Agent output exceeds declared byte limit")
    if result.steps > max_steps:
        raise ComputerExecutionError("budget", "Agent exceeded declared step limit")
    if not result.citation_ids <= citation_ids:
        raise ComputerExecutionError("provenance", "Agent widened citation authority")
    return ComputerResult(
        value=result.value,
        citation_ids=result.citation_ids,
        receipt={
            **result.receipt,
            "definition_refs": {
                "computer": {"ref": computer_ref, "hash": computer.definition_hash},
                "agent": {"ref": agent_ref, "hash": agent.definition_hash},
                "context_policy": {
                    "ref": context_policy_ref,
                    "hash": policy.definition_hash,
                },
            },
            "context_pressure": {
                "used_tokens": used_tokens,
                "ratio": pressure.ratio,
                "action": pressure.action,
            },
        },
        steps=result.steps,
        awaiting_input=result.awaiting_input,
    )


__all__ = [
    "FAKE_COMPUTER_PROVIDER",
    "ComputerExecutionError",
    "ComputerRequest",
    "ComputerResult",
    "execute_agent",
    "execute_program",
    "register_computer_provider",
]
