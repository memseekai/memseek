"""Structured JSON logging with deliberately sparse operational fields."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

LogLevel = Literal["debug", "info", "warning", "error", "critical"]
_SENSITIVE_FIELDS = frozenset(
    {"api_key", "authorization", "content", "error", "message", "prompt", "secret"}
)


class JsonFormatter(logging.Formatter):
    """Serialize a log record and explicitly supplied safe fields as JSON."""

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
        }
        fields = getattr(record, "event_fields", None)
        if isinstance(fields, dict):
            document.update(fields)
        if record.exc_info:
            exception_type = record.exc_info[0]
            if exception_type is not None:
                document["exception"] = exception_type.__name__
        return json.dumps(document, separators=(",", ":"), sort_keys=True, default=str)


def configure_logging(level: int = logging.INFO, *, stream: TextIO | None = None) -> None:
    """Give the ``memseek`` namespace a JSON-only handler.

    Uvicorn installs its logging configuration before application lifespan.
    Configuring our namespace during lifespan therefore remains deterministic
    and cannot fall back to Uvicorn's plaintext root formatter.
    """

    namespace = logging.getLogger("memseek")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    namespace.handlers.clear()
    namespace.addHandler(handler)
    namespace.setLevel(level)
    namespace.propagate = False


def log_event(logger: logging.Logger, level: LogLevel, event: str, **safe_fields: Any) -> None:
    """Emit an event containing only caller-audited operational fields."""

    method = getattr(logger, level)
    redacted = {
        key: "[redacted]" if key.casefold() in _SENSITIVE_FIELDS else value
        for key, value in safe_fields.items()
    }
    method(event, extra={"event": event, "event_fields": redacted})


def log_llm_debug(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit a full model exchange at DEBUG level, without redaction.

    Unlike :func:`log_event`, this deliberately does not scrub ``prompt`` or
    response ``content``: revealing exactly what a derivation or processor sends
    to the model is the entire purpose of LLM debug mode. Callers gate it on
    ``Settings.llm_debug`` (off by default), and the ``isEnabledFor`` guard skips
    the work unless the ``memseek`` namespace is at DEBUG, so production logs
    never carry prompt content unless an operator has explicitly opted in.
    """

    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug(event, extra={"event": event, "event_fields": fields})
