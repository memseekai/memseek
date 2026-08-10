---
title: Operations
eyebrow: Run, inspect, and repair
---

Use this page after the service is running. It is for operators evolving a
workspace, running maintenance, or diagnosing worker behavior—not for normal
application reads and writes. Start with [Getting started](getting-started.md)
and the [HTTP API guide](api-surface.md) for those paths. The
[Glossary](glossary.md) defines operational terms such as job, run, backfill,
projection, and erasure.

## Runtime settings

The main settings groups are:

| Area | Important settings |
| --- | --- |
| Database | `DATABASE_URL`, pool sizes, migration settings |
| LLM | provider API keys, `LLM_FAKE`, concurrency, context/prompt/output limits |
| Catalog | `COLLECTIONS_DIR`, `DERIVATIONS_DIR`, `TRIGGERS_DIR`, `VIEWS_DIR`, `ARTIFACTS_DIR`, `PACKAGES_DIR` and the `*_FILE` paths under `conf/` |
| Search | `SEARCH_BACKEND`, profile overrides, Turbopuffer credentials/layout/consistency, candidate and concurrency limits |
| Derivation | batch sizes, text/content limits, maximum depth, artifact/run limits |
| Contradiction detection | `derivations/contradiction.yaml` and `collections/relations.yaml` — see [Contradiction detection](contradiction-detection.md) |
| Artifact uses & feedback | `ARTIFACT_USE_RETENTION_DAYS`, `ARTIFACT_USE_PURGE_BATCH`, `MAX_FEEDBACK_COMMENT_CHARS`, `MAX_FEEDBACK_EVIDENCE_CHARS` — see [Artifact uses & feedback](artifact-uses.md) |
| Safety | workspace auth, `API_CORS_ORIGINS`, MCP Origin validation, workspace locks, erasure and projection settings |

Use `.env.example` and `src/memseek/config.py` as the authoritative environment-name reference for the current release. Secrets belong in process configuration, not catalog YAML.

### Remote MCP endpoint

