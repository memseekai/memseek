"""Structured catalog diagnostics."""

from __future__ import annotations

from pathlib import Path


class DefinitionError(ValueError):
    """A definition error with stable machine-readable context."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        file: str | Path | None = None,
        path: str = "",
    ) -> None:
        self.code = code
        self.file = str(file) if file is not None else None
        self.path = path
        prefix = ": ".join(part for part in (self.file, self.path) if part)
        super().__init__(f"{prefix}: [{code}] {message}" if prefix else f"[{code}] {message}")


DefinitionValidationError = DefinitionError


class CollectionDefinitionMismatch(ValueError):
    """A stored collection identity cannot be resolved by the loaded catalog."""

    def __init__(
        self,
        name: str,
        version: int,
        stored_hash: str,
        expected_hash: str | None,
    ) -> None:
        self.code = (
            "collection_definition_missing"
            if expected_hash is None
            else "collection_definition_mismatch"
        )
        self.name = name
        self.version = version
        self.stored_hash = stored_hash
        self.expected_hash = expected_hash
        if expected_hash is None:
            detail = f"stored collection {name}@{version} is absent from the loaded catalog"
        else:
            detail = (
                f"stored collection {name}@{version} hash {stored_hash!r} "
                f"does not match loaded hash {expected_hash!r}"
            )
        super().__init__(f"[{self.code}] {detail}")
