"""Structured logging safety tests."""

from __future__ import annotations

import io
import json
import logging

from memseek.logging import configure_logging, log_event


def test_memseek_logs_are_json_and_sensitive_fields_are_redacted() -> None:
    stream = io.StringIO()
    secret = "one-time-bearer-secret"
    configure_logging(stream=stream)
    log_event(
        logging.getLogger("memseek.security"),
        "error",
        "security.fixture",
        api_key=secret,
        error=secret,
        workspace="safe-workspace",
    )
    rendered = stream.getvalue()
    assert secret not in rendered
    document = json.loads(rendered)
    assert document["event"] == "security.fixture"
    assert document["api_key"] == "[redacted]"
    assert document["error"] == "[redacted]"
    assert document["workspace"] == "safe-workspace"
