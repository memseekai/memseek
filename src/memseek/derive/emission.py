"""Private inference from public emission intent to commit behavior."""

from __future__ import annotations

from typing import Literal

from memseek.derive.schema import EmitDefinition

type EmissionEffect = Literal["append", "patch", "replace"]
type EmissionStatus = Literal["active", "draft"]


def emission_effect(emit: EmitDefinition) -> EmissionEffect:
    if not emit.keys and not emit.driver_key and not emit.dynamic_keys:
        return "append"
    return "replace" if emit.complete else "patch"


def emission_status(emit: EmitDefinition) -> EmissionStatus:
    return "draft" if emit.review == "required" else "active"


__all__ = ["EmissionEffect", "EmissionStatus", "emission_effect", "emission_status"]
