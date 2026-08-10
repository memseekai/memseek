"""Trusted replay task for a saved gbrain synthesis with stale citations."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from memseek.answer import AnswerRequest
from memseek.derive.tasks import TaskConfigModel, TaskContext, TaskResult, register_task


class RepairSynthesisConfig(TaskConfigModel):
    """The replay contract has no workspace-authored knobs."""


class _SynthesisContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    question: str = Field(min_length=1)
    gaps: tuple[str, ...] = Field(max_length=20)
    rewrite: bool
    anchor: str | None = Field(default=None, min_length=1, max_length=128)
    since: datetime | None = None
    until: datetime | None = None


class _SynthesisInput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: UUID
    key: str = Field(min_length=1, max_length=128)
    content: _SynthesisContent


class RepairSynthesisInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    records: tuple[_SynthesisInput, ...] = Field(min_length=1, max_length=1)


class _RepairedSynthesisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    citations: tuple[UUID, ...] = Field(max_length=64)
    content: dict[str, Any]


async def repair_synthesis(
    context: TaskContext, value: RepairSynthesisInput, config: TaskConfigModel
) -> TaskResult[list[_RepairedSynthesisDraft]]:
    """Replay the exact saved request; only fresh answer evidence may be cited."""

    assert isinstance(config, RepairSynthesisConfig)
    saved = value.records[0]
    replay = AnswerRequest(
        question=saved.content.question,
        anchor=saved.content.anchor,
        since=saved.content.since,
        until=saved.content.until,
        rewrite=saved.content.rewrite,
        save=False,
    )
    result = await context.answer(replay)
    payload = result.value
    try:
        citations = tuple(UUID(str(item)) for item in payload["citations"])
        answer = str(payload["answer"])
        gaps = tuple(str(item) for item in payload["gaps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("answer replay returned an invalid payload") from exc
    return TaskResult(
        [
            _RepairedSynthesisDraft(
                key=saved.key,
                text=answer,
                citations=citations,
                content={
                    "question": saved.content.question,
                    "gaps": gaps,
                    "rewrite": saved.content.rewrite,
                    **(
                        {"anchor": saved.content.anchor} if saved.content.anchor is not None else {}
                    ),
                    **({"since": saved.content.since} if saved.content.since is not None else {}),
                    **({"until": saved.content.until} if saved.content.until is not None else {}),
                },
            )
        ],
        source_ids=result.source_ids,
        citation_ids=result.citation_ids,
    )


register_task(
    "repair_synthesis",
    implementation_hash=hashlib.sha256(b"memseek.repair_synthesis.v1").hexdigest(),
    config_model=RepairSynthesisConfig,
    input_type=RepairSynthesisInput,
    output_type=list[_RepairedSynthesisDraft],
    handler=repair_synthesis,
)


__all__ = ["RepairSynthesisConfig", "RepairSynthesisInput", "repair_synthesis"]
