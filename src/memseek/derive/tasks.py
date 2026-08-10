"""Trusted Task Adapter Interface for derivation pipelines.

Workspace YAML may select a registered Task, but it cannot upload Python.
Tasks receive a constrained context with bounded model/search capabilities and
return typed values; they never receive a database connection or canonical
record writer.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from memseek.definitions.base import ProcessorName, StrictModel

if TYPE_CHECKING:
    from memseek.answer import AnswerRequest
    from memseek.graph import GraphTraversalRequest


@dataclass(frozen=True, slots=True)
class TaskResult[T]:
    """Optional provenance-narrowing result returned by an installed Task."""

    value: T
    source_ids: frozenset[UUID] | None = None
    citation_ids: frozenset[UUID] | None = None


class TaskConfigModel(StrictModel):
    """Strict base for process-installed Task configuration contracts."""


class LLMTaskConfig(TaskConfigModel):
    model: ProcessorName | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    prompt: str = Field(min_length=1)
    output_schema: dict[str, Any]
    max_output_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_llm(self) -> LLMTaskConfig:
        if self.output_schema.get("type") != "object":
            raise ValueError("output_schema must describe a JSON object")
        if self.max_output_tokens is not None and "max_output_tokens" in self.params:
            raise ValueError("max_output_tokens cannot appear in both Task config and params")
        temperature = self.params.get("temperature")
        if temperature is not None:
            if isinstance(temperature, bool) or not isinstance(temperature, int | float):
                raise ValueError("temperature must be numeric")
            if not math.isfinite(float(temperature)) or not 0 <= float(temperature) <= 2:
                raise ValueError("temperature must be between 0 and 2")
        param_output = self.params.get("max_output_tokens")
        if param_output is not None and (
            isinstance(param_output, bool) or not isinstance(param_output, int) or param_output <= 0
        ):
            raise ValueError("params.max_output_tokens must be a positive integer")
        return self


class SearchTaskConfig(TaskConfigModel):
    q: str | None = None
    foreach: str | None = None
    spec: dict[str, Any]
    max_tokens: int = Field(default=6_000, ge=1)

    @model_validator(mode="after")
    def validate_form(self) -> SearchTaskConfig:
        if (self.q is None) == (self.foreach is None):
            raise ValueError("search requires exactly one of q or foreach")
        if self.q is not None and not self.q.strip():
            raise ValueError("search q must be non-blank")
        if self.foreach is not None and not self.foreach.strip():
            raise ValueError("search foreach must be non-blank")
        return self


class TemplateTaskConfig(TaskConfigModel):
    template: str


type TaskConfig = TaskConfigModel


class TaskContext(Protocol):
    """Capabilities available to trusted Task Implementations."""

    @property
    def entity(self) -> str: ...

    async def complete_json(self, config: LLMTaskConfig) -> TaskResult[Any]: ...

    async def search(self, config: SearchTaskConfig) -> TaskResult[Any]: ...

    async def traverse(self, request: GraphTraversalRequest) -> TaskResult[dict[str, Any]]: ...

    async def answer(self, request: AnswerRequest) -> TaskResult[dict[str, Any]]: ...

    def render(self, template: str) -> TaskResult[str]: ...


type TaskHandler = Callable[[TaskContext, Any, TaskConfig], Awaitable[TaskResult[Any] | Any]]


@dataclass(frozen=True, slots=True)
class RegisteredTask:
    """One immutable process-installed Task Adapter."""

    name: str
    implementation_hash: str
    config_adapter: TypeAdapter[Any]
    input_adapter: TypeAdapter[Any]
    output_adapter: TypeAdapter[Any]
    handler: TaskHandler

    def validate_config(self, value: Any) -> TaskConfig:
        parsed = self.config_adapter.validate_python(value)
        if not isinstance(parsed, TaskConfigModel):
            raise TypeError(f"Task {self.name!r} config adapter must produce a TaskConfigModel")
        return parsed

    def validate_input(self, value: Any) -> Any:
        return self.input_adapter.validate_python(value)

    def validate_output(self, value: Any) -> Any:
        parsed = self.output_adapter.validate_python(value)
        return self.output_adapter.dump_python(parsed, mode="json")


_TASKS: dict[str, RegisteredTask] = {}


def register_task(
    name: str,
    *,
    implementation_hash: str,
    config_model: type[TaskConfigModel],
    input_type: Any = Any,
    output_type: Any = Any,
    handler: TaskHandler,
) -> None:
    """Register one trusted Task before compiling a definition catalog."""

    if not name or not name.replace("_", "a").isalnum() or not name[0].islower():
        raise ValueError("Task name must be a lower-case public processor name")
    if len(name) > 32:
        raise ValueError("Task name exceeds 32 characters")
    if len(implementation_hash) != 64 or any(
        char not in "0123456789abcdef" for char in implementation_hash
    ):
        raise ValueError("Task implementation_hash must be 64 lower-case hex characters")
    if not inspect.iscoroutinefunction(handler):
        raise TypeError("Task handler must be async")
    if not issubclass(config_model, TaskConfigModel):
        raise TypeError("Task config_model must inherit TaskConfigModel")
    if name in _TASKS:
        raise ValueError(f"Task {name!r} is already registered")
    _TASKS[name] = RegisteredTask(
        name=name,
        implementation_hash=implementation_hash,
        config_adapter=TypeAdapter(config_model),
        input_adapter=TypeAdapter(input_type),
        output_adapter=TypeAdapter(output_type),
        handler=handler,
    )


def task_adapter(name: str) -> RegisteredTask:
    """Resolve one registered Task or raise a concise authoring error."""

    try:
        return _TASKS[name]
    except KeyError as exc:
        raise KeyError(f"unknown Task Adapter {name!r}") from exc


def task_implementation_hashes(names: Sequence[str]) -> dict[str, str]:
    return {name: task_adapter(name).implementation_hash for name in sorted(set(names))}


def import_task_modules(names: Sequence[str]) -> None:
    """Import trusted deployment modules before any catalog is compiled."""

    for name in names:
        importlib.import_module(name)


async def _llm(context: TaskContext, value: Any, config: TaskConfig) -> TaskResult[Any]:
    del value
    return await context.complete_json(cast(LLMTaskConfig, config))


async def _search(context: TaskContext, value: Any, config: TaskConfig) -> TaskResult[Any]:
    del value
    return await context.search(cast(SearchTaskConfig, config))


async def _template(context: TaskContext, value: Any, config: TaskConfig) -> TaskResult[Any]:
    del value
    return context.render(cast(TemplateTaskConfig, config).template)


def _builtin_hash(name: str) -> str:
    return hashlib.sha256(f"memseek-task-v1:{name}".encode()).hexdigest()


register_task(
    "llm",
    implementation_hash=_builtin_hash("llm"),
    config_model=LLMTaskConfig,
    handler=_llm,
)
register_task(
    "search",
    implementation_hash=_builtin_hash("search"),
    config_model=SearchTaskConfig,
    handler=_search,
)
register_task(
    "template",
    implementation_hash=_builtin_hash("template"),
    config_model=TemplateTaskConfig,
    handler=_template,
)


__all__ = [
    "LLMTaskConfig",
    "RegisteredTask",
    "SearchTaskConfig",
    "TaskConfigModel",
    "TaskContext",
    "TaskResult",
    "TemplateTaskConfig",
    "import_task_modules",
    "register_task",
    "task_adapter",
    "task_implementation_hashes",
]
