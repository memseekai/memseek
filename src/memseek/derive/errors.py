"""Shared derivation failures with stable machine-readable kinds."""

from __future__ import annotations


class DerivationError(RuntimeError):
    """A bounded derive attempt with a machine-readable failure kind."""

    def __init__(self, kind: str, detail: str, *, wm: int = 0) -> None:
        self.kind = kind
        self.detail = detail
        self.wm = wm
        super().__init__(detail)


NON_RETRYABLE_KINDS = frozenset({"config", "budget", "erased", "task"})


class RecordedDerivationFailure(DerivationError):
    """A failure whose run receipt and job transition are already committed.

    The worker must not apply its own retry or dead-letter transition to it:
    the claim is already released, so a second transition only reports a
    false lease loss.
    """


__all__ = ["NON_RETRYABLE_KINDS", "DerivationError", "RecordedDerivationFailure"]
