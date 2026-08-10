from __future__ import annotations

import pytest
from pydantic import ValidationError

from memseek.config import Settings


def test_settings_defaults_are_immutable_and_normative() -> None:
    settings = Settings()

    assert settings.database_url.startswith("postgresql://")
    assert settings.models_file.name == "models.yaml"
    assert settings.job_heartbeat_s < settings.job_lease_s / 2
    assert settings.max_artifact_input_records == settings.max_derived_from - 1

    with pytest.raises(ValidationError, match="frozen"):
        settings.worker_concurrency = 99  # type: ignore[misc]


def test_settings_read_case_insensitive_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/memseek_test")
    monkeypatch.setenv("WORKER_CONCURRENCY", "7")
    monkeypatch.setenv("LLM_FAKE", "1")
    monkeypatch.setenv("TASK_MODULES", '["acme_memory.tasks"]')

    settings = Settings()

    assert settings.database_url.endswith("/memseek_test")
    assert settings.worker_concurrency == 7
    assert settings.llm_fake is True
    assert settings.task_modules == ("acme_memory.tasks",)


def test_secret_resolves_the_variable_a_provider_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each declared endpoint names its own credential, so any name must resolve."""

    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-key")
    monkeypatch.setenv("EMPTY_API_KEY", "")

    settings = Settings()

    assert settings.secret("VOYAGE_API_KEY") == "voyage-key"
    assert settings.secret("EMPTY_API_KEY") == ""
    assert settings.secret("NEVER_SET_API_KEY") == ""
    assert settings.secret(None) == ""


def test_settings_reject_incompatible_values() -> None:
    with pytest.raises(ValidationError, match="JOB_HEARTBEAT_S"):
        Settings(job_lease_s=100, job_heartbeat_s=50)
    with pytest.raises(ValidationError, match="positive"):
        Settings(worker_concurrency=0)
    with pytest.raises(ValidationError, match="reserve"):
        Settings(max_derived_from=100, max_artifact_input_records=100)
    with pytest.raises(ValidationError, match="strong or eventual"):
        Settings(turbopuffer_consistency="maybe")
    with pytest.raises(ValidationError, match="TASK_MODULES"):
        Settings(task_modules=("bad-module",))


def test_cors_origins_are_exact_and_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "API_CORS_ORIGINS",
        '["http://localhost:4321/", "https://console.example.test", "http://localhost:4321"]',
    )

    settings = Settings()

    assert settings.api_cors_origins == (
        "http://localhost:4321",
        "https://console.example.test",
    )
    with pytest.raises(ValidationError, match="wildcards"):
        Settings(api_cors_origins=("*",))
