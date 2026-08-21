"""FastAPI application for health, canonical ingest, read views, and job operations."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from memseek.answer import AnswerError, AnswerRequest, answer_question
from memseek.artifact_uses import ArtifactUseRequest, FeedbackRequest
from memseek.auth import (
    ApiKeyCache,
    AuthenticationError,
    authenticate_api_key,
    parse_bearer_header,
)
from memseek.config import Settings, get_settings
from memseek.db import (
    DatabasePool,
    close_pool,
    create_pool,
    open_pool,
    verify_storage_compatibility,
)
from memseek.erase import ErasureError, ErasureRequest, erase
from memseek.logging import configure_logging, log_event
from memseek.records import (
    DedupeConflict,
    RecordBatchRequest,
    RecordValidationError,
    insert_public_records,
)
from memseek.search.engine import (
    SearchRequestError,
    SearchUnavailableError,
    execute_search,
    rank_schema_payload,
)
from memseek.search.named_views import ViewNotFound, execute_view, view_catalog_payload
from memseek.search.spec import SearchSpec
from memseek.views import (
    CursorRegression,
    CursorRequest,
    CursorScopeMismatch,
    DeltaQuery,
    DocumentQuery,
    DocumentTooLarge,
    HistoryQuery,
    ResponseTooLarge,
    TimelineQuery,
    build_document,
    fetch_delta,
    fetch_history,
    fetch_record,
    fetch_timeline,
    upsert_cursor,
)
from memseek.views.context import ContextQuery, ContextRequestError, build_context
from memseek.views.entities import EntitiesQuery, fetch_entities
from memseek.views.runs import RunNotFound, RunOutputsQuery, RunsQuery, fetch_run, fetch_runs
from memseek.workspace_catalog import (
    WorkspaceCatalogError,
    WorkspaceCatalogRegistry,
    WorkspaceCatalogRequest,
)

if TYPE_CHECKING:
    from memseek.definitions import DefinitionCatalog

LOGGER = logging.getLogger(__name__)


class ManualDerivationRequest(BaseModel):
    """Authenticated request to enqueue one entity-scoped derive processor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity: str = Field(min_length=1, max_length=255)
    run_after: datetime | None = None

    @field_validator("entity")
    @classmethod
    def non_blank_entity(cls, value: str) -> str:
        if not value.strip() or value == "*":
            raise ValueError("entity must be non-blank and cannot be '*'")
        return value

    @field_validator("run_after")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("run_after must include a timezone")
        return value


class PromotionRequest(BaseModel):
    """Authenticated request to activate one prior run's output snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity: str = Field(min_length=1, max_length=255)
    source_run_id: UUID
    artifact: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("entity")
    @classmethod
    def non_blank_entity(cls, value: str) -> str:
        if not value.strip() or value == "*":
            raise ValueError("entity must be non-blank and cannot be '*'")
        return value


class BackfillRequest(BaseModel):
    """Authenticated request to apply one processor to already-stored records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collection: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    processor: str = Field(min_length=1, max_length=32)
    # Omitted by default, which reaches every eligible record. A caller sets it
    # only to impose a ceiling — a cost cap, or a canary over part of the corpus.
    max_rows: int | None = Field(default=None, ge=1)


class CursorRebindRequest(BaseModel):
    """Authenticated request to repoint one changes cursor deliberately."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity: str = Field(min_length=1, max_length=255)
    policy: Literal["reset", "carry"]

    @field_validator("entity")
    @classmethod
    def non_blank_entity(cls, value: str) -> str:
        if not value.strip() or value == "*":
            raise ValueError("entity must be non-blank and cannot be '*'")
        return value


class ReindexRequest(BaseModel):
    """Authenticated request to rebuild one workspace's external projections."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Exactly one of the two, which is what makes the scope explicit: resume from
    # a sequence, or rebuild every ready record.
    since_seq: int | None = Field(default=None, ge=0)
    reset: bool = False
    # A full reset outside a test database is the one projection operation that
    # asks the caller to say so twice.
    confirm: bool = False


class _RequestRejected(Exception):
    """A request that the HTTP Adapter can reject without invoking a domain Module."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def _validation_detail(exc: ValidationError) -> str:
    issue = exc.errors(include_url=False)[0]
    path = ".".join(str(part) for part in issue.get("loc", ()))
    return f"{path}: {issue['msg']}" if path else str(issue["msg"])


async def _authenticated_workspace(request: Request) -> str:
    """Resolve the bearer's workspace and selected catalog or reject the request."""

    try:
        bearer = parse_bearer_header(request.headers.get("authorization"))
        workspace = await authenticate_api_key(
            request.app.state.pool,
            bearer,
            request.app.state.api_key_cache,
        )
    except AuthenticationError as exc:
        raise _RequestRejected(401, "unauthorized", "invalid bearer credential") from exc
    if workspace is None:
        raise _RequestRejected(401, "unauthorized", "invalid bearer credential")
    registry: WorkspaceCatalogRegistry = request.app.state.catalog_registry
    try:
        request.state.catalog = await registry.get(workspace)
    except WorkspaceCatalogError as exc:
        if exc.code != "no_catalog":
            raise
        # Publishing a package is how a workspace acquires a catalog, so a new
        # workspace must be able to authenticate before it has one. Routes that
        # need definitions raise this deferred error through `_request_catalog`.
        request.state.catalog_error = exc
    return workspace


