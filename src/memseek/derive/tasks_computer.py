"""Trusted derivation adapters for direct and agentic Computer execution."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any

from pydantic import Field, field_validator, model_validator

from memseek.definitions.base import split_exact_reference
from memseek.derive.tasks import TaskConfig, TaskConfigModel, TaskContext, TaskResult, register_task


def _exact(value: str, label: str) -> str:
    try:
        split_exact_reference(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an exact name@version reference") from exc
    return value


def _outbox_path(value: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or not str(path).startswith("/outbox/"):
        raise ValueError("output must be a safe absolute path below /outbox")
    return str(path)


class ComputerTaskConfig(TaskConfigModel):
    computer: str
    program: str
    output: str = "/outbox/result.json"

    @model_validator(mode="after")
    def validate_references(self) -> ComputerTaskConfig:
        _exact(self.computer, "computer")
        _exact(self.program, "program")
        return self

    @field_validator("output")
    @classmethod
    def validate_output(cls, value: str) -> str:
        return _outbox_path(value)


class AgentTaskConfig(TaskConfigModel):
    agent: str
    computer: str
    context_policy: str
    output: str = "/outbox/result.json"
    output_schema: dict[str, Any]
    max_steps: int | None = Field(default=None, ge=1, le=128)

    @model_validator(mode="after")
    def validate_references(self) -> AgentTaskConfig:
        _exact(self.agent, "agent")
        _exact(self.computer, "computer")
        _exact(self.context_policy, "context_policy")
        if self.output_schema.get("type") != "object":
            raise ValueError("output_schema must describe a JSON object")
        return self

    @field_validator("output")
    @classmethod
    def validate_output(cls, value: str) -> str:
        return _outbox_path(value)


async def _computer(context: TaskContext, value: Any, config: TaskConfig) -> TaskResult[Any]:
    return await context.run_computer(value, config)


async def _agent(context: TaskContext, value: Any, config: TaskConfig) -> TaskResult[Any]:
    return await context.run_agent(value, config)


def _implementation_hash(name: str) -> str:
    return hashlib.sha256(f"memseek-computer-task-v1:{name}".encode()).hexdigest()


register_task(
    "computer",
    implementation_hash=_implementation_hash("computer"),
    config_model=ComputerTaskConfig,
    handler=_computer,
)
register_task(
    "agent",
    implementation_hash=_implementation_hash("agent"),
    config_model=AgentTaskConfig,
    handler=_agent,
)


__all__ = ["AgentTaskConfig", "ComputerTaskConfig"]
