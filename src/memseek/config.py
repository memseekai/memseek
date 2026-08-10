"""Environment-backed, immutable application settings."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@cache
def _env_file_values(path: str) -> Mapping[str, str]:
    """Parse the deployment's env file once for catalog-named credentials."""

    return {name: value for name, value in dotenv_values(path).items() if value}


class Settings(BaseSettings):
    """Validated process configuration.

    Definition paths deliberately remain relative paths.  They are deployment
    assets and are resolved against the process working directory by the
    definition loader.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    database_url: str = "postgresql://postgres:postgres@localhost:5432/memseek"
    memseek_build_sha: str = "dev"

    # Which endpoints exist, what they can do, and which model is used for what
    # are all declared in this file rather than here.  A provider's credential is
    # the one part that must not live in a committed definition, so the catalog
    # names an environment variable and ``secret`` resolves it.
    models_file: Path = Path("conf/models.yaml")
    llm_fake: bool = False
    # When true, every model request (system message, prompt and effective
    # parameters, plus embedding inputs) and its raw response are logged at
    # DEBUG level for derivations and processors, with NO redaction, so
    # operators can see exactly what is sent to the LLM. This deliberately
    # bypasses the prompt/content redaction that normal logging applies; keep it
    # off outside of local debugging.
    llm_debug: bool = False
    llm_max_concurrency: int = 8
    model_context_tokens: int = 60_000
    max_prompt_tokens: int = 50_000
    max_output_tokens: int = 4_000

    # Catalog definitions are NOT loaded by default. Unset means "this process
    # ships no definitions of its own"; a workspace gets its catalog by
    # publishing one, and nothing is inherited from whatever happens to sit on
    # disk beside the service. Point these at a directory — `resources/` holds
    # the reference catalog, `examples/*_catalog/` hold self-contained ones —
    # only when you deliberately want that catalog compiled at startup.
    #
    # An explicitly configured path that does not exist is still an error. The
    # tolerated case is absence of configuration, never a typo in it.
    processors_file: Path | None = None
    collections_dir: Path | None = None
    triggers_dir: Path | None = None
    views_dir: Path | None = None
    artifacts_dir: Path | None = None
    mcp_dir: Path | None = None
    packages_dir: Path | None = None

    search_profiles_file: Path = Path("conf/search_profiles.yaml")
    search_backend: str = "pg"
    rank_default_file: Path = Path("conf/rank_default.yaml")
    search_profile_overrides_file: Path | None = None
    touch_on_read: bool = True
    rrf_rank_constant: int = 60
    max_collection_fanout: int = 8
    search_max_concurrency: int = 8

    turbopuffer_api_key: str = ""
    turbopuffer_region: str = "gcp-us-central1"
    turbopuffer_base_url: str = ""
    turbopuffer_layout: str = "shared"
    turbopuffer_consistency: str = "strong"

    derivations_dir: Path | None = None

    @property
    def has_configured_catalog(self) -> bool:
        """Whether this process was told to compile definitions of its own.

        False for the shipped defaults, and that is the point: a service with
        no configured catalog cannot hand a workspace definitions it never
        asked for. When it is True the operator named a directory on purpose,
        so serving it to a workspace that has published nothing is a choice
        they made rather than an accident of the working directory.
        """

        return any(
            source is not None
            for source in (
                self.collections_dir,
                self.derivations_dir,
                self.views_dir,
                self.artifacts_dir,
                self.packages_dir,
                self.mcp_dir,
                self.triggers_dir,
                self.processors_file,
            )
        )

    task_modules: tuple[str, ...] = (
        "memseek.derive.tasks_graph",
        "memseek.derive.tasks_facts",
        "memseek.derive.tasks_repair",
        "memseek.derive.tasks_migrate",
    )
    enrich_batch: int = 32
    enrich_llm_batch: int = 16
    scorer_text_chars: int = 12_000
    max_batch: int = 100
    max_text_chars: int = 65_536
    max_content_bytes: int = 131_072
    max_annotation_bytes: int = 32_768
    max_artifact_render_tokens: int = 50_000
    max_artifact_input_records: int = 255
    max_run_content_bytes: int = 262_144
    max_derivation_config_bytes: int = 65_536
    max_derived_from: int = 256
    max_response_bytes: int = 4_194_304
    max_document_records: int = 500
    max_query_chars: int = 8_192
    search_render_tokens: int = 16_000
    max_citations_per_output: int = 64
    max_derivation_depth: int = 4
    max_graph_depth: int = 4
    max_graph_paths: int = 100
    max_step_concurrency: int = 5
    max_run_total_tokens: int = 100_000
    max_run_wall_s: int = 180

    worker_poll_ms: int = 500
    worker_concurrency: int = 4
    index_concurrency: int = 1
    job_lease_s: int = 300
    job_heartbeat_s: int = 60
    job_max_attempts: int = 3
    unready_retry_s: int = 2
    cron_tick_s: int = 30
    max_cron_catchup: int = 100

    context_doc_order_score: str = "importance"

    # An artifact use is a correlation handle, not durable history.  Its
    # retention bounds how long delayed feedback can still name a render; the
    # learning signals it produces follow ordinary record retention.
    artifact_use_retention_days: int = 90
    artifact_use_purge_batch: int = 500
    max_feedback_comment_chars: int = 2_000
    max_feedback_evidence_chars: int = 4_000

    # Publishing an additive schema change over a collection that already allowed
    # arbitrary content keys is only provably safe once existing values for the
    # newly declared keys are checked.  This bounds that check: above it, the
    # publish asks for a new collection version instead of scanning unbounded.
    additive_verify_max_rows: int = 50_000
    # One backfill pass claims at most this many rows before yielding the lane.
    backfill_batch: int = 200

    api_key_cache_ttl_s: int = 60
    api_key_cache_size: int = 1_024

    # These are useful to runtime entrypoints and safe to configure, while the
    # normative worker defaults remain the fields above.
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65_535)
    # The workspace explorer is a separately hosted browser client. Origins
    # remain opt-in because allowing an arbitrary page to send a bearer token
    # to an API turns a user-controlled credential into ambient authority.
    api_cors_origins: tuple[str, ...] = ()

    @field_validator("api_cors_origins")
    @classmethod
    def validate_api_cors_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            parsed = urlsplit(value)
            if (
                value == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError(
                    "API_CORS_ORIGINS entries must be exact HTTP(S) origins; wildcards are not allowed"
                )
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin not in normalized:
                normalized.append(origin)
        return tuple(normalized)

    def secret(self, env_var: str | None) -> str:
        """Resolve a credential the catalog referenced by environment variable name.

        The process environment wins over the ``.env`` file, matching how every
        declared setting above is resolved; an empty value counts as absent so a
        placeholder line cannot mask a real key.
        """

        if not env_var:
            return ""
        value = os.environ.get(env_var, "")
        if value:
            return value
        env_file = self.model_config.get("env_file")
        return _env_file_values(str(env_file)).get(env_var, "") if env_file else ""

    @model_validator(mode="after")
    def validate_invariants(self) -> Settings:
        if self.search_backend not in {"pg", "turbopuffer"}:
            raise ValueError("SEARCH_BACKEND must be pg or turbopuffer")
        if self.turbopuffer_layout not in {"shared", "per_collection"}:
            raise ValueError("TURBOPUFFER_LAYOUT must be shared or per_collection")
        if self.turbopuffer_consistency not in {"strong", "eventual"}:
            raise ValueError("TURBOPUFFER_CONSISTENCY must be strong or eventual")
        if self.job_heartbeat_s >= self.job_lease_s / 2:
            raise ValueError("JOB_HEARTBEAT_S must be less than half JOB_LEASE_S")
        invalid_task_modules = [
            name
            for name in self.task_modules
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", name)
        ]
        if invalid_task_modules:
            raise ValueError(f"TASK_MODULES contains invalid module names: {invalid_task_modules}")

        positive = {
            "LLM_MAX_CONCURRENCY": self.llm_max_concurrency,
            "MODEL_CONTEXT_TOKENS": self.model_context_tokens,
            "MAX_PROMPT_TOKENS": self.max_prompt_tokens,
            "MAX_OUTPUT_TOKENS": self.max_output_tokens,
            "MAX_COLLECTION_FANOUT": self.max_collection_fanout,
            "SEARCH_MAX_CONCURRENCY": self.search_max_concurrency,
            "ENRICH_BATCH": self.enrich_batch,
            "ENRICH_LLM_BATCH": self.enrich_llm_batch,
            "SCORER_TEXT_CHARS": self.scorer_text_chars,
            "MAX_BATCH": self.max_batch,
            "MAX_TEXT_CHARS": self.max_text_chars,
            "MAX_CONTENT_BYTES": self.max_content_bytes,
            "MAX_ANNOTATION_BYTES": self.max_annotation_bytes,
            "MAX_ARTIFACT_RENDER_TOKENS": self.max_artifact_render_tokens,
            "MAX_ARTIFACT_INPUT_RECORDS": self.max_artifact_input_records,
            "MAX_RUN_CONTENT_BYTES": self.max_run_content_bytes,
            "MAX_DERIVATION_CONFIG_BYTES": self.max_derivation_config_bytes,
            "MAX_DERIVED_FROM": self.max_derived_from,
            "MAX_RESPONSE_BYTES": self.max_response_bytes,
            "MAX_DOCUMENT_RECORDS": self.max_document_records,
            "MAX_QUERY_CHARS": self.max_query_chars,
            "SEARCH_RENDER_TOKENS": self.search_render_tokens,
            "MAX_CITATIONS_PER_OUTPUT": self.max_citations_per_output,
            "MAX_DERIVATION_DEPTH": self.max_derivation_depth,
            "MAX_GRAPH_DEPTH": self.max_graph_depth,
            "MAX_GRAPH_PATHS": self.max_graph_paths,
            "MAX_STEP_CONCURRENCY": self.max_step_concurrency,
            "MAX_RUN_TOTAL_TOKENS": self.max_run_total_tokens,
            "MAX_RUN_WALL_S": self.max_run_wall_s,
            "WORKER_POLL_MS": self.worker_poll_ms,
            "WORKER_CONCURRENCY": self.worker_concurrency,
            "INDEX_CONCURRENCY": self.index_concurrency,
            "JOB_LEASE_S": self.job_lease_s,
            "JOB_HEARTBEAT_S": self.job_heartbeat_s,
            "JOB_MAX_ATTEMPTS": self.job_max_attempts,
            "UNREADY_RETRY_S": self.unready_retry_s,
            "CRON_TICK_S": self.cron_tick_s,
            "MAX_CRON_CATCHUP": self.max_cron_catchup,
            "ARTIFACT_USE_RETENTION_DAYS": self.artifact_use_retention_days,
            "ARTIFACT_USE_PURGE_BATCH": self.artifact_use_purge_batch,
            "MAX_FEEDBACK_COMMENT_CHARS": self.max_feedback_comment_chars,
            "MAX_FEEDBACK_EVIDENCE_CHARS": self.max_feedback_evidence_chars,
            "API_KEY_CACHE_TTL_S": self.api_key_cache_ttl_s,
            "API_KEY_CACHE_SIZE": self.api_key_cache_size,
            "ADDITIVE_VERIFY_MAX_ROWS": self.additive_verify_max_rows,
            "BACKFILL_BATCH": self.backfill_batch,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ValueError(f"settings must be positive: {', '.join(sorted(invalid))}")
        if self.max_prompt_tokens + self.max_output_tokens > self.model_context_tokens:
            raise ValueError("MAX_PROMPT_TOKENS plus MAX_OUTPUT_TOKENS exceeds model context")
        if self.max_artifact_input_records > self.max_derived_from - 1:
            raise ValueError("MAX_ARTIFACT_INPUT_RECORDS must reserve one provenance parent")
        if self.max_derived_from > 256:
            raise ValueError("MAX_DERIVED_FROM cannot exceed the schema limit of 256")
        if self.max_derivation_depth > 16:
            raise ValueError("MAX_DERIVATION_DEPTH cannot exceed the schema limit of 16")
        if self.max_graph_depth > 16:
            raise ValueError("MAX_GRAPH_DEPTH cannot exceed the graph schema limit of 16")
        if self.max_graph_paths > 500:
            raise ValueError("MAX_GRAPH_PATHS cannot exceed the graph schema limit of 500")
        if self.max_citations_per_output > self.max_derived_from - 1:
            raise ValueError("MAX_CITATIONS_PER_OUTPUT exceeds the provenance parent budget")
        if self.max_artifact_render_tokens > self.max_prompt_tokens:
            raise ValueError("MAX_ARTIFACT_RENDER_TOKENS cannot exceed MAX_PROMPT_TOKENS")
        if self.search_render_tokens > self.max_prompt_tokens:
            raise ValueError("SEARCH_RENDER_TOKENS cannot exceed MAX_PROMPT_TOKENS")
        if self.max_collection_fanout > 8:
            raise ValueError("MAX_COLLECTION_FANOUT cannot exceed 8")
        if self.api_key_cache_ttl_s > 60:
            raise ValueError("API_KEY_CACHE_TTL_S cannot exceed 60 seconds")
        if self.artifact_use_retention_days > 3_650:
            raise ValueError("ARTIFACT_USE_RETENTION_DAYS cannot exceed 3650")
        if self.max_feedback_comment_chars > self.max_text_chars:
            raise ValueError("MAX_FEEDBACK_COMMENT_CHARS cannot exceed MAX_TEXT_CHARS")
        if self.max_feedback_evidence_chars > self.max_text_chars:
            raise ValueError("MAX_FEEDBACK_EVIDENCE_CHARS cannot exceed MAX_TEXT_CHARS")
        if not 1 <= self.rrf_rank_constant <= 1_000:
            raise ValueError("RRF_RANK_CONSTANT must be between 1 and 1000")
        if self.turbopuffer_base_url:
            self._validate_url("TURBOPUFFER_BASE_URL", self.turbopuffer_base_url)
        return self

    @staticmethod
    def _validate_url(name: str, value: str) -> None:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{name} must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(f"{name} must use HTTPS except on localhost")


def get_settings() -> Settings:
    """Construct settings from the current environment."""

    return Settings()