_AuthenticatedWorkspace = Annotated[str, Depends(_authenticated_workspace)]


async def _json_payload(request: Request) -> Any:
    try:
        return await request.json()
    except (UnicodeDecodeError, ValueError) as exc:
        raise _RequestRejected(422, "invalid_json", "request body must be valid JSON") from exc


async def _validated_json[T: BaseModel](request: Request, model: type[T]) -> T:
    try:
        return model.model_validate(await _json_payload(request))
    except ValidationError as exc:
        raise _RequestRejected(422, "request_schema", _validation_detail(exc)) from exc


def _json_body[T: BaseModel](model: type[T]) -> Callable[..., Awaitable[T]]:
    async def parse(request: Request, _workspace: _AuthenticatedWorkspace) -> T:
        return await _validated_json(request, model)

    return parse


def _json_object(detail: str) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def parse(request: Request, _workspace: _AuthenticatedWorkspace) -> dict[str, Any]:
        payload = await _json_payload(request)
        if not isinstance(payload, dict):
            raise _RequestRejected(422, "request_schema", detail)
        return payload

    return parse


def _query_model[T: BaseModel](model: type[T]) -> Callable[..., Awaitable[T]]:
    async def parse(request: Request, _workspace: _AuthenticatedWorkspace) -> T:
        try:
            return model.model_validate(dict(request.query_params))
        except ValidationError as exc:
            raise _RequestRejected(422, "request_schema", _validation_detail(exc)) from exc

    return parse


def _uuid_path(
    parameter: str,
    detail: str,
    *,
    code: str = "invalid_id",
) -> Callable[..., Awaitable[UUID]]:
    async def parse(request: Request, _workspace: _AuthenticatedWorkspace) -> UUID:
        try:
            return UUID(request.path_params[parameter])
        except ValueError as exc:
            raise _RequestRejected(422, code, detail) from exc

    return parse


_WorkspaceCatalogBody = Annotated[
    WorkspaceCatalogRequest, Depends(_json_body(WorkspaceCatalogRequest))
]
_BackfillBody = Annotated[BackfillRequest, Depends(_json_body(BackfillRequest))]
_CursorRebindBody = Annotated[CursorRebindRequest, Depends(_json_body(CursorRebindRequest))]
_ReindexBody = Annotated[ReindexRequest, Depends(_json_body(ReindexRequest))]
_RecordBatchBody = Annotated[RecordBatchRequest, Depends(_json_body(RecordBatchRequest))]
_CursorBody = Annotated[CursorRequest, Depends(_json_body(CursorRequest))]
_SearchBody = Annotated[SearchSpec, Depends(_json_body(SearchSpec))]
_AnswerBody = Annotated[AnswerRequest, Depends(_json_body(AnswerRequest))]
_PromotionBody = Annotated[PromotionRequest, Depends(_json_body(PromotionRequest))]
_ErasureBody = Annotated[ErasureRequest, Depends(_json_body(ErasureRequest))]
_ArtifactUseBody = Annotated[ArtifactUseRequest, Depends(_json_body(ArtifactUseRequest))]
_FeedbackBody = Annotated[FeedbackRequest, Depends(_json_body(FeedbackRequest))]

_TimelineParams = Annotated[TimelineQuery, Depends(_query_model(TimelineQuery))]
_EntitiesParams = Annotated[EntitiesQuery, Depends(_query_model(EntitiesQuery))]
_DocumentParams = Annotated[DocumentQuery, Depends(_query_model(DocumentQuery))]
_HistoryParams = Annotated[HistoryQuery, Depends(_query_model(HistoryQuery))]
_DeltaParams = Annotated[DeltaQuery, Depends(_query_model(DeltaQuery))]
_RunsParams = Annotated[RunsQuery, Depends(_query_model(RunsQuery))]
_RunOutputsParams = Annotated[RunOutputsQuery, Depends(_query_model(RunOutputsQuery))]
_ContextParams = Annotated[ContextQuery, Depends(_query_model(ContextQuery))]

_ViewParameters = Annotated[
    dict[str, Any], Depends(_json_object("view parameters must be an object"))
]
_ArtifactParameters = Annotated[
    dict[str, Any], Depends(_json_object("artifact parameters must be an object"))
]

_JobId = Annotated[UUID, Depends(_uuid_path("job_id", "job id must be a UUID"))]
_BackfillId = Annotated[UUID, Depends(_uuid_path("backfill_id", "backfill id must be a UUID"))]
_RecordId = Annotated[UUID, Depends(_uuid_path("record_id", "record id must be a UUID"))]
_RunId = Annotated[UUID, Depends(_uuid_path("run_id", "run id must be a UUID"))]
_ArtifactUseId = Annotated[
    UUID,
    Depends(_uuid_path("use_id", "use_id must be a UUID", code="request_schema")),
]


def _load_catalog(settings: Settings) -> DefinitionCatalog:
    from memseek.definitions import load_definition_catalog

    return load_definition_catalog(settings)


