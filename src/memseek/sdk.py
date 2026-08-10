"""Small async HTTP client for the workspace-scoped Memseek service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx


class MemseekHTTPError(RuntimeError):
    """An API response outside the successful 2xx range."""

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        self.payload = payload
        super().__init__(f"Memseek request failed with HTTP {response.status_code}: {payload}")


class _CatalogClient:
    def __init__(self, client: MemseekClient) -> None:
        self._client = client

    async def publish(
        self,
        *,
        package: str,
        directory: str | Path,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Publish every YAML definition below a catalog directory.

        With ``dry_run=True`` nothing is installed and the compatibility report is
        returned instead — the same report the publish would have acted on.
        """

        files = await asyncio.to_thread(_read_catalog_directory, Path(directory))
        return await self.publish_files(package=package, files=files, dry_run=dry_run)

    async def publish_files(
        self,
        *,
        package: str,
        files: Mapping[str, str],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Publish an in-memory catalog assembled by an application."""

        return await self._client._request(
            "POST",
            "/catalog",
            params={"dry_run": "true"} if dry_run else None,
            json={"package": package, "files": files},
        )

    async def check(
        self,
        *,
        package: str,
        directory: str | Path,
    ) -> dict[str, Any]:
        """Preflight a directory publish and return its compatibility report."""

        return await self.publish(package=package, directory=directory, dry_run=True)

    async def retrieve(self) -> dict[str, Any]:
        """Return the selected package identity and catalog hash."""

        return await self._client._request("GET", "/catalog")

    async def compatibility(self) -> dict[str, Any]:
        """Return the installed catalog's standing against its own records."""

        return await self._client._request("GET", "/catalog/compatibility")

    async def prune(self) -> dict[str, Any]:
        """Report which inactive definitions nothing in the workspace references.

        Read-only: it counts real records, annotations, and runs against every
        definition that is not the active choice, so retiring one is a decision
        made against evidence rather than a guess.
        """

        return await self._client._request("GET", "/catalog/prune")


def _read_catalog_directory(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise ValueError(f"catalog directory does not exist: {root}")
    paths = sorted(
        (path for pattern in ("*.yaml", "*.yml") for path in root.rglob(pattern)),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not paths:
        raise ValueError(f"catalog directory contains no YAML files: {root}")
    return {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8") for path in paths}


class _BackfillClient:
    """Apply a processor to records that already exist, and watch it run."""

    def __init__(self, client: MemseekClient) -> None:
        self._client = client

    async def start(
        self,
        *,
        collection: str,
        version: int,
        processor: str,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        """Register a backfill and return its handle immediately."""

        body: dict[str, Any] = {
            "collection": collection,
            "version": version,
            "processor": processor,
        }
        if max_rows is not None:
            body["max_rows"] = max_rows
        return await self._client._request("POST", "/backfill", json=body)

    async def retrieve(self, backfill_id: str) -> dict[str, Any]:
        """Return one backfill's state, cursor, and counters."""

        return await self._client._request("GET", f"/backfill/{backfill_id}")

    async def list(self) -> dict[str, Any]:
        """Return this workspace's most recent backfills, newest first."""

        return await self._client._request("GET", "/backfill")

    async def cancel(self, backfill_id: str) -> dict[str, Any]:
        """Stop a live backfill. Annotations already written are kept."""

        return await self._client._request("POST", f"/backfill/{backfill_id}/cancel")


class _RecordsClient:
    def __init__(self, client: MemseekClient) -> None:
        self._client = client

    async def ingest(self, **record: Any) -> dict[str, Any]:
        """Insert one durable record using the public batch endpoint."""

        return await self._client._request("POST", "/records", json={"records": [record]})

    async def ingest_many(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Atomically insert a bounded batch of durable records."""

        return await self._client._request(
            "POST",
            "/records",
            json={"records": [dict(record) for record in records]},
        )


@dataclass(frozen=True, slots=True)
class BoundArtifact:
    """One rendered artifact plus the handle a later outcome can name.

    ``content`` is the rendered text to pass to whatever SDK executes the run.
    ``id`` is the one short field the application stores next to its own result,
    the way it already stores a payment-intent or job ID.  Nothing here inspects
    or requires a model response.
    """

    id: str
    content: str
    artifact: Mapping[str, Any]
    render_sha256: str
    telemetry_attributes: Mapping[str, str | int]
    snapshot_id: str | None = None
    learning_target: Mapping[str, Any] | None = None
    expires_at: str | None = None
    truncated: bool = False

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> BoundArtifact:
        render = payload.get("render") or {}
        return cls(
            id=str(payload["id"]),
            content=str(payload["content"]),
            artifact=dict(payload.get("artifact") or {}),
            render_sha256=str(payload["render_sha256"]),
            telemetry_attributes=dict(payload.get("telemetry") or {}),
            snapshot_id=payload.get("snapshot_id"),
            learning_target=payload.get("learning_target"),
            expires_at=payload.get("expires_at"),
            truncated=bool(render.get("truncated", False)),
        )


class ArtifactHandle:
    """A reusable reference to one named artifact."""

    def __init__(self, client: MemseekClient, name: str) -> None:
        self._client = client
        self._name = name

    async def render(self, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Render without registering a use, for inspection and testing."""

        return await self._client._request(
            "POST", f"/artifacts/{self._name}/render", json=dict(parameters or {})
        )

    async def bind(
        self,
        parameters: Mapping[str, Any] | None = None,
        *,
        snapshot: bool = False,
    ) -> BoundArtifact:
        """Render, resolve the learning target, and register the artifact use.

        This does not touch ambient telemetry state; attach
        ``telemetry_attributes`` wherever the caller's tracing already lives.
        """

        payload = await self._client._request(
            "POST",
            f"/artifacts/{self._name}/uses",
            json={"parameters": dict(parameters or {}), "snapshot": snapshot},
        )
        return BoundArtifact.from_payload(payload)

    @asynccontextmanager
    async def use(
        self,
        parameters: Mapping[str, Any] | None = None,
        *,
        snapshot: bool = False,
    ) -> AsyncIterator[BoundArtifact]:
        """Bind, keep the correlation attributes active, then restore context.

        The span exists only to carry bounded identities. This never inspects the
        wrapped SDK's request or response, so it stays correct for any provider.
        """

        bound = await self.bind(parameters, snapshot=snapshot)
        with _telemetry_scope(self._name, bound):
            yield bound


def _telemetry_scope(name: str, bound: BoundArtifact) -> Any:
    """Enter an OpenTelemetry span when the optional extra is installed."""

    try:
        from opentelemetry import trace
    except ImportError:
        return nullcontext()
    tracer = trace.get_tracer("memseek")
    return tracer.start_as_current_span(
        f"memseek.artifact.use {name}",
        attributes=dict(bound.telemetry_attributes),
    )


class _FeedbackForUse:
    """A fluent convenience surface bound to one artifact use."""

    def __init__(self, client: _FeedbackClient, use_id: str) -> None:
        self._client = client
        self._use_id = use_id

    async def thumbs_up(self, **fields: Any) -> dict[str, Any]:
        return await self._client.submit(
            use_id=self._use_id, kind="thumbs_up", source="end_user", **fields
        )

    async def thumbs_down(self, **fields: Any) -> dict[str, Any]:
        return await self._client.submit(
            use_id=self._use_id, kind="thumbs_down", source="end_user", **fields
        )

    async def correction(self, *, expected: str, **fields: Any) -> dict[str, Any]:
        return await self._client.submit(
            use_id=self._use_id,
            kind="correction",
            source=fields.pop("source", "operator"),
            expected=expected,
            **fields,
        )

    async def evaluation(self, *, score: float, **fields: Any) -> dict[str, Any]:
        return await self._client.submit(
            use_id=self._use_id,
            kind="evaluation",
            source=fields.pop("source", "evaluator"),
            score=score,
            **fields,
        )


class _FeedbackClient:
    """Submit selected outcomes against a previously registered use."""

    def __init__(self, client: MemseekClient) -> None:
        self._client = client

    def for_use(self, use_id: str) -> _FeedbackForUse:
        return _FeedbackForUse(self, use_id)

    async def submit(
        self,
        *,
        use_id: str,
        kind: str,
        source: str,
        score: float | None = None,
        label: str | None = None,
        comment: str | None = None,
        expected: str | None = None,
        actual_excerpt: str | None = None,
        dedupe_key: str | None = None,
        execution_refs: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Create one ordinary learning-signal record from a delayed outcome.

        The only thing the caller must have kept is ``use_id``; everything else
        describes the outcome itself.
        """

        payload: dict[str, Any] = {"kind": kind, "source": source}
        optional = {
            "score": score,
            "label": label,
            "comment": comment,
            "expected": expected,
            "actual_excerpt": actual_excerpt,
            "dedupe_key": dedupe_key,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        if execution_refs:
            payload["execution_refs"] = [dict(ref) for ref in execution_refs]
        return await self._client._request(
            "POST", f"/artifact-uses/{use_id}/feedback", json=payload
        )


class MemseekClient:
    """Minimal async SDK covering catalog publication, ingest, document, and search."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"))
        self._owns_client = client is None
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self.catalog = _CatalogClient(self)
        self.records = _RecordsClient(self)
        self.feedback = _FeedbackClient(self)
        self.backfill = _BackfillClient(self)

    def artifact(self, name: str) -> ArtifactHandle:
        """A reusable handle for rendering and binding one named artifact."""

        return ArtifactHandle(self, name)

    async def artifact_use(self, use_id: str) -> dict[str, Any]:
        """Read one registered use's metadata."""

        return await self._request("GET", f"/artifact-uses/{use_id}")

    async def __aenter__(self) -> MemseekClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(
        self,
        *,
        query: str,
        collections: Sequence[str] = (),
        entity: str | None = None,
        mode: str = "hybrid",
        k: int = 20,
        **options: Any,
    ) -> dict[str, Any]:
        """Run a small hybrid search request against the selected catalog."""

        payload: dict[str, Any] = {
            "q": query,
            "mode": mode,
            "scope": {"collections": list(collections), "entities": [entity] if entity else []},
            "k": k,
            "include": options.pop("include", ["text", "collection", "entity"]),
            "render": options.pop("render", True),
            **options,
        }
        return await self._request("POST", "/search", json=payload)

    async def views(self) -> dict[str, Any]:
        """List the catalog's versioned read-only view contracts."""

        return await self._request("GET", "/views")

    async def query_view(self, name: str, **parameters: Any) -> dict[str, Any]:
        """Execute a named view, including graph-derived views."""

        return await self._request("POST", f"/views/{name}/query", json=parameters)

    async def answer(
        self,
        *,
        question: str,
        entities: Sequence[str] = (),
        anchor: str | None = None,
        graph: str | None = None,
        since: str | None = None,
        until: str | None = None,
        rewrite: bool = False,
        save: bool = False,
    ) -> dict[str, Any]:
        """Produce one bounded cited synthesis from answerable workspace records.

        ``entities`` narrows the synthesis to one or more memory scopes. Omitting
        it answers over every entity in the answerable collections, which is only
        what you want when the workspace holds a single corpus.
        """

        payload: dict[str, Any] = {"question": question, "rewrite": rewrite, "save": save}
        if entities:
            payload["entities"] = list(entities)
        if anchor is not None:
            payload["anchor"] = anchor
        if graph is not None:
            payload["graph"] = graph
        if since is not None:
            payload["since"] = since
        if until is not None:
            payload["until"] = until
        return await self._request("POST", "/answer", json=payload)

    async def document(self, *, entity: str, **parameters: Any) -> dict[str, Any]:
        """Read current state and freshness for one entity."""

        return await self._request(
            "GET",
            "/document",
            params={"entity": entity, **parameters},
        )

    async def record(self, record_id: str) -> dict[str, Any]:
        """Dereference one full canonical record by id.

        This is what turns a belief's citation id into the concrete event it
        was derived from: its text, source, occurred_at, provenance, and the
        run that wrote it.
        """

        return await self._request("GET", f"/records/{record_id}")

    async def document_history(
        self, *, entity: str, collection: str, key: str, **parameters: Any
    ) -> dict[str, Any]:
        """Every version of one collection-scoped keyed belief, newest first.

        This is the audit view of a belief: each version carries the run that
        wrote it and the evidence it cited, so the full evolution of a belief —
        and exactly why it changed at each step — is reconstructable.
        """

        return await self._request(
            "GET",
            "/document/history",
            params={"entity": entity, "collection": collection, "key": key, **parameters},
        )

    async def runs(
        self,
        *,
        entity: str,
        processor: str | None = None,
        operation: str | None = None,
        source: Literal["changes", "snapshot"] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List audited runs for one entity."""

        params: dict[str, Any] = {"entity": entity, "limit": limit}
        if processor is not None:
            params["processor"] = processor
        if operation is not None:
            params["operation"] = operation
        if source is not None:
            params["source"] = source
        return await self._request("GET", "/runs", params=params)

    async def run_processor(self, name: str, *, entity: str) -> dict[str, Any]:
        """Enqueue one entity-scoped changes or snapshot pipeline."""

        return await self._request("POST", f"/processors/{name}/run", json={"entity": entity})

    async def job(self, job_id: str) -> dict[str, Any]:
        """Read one content-free job status projection."""

        return await self._request("GET", f"/jobs/{job_id}")

    async def run(self, run_id: str) -> dict[str, Any]:
        """Read one audited run and its ordered emitted records."""

        return await self._request("GET", f"/runs/{run_id}")

    async def promote(
        self,
        *,
        entity: str,
        source_run_id: str,
        artifact: str | None = None,
    ) -> dict[str, Any]:
        """Atomically activate one non-stale reviewed emission."""

        payload: dict[str, Any] = {"entity": entity, "source_run_id": source_run_id}
        if artifact is not None:
            payload["artifact"] = artifact
        return await self._request("POST", "/promote", json=payload)

    async def rebind_cursor(
        self,
        derivation: str,
        *,
        entity: str,
        policy: Literal["reset", "carry"],
    ) -> dict[str, Any]:
        """Repoint one `changes` cursor after a deliberate source-scope change.

        ``reset`` re-reads the widened scope from the beginning; ``carry`` keeps
        the watermark and adopts the new scope. Both write an audit row naming
        the old and new source hashes.
        """

        return await self._request(
            "POST",
            f"/derivations/{derivation}/rebind",
            json={"entity": entity, "policy": policy},
        )

    async def reindex(
        self,
        *,
        since_seq: int | None = None,
        reset: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Queue a projection rebuild so an external index adopts new attributes.

        Canonical PostgreSQL is never rewritten by this: it enqueues ordinary
        projection jobs the worker drains. Pass exactly one of ``since_seq`` (the
        sequence to resume from) or ``reset=True`` (every ready record), and
        ``confirm=True`` alongside a reset outside a test database.
        """

        payload: dict[str, Any] = {"reset": reset, "confirm": confirm}
        if since_seq is not None:
            payload["since_seq"] = since_seq
        return await self._request("POST", "/reindex", json=payload)

    async def erase(
        self,
        *,
        entity: str | None = None,
        record_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Erase one entity, or explicit records, with its provenance closure.

        Exactly one selector. Erasure expands the bounded provenance closure, so
        erasing an original also removes what was derived from it. It cannot be
        undone through the API.
        """

        payload: dict[str, Any] = {}
        if entity is not None:
            payload["entity"] = entity
        if record_ids:
            payload["record_ids"] = list(record_ids)
        return await self._request("POST", "/erase", json=payload)

    async def render_artifact(self, name: str, **parameters: Any) -> dict[str, Any]:
        """Render a live artifact from canonical workspace state."""

        return await self._request("POST", f"/artifacts/{name}/render", json=parameters)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(self._headers)
        headers.update(kwargs.pop("headers", {}))
        response = await self._client.request(method, path, headers=headers, **kwargs)
        if response.is_error:
            raise MemseekHTTPError(response)
        value = response.json()
        if not isinstance(value, dict):
            raise MemseekHTTPError(response)
        return value


__all__ = ["ArtifactHandle", "BoundArtifact", "MemseekClient", "MemseekHTTPError"]
