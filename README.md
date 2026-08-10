# memseek

Memseek is the **declarative context engine for AI agents**. Your app writes
down what happens — messages, events, observations — and Memseek turns that raw
stream into the context an agent should act on now: a **current profile that
maintains itself**, context retrieval over everything ever recorded, and
prompt-ready **context artifacts** assembled on demand under a token budget.
Every derived fact cites the evidence it came from, nothing is ever silently
overwritten, and the whole design is a few files of YAML you can review like a
schema migration.

Long-term agent memory is one capability inside that engine, not the whole of
it: derivations maintain current state from immutable evidence, views retrieve
relevant evidence, and artifacts assemble task-specific context that your agents
read over the SDK, the HTTP API, or MCP.

Raw records come in; **processors** enrich each one (score it, embed it,
classify it); **derivations** combine the enriched records into durable state
like profiles and reflections; and **views** and **artifacts** hand that state
back to your application as search results and finished prompts. When a run goes
wrong, the outcome comes back as evidence — a learning signal that can draft a
revision to the very skill or policy that produced it, for a human to approve.
You describe each stage once, in YAML.

## Documentation

The [Memseek documentation](docs/index.md) is a GitHub Pages–ready site covering
the catalog layout, YAML definitions, models, processors, source/Task pipelines,
runtime receipts, triggers, views, artifacts, artifact uses and feedback,
packages, MCP, SDK, and operations. The existing [authoring guide](docs/authoring-definitions.md)
and [CRM quickstart](docs/sdk-user-profile-quickstart.md) remain available as
focused walkthroughs.

Evolving a live catalog — what a publish will do to stored records, applying a
processor to records you already have, moving a corpus to a new version, and
changing the embedding model — is documented in
[Changing definitions](docs/changing-definitions.md), with the design record in
[the schema evolution plan](docs/schema-evolution-plan.md).

To expose a deliberately small tool surface to an MCP client, see the [MCP
guide](docs/mcp.md). It covers the package-owned declaration, authenticated
`GET /tools` discovery, MCP `2026-07-28` compatibility, preflight diagnostics,
the authenticated `/mcp` Streamable HTTP endpoint, and exact remote/local
Claude Code and Codex configuration.

To preview the site locally, install the locked development environment with
`uv sync --frozen --all-groups`, then run `make docs` and open
<http://127.0.0.1:8001>.

This repository currently implements **M6 plus the M7 erasure/projection slice** of the v3.2 specification. Alongside the M0
operational foundation, the M1 ingest/enrichment pipeline, and the M2 canonical read views,
M3 adds canonical search and ranking: typed SearchSpec validation, declared-field structured
filters and ordering, named views, search-profile routing, canonical PostgreSQL scope rechecks,
single-source and multi-source weighted RRF, hit rendering, and the `/search`, `/views`, and
`/rank/schema` API surfaces. M4 adds bounded derive execution with provenance-carrying Task
values, search Tasks, citation validation, audited runs, stale guards, and the manual
derive-processor enqueue endpoint. M5 adds transactional write/accumulator trigger evaluation,
read stale-while-revalidate coalescing, cooldowns, successor reconciliation, persisted cron scan
jobs, lexical scan pagination, and authenticated job status/retry controls.
M6 adds budgeted `/context`, expanded run review, deterministic live artifact render/snapshot
flows, guarded reviewed-proposal promotion, catalog/tool discovery, and shipped profile/reflection/skill
artifact contracts. The M7 slice adds bounded provenance erasure, claim-fenced index-delete jobs,
keyed-current refresh, hash-only erasure audits, Turbopuffer candidate/projection contracts, and
incremental/reset reindex planning. On top of that, the artifact-use slice closes the loop from a
render to a real outcome: registered correlation handles with resolved learning targets,
OpenTelemetry-safe attribute maps, selected-outcome ingest into a `learning_signals` collection, and
bounded handle expiry — without ingesting traces, storing invocations, or promoting anything
automatically.

## Requirements