def create_app(
    settings: Settings | None = None,
    *,
    catalog: DefinitionCatalog | None = None,
    pool: DatabasePool | None = None,
    verify_storage: bool = True,
) -> FastAPI:
    """Build an application whose lifespan owns its pool explicitly."""

    runtime_settings = settings or get_settings()
    from memseek.derive.tasks import import_task_modules

    import_task_modules(runtime_settings.task_modules)
    from memseek.mcp_http import McpEndpointMiddleware, McpHttpRuntime

    mcp_http_runtime: McpHttpRuntime | None = None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configure_logging(logging.DEBUG if runtime_settings.llm_debug else logging.INFO)
        runtime_pool = pool or create_pool(runtime_settings)
        try:
            runtime_catalog = catalog or _load_catalog(runtime_settings)
            await open_pool(runtime_pool)
            if verify_storage:
                # Semantic compatibility is workspace-scoped once packages can
                # replace the bootstrap catalog. Keep startup checks structural;
                # each resolved catalog is checked again at its operation seam.
                await verify_storage_compatibility(
                    runtime_pool, runtime_settings, runtime_catalog, semantics=False
                )
            catalog_registry = WorkspaceCatalogRegistry(
                runtime_pool,
                runtime_settings,
                runtime_catalog,
            )
            application.state.settings = runtime_settings
            application.state.catalog = runtime_catalog
            application.state.catalog_registry = catalog_registry
            application.state.pool = runtime_pool
            application.state.api_key_cache = ApiKeyCache(
                ttl_s=runtime_settings.api_key_cache_ttl_s,
                max_size=runtime_settings.api_key_cache_size,
            )
            assert mcp_http_runtime is not None
            async with mcp_http_runtime.run():
                log_event(LOGGER, "info", "api.started")
                yield
        except BaseException as exc:
            log_event(
                LOGGER,
                "error",
                "api.lifecycle_failed",
                exception_type=type(exc).__name__,
            )
            raise
        finally:
            if not runtime_pool.closed:
                await close_pool(runtime_pool)
            log_event(LOGGER, "info", "api.stopped")

    application = FastAPI(
        title="memseek",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    if runtime_settings.api_cors_origins:
        # Bearer credentials are supplied explicitly by the browser client;
        # never enable credentialed wildcard CORS for a workspace API.
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(runtime_settings.api_cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )
    mcp_http_runtime = McpHttpRuntime(
        application,
        allowed_origins=runtime_settings.api_cors_origins,
    )
    application.add_middleware(McpEndpointMiddleware, endpoint=mcp_http_runtime.endpoint)

    @application.exception_handler(_RequestRejected)
    async def _handle_rejected_request(_request: Request, exc: _RequestRejected) -> JSONResponse:
        return _error_response(exc.status, exc.code, exc.detail)

    @application.get("/health")
    async def health(request: Request) -> JSONResponse:
        try:
            async with request.app.state.pool.connection() as conn:
                result = await conn.execute("select 1 as ok")
                row = await result.fetchone()
            if row is None or row["ok"] != 1:
                raise RuntimeError("database liveness query returned no result")
        except Exception as exc:  # The health boundary intentionally maps driver failures.
            log_event(
                LOGGER,
                "error",
                "health.database_unavailable",
                exception_type=type(exc).__name__,
            )
            return JSONResponse(status_code=503, content={"ok": False, "db": False})
        return JSONResponse(status_code=200, content={"ok": True, "db": True})

    @application.get("/catalog")
    async def read_catalog(request: Request, workspace: _AuthenticatedWorkspace) -> JSONResponse:
        registry: WorkspaceCatalogRegistry = request.app.state.catalog_registry
        metadata = await registry.metadata(workspace)
        return JSONResponse(status_code=200, content=metadata)

    @application.get("/catalog/compatibility")
    async def read_catalog_compatibility(
        request: Request, workspace: _AuthenticatedWorkspace
    ) -> JSONResponse:
        """Report the installed catalog's standing against its own records."""

        registry: WorkspaceCatalogRegistry = request.app.state.catalog_registry
        try:
            report = await registry.compatibility(workspace)
        except WorkspaceCatalogError as exc:
            return _catalog_error_response(exc)
        return JSONResponse(status_code=200, content=report.as_json())

    @application.get("/catalog/prune")
    async def read_catalog_prune(
        request: Request, workspace: _AuthenticatedWorkspace
    ) -> JSONResponse:
        """Report which inactive definitions nothing in the workspace references."""

        from memseek.evolution import prune_definitions

        try:
            report = await prune_definitions(
                request.app.state.pool,
                workspace=workspace,
                catalog=_request_catalog(request),
            )
        except Exception as exc:
            return _read_failure(exc, "catalog.prune_failed", workspace)
        return JSONResponse(status_code=200, content=report.as_json())

    @application.post("/catalog")
    async def load_catalog(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        body: _WorkspaceCatalogBody,
        dry_run: bool = False,
    ) -> JSONResponse:
        registry: WorkspaceCatalogRegistry = request.app.state.catalog_registry
        try:
            if dry_run:
                # A preflight compiles and plans exactly as a publish does, then
                # returns the plan instead of applying it. Nothing is installed.
                report, *_ = await registry.preflight(workspace, body)
                return JSONResponse(status_code=200, content=report.as_json())
            result = await registry.install(workspace, body)
            request.state.catalog = await registry.get(workspace)
        except WorkspaceCatalogError as exc:
            return _catalog_error_response(exc)
        except Exception as exc:
            log_event(
                LOGGER,
                "error",
                "catalog.install_failed",
                workspace=workspace,
                exception_type=type(exc).__name__,
            )
            return _error_response(500, "internal_error", "catalog installation failed")
        return JSONResponse(status_code=200, content=result.as_json())

    @application.post("/processors/{processor_name}/run")
    async def run_processor(
        request: Request,
        processor_name: str,
        workspace: _AuthenticatedWorkspace,
    ) -> JSONResponse:
        if processor_name not in _request_catalog(request).derivations:
            return _error_response(422, "processor_kind", "only derive processors can be run")
        body = await _validated_json(request, ManualDerivationRequest)
        try:
            from memseek.derive.runner import enqueue_derivation_job

            job_id, coalesced, run_after = await enqueue_derivation_job(
                request.app.state.pool,
                workspace=workspace,
                derivation=processor_name,
                entity=body.entity,
                run_after=body.run_after,
            )
        except Exception as exc:
            log_event(
                LOGGER,
                "error",
                "derive.enqueue_failed",
                workspace=workspace,
                derivation=processor_name,
                exception_type=type(exc).__name__,
            )
            return _error_response(500, "internal_error", "derive job enqueue failed")
        return JSONResponse(
            status_code=200,
            content={
                "job_id": str(job_id),
                "enqueued": True,
                "coalesced": coalesced,
                "run_after": run_after.isoformat(),
            },
        )

    @application.get("/jobs/{job_id}")
    async def read_job(
        request: Request, job_id: _JobId, workspace: _AuthenticatedWorkspace
    ) -> JSONResponse:
        try:
            from memseek.jobs import get_job_status

            status = await get_job_status(
                request.app.state.pool,
                workspace=workspace,
                job_id=job_id,
            )
        except Exception as exc:
            from memseek.jobs import JobNotFound

            if isinstance(exc, JobNotFound):
                return _error_response(404, "job_not_found", str(exc))
            return _read_failure(exc, "jobs.status_failed", workspace)
        return JSONResponse(status_code=200, content=status)

    @application.post("/jobs/{job_id}/retry")
    async def retry_job(
        request: Request, job_id: _JobId, workspace: _AuthenticatedWorkspace
    ) -> JSONResponse:
        try:
            from memseek.jobs import retry_dead_job

            status = await retry_dead_job(
                request.app.state.pool,
                workspace=workspace,
                job_id=job_id,
            )
        except Exception as exc:
            from memseek.jobs import JobNotFound, JobRetryConflict

            if isinstance(exc, JobNotFound):
                return _error_response(404, "job_not_found", str(exc))
            if isinstance(exc, JobRetryConflict):
                return _error_response(409, "job_retry_conflict", str(exc))
            return _read_failure(exc, "jobs.retry_failed", workspace)
        return JSONResponse(status_code=200, content=status)

    @application.post("/backfill")
    async def create_backfill(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        body: _BackfillBody,
    ) -> JSONResponse:
        """Apply one processor to records that already exist in a collection version."""

        from memseek.backfill import BackfillError, request_backfill

        try:
            handle = await request_backfill(
                request.app.state.pool,
                workspace=workspace,
                collection=body.collection,
                version=body.version,
                processor=body.processor,
                catalog=_request_catalog(request),
                max_rows=body.max_rows,
            )
        except BackfillError as exc:
            return _error_response(exc.status, exc.code, exc.detail)
        except Exception as exc:
            return _read_failure(exc, "backfill.request_failed", workspace)
        return JSONResponse(status_code=202, content=handle.as_json())

    @application.get("/backfill")
    async def read_backfills(request: Request, workspace: _AuthenticatedWorkspace) -> JSONResponse:
        from memseek.backfill import list_backfills

        try:
            handles = await list_backfills(request.app.state.pool, workspace=workspace)
        except Exception as exc:
            return _read_failure(exc, "backfill.list_failed", workspace)
        return JSONResponse(
            status_code=200,
            content={"backfills": [handle.as_json() for handle in handles]},
        )

    @application.get("/backfill/{backfill_id}")
    async def read_backfill(
        request: Request,
        backfill_id: _BackfillId,
        workspace: _AuthenticatedWorkspace,
    ) -> JSONResponse:
        from memseek.backfill import BackfillError, get_backfill

        try:
            handle = await get_backfill(
                request.app.state.pool, workspace=workspace, backfill_id=backfill_id
            )
        except BackfillError as exc:
            return _error_response(exc.status, exc.code, exc.detail)
        except Exception as exc:
            return _read_failure(exc, "backfill.read_failed", workspace)
        return JSONResponse(status_code=200, content=handle.as_json())

    @application.post("/backfill/{backfill_id}/cancel")
    async def stop_backfill(
        request: Request,
        backfill_id: _BackfillId,
        workspace: _AuthenticatedWorkspace,
    ) -> JSONResponse:
        """Stop a live backfill. Annotations already written are valid and kept."""

        from memseek.backfill import BackfillError, cancel_backfill

        try:
            handle = await cancel_backfill(
                request.app.state.pool, workspace=workspace, backfill_id=backfill_id
            )
        except BackfillError as exc:
            return _error_response(exc.status, exc.code, exc.detail)
        except Exception as exc:
            return _read_failure(exc, "backfill.cancel_failed", workspace)
        return JSONResponse(status_code=200, content=handle.as_json())

    @application.post("/derivations/{derivation_name}/rebind")
    async def rebind_derivation_cursor(
        request: Request,
        derivation_name: str,
        workspace: _AuthenticatedWorkspace,
        body: _CursorRebindBody,
    ) -> JSONResponse:
        """Repoint a changes cursor after a deliberate source-scope change."""

        from memseek.evolution import EvolutionError, rebind_cursor

        try:
            result = await rebind_cursor(
                request.app.state.pool,
                workspace=workspace,
                derivation=derivation_name,
                entity=body.entity,
                policy=body.policy,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except EvolutionError as exc:
            return _error_response(exc.status, exc.code, exc.detail)
        except Exception as exc:
            return _read_failure(exc, "derivations.rebind_failed", workspace)
        return JSONResponse(status_code=200, content=result.as_json())

    @application.post("/reindex")
    async def reindex_projections(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        body: _ReindexBody,
    ) -> JSONResponse:
        """Queue a bounded external-projection rebuild for this workspace."""

        from memseek.reindex import ReindexError, reindex

        try:
            result = await reindex(
                request.app.state.pool,
                workspace=workspace,
                settings=request.app.state.settings,
                catalog=_request_catalog(request),
                since_seq=body.since_seq,
                reset=body.reset,
                confirm=body.confirm,
            )
        except ReindexError as exc:
            return _error_response(422, "reindex_request", str(exc))
        except Exception as exc:
            log_event(
                LOGGER,
                "error",
                "reindex.failed",
                workspace=workspace,
                exception_type=type(exc).__name__,
            )
            return _error_response(500, "internal_error", "reindex planning failed")
        return JSONResponse(status_code=200, content=result.as_json())

    @application.post("/records")
    async def create_records(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        batch: _RecordBatchBody,
    ) -> JSONResponse:
        try:
            result = await insert_public_records(
                request.app.state.pool,
                workspace=workspace,
                request=batch,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except DedupeConflict as exc:
            return _error_response(409, exc.code, str(exc))
        except RecordValidationError as exc:
            status = 404 if exc.code == "workspace_not_found" else 422
            return _error_response(status, exc.code, str(exc))
        except Exception as exc:
            log_event(
                LOGGER,
                "error",
                "records.insert_failed",
                workspace=workspace,
                exception_type=type(exc).__name__,
            )
            return _error_response(500, "internal_error", "record insertion failed")
        return JSONResponse(status_code=200, content=result.model_dump(mode="json"))

    @application.get("/records/{record_id}")
    async def read_record(
        request: Request,
        record_id: _RecordId,
        workspace: _AuthenticatedWorkspace,
    ) -> JSONResponse:
        try:
            detail = await fetch_record(
                request.app.state.pool,
                workspace=workspace,
                record_id=record_id,
                settings=request.app.state.settings,
            )
        except Exception as exc:
            return _read_failure(exc, "reads.record_failed", workspace)
        if detail is None:
            return _error_response(404, "record_not_found", "record does not exist")
        return JSONResponse(status_code=200, content=detail)

    @application.get("/timeline")
    async def read_timeline(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        query: _TimelineParams,
    ) -> JSONResponse:
        try:
            timeline = await fetch_timeline(
                request.app.state.pool,
                workspace=workspace,
                query=query,
                settings=request.app.state.settings,
            )
        except Exception as exc:
            return _read_failure(exc, "reads.timeline_failed", workspace)
        return JSONResponse(status_code=200, content=timeline)

    @application.get("/entities")
    async def list_entities(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        query: _EntitiesParams,
    ) -> JSONResponse:
        try:
            entities = await fetch_entities(
                request.app.state.pool, workspace=workspace, query=query
            )
        except Exception as exc:
            return _read_failure(exc, "reads.entities_failed", workspace)
        return JSONResponse(status_code=200, content=entities)

    @application.get("/document")
    async def read_document(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        query: _DocumentParams,
    ) -> JSONResponse:
        try:
            document = await build_document(
                request.app.state.pool,
                workspace=workspace,
                query=query,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except DocumentTooLarge as exc:
            return _error_response(409, exc.code, exc.detail)
        except Exception as exc:
            return _read_failure(exc, "reads.document_failed", workspace)
        return JSONResponse(status_code=200, content=document)

    @application.get("/document/history")
    async def read_document_history(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        query: _HistoryParams,
    ) -> JSONResponse:
        try:
            history = await fetch_history(
                request.app.state.pool,
                workspace=workspace,
                query=query,
                settings=request.app.state.settings,
            )
        except Exception as exc:
            return _read_failure(exc, "reads.history_failed", workspace)
        return JSONResponse(status_code=200, content=history)

    @application.get("/delta")
    async def read_delta(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        query: _DeltaParams,
    ) -> JSONResponse:
        try:
            delta = await fetch_delta(
                request.app.state.pool,
                workspace=workspace,
                query=query,
                settings=request.app.state.settings,
            )
        except CursorScopeMismatch as exc:
            return _error_response(409, exc.code, exc.detail)
        except Exception as exc:
            return _read_failure(exc, "reads.delta_failed", workspace)
        return JSONResponse(status_code=200, content=delta)

    @application.post("/cursor")
    async def write_cursor(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        body: _CursorBody,
    ) -> JSONResponse:
        try:
            cursor = await upsert_cursor(
                request.app.state.pool,
                workspace=workspace,
                request=body,
            )
        except (CursorScopeMismatch, CursorRegression) as exc:
            return _error_response(409, exc.code, exc.detail)
        except Exception as exc:
            return _read_failure(exc, "reads.cursor_failed", workspace)
        return JSONResponse(status_code=200, content=cursor)

    @application.post("/search")
    async def search(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        spec: _SearchBody,
    ) -> JSONResponse:
        return await _run_search(request, workspace, spec)

    @application.get("/search")
    async def search_sugar(request: Request, workspace: _AuthenticatedWorkspace) -> JSONResponse:
        params = dict(request.query_params)
        query = params.pop("q", "").strip()
        if not query:
            return _error_response(422, "request_schema", "q is required and must be non-blank")
        entity = params.pop("entity", None)
        collection = params.pop("collection", None)
        k_raw = params.pop("k", "20")
        if params:
            return _error_response(
                422, "request_schema", f"unknown parameter(s): {', '.join(sorted(params))}"
            )
        try:
            spec = SearchSpec.model_validate(
                {
                    "q": query,
                    "mode": "hybrid",
                    "scope": {
                        "entities": [entity] if entity else [],
                        "collections": [collection] if collection else [],
                    },
                    "k": int(k_raw),
                    "include": ["text", "collection", "type", "occurred_at"],
                    "render": True,
                }
            )
        except (ValueError, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                return _error_response(422, "request_schema", _validation_detail(exc))
            return _error_response(422, "request_schema", "k must be an integer")
        return await _run_search(request, workspace, spec)

    @application.post("/answer")
    async def answer(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        body: _AnswerBody,
    ) -> JSONResponse:
        try:
            result = await answer_question(
                request.app.state.pool,
                workspace=workspace,
                request=body,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except AnswerError as exc:
            return _error_response(exc.status, exc.code, exc.detail)
        except Exception as exc:
            log_event(
                LOGGER,
                "error",
                "answer.failed",
                workspace=workspace,
                exception_type=type(exc).__name__,
            )
            return _error_response(500, "internal_error", "answer generation failed")
        return _bounded_json(result, request.app.state.settings)

    async def _run_search(request: Request, workspace: str, spec: SearchSpec) -> JSONResponse:
        try:
            result = await execute_search(
                request.app.state.pool,
                workspace=workspace,
                spec=spec,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except SearchRequestError as exc:
            return _error_response(422, exc.code, exc.detail)
        except SearchUnavailableError as exc:
            return _error_response(503, exc.code, exc.detail)
        except Exception as exc:
            return _read_failure(exc, "search.failed", workspace)
        return _bounded_json(result, request.app.state.settings)

    @application.get("/views")
    async def list_views(request: Request, _workspace: _AuthenticatedWorkspace) -> JSONResponse:
        return JSONResponse(
            status_code=200,
            content=view_catalog_payload(_request_catalog(request)),
        )

    @application.post("/views/{view_name}/query")
    async def query_view(
        request: Request,
        view_name: str,
        workspace: _AuthenticatedWorkspace,
        parameters: _ViewParameters,
    ) -> JSONResponse:
        try:
            result = await execute_view(
                request.app.state.pool,
                workspace=workspace,
                name=view_name,
                parameters=parameters,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except ViewNotFound as exc:
            return _error_response(404, exc.code, exc.detail)
        except SearchRequestError as exc:
            return _error_response(422, exc.code, exc.detail)
        except SearchUnavailableError as exc:
            return _error_response(503, exc.code, exc.detail)
        except Exception as exc:
            return _read_failure(exc, "views.query_failed", workspace)
        return _bounded_json(result, request.app.state.settings)

    @application.get("/rank/schema")
    async def rank_schema(request: Request, _workspace: _AuthenticatedWorkspace) -> JSONResponse:
        return JSONResponse(
            status_code=200,
            content=rank_schema_payload(_request_catalog(request), request.app.state.settings),
        )

    @application.get("/runs")
    async def list_runs(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        query: _RunsParams,
    ) -> JSONResponse:
        try:
            runs = await fetch_runs(
                request.app.state.pool,
                workspace=workspace,
                query=query,
                settings=request.app.state.settings,
            )
        except Exception as exc:
            return _read_failure(exc, "reads.runs_failed", workspace)
        return JSONResponse(status_code=200, content=runs)

    @application.get("/runs/{run_id}")
    async def read_run(
        request: Request,
        run_id: _RunId,
        workspace: _AuthenticatedWorkspace,
        query: _RunOutputsParams,
    ) -> JSONResponse:
        try:
            run = await fetch_run(
                request.app.state.pool,
                workspace=workspace,
                run_id=run_id,
                query=query,
                settings=request.app.state.settings,
            )
        except RunNotFound as exc:
            return _error_response(404, exc.code, exc.detail)
        except Exception as exc:
            return _read_failure(exc, "reads.run_failed", workspace)
        return JSONResponse(status_code=200, content=run)

    @application.get("/context")
    async def read_context(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        query: _ContextParams,
    ) -> JSONResponse:
        try:
            context = await build_context(
                request.app.state.pool,
                workspace=workspace,
                query=query,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except ContextRequestError as exc:
            return _error_response(422, exc.code, exc.detail)
        except CursorScopeMismatch as exc:
            return _error_response(409, exc.code, exc.detail)
        except SearchRequestError as exc:
            return _error_response(422, exc.code, exc.detail)
        except SearchUnavailableError as exc:
            return _error_response(503, exc.code, exc.detail)
        except Exception as exc:
            return _read_failure(exc, "reads.context_failed", workspace)
        return _bounded_json(context, request.app.state.settings)

    @application.get("/collections")
    async def list_collections(
        request: Request, _workspace: _AuthenticatedWorkspace
    ) -> JSONResponse:
        from memseek.catalog_views import collections_payload

        return JSONResponse(status_code=200, content=collections_payload(_request_catalog(request)))

    @application.get("/processors")
    async def list_processors(
        request: Request, _workspace: _AuthenticatedWorkspace
    ) -> JSONResponse:
        from memseek.catalog_views import processors_payload

        return JSONResponse(status_code=200, content=processors_payload(_request_catalog(request)))

    @application.get("/triggers")
    async def list_triggers(request: Request, _workspace: _AuthenticatedWorkspace) -> JSONResponse:
        from memseek.catalog_views import triggers_payload

        return JSONResponse(status_code=200, content=triggers_payload(_request_catalog(request)))

    @application.get("/tools")
    async def list_tools(request: Request, workspace: _AuthenticatedWorkspace) -> JSONResponse:
        from memseek.tools import tool_definitions_payload

        registry: WorkspaceCatalogRegistry = request.app.state.catalog_registry
        return JSONResponse(
            status_code=200,
            content=tool_definitions_payload(
                request.app.state.settings,
                catalog=_request_catalog(request),
                package=await registry.selected_package(
                    workspace,
                    catalog=_request_catalog(request),
                ),
            ),
        )

    @application.get("/artifacts")
    async def list_artifacts(request: Request, _workspace: _AuthenticatedWorkspace) -> JSONResponse:
        from memseek.artifacts import artifact_catalog_payload

        return JSONResponse(
            status_code=200, content=artifact_catalog_payload(_request_catalog(request))
        )

    @application.post("/artifacts/{artifact_name}/render")
    async def render_artifact_route(
        request: Request,
        artifact_name: str,
        workspace: _AuthenticatedWorkspace,
        parameters: _ArtifactParameters,
    ) -> JSONResponse:
        from memseek.artifacts import render_artifact

        try:
            result = await render_artifact(
                request.app.state.pool,
                workspace=workspace,
                name=artifact_name,
                parameters=parameters,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except Exception as exc:
            return _artifact_failure(exc, "artifacts.render_failed", workspace)
        return _bounded_json(result, request.app.state.settings)

    @application.post("/artifacts/{artifact_name}/snapshot")
    async def snapshot_artifact_route(
        request: Request,
        artifact_name: str,
        workspace: _AuthenticatedWorkspace,
        parameters: _ArtifactParameters,
    ) -> JSONResponse:
        from memseek.artifacts import snapshot_artifact

        try:
            result = await snapshot_artifact(
                request.app.state.pool,
                workspace=workspace,
                name=artifact_name,
                parameters=parameters,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except Exception as exc:
            return _artifact_failure(exc, "artifacts.snapshot_failed", workspace)
        return _bounded_json(result, request.app.state.settings)

    @application.post("/artifacts/{artifact_name}/uses")
    async def bind_artifact_use_route(
        request: Request,
        artifact_name: str,
        workspace: _AuthenticatedWorkspace,
        body: _ArtifactUseBody,
    ) -> JSONResponse:
        """Render one artifact and register the correlation handle for its use."""

        from memseek.artifact_uses import bind_artifact_use

        try:
            result = await bind_artifact_use(
                request.app.state.pool,
                workspace=workspace,
                name=artifact_name,
                request=body,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except Exception as exc:
            return _artifact_use_failure(exc, "artifact_uses.bind_failed", workspace)
        return _bounded_json(result, request.app.state.settings)

    @application.get("/artifact-uses/{use_id}")
    async def read_artifact_use_route(
        request: Request,
        use_id: _ArtifactUseId,
        workspace: _AuthenticatedWorkspace,
    ) -> JSONResponse:
        """Return use metadata only; never a render and never an external trace."""

        from memseek.artifact_uses import read_artifact_use

        try:
            result = await read_artifact_use(
                request.app.state.pool,
                workspace=workspace,
                use_id=use_id,
            )
        except Exception as exc:
            return _artifact_use_failure(exc, "artifact_uses.read_failed", workspace)
        return JSONResponse(status_code=200, content=result)

    @application.post("/artifact-uses/{use_id}/feedback")
    async def submit_feedback_route(
        request: Request,
        use_id: _ArtifactUseId,
        workspace: _AuthenticatedWorkspace,
        body: _FeedbackBody,
    ) -> JSONResponse:
        """Record one selected outcome as an ordinary learning-signal record."""

        from memseek.artifact_uses import submit_feedback

        try:
            result = await submit_feedback(
                request.app.state.pool,
                workspace=workspace,
                use_id=use_id,
                request=body,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except Exception as exc:
            return _artifact_use_failure(exc, "artifact_uses.feedback_failed", workspace)
        return _bounded_json(result, request.app.state.settings)

    @application.get("/artifacts/{artifact_name}")
    async def read_artifact_route(
        request: Request,
        artifact_name: str,
        workspace: _AuthenticatedWorkspace,
    ) -> JSONResponse:
        from memseek.artifacts import read_artifact_snapshot

        try:
            result = await read_artifact_snapshot(
                request.app.state.pool,
                workspace=workspace,
                name=artifact_name,
                parameters=dict(request.query_params),
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except Exception as exc:
            return _artifact_failure(exc, "artifacts.read_failed", workspace)
        return _bounded_json(result, request.app.state.settings)

    @application.post("/promote")
    async def promote(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        body: _PromotionBody,
    ) -> JSONResponse:
        from memseek.promote import PromotionError, promote_run

        try:
            result = await promote_run(
                request.app.state.pool,
                workspace=workspace,
                entity=body.entity,
                source_run_id=body.source_run_id,
                artifact=body.artifact,
                catalog=_request_catalog(request),
                settings=request.app.state.settings,
            )
        except PromotionError as exc:
            return _error_response(exc.status, exc.code, exc.detail)
        except Exception as exc:
            log_event(
                LOGGER,
                "error",
                "promote.failed",
                workspace=workspace,
                exception_type=type(exc).__name__,
            )
            return _error_response(500, "internal_error", "promotion failed")
        return JSONResponse(status_code=200, content=result)

    @application.post("/erase")
    async def erase_records(
        request: Request,
        workspace: _AuthenticatedWorkspace,
        body: _ErasureBody,
    ) -> JSONResponse:
        try:
            result = await erase(
                request.app.state.pool,
                workspace=workspace,
                request=body,
                settings=request.app.state.settings,
                catalog=_request_catalog(request),
            )
        except ErasureError as exc:
            return _error_response(exc.status, exc.code, exc.detail)
        except Exception as exc:
            log_event(
                LOGGER,
                "error",
                "erase.failed",
                workspace=workspace,
                exception_type=type(exc).__name__,
            )
            return _error_response(500, "internal_error", "erasure failed")
        return JSONResponse(status_code=200, content=result.as_json())

    @application.exception_handler(WorkspaceCatalogError)
    async def _handle_catalog_error(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, WorkspaceCatalogError)
        log_event(
            LOGGER,
            "error",
            "catalog.resolution_failed",
            code=exc.code,
            status=exc.status,
        )
        return _catalog_error_response(exc)

    def _request_catalog(request: Request) -> DefinitionCatalog:
        catalog = getattr(request.state, "catalog", None)
        if catalog is not None:
            return catalog
        # Deferred from authentication: this workspace has published nothing and
        # the service offers no catalog of its own, so a route that needs
        # definitions has none and says exactly that.
        error = getattr(request.state, "catalog_error", None)
        if error is not None:
            raise error
        return request.app.state.catalog

    return application


def _error_response(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": code, "detail": detail})


def _catalog_error_response(exc: WorkspaceCatalogError) -> JSONResponse:
    """Render a catalog failure, including its compatibility report when present.

    A refused publish returns the same structure a preflight would have, so the
    conflict names every blocker and the row counts behind it.
    """

    content: dict[str, Any] = {"error": exc.code, "detail": exc.detail}
    if exc.report is not None:
        content["compatibility"] = exc.report.as_json()
    return JSONResponse(status_code=exc.status, content=content)


def _bounded_json(content: dict[str, Any], settings: Settings) -> JSONResponse:
    from memseek.views.shared import json_size

    if json_size(content) > settings.max_response_bytes:
        return _error_response(
            409,
            "response_too_large",
            "response exceeds MAX_RESPONSE_BYTES; reduce k, include, fields, or annotations",
        )
    return JSONResponse(status_code=200, content=content)


def _artifact_failure(exc: Exception, event: str, workspace: str) -> JSONResponse:
    from memseek.artifacts import ArtifactNotFound, ArtifactRequestError
    from memseek.search.engine import SearchRequestError, SearchUnavailableError

    if isinstance(exc, ArtifactNotFound):
        return _error_response(404, exc.code, exc.detail)
    if isinstance(exc, ArtifactRequestError):
        return _error_response(exc.status, exc.code, exc.detail)
    if isinstance(exc, SearchRequestError):
        return _error_response(422, exc.code, exc.detail)
    if isinstance(exc, SearchUnavailableError):
        return _error_response(503, exc.code, exc.detail)
    return _read_failure(exc, event, workspace)


def _artifact_use_failure(exc: Exception, event: str, workspace: str) -> JSONResponse:
    from memseek.artifact_uses import ArtifactUseError, ArtifactUseNotFound

    if isinstance(exc, ArtifactUseNotFound):
        return _error_response(exc.status, exc.code, exc.detail)
    if isinstance(exc, ArtifactUseError):
        return _error_response(exc.status, exc.code, exc.detail)
    if isinstance(exc, DedupeConflict):
        return _error_response(409, exc.code, exc.detail)
    if isinstance(exc, RecordValidationError):
        return _error_response(422, exc.code, exc.detail)
    return _artifact_failure(exc, event, workspace)


def _read_failure(exc: Exception, event: str, workspace: str) -> JSONResponse:
    if isinstance(exc, ResponseTooLarge):
        return _error_response(409, exc.code, exc.detail)
    log_event(
        LOGGER,
        "error",
        event,
        workspace=workspace,
        exception_type=type(exc).__name__,
    )
    return _error_response(500, "internal_error", "canonical read failed")


app = create_app()