The API process serves authenticated Streamable HTTP at `/mcp`; it needs no
separate MCP daemon. A reverse proxy must preserve the bearer and MCP routing
headers, allow 180-second calls, and avoid buffering request-scoped SSE. The
current protocol is stateless, so replicas do not need sticky sessions. See
[MCP](mcp.md#3-serve-remote-mcp-over-http) for the TLS, proxy, Origin, Claude
Code, and Codex configuration.

### Browser workspace explorer

The full-screen workspace explorer is a read-only browser client for the same
authenticated API. It reads catalog contracts, entity timelines and record
dereferences, derivation runs, named views, and artifact renders; it never
writes records, advances cursors, or queues work.

The browser passes the workspace bearer key directly to the API, so the API
must explicitly allow the explorer's origin. Set exact origins as a JSON array;
wildcards are rejected:

    # From the repository root, for the local marketing dev server.
    # The database must already be running and migrated.
    export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
    export LLM_FAKE=1
    export API_CORS_ORIGINS='["http://localhost:4321"]'
    uv run uvicorn memseek.api:app --host 127.0.0.1 --port 8000

    # Add every production console origin explicitly, for example:
    export API_CORS_ORIGINS='["https://console.example.com","https://memseek.pages.dev"]'

The CORS list is read at API startup: stop and restart Uvicorn after changing
it. The explorer keeps a supplied key only in browser memory, and its terminal
handoff links include the API URL but never the key.

## Real providers

Set `LLM_FAKE=0`, provide the OpenAI-compatible key/base URL, and choose model IDs in `conf/models.yaml` or the uploaded package:

Endpoints live in `conf/models.yaml`, so the only thing a deployment supplies
is each endpoint's credential, under the variable name that provider declared in
`api_key_env`:

```console
export OPENAI_API_KEY='replace-me'
```

```yaml
# conf/models.yaml
providers:
  openai:
    adapter: openai_compat
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
    token_limit_field: max_completion_tokens
    json_capability: json_schema
    json_schema_strict: false
```

Alias targets then read `openai:model-id`. The token-limit field can be
`max_completion_tokens` or `max_tokens` for compatible servers. Native
`json_schema` output is the default. Configure `json_object` or `none` on that
provider for an endpoint without the capability; a provider error never causes
an automatic downgrade. Declaring a second provider is how you point embeddings
at a different service — see [Embeddings](embeddings.md). The worker, not the API
process, makes model calls.

Keep `LLM_FAKE=1` for repeatable tests and local schema work. A real provider can produce different prose and scores; test the contracts and citations rather than exact model wording.

## Inspect the loaded graph

Authenticated read surfaces expose the normalized catalog without requiring access to source YAML:

```console
curl -sS http://127.0.0.1:8000/catalog -H "$MEMSEEK_AUTH"
curl -sS http://127.0.0.1:8000/collections -H "$MEMSEEK_AUTH"
curl -sS http://127.0.0.1:8000/processors -H "$MEMSEEK_AUTH"
curl -sS http://127.0.0.1:8000/triggers -H "$MEMSEEK_AUTH"
curl -sS http://127.0.0.1:8000/artifacts -H "$MEMSEEK_AUTH"
curl -sS http://127.0.0.1:8000/rank/schema -H "$MEMSEEK_AUTH"
```

One registered artifact use reads back the same way, for support and debugging:

```console
curl -sS "http://127.0.0.1:8000/artifact-uses/$USE_ID" -H "$MEMSEEK_AUTH"
```

That returns identities, hashes, the resolved learning target, and expiry — and
deliberately never a render, a prompt, a model response, or an external trace.
See [Artifact uses & feedback](artifact-uses.md).

`/collections`, `/processors`, and `/triggers` include semantic hashes and normalized bindings. This is useful for deployment audits and client tooling.

## Jobs and freshness

```console
curl -sS 'http://127.0.0.1:8000/document?entity=user-42' -H "$MEMSEEK_AUTH"
curl -sS 'http://127.0.0.1:8000/runs?entity=user-42&processor=profile&operation=derive' \
  -H "$MEMSEEK_AUTH"
curl -sS "http://127.0.0.1:8000/jobs/$JOB_ID" -H "$MEMSEEK_AUTH"
```

Document freshness reports derivation watermark, dirty input, unready barriers, last successful run, and queued/running/dead job information. Runs record model resolution, visible/cited inputs, output IDs, and status.

## Erasure and projection repair

Erasure expands a bounded provenance closure, holds off active derivation jobs, deletes the records, queues external index deletion, refreshes keyed predecessors, and writes a hash-only `_system/erasure` audit record:

```console
curl -sS -X POST http://127.0.0.1:8000/erase \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"entity":"user-42"}'
```

External search indexes are disposable copies. Rebuild one without touching stored records:

```console
uv run memseek reindex --workspace local --since-seq 100
uv run memseek reindex --workspace local --reset --yes
```

The same rebuild is a route, for a caller that holds a workspace key and no shell:

```console
curl -sS -X POST http://127.0.0.1:8000/reindex \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"since_seq":100}'
curl -sS -X POST http://127.0.0.1:8000/reindex \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"reset":true,"confirm":true}'
```

The worker processes index deletion and update jobs after the database transaction commits, so the index can never get ahead of the records.

### Evolving a live catalog

Six operator commands cover definition change. All of them work through ordinary
database transactions under the workspace lock, and none of them changes record content.
[Changing definitions](changing-definitions.md) has the full guide; this is the
operational summary.

```console
# What would publishing this catalog do to the workspace? (exit 1 if blocked)
uv run memseek catalog-check --workspace local --dir ./catalog --package acme@1.4.0

# Apply one processor to every record that already exists (no budget needed).
uv run memseek backfill --workspace local \
    --collection customer_events --version 1 --processor sentiment_v2

# ...or cap it, when you want a cost ceiling or a canary slice.
uv run memseek backfill --workspace local \
    --collection customer_events --version 1 --processor sentiment_v2 --max-rows 5000

# Change the embedding model: stage, then promote once coverage is complete.
uv run memseek reembed --workspace local --space default-v2
uv run memseek reembed --workspace local --space default-v2 --cutover

# Repoint a changes cursor after a deliberate source-scope change.
uv run memseek rebind-cursor --workspace local \
    --derivation profile --entity contact:avery-chen --policy reset

# Which inactive definitions does nothing reference any more?
uv run memseek catalog-prune --workspace local

# One-time: move records written before the record-contract identity forward.
uv run memseek migrate-collection-hashes --dry-run
uv run memseek migrate-collection-hashes --workspace local
```

Each prints one JSON object. `catalog-check` and `migrate-collection-hashes` exit
non-zero when the workspace is blocked or incomplete, so they compose in a
deployment pipeline.

These commands connect to the database directly and are scoped by `--workspace`,
which is what makes them operator tools. Every one of them except `reembed` and
`migrate-collection-hashes` is also a workspace-scoped route, so a tenant can run
its own evolution without shell access to the deployment:

| Command | Route | Client |
| --- | --- | --- |
| `catalog-check` | `POST /catalog?dry_run=true` | `catalog.check()` |
| `backfill` | `POST /backfill` | `backfill.start()` |
| `rebind-cursor` | `POST /derivations/{name}/rebind` | `rebind_cursor()` |
| `catalog-prune` | `GET /catalog/prune` | `catalog.prune()` |
| `reindex` | `POST /reindex` | `reindex()` |

[A migration, start to finish](migration-walkthrough.md) runs the whole sequence
through the client, with no command line at any step.

### Operating a backfill

A backfill is a job lane, so the worker must be running to drain it. **Omitting
`max_rows` is the normal case** — the backfill then reaches every eligible record
and needs no chunking or supervision. Two bounds apply:

| Bound | Default | Limits |
| --- | --- | --- |
| `max_rows` on the request | **unlimited** | records this backfill will ever **scan** |
| `BACKFILL_BATCH` | 200 | records per batch — and one batch is all a worker pass does |

`max_rows` bounds records *scanned*, which is the quantity that costs money — a
record that is scanned and then terminally fails still spent its provider call.
`scanned` and `annotated` on the handle differ by exactly the terminal failures, so
a widening gap between them means the processor is failing on real content; check
`GET /runs` for those records.

Provider calls are sub-batched by `ENRICH_LLM_BATCH` (default 16) for `llm`
processors and 64 for embeddings, so a 50,000-record backfill of an LLM processor
is roughly 3,000 completion calls. `BACKFILL_BATCH` does not change that.

`BACKFILL_BATCH` is the interleaving granularity, and it is what makes an
unbudgeted whole-corpus backfill safe: one pass runs exactly one batch, then
services every other lane before returning. Ingest enrichment runs before the
backfill lane, so improving history never delays admitting new records. A pass that
did backfill work is marked busy, so the next pass starts without a poll delay —
`worker.pass` logs `backfill_batches` and `backfilled_annotations` to make the rate
visible. Raise `BACKFILL_BATCH` for throughput on a quiet deployment; lower it to
tighten latency for the other lanes.

Reaching `max_rows` finishes the backfill as `done` with `scanned == max_rows`;
request the same target again to take another slice. Because selection is by
absence of the annotation, the next slice resumes automatically. If you are
scripting that loop, drop `max_rows` instead.

A completed backfill reports `cursor_seq: 0`. That is deliberate evidence: row
selection skips records another lane holds locked, so an exhausted sweep rewinds
and sweeps again from the first record, and `done` is only set once a sweep from
the start finds nothing eligible.

`ADDITIVE_VERIFY_MAX_ROWS` (default 50,000) is unrelated to backfills — it bounds
the row check a *publish* runs before accepting an additive schema change.

### Scheduled tombstone retention

Packages can declare [tombstone retention](packages.md#tombstone-retention)
for delayed physical deletion. This creates internal `retention_purge` work
only; there is no retention HTTP endpoint. The worker selects current keyed
retractions old enough by server `created_at`, then runs the same
erasure and projection repair described above. Check the package YAML, worker
logs, and `_system/erasure` audit rows when operating one of these policies.

### Expiring artifact uses

An artifact use is operational metadata, not durable history, and it expires
after `ARTIFACT_USE_RETENTION_DAYS` (default 90). Unlike tombstone retention
this is a deployment setting rather than a package policy, so it creates no job
and needs no cron declaration: each worker pass deletes one bounded
`ARTIFACT_USE_PURGE_BATCH` page (default 500) of expired rows across every
workspace and logs `artifact_uses.expired_purged` when it removed any. A purged
page marks the pass busy, so a backlog drains without waiting on the poll
interval.

No stored record is removed. The learning signals that feedback produced and
any artifact snapshots bound to those uses are ordinary records and follow their
own retention and erasure rules — which is why a signal outlives the handle that
created it. Lowering the setting is safe: handles registered under a longer
window become expired immediately and are purged on the next pass.

An expired handle refuses new feedback with `410 artifact_use_expired` and
reports `"expired": true` on `GET /artifact-uses/{id}` until it is purged, so
the two states are distinguishable while operating.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `401 unauthorized` | Export the one-time workspace key as `MEMSEEK_API_KEY`. |
| `422 definition` | Read the machine-readable code, file, and dotted path; no partial catalog was installed. |
| `409 catalog_incompatible` | Read `compatibility.blockers` in the response: each names the rows it protects and the action that fixes it. Run `catalog-check` first next time. |
| `409 backfill_exists` | A live backfill already targets that collection version and processor. Inspect it with `GET /backfill`, or cancel it. |
| `409 incomplete_space` on cutover | Records still have no vector staged in the target space. Finish `reembed`, then cut over. |
| Backfill stuck in `queued` | The worker is not running, or its lane is behind. Check `worker.pass` logs for `backfilled_annotations`. |
| Backfill state `failed` | Read `last_error` on `GET /backfill/{id}`; the target became impossible (for example a processor was removed). |
| Rows never become ready | Confirm worker/database settings and required processor credentials. |
| Derivation does not run | Inspect readiness, metric scores, watermark, cooldown, and worker job status. |
| Provider rejects request | Check alias target, configured JSON capability, supported generation params, token-limit field, and provider environment. |
| External index is stale | Inspect projection jobs; the database remains the source of truth, and the index can be rebuilt. |
| `410 artifact_use_expired` | The handle outlived `ARTIFACT_USE_RETENTION_DAYS`. Raise it only if your real feedback window is longer; already-purged handles cannot be recovered. |
| Feedback returns `422 learning_signals_unavailable` | The workspace package does not include a `learning_signals` collection. Add it and republish. |
| A learning signal has no learning target | The render's target block read no active head, or the artifact declares no `learning:`. Check `GET /artifact-uses/{id}` and the artifact definition. |
| Candidate derivation sees no evidence | Confirm the signal's entity (`artifact:<name>`) and type (the signal kind) match the derivation's source scope. |

## Verification gate

```console
make check
```

The project gate synchronizes the locked environment, checks formatting and lint, type-checks, builds the package, runs the reference parity check, and runs the test suite against the isolated database. `make e2e` runs the deterministic ingest/search acceptance flow.
