"""Shared derivation failures with stable machine-readable kinds."""

from __future__ import annotations


class DerivationError(RuntimeError):
    """A bounded derive attempt with a machine-readable failure kind."""

    def __init__(self, kind: str, detail: str, *, wm: int = 0) -> None:
        self.kind = kind
        self.detail = detail
        self.wm = wm
        super().__init__(detail)


__all__ = ["DerivationError"]