- Python 3.14.6 (the project intentionally targets the latest stable release series only)
- [`uv`](https://docs.astral.sh/uv/)
- Docker with Compose for the isolated PostgreSQL test service

PostgreSQL 16 with pgvector is required at runtime. The supplied Compose service uses
`pgvector/pgvector:0.8.2-pg16` and exposes its test database on port `55432` by default.

## Programmer quickstart

This walkthrough demonstrates the main programming loop: ingest immutable observations, let the
worker enrich them, read current state and freshness, search canonical memory, enqueue a profile
derivation, inspect its job/run, render a deterministic prompt artifact, and erase the resulting
provenance graph. It uses PostgreSQL/pgvector in Docker and the deterministic fake provider, so no
LLM credentials or external network calls are needed.

### 1. Install and start the local database

Requirements are Python 3.14.6, `uv`, and Docker Compose. Synchronize the locked environment and
start the disposable PostgreSQL 16 + pgvector service:

```console
uv sync --frozen --all-groups
cp .env.example .env
source .env.sh
make database
export LLM_FAKE=1
```

The test service deliberately uses a database name containing `test`. Test and reindex commands
refuse an unmarked database. Apply the schema with Alembic:

```console
uv run memseek migrate
# {"revision":"..."}
```

`make quickstart` does these three steps in one command — start the database, apply the schema,
and create the workspace credential of [step 3](#3-create-a-workspace-credential) (override its
name with `WORKSPACE=…`). The API and worker still belong in their own terminals.

### 2. Start the API and worker

Use two terminals from the repository root. In terminal A:

```console
source .env.sh
export LLM_FAKE=1
uv run uvicorn memseek.api:app --host 127.0.0.1 --port 8000
```

In terminal B:

```console
source .env.sh
export LLM_FAKE=1
uv run memseek worker
```

Both processes explicitly open, check, and close their async connection pools. The worker claims
only implemented lanes, heartbeats long-running work, and retries/dead-letters failures through
claim-token fencing.

### 3. Create a workspace credential

In terminal C, create a disposable workspace. The command prints the bearer key exactly once; only
its lowercase SHA-256 digest is persisted. Keep the output out of logs and source control:

```console
workspace_json="$(uv run memseek create-workspace local)"
export MEMSEEK_API_KEY="$(printf '%s' "$workspace_json" | \
  uv run python -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"
export MEMSEEK_AUTH="Authorization: Bearer $MEMSEEK_API_KEY"
```

If `local` already exists, use a new workspace name or recreate the disposable database.

### 4. Install a workspace package (required)

Nothing is loaded by default. A service ships no definitions of its own, so a
workspace has no catalog until it publishes one — until then every route that
needs definitions answers `409 no_catalog`. Upload a
collection/processor/derivation/view/artifact YAML package; `resources/` holds
the reference catalog and `examples/*_catalog/` hold self-contained ones. See
the complete [workspace authoring guide](docs/authoring-definitions.md):

```console
curl -sS -X POST http://127.0.0.1:8000/catalog \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  --data-binary @catalog-upload.json
```

After a successful upload, all authenticated reads, writes, and worker jobs for
that workspace resolve the returned `catalog_hash`.

For application code, `memseek.sdk.MemseekClient` wraps this flow with
`client.catalog.publish(...)`, `client.records.ingest(...)`, `client.document(...)`,
and `client.search(...)`.

### 5. Ingest observations

`POST /records` accepts up to `MAX_BATCH` records atomically. Dedupe keys make retries safe;
reusing a key with different canonical data returns `409 dedupe_conflict`.

```console
curl -sS -X POST http://127.0.0.1:8000/records \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"records":[
    {"entity":"maria","type":"event",
     "text":"Maria confirmed the Q3 budget of $40k. [importance=10]",
     "dedupe_key":"quickstart:event:budget"},
    {"entity":"maria","type":"event",
     "text":"Maria prefers async updates. [importance=10]",
     "dedupe_key":"quickstart:event:preference"},
    {"entity":"maria","type":"event",
     "text":"Maria leads the platform team. [importance=10]",
     "dedupe_key":"quickstart:event:role"}
  ]}'
```

The response separates `inserted` and exact `duplicates`, and reports `ready` per row. Required
embedding, score, and JSON processors initially produce `ready:false`; those rows are visible in
document reads but are not eligible for search or triggers until the worker clears the barrier.

### 6. Read state and freshness

`/document` is the current-state read surface. It returns latest-per-key `beliefs`, keyed
`retractions`, and one `freshness` entry per read-triggered derivation:

```console
curl -sS 'http://127.0.0.1:8000/document?entity=maria' \
  -H "$MEMSEEK_AUTH" | uv run python -m json.tool
```

Freshness reports the derivation watermark, whether matching input is dirty, whether an unready
row blocks the prefix (`pending_unready`), the last successful run, and queued/running/dead job
metadata. `/timeline` provides compact newest-first entity rows:

```console
curl -sS 'http://127.0.0.1:8000/timeline?entity=maria&limit=20' -H "$MEMSEEK_AUTH"
```

For replay consumers, `/delta` returns ready and unready rows in sequence order and a `scope_hash`;
`/cursor` advances explicitly and monotonically under that same hash. Reading `/delta` never moves
the cursor.

### 7. Search canonical memory

Search is a typed request rather than a pass-through backend query. Candidate IDs may come from
PostgreSQL or Turbopuffer, but Memseek reloads canonical rows, reapplies scope and typed field
predicates, computes the configured rank expression, and bounds the response:

```console
curl -sS -X POST http://127.0.0.1:8000/search \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"q":"budget","mode":"hybrid",
       "scope":{"entities":["maria"],"collections":["main"]},
       "k":10,"include":["text","collection","entity"],"render":true}' \
  | uv run python -m json.tool
```

Use `mode:text` when no embedding is needed, `mode:recent` for chronological retrieval, and
`mode:structured` with declared fields and `order_by`. Named views are immutable SearchSpec
templates:

```console
curl -sS -X POST http://127.0.0.1:8000/views/upcoming_calendar/query \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"entity":"maria","start":"2026-07-16T00:00:00Z",
       "end":"2026-07-17T00:00:00Z"}'
```

`GET /rank/schema` exposes the SearchSpec schema, rank grammar, active bindings, and backend
capabilities for client tooling.

### 8. Derive a profile and inspect the job

The shipped `profile` derivation consumes ready `main` events and writes cited `profiles` rows.
Reads normally enqueue it when evidence is stale; it can also be enqueued explicitly:

```console
profile_job="$(curl -sS -X POST http://127.0.0.1:8000/processors/profile/run \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"entity":"maria"}' | \
  uv run python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"

curl -sS "http://127.0.0.1:8000/jobs/$profile_job" -H "$MEMSEEK_AUTH" \
  | uv run python -m json.tool
curl -sS 'http://127.0.0.1:8000/runs?entity=maria&processor=profile&operation=derive' \
  -H "$MEMSEEK_AUTH"
```

The worker performs provider calls before the commit lock, then rechecks workspace, claim token,
its internal source cursor, guarded reads and target heads, and cited parents before writing a run
and emitted records. The fake provider is
deterministic but generic when no completion is queued. The exact controlled fake profile response
is shown in [`tests/test_e2e.py`](tests/test_e2e.py); `make e2e` runs that full acceptance flow.

### 9. Render a deterministic artifact

The daily agent prompt artifact composes canonical state and search results without making an LLM
call. Its manifest records exact input IDs, definition/package hashes, freshness, truncation, and
stable rendered content hash:

```console
curl -sS -X POST http://127.0.0.1:8000/artifacts/daily_agent_prompt/render \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"entity":"maria","task":"prepare the next update",
       "start":"2026-01-01T00:00:00Z","end":"2027-01-01T00:00:00Z"}' \
  | uv run python -m json.tool
```

Snapshots use ordinary provenance-carrying records. Reviewed skill snapshots remain drafts until
explicit promotion; `/runs/{id}` reports erased output IDs instead of silently changing review
order.

### 10. Bind that render to its outcome

`render` gives you text. `uses` gives you the same text plus a handle the eventual outcome can name,
along with the resolved learning target and an OpenTelemetry-safe attribute map:

```console
curl -sS -X POST http://127.0.0.1:8000/artifacts/daily_agent_prompt/uses \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"parameters":{"entity":"maria","task":"prepare the next update",
       "start":"2026-01-01T00:00:00Z","end":"2027-01-01T00:00:00Z"},
       "snapshot":false}' | uv run python -m json.tool
```

Your application runs the agent with `content`, then stores the returned `id` next to its own
result — one short column, the way it already stores a job or payment-intent ID. When the outcome
turns out to be worth learning from, that ID is the only thing it needs:

```console
curl -sS -X POST "http://127.0.0.1:8000/artifact-uses/$USE_ID/feedback" \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"kind":"correction","source":"operator",
       "expected":"Tell the customer the refund is pending.",
       "comment":"The authoritative system still showed pending.",
       "dedupe_key":"message:msg_123:correction"}' | uv run python -m json.tool
```

That writes one ordinary `learning_signals` record, routed by the render's learning target to
`artifact:maintained_skill` — evidence a candidate Pipeline can consume, and nothing more. Memseek
stores no invocation, no model response, and no trace, and promotes nothing on its own. See
[`docs/artifact-uses.md`](docs/artifact-uses.md).

### 11. Erase and repair projections

Erasure accepts either an entity or explicit record IDs. It takes the exclusive workspace lock,
expands the bounded `derived_from` closure, fences active derive jobs, deletes canonical rows,
queues an `index_delete` job, refreshes exposed keyed predecessors, and writes a hash-only
`_system/erasure` audit record:

```console
curl -sS -X POST http://127.0.0.1:8000/erase \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"entity":"maria"}' | uv run python -m json.tool
```

The response contains `erasure_record_id`, `deleted_count`, `affected_entity_count`, and
`index_delete_job_id`. External indexes are disposable projections; PostgreSQL is canonical, and
the worker drains delete/upsert jobs after the transaction commits.

For sequence-bounded repair or a complete test rebuild, enqueue ordinary claim-fenced projection
jobs without changing canonical records:

```console
uv run memseek reindex --workspace local --since-seq 100
uv run memseek reindex --workspace local --reset --yes
```

### 12. Stop services and run the automated gate

Stop the API and worker with `Ctrl-C`, then remove the disposable database:

```console
make database-down
```

The complete local gate provisions its own database, checks Ruff and `ty`, builds the package,
verifies reference parity/execution, and runs pytest:

```console
make test
```

For only the realistic HTTP/worker flow:

```console
make e2e
```

It covers workspace authentication, ingest, fake-provider enrichment, worker lifecycle, freshness,
hybrid search, manual and importance-threshold profile derivation, job/run status, and deterministic
artifact rendering.

For a separate real-provider walkthrough of evidence-driven skill maintenance—complete cited
drafts, per-key divergence, explicit Promotion, and stale-candidate protection—see
[`docs/skill-maintenance.md`](docs/skill-maintenance.md).

For a complete workspace-package example driven through `MemseekClient`—fake CRM events,
importance enrichment, automatic threshold trigger, cited profile state, run audit, search, and
artifact rendering—see
[`docs/sdk-user-profile-quickstart.md`](docs/sdk-user-profile-quickstart.md).

For a toy simulation of the *Generative Agents* paper (Park et al., UIST '23)—memory stream,
importance/relevance/recency retrieval, information diffusion, reflection, and paper-style agent
interviews on the shipped catalog—see
[`docs/generative-agents-example.md`](docs/generative-agents-example.md) and
[`examples/generative_agents_toy.py`](examples/generative_agents_toy.py).

### API surface at a glance

| Surface | What it is for | Auth |
|---|---|---|
| `GET /health` | Live database health check | No |
| `GET /catalog`, `POST /catalog` | Inspect or atomically install the authenticated workspace package | Yes |
| `POST /records` | Atomic immutable ingest with dedupe | Yes |
| `GET /records/{id}` | Full canonical row dereference | Yes |
| [`GET /timeline`, `/document`, `/document/history`](docs/api-surface.md) | Entity timeline and current/history views | Yes |
| `GET /delta`, `POST /cursor` | Replay feed and consumer watermark | Yes |
| `POST /search`, `GET /rank/schema` | Canonical retrieval and rank contracts | Yes |
| `GET /views`, `POST /views/{name}/query` | Named typed search views | Yes |
| `POST /processors/{name}/run` | Manual derivation enqueue | Yes |
| `GET /jobs/{id}`, `POST /jobs/{id}/retry` | Bounded job status and retry | Yes |
| `GET /runs`, `GET /runs/{id}` | Audited derivation review | Yes |
| `GET /context` | Bounded prompt-ready context assembly | Yes |
| `GET /collections`, `/processors`, `/triggers`, `/tools` | Machine-readable catalog contracts; `/tools` is the package-declared MCP allowlist | Yes |
| `POST /artifacts/{name}/render` | Deterministic live artifact render | Yes |
| `POST /artifacts/{name}/snapshot`, `GET /artifacts/{name}` | Materialized artifact review/read | Yes |
| `POST /artifacts/{name}/uses` | Render and register a correlation handle for external use | Yes |
| `POST /artifact-uses/{id}/feedback`, `GET /artifact-uses/{id}` | Selected-outcome ingest and handle metadata | Yes |
| `POST /promote` | Promote a reviewed snapshot | Yes |
| `POST /erase` | Provenance-aware canonical erasure | Yes |

See the [API surface guide](docs/api-surface.md) for concrete request/response
examples and guidance on choosing between the endpoint families.

All authenticated errors use `{"error":"machine_code","detail":"human-readable detail"}`.
Responses are bounded by endpoint row/token limits and `MAX_RESPONSE_BYTES`; a request that would
otherwise become partial fails with a precise 409/422/503 response.

## Endpoint and architecture reference

The quickstart above is the shortest executable path. The reference below explains the endpoint
semantics, catalog layout, and milestone guarantees in more detail.

Install the locked development environment and run the complete local gate:

```console
uv sync --frozen --all-groups
make test
```

`make test` checks that the configured database name contains `test`, starts the isolated
Compose database when the default URL is used, runs formatting, lint, type, build, reference,
and pytest checks, then removes the database container. To use an existing test database:

```console
make test TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/my_test_db
```

To run only the PostgreSQL-backed end-to-end smoke flow:

```console
make e2e
```

It exercises workspace authentication, record ingestion, fake-provider enrichment, worker
lifecycle, document freshness/revalidation, hybrid search, job status, a fake user profile, and
the deterministic live prompt artifact.

For a local development process session:

```console
cp .env.example .env
make database
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
uv run memseek migrate
uv run memseek create-workspace local
uv run uvicorn memseek.api:app
```

The workspace command prints one JSON object containing the workspace and its bearer key. The
key is disclosed once; only its SHA-256 digest is persisted. Keep that output out of shell logs
and source control.

Health remains unauthenticated:

```console
curl http://127.0.0.1:8000/health
# {"ok":true,"db":true}
```

It performs a live `SELECT 1`. Loss of database connectivity returns HTTP 503 with
`{"ok":false,"db":false}`. Every data route requires the one-time workspace bearer key. Insert
up to `MAX_BATCH` records atomically with `POST /records`:

```console
curl -X POST http://127.0.0.1:8000/records \
  -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"records":[{"entity":"maria","type":"event","text":"Maria confirmed the budget.","dedupe_key":"crm:event:123"}]}'
```

The response separates newly inserted rows from exact duplicates and reports whether each row is
ready. Collections with required processors initially return `ready:false`; start the worker to
enrich them and drain their durable projection outbox:

```console
uv run memseek worker
```

Read canonical state and retrieval surfaces. `GET /records/{id}` dereferences one full row
(and touches `last_accessed` unless `TOUCH_ON_READ=0`), `GET /timeline` pages compact
newest-first entity rows, and `GET /document` assembles latest-per-key current state:

```console
curl -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  'http://127.0.0.1:8000/document?entity=maria'
```

The document response contains `beliefs` (current keyed rows, visible immediately even while
enrichment is pending), `retractions` (tombstoned keys with collection, key, record ID, and
sequence), and `freshness` (one entry per read-triggered derivation with its watermark, dirty
and pending-unready flags, and any queued/running/dead derive job). A current-state set that
would exceed `MAX_DOCUMENT_RECORDS` or `MAX_RESPONSE_BYTES` returns `409 document_too_large`
rather than a silently partial document. `GET /document/history` pages every version of one
collection-scoped key, including drafts and tombstones.

Cache consumers replay changes with the delta feed and advance their cursor explicitly:

```console
curl -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  'http://127.0.0.1:8000/delta?consumer=cache'
curl -X POST http://127.0.0.1:8000/cursor \
  -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"consumer":"cache","entity":"*","position":42,"scope_hash":"<from /delta>"}'
```

`GET /delta` returns matching rows in ascending sequence — ready or unready, tombstones
included — plus a `scope_hash` binding the visibility filters. It never moves the cursor;
`POST /cursor` advances it monotonically and only under the same scope hash. A stored cursor
read or written under a different scope returns 409 until it is reset explicitly with
`force=true` or a new consumer name.

Run canonical retrieval through M3 search and named views:

```console
curl -X POST http://127.0.0.1:8000/search \
  -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"q":"budget","mode":"hybrid","scope":{"entities":["maria"]},"k":10,"render":true}'

curl -X POST http://127.0.0.1:8000/views/upcoming_calendar/query \
  -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"entity":"maria","start":"2026-07-16T00:00:00Z","end":"2026-07-17T00:00:00Z"}'

curl -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  http://127.0.0.1:8000/rank/schema
```

The worker runs one bounded oldest-first enrichment unit at a time, then executes projection and
configured derivation jobs with the same lease heartbeat and stale-commit fencing.
It also schedules due cron buckets and drains persisted, lexically paged `cron_scan` jobs. `LLM_FAKE=1` selects the
deterministic offline provider; normal deployments use the configured OpenAI-compatible adapter.

Enqueue one manual derive run for an authenticated entity:

```console
curl -X POST http://127.0.0.1:8000/processors/profile/run \
  -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"entity":"maria"}'
```

Inspect or retry a job without exposing record payloads:

```console
curl -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  http://127.0.0.1:8000/jobs/<job-id>
curl -X POST -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  http://127.0.0.1:8000/jobs/<job-id>/retry
uv run memseek retry-job <job-id>
```

Erase one entity or an explicit provenance seed set. The response contains the audit record and
durable projection-delete job; the audit row stores hashes and counts, never erased content:

```console
curl -X POST http://127.0.0.1:8000/erase \
  -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"entity":"maria"}'
```

## Developer commands

| Command | Purpose |
|---|---|
| `make sync` | Synchronize the environment from `uv.lock` |
| `make format` | Format Python and apply safe Ruff fixes |
| `make lint` | Check Ruff formatting and lint rules |
| `make typecheck` | Run `ty` over `src/` and `tests/` |
| `make build` | Build the source distribution and wheel with `uv_build` |
| `make database` | Start the isolated PostgreSQL/pgvector test service |
| `make database-down` | Remove the isolated database service |
| `make migrate` | Upgrade `DATABASE_URL` to the Alembic head revision |
| `make migration-current` | Verify `DATABASE_URL` is at the current Alembic head |
| `make test` | Run the complete M6 + erasure gate with an isolated database |
| `make e2e` | Run the focused ingest → enrich → freshness → search → status smoke test |

Projection repair is explicit and never changes canonical records:

```console
uv run memseek reindex --workspace local --since-seq 100
uv run memseek reindex --workspace local --reset --yes
```

Definition change has its own operator commands. See
[Changing definitions](docs/changing-definitions.md) for the full guide:

```console
uv run memseek catalog-check --workspace local --dir ./catalog --package acme@1.4.0
uv run memseek backfill --workspace local --collection main --version 1 --processor sentiment_v2  # add --max-rows to cap it
uv run memseek reembed --workspace local --space default-v2 [--cutover]
uv run memseek rebind-cursor --workspace local --derivation profile --entity user-42 --policy reset
uv run memseek catalog-prune --workspace local
uv run memseek migrate-collection-hashes --dry-run
```

Ruff and `ty` are project dependencies and always run through `uv`. The deliberately compact
reference oracle is excluded from both tools; byte parity with `spec/reference.py` and successful
execution are separate test requirements.

Schema history uses Alembic. `memseek migrate` invokes Alembic through an async SQLAlchemy
connection and upgrades to `head`; direct operational commands such as `alembic current` use
`DATABASE_URL`. SQLAlchemy is confined to this migration boundary—the API, worker, authentication,
and job paths use raw async psycopg. The initial revision delegates to the immutable normative SQL
asset in `migrations/001_init.sql`. Add later schema changes as conventional files under
`alembic/versions/` rather than editing that initial asset.

## Public model and ownership boundary

Memseek's public vocabulary has six primitives:

- A **collection** is a versioned content schema, projection, processor policy, and search route.
- A **record** is an immutable event or a version of keyed state.
- A **processor** either annotates one record or derives new cited records.
- A **trigger** schedules a derivation processor.
- A **view** is a versioned named query or current-state read.
- An **artifact** is a deterministic render recipe or a reviewed maintained snapshot.

Packages bundle exact versions of those definitions for deployment and audit. A package is
associated with one workspace when it is loaded through `POST /catalog`; it does not create a
second data namespace, and another workspace may select a different package.

The calling application remains responsible for environment transitions, simulation time,
planning and action policy, dialogue, and tool execution. Memseek can store observations,
plans, actions, and dialogue supplied by that application; doing so does not make Memseek a
simulator, planner, actor, world model, or authoritative CRM.

## Declarative catalog

The shipped definitions are a bootstrap catalog. In the service workflow, each workspace can
replace that fallback with its own validated package using `POST /catalog`; subsequent ingest,
enrichment, derivation, search, views, and artifacts resolve the workspace package. Definitions
are deployment assets when running a local filesystem process, resolved relative to the process
working directory:

- `conf/` contains model aliases, unified per-record processors, rank expressions, and search
  profiles.
- `collections/`, `derivations/`, `views/`, and `artifacts/` contain immutable definitions.
- `packages/agentic_memory_core.yaml` binds exact shipped versions and semantic processor names.
- `triggers/` accepts optional standalone trigger definitions; all shipped triggers are currently
  normalized from derivation-local declarations.

YAML is the primary reviewed/deployment authoring format. The intended user-owned workflow is
collection first, then processor, derivation, view, and package YAML; see
[`docs/authoring-definitions.md`](docs/authoring-definitions.md). Applications that generate
definitions can optionally use `DefinitionSources` and `compile_definition_catalog()`; generated
definitions still pass through the same global validation, graph checks, and content hashes.

Loading is deterministic and strict: duplicate YAML keys and unknown fields are rejected, JSON
Schemas use Draft 2020-12, references are resolved across the complete graph, and definitions are
hashed from canonical JSON. The operational `active` selector is excluded from a versioned
definition's semantic hash; active selections and deployment search bindings are included in the
catalog hash.

`conf/search_profile_overrides.example.yaml` demonstrates an optional deployment binding. Copy it
to a deployment-owned file and set `SEARCH_PROFILE_OVERRIDES_FILE`; do not edit immutable
collection versions merely to change a backend route. The Turbopuffer profile remains unavailable
without credentials, while `pg_default` is always usable.

## Prompt, skill, and CRM patterns

The shipped daily-agent-prompt artifact is a deterministic live recipe. M6 assembles the current
profile, upcoming calendar, and relevant memory without calling an LLM in the renderer; any
model-computed content must already exist as cited processor output. Its render manifest records
the exact block scopes, definition hashes, input IDs, and stable content hash.

The shipped skill lifecycle is reviewed rather than self-deploying. The skill processor produces a
complete draft snapshot with `steps`, `pitfalls`, and `examples`; promotion creates a new audited
run, and rollback promotes an older complete snapshot. The system does not claim that a new draft
is better or perform automatic skill evaluation.

The shipped prompt also declares a `learning:` target naming `maintained_skill@1`, so an artifact
use bound from it resolves the exact promoted skill heads that were in force. Feedback about that
render becomes a `learning_signals` record routed to `artifact:maintained_skill`, which the skill
Pipeline can consume as ordinary evidence. See
[`docs/artifact-uses.md`](docs/artifact-uses.md).

The package manifest also demonstrates the CRM-augmentation pattern: applications may supply
domain collections, processors, views, and artifacts while PostgreSQL remains canonical and the
application remains the authority for permissions and transactional business workflows.

## M1 ingest and enrichment guarantees

- `POST /records` validates the exact stored collection version/hash, record mode, declared field
  types, Draft 2020-12 content schema, text/content limits, client outputs, parents, depth, and
  workspace ownership before committing an all-or-nothing batch.
- Dedupe keys are idempotent only for the same canonical immutable payload. Reusing a key with
  different content, provenance, explicit time, score, or annotation returns
  `409 dedupe_conflict` and rolls back the batch.
- Required processor output is write-once. Embeddings, score processors, and generic JSON annotations use
  deterministic middle truncation and bounded sub-batches. Provider exhaustion records compact
  diagnostics, applies schema-valid defaults, and still removes the readiness barrier.
- All public and internal record creation crosses one canonical storage boundary. Derivation
  output groups transition to ready all-or-none, while an optional processor with no fallback
  records a terminal failed attempt without inventing annotation data or hot-looping forever.
- Every annotation writes a ready `_system/run` record citing exactly its target. The transaction
  that marks a target ready also enqueues its search projection, refreshes keyed current state,
  and reaches the single post-readiness trigger seam; no ingest-only trigger path exists.
- PostgreSQL is canonical. Projection jobs carry only record IDs and last-known collections,
  refetch truth on every retry, recompute `is_current` against unready replacements, translate
  missing rows to deletes, and use idempotent backend calls. PostgreSQL projection execution is a
  no-op; configured external adapters use the same durable contract.
- Contradiction detection is the ordinary `contradiction` YAML derivation. It compares named
  `changes` and `current` sources and emits cited `relations/contradiction` public events whose
  content is validated by the `relations@1` collection schema. It uses the same worker, run audit,
  readiness, search, trigger, and erasure paths as every other derivation.
- Startup and every backend projection reject stored collection or processor-hash drift before
  provider/backend I/O. Structured completion logs report safe identifiers, timings, counts, and
  usage without record content, prompts, model responses, or exception messages.

## M2 canonical read guarantees

- `GET /records/{id}` returns the complete canonical row — collection identity, content, scores,
  annotations and their metadata, enrichment state, provenance, and timestamps — without the raw
  embedding vector. Cross-workspace IDs are indistinguishable from missing rows. When
  `TOUCH_ON_READ` is enabled the read updates `last_accessed`; a failed touch is logged and never
  fails the read.
- `/timeline`, `/document/history`, and `/delta` stop at the last complete emitted row before
  `MAX_RESPONSE_BYTES`, report `truncated:true` for a byte-bound stop, and leave their cursor at
  the last emitted row so the next page resumes without gaps or overlap.
- `/document` selects the latest row per collection-scoped key by sequence within one status
  lane. Keyed current state is read-visible immediately after its insert transaction; readiness
  gates search and trigger visibility, not document visibility. Tombstoned keys appear only under
  `retractions`. The document is never partial: exceeding `MAX_DOCUMENT_RECORDS` or
  `MAX_RESPONSE_BYTES` returns 409 with a narrowing instruction.
- Document `freshness` reports, per read-triggered derivation, the run-record watermark, the last
  successful completion time, whether matching input exists above the watermark (including an
  unready first record as `pending_unready`), and the derive-job state. A dead-lettered job stays
  reported as `dead` with its error kind until a later successful or noop run supersedes it. M5
  performs stale-while-revalidate enqueueing through the same explicit seam without delaying the
  current document response.
- Delta visibility filters are canonicalized into a `scope_hash`. Cursors advance monotonically
  per `(workspace, consumer, entity)` and only under a matching hash; scope changes and position
  regressions require an explicit `force:true` reset. Reading `/delta` never mutates the cursor.

## M3 canonical search guarantees

- Search requests use one immutable typed `SearchSpec` model with strict shape checks for
  single-source and multi-source forms, portable mode legality, rank grammar validation, and
  declared-field predicate/operator type compatibility.
- Candidate backends are recall channels only. Core always reloads canonical rows from PostgreSQL,
  reapplies scope and declared-field predicates, recomputes exact similarity/text signals, applies
  one canonical rank expression (or structured `order_by`), and enforces current-version rules.
- Vector query embedding executes only when at least one source needs vector or hybrid mode.
  Text, recent, and structured-only requests do not invoke embedding.
- Multi-source requests canonically rank each source first, then fuse with weighted reciprocal-rank
  fusion and optional post-fusion boost, with deterministic tie-breaks and per-hit `source_ranks`.
- Named views are versioned immutable SearchSpec templates with typed parameters. Request-time
  parameters are validated, rendered through the template resolver, and revalidated as SearchSpec
  before execution.
- Search and view responses are response-byte bounded, optional rendering is token bounded, and
  emitted IDs are touched only when read-touch is enabled.

## M4 derivation guarantees

- A `changes` source consumes the oldest ready suffix after the pipeline's private cursor, never
  skips an earlier unready record, and refunds the attempt when enrichment has not reached the
  barrier. A `snapshot` source evaluates one complete bounded scope through an exact sequence
  checkpoint and fails rather than silently truncating when its record or token bound is exceeded.
- Named record sources render complete deterministic `derivation_input` rows, escaped and
  otherwise unadorned; the task prompt supplies the element that marks them as untrusted data.
  Task values retain transitive source IDs, while emitted citations are
  accepted only when the full UUID handle was visible to the producing Task.
- Workspace YAML selects process-installed Task Adapters. Their `input`, strict static `with`
  configuration, and output are type-validated; `TASK_MODULES` installs the same registry in API
  and worker processes. Built-in LLM results satisfy a complete JSON `output_schema` authored
  inline in YAML before a downstream Task can consume them. Schema-capable provider Adapters use
  that exact contract as their primary structured-output mode, while local validation remains
  authoritative. Handlers receive bounded model/search/render capabilities,
  never a database connection or canonical writer.
- Search Tasks are bounded by their declared token budget, run-wide visible-record budget, and
  `MAX_STEP_CONCURRENCY`; fan-out results preserve order and expose exactly the selected hit IDs.
- Every successful, noop, or failed attempt writes an auditable `_system/run` row with config and
  contract hashes, a private source/read receipt, active-head preconditions, normalized emission
  effect/coverage, keyed divergence, model-call hashes/usage, provenance IDs, output IDs, timing,
  and error kind.
- The static `emit` boundary normalizes append, partial patch, or complete replacement before
  canonical write. Workspace/entity locks, claim-token fencing, receipt verification, and read/head
  guards reject stale concurrent commits. M5 re-evaluates write and accumulator triggers after
  readiness and successful changes commits so mid-run arrivals reconcile into a successor mailbox.

## M5 trigger and worker guarantees

- Ready transitions evaluate write and accumulator conditions against canonical PostgreSQL rows,
  including typed field predicates and numeric score/JSON thresholds. Unready rows never
  satisfy a trigger.
- Read-triggered document requests enqueue stale work asynchronously, coalesce reason keys, and
  honor per-trigger cooldowns without delaying the current-state response.
- The worker schedules UTC cron buckets, persists `cron_scan` jobs, and pages `entities:any` or
  `entities:dirty` in lexical batches of 500. Chained cursors survive process restarts.
- A package can additionally declare bounded tombstone retention. The worker schedules only the
  latest due tick as an internal `retention_purge` job, then reuses canonical erasure; see
  [Packages](docs/packages.md#tombstone-retention).
- `GET /jobs/{id}`, `POST /jobs/{id}/retry`, and `memseek retry-job` expose only bounded job
  metadata and retry dead jobs under workspace/active-partition fencing.

## M6 context, artifacts, and review guarantees

- `/context` assembles document, search, recent, and optional delta sections under one token
  budget, deduplicates canonical IDs, escapes record content, and wraps the rendering only in the
  element the request declared.
- Live artifact rendering makes no LLM calls and returns exact input IDs, block scopes, definition
  hashes, package bindings, freshness markers, and a stable rendered-content hash.
- Artifact snapshots are ordinary provenance-carrying records behind materialization runs; live
  snapshots are active and reviewed skill snapshots remain draft until explicit promotion.
- Promotion copies one complete reviewed draft emission into new active successors, remains
  idempotent after success, and rejects a first activation with `promotion_stale` when a touched
  active head no longer matches the run's captured receipt.
- `/runs` and `/runs/{id}` expose bounded run summaries and exact output review order, while
  `/tools`, `/collections`, `/processors`, and `/triggers` publish machine-readable contracts.
  `/tools` is deliberately an explicit package MCP allowlist: views, artifacts, and routes do not
  become agent tools until a versioned `mcp/*.yaml` declaration names them.

## M7 erasure and projection guarantees

- `POST /erase` takes the exclusive workspace lock, computes a bounded transitive closure, fences
  active derive jobs, acquires sorted entity locks, deletes canonical rows, and writes one ready
  hash-only `_system/erasure` audit record.
- Every erased row is captured in one durable `index_delete` payload. Keyed identities also enqueue
  an `index_upsert` refresh for the surviving current row, so external projections converge after
  the worker drains the queue.
- `reindex --since-seq` queues ready rows at or above the sequence watermark plus the latest ready
  predecessor for touched keyed identities. `reindex --reset` queues every ready row; reset is
  confirmation-gated outside test databases.
- Turbopuffer namespaces are deterministic workspace/collection hashes, use bounded HTTP retries,
  and remain a disposable candidate/projection layer. Canonical PostgreSQL reload and ranking
  semantics still decide every returned hit.

## Artifact-use, learning-signal, and feedback guarantees

- `POST /artifacts/{name}/uses` renders an artifact, resolves its declared learning target, and
  registers one `artifact_use` row holding identities, definition and rendered-content hashes, the
  resolved target, an optional snapshot reference, and an expiry. The table has no column able to
  hold a render, request parameters, a model response, tool calls, token usage, latency, or trace
  spans. Request parameters are excluded deliberately, because an artifact parameter can carry
  untrusted user content.
- An artifact use asserts only that Memseek rendered an artifact with a given identity. It never
  claims a model ran, that a call succeeded, what was returned, or that a user saw the result.
- A use ID is not a credential. Feedback still requires normal workspace authentication, and a use
  owned by another workspace is indistinguishable from one that does not exist.
- Learning-target resolution captures the exact active keyed heads the render read. A single shared
  promotion run becomes the base version; heads promoted separately resolve to no single base; a
  target block that read no head resolves to no target at all, so a signal is never attributed to a
  version that never influenced an execution.
- `snapshot: true` persists the snapshot from the same resolution as the handle, so the record and
  the use name one identical rendered-content hash.
- `POST /artifact-uses/{id}/feedback` writes through the public record path into `learning_signals`,
  so dedupe, schema validation, declared fields, provenance, search, and erasure keep their existing
  semantics. Client dedupe keys are namespaced and cannot collide with application record keys.
- With a snapshot the signal cites it in `derived_from`, so ordinary erasure closure reaches the
  signal and anything derived from it. Without one the signal carries identity and hashes only and
  claims no provenance edge; the render is not reconstructable after its sources change.
- `execution_refs` are bounded and informational. They never become provenance edges, and no
  processor fetches an external trace during a transaction.
- Telemetry attributes are bounded scalar `memseek.*` identities and hashes only, with the snapshot
  attribute omitted rather than null when absent. OpenTelemetry is an optional extra; the loop works
  with no telemetry backend at all.
- Artifact uses expire after `ARTIFACT_USE_RETENTION_DAYS`. Each worker pass deletes one bounded
  `ARTIFACT_USE_PURGE_BATCH` page across all workspaces; expired handles reject new feedback with
  `410 artifact_use_expired`. Learning signals and snapshots are canonical records and outlive the
  handle.

## Operational foundation guarantees and limits

- Pools are constructed closed and explicitly opened, checked, and closed by API and worker
  lifespans. Database sessions use UTC.
- Alembic owns schema history in `alembic/` and records the current revision in its standard
  `alembic_version` table. Online upgrades run transactionally under a PostgreSQL advisory lock.
  The initial revision executes the normative `migrations/001_init.sql` asset after verifying its
  pinned SHA-256 digest; rerunning an upgrade at head is a no-op.
- Workspace/entity advisory-lock keys are stable signed 64-bit values derived from domain-separated
  SHA-256 input.
- Job ownership is fenced by random claim tokens and PostgreSQL wall-clock lease expiry. Claims use
  `SKIP LOCKED`; heartbeat, completion, retry, and not-ready release reject stale ownership.
- Structured logs omit bearer keys, record text, prompts, model output, and arbitrary content.
- Full external Turbopuffer reset/orphan enumeration and the final M7 walkthrough/agent-loop
  examples remain follow-up hardening; the canonical erasure, delete queue, adapter contract, and
  reindex planner are implemented.
- Fake-provider mode is for the dedicated test database or an empty development workspace only.
- External indexes are disposable projections; PostgreSQL is canonical.

Configuration defaults and safety bounds are documented in [`.env.example`](.env.example). The
normative architecture and behavior remain in
[`spec/memseek-spec-v3.2-agentic-data-substrate.md`](spec/memseek-spec-v3.2-agentic-data-substrate.md),
and implementation choices not fixed there are recorded in [`DECISIONS.md`](DECISIONS.md).
