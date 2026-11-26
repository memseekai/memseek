---
title: "Configuration reference"
eyebrow: Every process setting
---

This page lists every setting the API, the worker, and the CLI read from the
environment. It follows the order of `Settings` in `src/memseek/config.py`.
For how to run and tune a deployment, see [Operations](operations.md).

## How settings load

Each setting is a field of `Settings`. Its environment variable is the field
name in upper case: `job_lease_s` is `JOB_LEASE_S`. Names are not
case-sensitive.

A process resolves each setting once, at startup, in this order:

1. The process environment.
2. The `.env` file in the working directory.
3. The default on this page.

The rules that apply to every setting:

| Rule | Effect |
| --- | --- |
| An empty value counts as unset | `MAX_BATCH=` uses the default `100`. |
| Unknown variables are ignored | A misspelled name changes nothing and raises no error. |
| Settings are frozen | A change takes effect only after the process restarts. |
| Booleans | `1`, `true`, `yes`, and `on` are true. `0`, `false`, `no`, and `off` are false. |
| Lists | `TASK_MODULES`, `HARNESS_PATHS`, and `SKILLPACK_PATHS` take a JSON array, for example `["a", "b"]`. `API_CORS_ORIGINS` has its own format. See [API process](#api-process). |
| Relative paths | They resolve against the process working directory. |
| Validation | The process refuses to start if a value is out of range. The error names the variable. See [Rules checked at startup](#rules-checked-at-startup). |

Provider credentials are not fields of `Settings`. See
[Credentials outside `Settings`](#credentials-outside-settings).

## Database

All database settings are shared by the API and the worker. `memseek migrate`
reads `DATABASE_URL` but none of the session guardrails.

| Setting | Default | What it does |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/memseek` | The PostgreSQL connection string. If the database name contains `test`, `LLM_FAKE` can run over existing provider history. See [`LLM_FAKE`](#models-and-llm-calls). |
| `DATABASE_STATEMENT_TIMEOUT_MS` | `60000` | Postgres `statement_timeout` for every pooled session. `0` sends nothing and keeps the server or role default. |
| `DATABASE_LOCK_TIMEOUT_MS` | `10000` | Postgres `lock_timeout` for every pooled session. `0` keeps the server or role default. |
| `DATABASE_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS` | `120000` | Postgres `idle_in_transaction_session_timeout`. Postgres ends a session that leaves a transaction open this long with no statement running. `0` keeps the server or role default. |
| `DATABASE_TEMP_FILE_LIMIT_KB` | unset | Postgres `temp_file_limit`, in kilobytes. Memseek sets it only when its role has `SET` privilege on the parameter, and logs `database.temp_file_limit_not_applied` otherwise. `0` means unset. |
| `MEMSEEK_BUILD_SHA` | `dev` | The build identifier. Memseek stores `<package version>+<MEMSEEK_BUILD_SHA>` as `engine_version` on the run metadata it writes during ingest, enrichment, promotion, artifact renders, and definition evolution. Set it to the commit your image was built from. |

[Database guardrails](operations.md#database-guardrails) explains the default
values and how to set `temp_file_limit` on the role instead.

Each process opens one connection pool. It holds at least 1 connection and at
most `max(4, WORKER_CONCURRENCY + INDEX_CONCURRENCY)`. Every session runs in
UTC with `application_name` set to `memseek`.

## Models and LLM calls

Endpoints, models, and aliases are declared in a models file, not here. See
[Providers and model aliases](models.md).

| Setting | Default | What it does |
| --- | --- | --- |
| `MODELS_FILE` | `conf/models.yaml` | The models file for the process's built-in catalog. It applies only when `CATALOG_FILE` is unset. A `catalog.yaml` names its own models file under `config.models`, and a published workspace catalog brings its own. |
| `LLM_FAKE` | `false` | Replaces every provider adapter with a deterministic fake. The declared connections stay in place. Embeddings are recorded under `fake:<model>`. Startup fails if the database already holds embeddings or annotations from a real provider, unless the database name contains `test`. |
| `LLM_DEBUG` | `false` | Sets the log level of the API, the worker, and the CLI to `DEBUG`. Every model request (system message, prompt, effective parameters, and embedding inputs) and every raw response is logged **without redaction**. Use it for local debugging only. |
| `LLM_MAX_CONCURRENCY` | `8` | The maximum number of model calls in flight in one process. Completions, judgments, and embeddings share this limit. |
| `MODEL_CONTEXT_TOKENS` | `60000` | The context window assumed for a model alias that declares no `context_tokens`. The catalog compile rejects such an alias if this value is below 4,096. It is also the largest `budget` a context request may ask for. |
| `MAX_PROMPT_TOKENS` | `50000` | The largest prompt one model call may send. A processor's prompt budget for an alias is `min(MAX_PROMPT_TOKENS, context tokens - output tokens)`. A record that does not fit fails its processor with `budget: ... does not fit MAX_PROMPT_TOKENS`. |
| `MAX_OUTPUT_TOKENS` | `4000` | The output-token limit for an alias whose `params` set no `max_output_tokens`. The answer endpoint uses at most 256 for its retrieval query and at most 4,000 for its answer, capped by this value. |

## Catalog

A catalog is the set of definitions a workspace runs: collections, processors,
derivations, views, and the rest. See [Catalog layout](catalog-layout.md).

| Setting | Default | What it does |
| --- | --- | --- |
| `CATALOG_FILE` | unset | The path to a service-owned `catalog.yaml`. The file must be named exactly `catalog.yaml`. Its source map selects every other definition file. A workspace that has published no catalog of its own is served this one. If unset, each workspace must publish its own catalog. |
| `TASK_MODULES` | see below | A JSON array of Python modules to import at startup. Importing a module registers the custom derivation tasks it defines. The API, the worker, and every catalog compile import the same list. |

The default `TASK_MODULES` is:

```json
[
	"memseek.derive.tasks_computer",
	"memseek.derive.tasks_graph",
	"memseek.derive.tasks_facts",
	"memseek.derive.tasks_repair",
	"memseek.derive.tasks_migrate"
]
```

A value replaces this list. To add a module, include the five defaults as well.
A catalog that uses a task from a module left out of the list fails to compile.

### Internal catalog slots

The following fields exist so the catalog compiler can record which document
families a catalog contains. Do not set them. If any of them is set, the
process fails to start with `set catalog_file to catalog.yaml; directory
discovery is no longer supported`.

`PROCESSORS_FILE`, `COLLECTIONS_DIR`, `DERIVATIONS_DIR`, `TRIGGERS_DIR`,
`VIEWS_DIR`, `ARTIFACTS_DIR`, `COMPUTERS_DIR`, `PROGRAMS_DIR`, `AGENTS_DIR`,
`CONTEXT_POLICIES_DIR`, `TOOLSETS_DIR`, `MCP_DIR`, `PACKAGES_DIR`.

## Search

See [Views & search](views-search.md) for search profiles and ranking.

| Setting | Default | What it does |
| --- | --- | --- |
| `SEARCH_PROFILES_FILE` | `conf/search_profiles.yaml` | The search profiles for the built-in catalog. It applies only when `CATALOG_FILE` is unset. A `catalog.yaml` names its own under `config.search_profiles`. |
| `RANK_DEFAULT_FILE` | `conf/rank_default.yaml` | The default rank expression for the built-in catalog. It applies only when `CATALOG_FILE` is unset. A `catalog.yaml` names its own under `config.ranking`. |
| `SEARCH_PROFILE_OVERRIDES_FILE` | unset | A YAML file applied over the search profiles of `CATALOG_FILE` when the process compiles it. `conf/search_profile_overrides.example.yaml` shows the format. |
| `SEARCH_BACKEND` | `pg` | `pg` or `turbopuffer`. The backend for `_system` records, such as audited runs, which have no search profile of their own. Public collections use the backend of their search profile. |
| `TOUCH_ON_READ` | `true` | Sets `last_accessed` to the current time on every record a search, a dereference, or a context document returns. When `false`, reads do not write, and `last_accessed` stops changing. |
| `RRF_RANK_CONSTANT` | `60` | Must be between 1 and 1,000. No code reads it. The fusion constant comes from `fuse.rank_constant` in each search profile, which also defaults to 60. |
| `MAX_COLLECTION_FANOUT` | `8` | The most collections one search may span under the `per_collection` Turbopuffer layout. Cannot exceed 8. A wider search fails with `per_collection search exceeds MAX_COLLECTION_FANOUT; narrow collections`. |
| `SEARCH_MAX_CONCURRENCY` | `8` | The maximum number of PostgreSQL search queries running at once in one process. |

### Turbopuffer

These settings apply only to search profiles whose backend is `turbopuffer`, or
when `SEARCH_BACKEND=turbopuffer`.

| Setting | Default | What it does |
| --- | --- | --- |
| `TURBOPUFFER_API_KEY` | empty | The bearer token sent to Turbopuffer. A Turbopuffer search fails if it is empty. |
| `TURBOPUFFER_REGION` | `gcp-us-central1` | Selects the endpoint `https://<region>.turbopuffer.com`. Ignored when `TURBOPUFFER_BASE_URL` is set. |
| `TURBOPUFFER_BASE_URL` | empty | An explicit endpoint that replaces the region URL. It must be an absolute URL, and it must use HTTPS unless the host is `localhost`, `127.0.0.1`, or `::1`. |
| `TURBOPUFFER_LAYOUT` | `shared` | `shared` or `per_collection`. With `shared`, the collections of a workspace share one namespace. With `per_collection`, each collection has its own namespace. A search profile can set its own layout. |
| `TURBOPUFFER_CONSISTENCY` | `strong` | `strong` or `eventual`. Sent with every Turbopuffer query. |

## Record and pipeline limits

These bound the size of what one request, one record, or one derivation run can
hold. Most fail the request or the run with a named error. Some truncate
instead, and the table says which.

### Ingest

| Setting | Default | What it does |
| --- | --- | --- |
| `MAX_BATCH` | `100` | The most records one `POST /records` request may carry. |
| `MAX_TEXT_CHARS` | `65536` | The longest `text` a record may have. A longer one fails with `text_too_large`. |
| `MAX_CONTENT_BYTES` | `131072` | The largest serialized `content` a record may have. A larger one fails with `content_too_large`. |
| `MAX_ANNOTATION_BYTES` | `32768` | The largest serialized annotation, whether a processor produced it or a client supplied it. |
| `MAX_RUN_CONTENT_BYTES` | `262144` | The largest serialized content of a run record. A larger one fails with `run_too_large`. |
| `MAX_DERIVED_FROM` | `256` | The most parent records one record may cite in `derived_from`. Cannot exceed 256. More fail with `too_many_parents`. |
| `MAX_DERIVATION_DEPTH` | `4` | The deepest a record may be. A record with no parents has depth 0. Any other record is one deeper than its deepest parent, except that a new version of a keyed record keeps the depth of the version it replaces. A deeper record fails with `depth_limit`. Cannot exceed 16. |

### Enrichment

Enrichment is the worker lane that embeds, scores, and annotates new records.
See [Processors](processors.md).

| Setting | Default | What it does |
| --- | --- | --- |
| `ENRICH_BATCH` | `32` | The most records one enrichment pass claims. |
| `ENRICH_LLM_BATCH` | `16` | The most records an LLM scorer or annotation processor sends in one model call. |
| `SCORER_TEXT_CHARS` | `12000` | The longest record text an LLM scorer, an annotation processor, or a judgment model sees. Longer text is cut from the middle, so its beginning and end remain, and the result records that it was truncated. |

### Derivations

See [Derivations](derivations.md). Each derivation declares its own `limits`.
These settings are the ceilings those limits must stay under.

| Setting | Default | What it does |
| --- | --- | --- |
| `MAX_DERIVATION_CONFIG_BYTES` | `65536` | The largest a normalized derivation definition may be. A larger one fails the catalog compile. |
| `MAX_CITATIONS_PER_OUTPUT` | `64` | The most citations one record emitted by a derivation may carry. Every emitted record needs at least one. |
| `MAX_STEP_CONCURRENCY` | `5` | The most steps of one derivation run that execute at once. |
| `MAX_RUN_TOTAL_TOKENS` | `100000` | The largest `limits.max_total_tokens` a derivation may declare. The answer endpoint uses at most 60,000, capped by this value. |
| `MAX_RUN_WALL_S` | `180` | The largest `limits.max_wall_s` a derivation may declare. The answer endpoint uses at most 150, capped by this value. |

### Reads and renders

| Setting | Default | What it does |
| --- | --- | --- |
| `MAX_RESPONSE_BYTES` | `4194304` | The largest JSON response body. A larger one returns HTTP 409 `response_too_large`. Paginated views end a page before it would pass this size. |
| `MAX_DOCUMENT_RECORDS` | `500` | The most keyed records `GET /document` returns. More fail with a request to narrow the collection scope. |
| `MAX_QUERY_CHARS` | `8192` | The longest question or retrieval query the answer endpoint accepts. MCP tool schemas declare it as `maxLength`. |
| `SEARCH_RENDER_TOKENS` | `16000` | The token budget for the rendered text of search results. Results past it are replaced by `[...] truncated`. |
| `MAX_ARTIFACT_RENDER_TOKENS` | `50000` | The largest an artifact render may be. The catalog compile rejects an artifact whose block budgets add up to more. A render that exceeds it fails. |
| `MAX_ARTIFACT_INPUT_RECORDS` | `255` | The most distinct records one artifact render may include. |
| `MAX_GRAPH_DEPTH` | `4` | The deepest graph traversal a request may ask for. Cannot exceed 16. MCP tool schemas use it as the `depth` maximum. |
| `MAX_GRAPH_PATHS` | `100` | The most paths one graph request may return. Cannot exceed 500. MCP tool schemas use it as the `limit` maximum. |

## Computers

See [Computers, Programs & Agents](computers.md).

| Setting | Default | What it does |
| --- | --- | --- |
| `COMPUTER_RUNTIME_URL` | empty | The base URL of the Cloudflare Computer runtime. Required when a Computer sets `provider: cloudflare`. |
| `COMPUTER_RUNTIME_TOKEN` | empty | The shared secret Memseek signs each runtime request with. It must equal the Worker's `MEMSEEK_RUNTIME_SECRET`. |
| `COMPUTER_REQUEST_TIMEOUT_S` | `300` | The timeout for one request to the runtime. |
| `COMPUTER_RESPONSE_MAX_BYTES` | `16777216` | The largest runtime response Memseek accepts. |
| `LOCAL_COMPUTER_ROOT` | `~/.memseek/computers` | Where the `local` provider creates one directory per session, on the machine that runs the worker. |
| `HARNESS_PATHS` | `["<repository>/harnesses"]` | A JSON array of directories searched for harness modules. The first match wins. |
| `SKILLPACK_PATHS` | `["<repository>/skillpacks"]` | A JSON array of directories searched for skill pack modules. The first match wins. |
| `SKILLPACK_CACHE` | `~/.memseek/skillpacks` | Where the files a skill pack copies into a workspace are cached, once per pinned ref. |

| `TRACE_EXPORT_URL` | empty | The OTLP/HTTP traces endpoint that runs of traced Agents export to. Empty exports nothing. HTTPS is required except on localhost or a single-label host such as a compose service name. See [Agent traces](agent-traces.md). |
| `TRACE_EXPORT_TOKEN` | empty | An ingest-only bearer token for `TRACE_EXPORT_URL`. It is passed to tracing harnesses, so it must not grant read access. |
| `TRACE_STORE_URL` | empty | The Jaeger-compatible query API the `trace` task reads runs' traces from, for example VictoriaTraces' `/select/jaeger`. Empty makes `trace` fail with `config`. |
| `TRACE_STORE_TOKEN` | empty | The bearer token for `TRACE_STORE_URL`. It can read every workspace's traces, so only the worker holds it. |
| `TRACE_STORE_TIMEOUT_S` | `10` | The timeout for one trace read. |
| `TRACE_STORE_MAX_BYTES` | `8388608` | The largest trace a read accepts. A larger trace is skipped as `too_large`. |
| `TRACE_STORE_SETTLE_S` | `30` | How long after a run ends its trace may still be arriving. A missing trace younger than this retries the run; an older one is skipped. |

The `COMPUTER_*` and `TRACE_*` names have no `MEMSEEK_` prefix. A prefixed
variable is ignored. [The Cloudflare Computer runtime](computer-cloudflare.md#configuring-memseeks-side)
covers the runtime side.

## Worker and job queue

See [Jobs and freshness](operations.md#jobs-and-freshness).

| Setting | Default | What it does |
| --- | --- | --- |
| `WORKER_POLL_MS` | `500` | How long the worker waits between passes when it found nothing to do. After failed passes, the wait doubles from this value up to 30 seconds. |
| `WORKER_CONCURRENCY` | `4` | Sizes the connection pool. It does not set how many jobs run at once. |
| `INDEX_CONCURRENCY` | `1` | Sizes the connection pool. It does not set how many index jobs run at once. |
| `JOB_LEASE_S` | `300` | How long a claimed job stays with its worker without a heartbeat. After the lease expires, another worker can claim the job. |
| `JOB_HEARTBEAT_S` | `60` | How often a worker renews the lease of a running job. Must be less than half of `JOB_LEASE_S`. If a renewal fails, the worker retries about every second and abandons the job before the lease runs out. |
| `JOB_MAX_ATTEMPTS` | `3` | How many times a job is attempted before it is dead-lettered. |
| `JOB_MAX_RUNTIME_S` | `900` | The wall-time limit on one attempt of a job that is not a derivation. Derivations use their own `limits.max_wall_s`. |
| `JOB_RETENTION_DAYS` | `14` | Finished and dead jobs older than this are deleted. The purge runs every 10 minutes. `0` keeps them forever. |
| `UNREADY_RETRY_S` | `2` | When a derivation job's inputs are not ready, the worker releases it and retries after this many seconds. |
| `UNREADY_TIMEOUT_S` | `3600` | A derivation job still not ready this long after its first release is dead-lettered as `not_ready_timeout`. |
| `CRON_TICK_S` | `15` | How often the worker asks the cron scheduler for due jobs. Must be between 1 and 59. |

## Context, artifact uses, and feedback

| Setting | Default | What it does |
| --- | --- | --- |
| `CONTEXT_DOC_ORDER_SCORE` | `importance` | The score that orders keyed records in context documents. A catalog that defines collections fails to compile unless a processor produces this score. |
| `ARTIFACT_USE_RETENTION_DAYS` | `90` | How long an artifact use accepts feedback. It sets the use's `expires_at`. Cannot exceed 3,650. The learning signals that feedback creates are ordinary records and do not expire with the use. |
| `ARTIFACT_USE_PURGE_BATCH` | `500` | The most expired artifact uses deleted in one purge pass. |
| `MAX_FEEDBACK_COMMENT_CHARS` | `2000` | The longest feedback `comment`. Longer comments are truncated, not rejected. Cannot exceed `MAX_TEXT_CHARS`. |
| `MAX_FEEDBACK_EVIDENCE_CHARS` | `4000` | The longest feedback `expected` and `actual_excerpt`. Longer values are truncated, not rejected. Cannot exceed `MAX_TEXT_CHARS`. |

See [Artifact uses & feedback](artifact-uses.md).

## Catalog publish

| Setting | Default | What it does |
| --- | --- | --- |
| `ADDITIVE_VERIFY_MAX_ROWS` | `50000` | The most stored records a publish checks when it adds a declared key to a collection that already allowed arbitrary content keys. Above this count, the publish asks for a new collection version instead. |
| `BACKFILL_BATCH` | `200` | The most records one backfill pass claims before the worker moves to its other lanes. |

See [Changing definitions](changing-definitions.md).

## API process

| Setting | Default | What it does |
| --- | --- | --- |
| `API_KEY_CACHE_TTL_S` | `60` | How long the API caches a verified API key. Cannot exceed 60. A revoked key can authenticate on a running API process until its entry expires. |
| `API_KEY_CACHE_SIZE` | `1024` | The most API keys the cache holds. The cache stores key hashes and workspace IDs, never keys. |
| `API_HOST` | `127.0.0.1` | No code reads it. The `uvicorn` command line sets the bind address. |
| `API_PORT` | `8000` | Must be between 1 and 65,535. No code reads it. The `uvicorn` command line sets the port. |
| `API_CORS_ORIGINS` | none | The browser origins allowed to send authenticated requests to the API and to `/mcp`. |

`API_CORS_ORIGINS` accepts one origin, several separated by commas or spaces,
or a JSON array. Surrounding quotes are removed. Each entry must be an exact
`http` or `https` origin with no path, query, fragment, or credentials.
Wildcards are rejected. Command-line MCP clients send no `Origin` header and do
not need an entry. See
[Browser workspace explorer](operations.md#browser-workspace-explorer).

## Credentials outside `Settings`

A catalog names the environment variable that holds each provider key, with
`api_key_env`. For example, `OPENAI_API_KEY` for an OpenAI endpoint or
`TYPESAFE_API_KEY` for the `jev` judgment alias. Memseek reads that variable
from the process environment first, then from `.env`. An empty value counts as
absent. The key never appears in the catalog.

The CLI also reads `MEMSEEK_URL` and `MEMSEEK_API_KEY` as defaults for its
`--url` and `--api-key` options.

## Rules checked at startup

The process refuses to start if any of these rules fails.

| Rule | Why |
| --- | --- |
| Every count, size, and duration on this page is positive. `JOB_RETENTION_DAYS` may be `0`. The four `DATABASE_*` guardrails may be `0`, which means unset. | A zero or negative limit has no meaning. |
| `MAX_PROMPT_TOKENS + MAX_OUTPUT_TOKENS <= MODEL_CONTEXT_TOKENS` | A prompt and its answer must fit in the context window. |
| `MAX_ARTIFACT_RENDER_TOKENS <= MAX_PROMPT_TOKENS` | A rendered artifact goes into a prompt. |
| `SEARCH_RENDER_TOKENS <= MAX_PROMPT_TOKENS` | Rendered search results go into a prompt. |
| `MAX_ARTIFACT_INPUT_RECORDS <= MAX_DERIVED_FROM - 1` | A render's inputs become parents, and one parent stays reserved for provenance. |
| `MAX_CITATIONS_PER_OUTPUT <= MAX_DERIVED_FROM - 1` | An emitted record's citations become parents, and one parent stays reserved for provenance. |
| `MAX_DERIVED_FROM <= 256` | The record schema allows 256 parents. |
| `MAX_DERIVATION_DEPTH <= 16`, `MAX_GRAPH_DEPTH <= 16`, `MAX_GRAPH_PATHS <= 500` | Schema limits. |
| `MAX_COLLECTION_FANOUT <= 8` | Hard limit on a `per_collection` search. |
| `JOB_HEARTBEAT_S < JOB_LEASE_S / 2` | A worker gets at least two chances to renew before its lease expires. |
| `API_KEY_CACHE_TTL_S <= 60` | A revoked key stops working within a minute. |
| `ARTIFACT_USE_RETENTION_DAYS <= 3650` | Ten years at most. |
| `MAX_FEEDBACK_COMMENT_CHARS` and `MAX_FEEDBACK_EVIDENCE_CHARS` are at most `MAX_TEXT_CHARS` | No feedback field may be longer than record text. |
| `1 <= RRF_RANK_CONSTANT <= 1000`, `1 <= CRON_TICK_S <= 59`, `1 <= API_PORT <= 65535` | Range checks. |
| `SEARCH_BACKEND`, `TURBOPUFFER_LAYOUT`, and `TURBOPUFFER_CONSISTENCY` hold one of their listed values | Each accepts a fixed set. |
| `TASK_MODULES` entries are dotted Python module names | Each entry is imported. |
| `TURBOPUFFER_BASE_URL`, when set, is an absolute URL that uses HTTPS unless the host is local | The API key travels in a header. |
