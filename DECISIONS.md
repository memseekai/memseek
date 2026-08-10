# Memseek implementation decisions

This file records choices left open by the v3.2 specification. Normative requirements in the
specification take precedence, except where a later dated entry below explicitly amends the
specification's authoring surface.

## Answer scope is declared, not a fixed vocabulary — 2026-08-03

`POST /answer` resolved a hardcoded tuple of ten collection names — `main`, `pages`,
`profiles`, `reflections`, `worldview`, `atoms`, `facts`, `patterns`, `concepts`, `takes` —
which was the union of the default catalog and the gbrain example. Every other read surface
is author-named, so a workspace whose collections describe its own domain got
`422 answer_unavailable` from the one capability it could not rename its way into. Building
`examples/agent_memory_catalog/` surfaced it: a catalog whose layers are `messages`,
`memories`, `scenes`, and `persona` could not answer at all.

### Collections declare `answerable`, and the default is `false`

A collection opts in with `answerable: true`. Two alternatives were rejected. Inferring
answerability from *having an embedding processor* would have silently made raw transcripts,
prompt snapshots, and learning signals into synthesis sources — searchable and citable are
not the same permission as paraphrasable, and the old name list was in fact an implicit
curation of exactly that distinction. Inferring it from *being in a package* would have made
the permission a property of packaging rather than of the data.

The default is `false` because the failure mode of a wrong default is asymmetric: a missing
opt-in produces one clear error at the call site, while an unwanted opt-in silently widens
what a model may read and restate.

`answerable` is a **binding**, not part of the record contract. It changes what else may read
a row, never what the row means, so it joins `active`, `optional_processors`,
`search_profile`, and `allowed_search_profiles` in `BINDING_FIELDS` — editing it never
strands a stored record or requires a new collection version.

### `POST /answer` takes an `entities` scope

The endpoint had no entity filter, so it synthesized over every entity in those collections.
For a single-corpus workspace that is merely imprecise; for a workspace holding several
agents or customers it is a disclosure surface, and it was reachable through the read-only
MCP `answer` tool. `entities` is now an optional, bounded list (≤100 values, each ≤128
characters, no `*`), applied to the one search the endpoint runs, and it is part of the
saved-answer dedupe key so two scopes cannot collide on one stored synthesis. Omitting it
still answers over everything, because narrowing an existing default silently would be the
worse failure.

The save target remains `syntheses` with entity `answer`, which is still a fixed name: a
catalog without that collection cannot use `save: true`. That is a smaller version of the
same defect and is deliberately left for a later decision.

## Definition evolution — 2026-08-03

Implements every phase of `docs/schema-evolution-plan.md`. The safety guarantee is
unchanged — a stored record is still never reinterpreted — and everything below
exists so that guarantee stops making ordinary change expensive.

### A record is bound to its contract, not to its whole definition

`record.collection_hash` now covers the **record contract** (`mode`, `schema`,
`text_projection`, `fields`, `required_processors`) rather than the whole
collection block. `active`, `optional_processors`, `search_profile`, and
`allowed_search_profiles` are *bindings*: they change what else happens to a
record, never what the record means, so editing them cannot strand a row.
`required_processors` deliberately stays in the contract because readiness gates
visibility. `definition_hash` is unchanged and still feeds `catalog_hash`, which
is what makes the pre-split rewrite deterministic — the loader computes both
identities from the same definition.

### Additive acceptance is proved, never assumed

A closed allowlist publishes in place over stored records: a new optional
property, a new declared field, `additionalProperties` relaxed from `false` to
`true`, reordered `required_processors`, and a field repointed along a declared
supersession chain. Each has a subsumption argument. Two cases cannot be settled
structurally, so the publish checks real rows instead of guessing: a property
added to an `additionalProperties: true` schema is validated against the values
that already exist, and a repointed field requires that no record yet holds the
newer annotation. Both are bounded by `ADDITIVE_VERIFY_MAX_ROWS`; above it the
publish asks for a new version rather than scanning an unbounded table inside a
transaction. The allowlist is kept short on purpose — "probably fine" cases such
as a widened `enum` were rejected until someone asks with a real workload.

### Stored hashes are rewritten eagerly, inside the publish

An accepted additive publish rewrites stored contract hashes in the same
transaction. Lazy rewriting would leave two identities live for one version.
`memseek migrate-collection-hashes` exists for workspaces written before the
split; it is idempotent, resumable, batched under the workspace lock, and reports
rather than repairs genuine drift.

### One classifier, three consumers

`definitions/compat.py` is the single authority for what a change means. The
preflight (`POST /catalog?dry_run=true`, `GET /catalog/compatibility`), the publish
gate, and the hash-rewrite command all read it, so they cannot disagree. A refused
publish returns the same report a preflight would have, under `compatibility`, so
`409 catalog_incompatible` names every blocker with the rows it protects and the
action that fixes it.

### Backfill is an explicit lane, not a wider sweep

Applying a processor to stored records became a first-class operation
(`annotation_backfill` job kind plus a durable `backfill` row) rather than a
broadened implicit sweep. An explicit target is what lets a backfill reach a
frozen collection version whose YAML must not change, and a durable handle is what
makes progress, cost, cancellation, and completion visible. Annotation names stay
write-once, so a backfill can only fill absences — never rewrite history — which
also makes a repeat run a no-op. A unique partial index enforces one live backfill
per `(collection version, processor)` so two operators cannot race the same rows.

**No budget is the default.** `max_rows` is optional and omitted means "reach every
eligible record", because "just migrate everything" is the common intent and should
not require the caller to chunk anything. A budget exists only to impose a ceiling,
and reaching one finishes the backfill as `done` with `scanned == max_rows`; a
budgeted backfill is deliberately a *slice*, and re-requesting the same target
resumes automatically because selection is by absence of the annotation.

Making that safe is what fixes the lane's granularity: **one batch per claim, one
claim per worker pass.** A successor is queued the moment its predecessor
completes, so a drain loop here would let one whole-corpus backfill occupy the
worker until it finished. Bounding the pass to `BACKFILL_BATCH` records keeps every
other lane serviced while a long migration runs, and marking such a pass busy keeps
throughput high without a poll delay. `BACKFILL_BATCH` is therefore the single
operator knob, and it means one thing: the interleaving granularity.

**Completion is confirmed, not assumed.** Row selection skips records another lane
holds locked and the cursor advances past them, so reaching the end of a sweep does
not prove the work is done. An exhausted sweep rewinds and sweeps again, and the
state becomes `done` only when a sweep from the first record finds nothing
eligible — which is why a completed handle reports `cursor_seq: 0`. The cost is one
extra filtered scan per backfill, and it buys the right to treat `done` as a fact.
For the same reason the lane's busy signal counts *claims*, not annotations: a
confirming sweep writes nothing and must still continue.

### Supersession is a read preference, never a rewrite

`supersedes` on a processor leaves both annotations on the record and changes only
what readers prefer. The loader injects fallback paths into declared fields and
excludes them from serialization, so declaring a supersession cannot change a
record contract. The SQL expression, the canonical Python recheck, and index
projection resolve the same chain in the same order — the recheck is authoritative,
so they must not drift. A filter or sort over an annotation-backed field is legal
when *any* name in the chain is required, which generalizes the previous rule
without weakening it.

### A migration is a derivation, not a new concept

Moving a corpus to a new version is a pipeline emitting through the deterministic
`map_records` Task. That inherits provenance (`derived_from` back to each
original), bounded claim-fenced execution, reviewed emission, and erasability from
machinery that already exists. The mapping is deliberately not a language — copy,
read a path, default, cast — because anything richer belongs in an `llm` Task where
the output is still schema-validated and reviewable.

A whole-corpus migration uses a **`changes`** source, not `snapshot`. Emission is
capped at 100 records per run, so covering a corpus needs a cursor; a `changes`
source has one, consumes forward, and the lane queues its own successors until it
drains, so one request migrates everything exactly once. A `snapshot` source
deliberately reads its complete declared scope or refuses with `budget` — it never
silently truncates — which is the right guarantee for a reviewed correctness check
over a bounded set and the wrong one for bulk migration. That asymmetry is
intentional and documented rather than smoothed over.

### Embedding spaces are staged, not swapped

`record_embedding` holds vectors for spaces that are not active. That is what
allows a model migration to be prepared while the active space serves every read,
verified for coverage before promotion, and reversed afterwards — the outgoing
vector is staged under its own space id on the way out, so the previous space stays
complete. Cutover refuses an incomplete space because promoting a partial one
silently drops records out of vector recall. `embedding.dimensions` must match the
`vector(n)` column the schema provides, which ships as 1536, so same-dimension
swaps are the supported case.

### A provider is a named connection, and the embedding model is its own block

Providers used to be adapter names, and every model in a deployment shared one
base URL and key read from process settings. That made "use a different embedding
model than the completion model" impossible to express, however the alias was
written. A `providers:` entry now names one *endpoint* — adapter, base URL, the
environment variable holding its key, and the JSON/token-limit quirks that belong
to that endpoint rather than to the process — and alias targets read
`provider_id:model`. Two entries may share an adapter and differ only in URL,
which is exactly the case that was unreachable before.

The embedding model was, in the same spirit, a specially-named alias (`embed`)
policed by ad-hoc checks in four files, with the properties that actually make
two vectors comparable — dimension, space, truncation bound — living in the
environment instead. It is now an `embedding:` block declaring all of them
together, because a stored, later-compared value needs more said about it than a
chat model does, and because a model change and the space change it forces should
be visible in one diff. The whole block feeds the embedding processor's config
hash, so an annotation is always attributable to the model that produced it.

Credentials stay out of YAML: the definition names an environment variable rather
than holding a key, which keeps a catalog safe to commit while still stating which
credential an endpoint uses instead of leaving it to a global fallback.

One consequence is deliberate and not smoothed over: the persisted `resolved`
identity is now `provider_id:model` rather than `adapter:model`. Two endpoints
serving the same model name are not interchangeable, and the stored identity has
to be able to say so — so an existing deployment's startup check will report the
change rather than accept vectors whose origin it can no longer name.

### Cursor rebinding makes the operator say which they meant

A `changes` pipeline still refuses to run when its source scope no longer matches
its cursor. `reset` and `carry` are the two intents, and both write a
`_system/cursor_rebind` audit naming the old and new source hashes. The refusal
remains the default because silently skipping or double-counting rows is worse
than stopping.

### Pruning reports proof, not permission

`memseek catalog-prune` counts real references — records for a collection version,
annotations for a processor, runs for a derivation, use handles for an artifact —
and marks `safe_to_delete` only at zero. Active versions are never offered. A
`retired: true` author assertion was considered and deferred: the counts are the
evidence, and an assertion that can be wrong adds nothing.

### Evolution is reachable without a shell

A tenant of a hosted deployment has a workspace key and no command line, so every
evolution operation it owns is a workspace-scoped route: preflight, backfill,
cursor rebinding, `GET /catalog/prune`, and `POST /reindex`. The CLI keeps the same
operations for an operator standing next to the database, and remains the only
entry point for the two that are genuinely deployment-wide — `reembed` and
`migrate-collection-hashes`. The split is by trust boundary, not by convenience:
CLI commands are scoped by `--workspace` against a direct connection, routes are
scoped by the bearer key and can only ever touch the caller's own workspace.
`prune` is read-only; `reindex` plans projection jobs and never rewrites canonical
records, and a full `reset` still requires an explicit `confirm` outside a test
database.

## Artifact uses, learning targets, and feedback — 2026-07-26

Implements milestones 1–3 of `spec/Artifacts, Feedback L.md`. The public
vocabulary stays at six primitives: an artifact use is a system-owned
correlation handle, not a seventh primitive and not a `context` primitive.

### A stored use row, not a signed receipt

`POST /artifacts/{name}/uses` renders and registers one `artifact_use` row and
returns its ID. The signed-token alternative was rejected for now: a stored row
keeps the SDK surface small, the client payload short, and revocation and
lookup trivial, at the cost of one lightweight row per bound use plus a
retention policy. Revisit if stateless edge use or write volume demands it.

- The use ID is a plain UUID rather than the specification's illustrative
  `use_01K…`. Every other identity on this API surface is a UUID, and a second
  ID encoding would have to be threaded through storage, erasure, and the SDK
  for no behavioral gain.
- The row has columns for identity, hashes, resolved learning target, optional
  snapshot ID, and expiry — and no column able to hold a render, request
  parameters, a response, or a trace. Request parameters are excluded
  deliberately: a `task` parameter is untrusted user content.
- `snapshot_id` is a real foreign key with `on delete set null`, so erasing a
  snapshot cannot leave a dangling reference or block a purge.
- There is no ordering constraint between `created_at` and `expires_at`:
  shortening `ARTIFACT_USE_RETENTION_DAYS` must be able to retire handles
  registered under a longer window.

### Learning targets bind a block to a reviewed artifact

`ArtifactDefinition.learning` declares `target_block` plus an exact
`name@version` reference to the reviewed artifact that owns that value's
promotion lifecycle. The specification's nested `artifact:` block syntax was not
adopted: artifact composition remains a separate future change, and the shipped
model already reaches a maintained skill through a document block over its
collection.

Resolution happens at render time and yields the exact keyed heads in force
plus `base_run_id`. Consequences, all enforced rather than documented:

- A target block must be a required document block on `status: active`. A view
  block is a ranked selection whose membership is not a promotable unit.
- The referenced artifact must be `lifecycle: reviewed` and must maintain the
  target block's collections. A package must list it.
- Heads sharing one run yield that run as `base_run_id`; mixed runs yield
  `null` rather than guessing a base. A block that read no head resolves to no
  target at all, so a signal can never be attributed to a version that was
  never used.
- Cross-artifact resolution runs at the end of `_load_artifacts`, before package
  closure, so an unknown reference reports `reference` rather than the less
  specific `package_dependency`.

### Feedback is an ordinary record

`POST /artifact-uses/{id}/feedback` writes through the public record path into
the `learning_signals` collection (`agentic_memory_core@2.2.0`), so dedupe,
schema validation, declared fields, provenance, search, and erasure keep their
existing semantics. The entity is `artifact:<reviewed artifact name>` and the
record type is the signal kind, which is what lets a candidate derivation
select signals with an ordinary `changes` source.

- Optional signal members are omitted rather than written as `null`, because a
  declared field must hold its declared scalar type when present. `snapshot_id`
  and `learning_target` keep explicit `null`, where the null is itself a claim.
- Client dedupe keys are namespaced `feedback:<use_id>:<key>` so feedback cannot
  collide with a client's own record dedupe keys.
- With a snapshot the signal cites it in `derived_from`, so ordinary erasure
  closure reaches the signal. Without one the signal carries identity and hashes
  as metadata and claims no provenance edge: the render is not reconstructable
  after its sources change, and the system does not pretend otherwise.
- `execution_refs` are bounded and informational. They never become provenance
  edges, and no processor may fetch one.

### Expiry purges in the worker pass, not a job lane

Retention is a deployment setting rather than a package policy, and the rows are
operational metadata with no provenance closure, so one bounded
`ARTIFACT_USE_PURGE_BATCH` page per worker pass across all workspaces replaces a
new job kind. A purged page marks the pass busy so the backlog drains without a
poll delay.

### Telemetry stays scalar and optional

`telemetry_attributes` emits only the reserved `memseek.*` scalars, omitting
`snapshot_id` when absent. OpenTelemetry is an optional extra
(`memseek[opentelemetry]`); `use()` opens a span when it is installed and is a
no-op context otherwise. Memseek requires no specific backend and works with
none.

## Pipeline computation Interface — 2026-07-18

This entry supersedes the author-facing derivation shape recorded under
"Derivation prompt names are author-declared" and the public configuration
details in M4. The M4 storage, provenance, boundedness, audit, and transactional
commit guarantees remain in force.

### Named Sources replace input/state/context transition configuration

- A derivation file now declares a general `PipelineDefinition`: named
  `sources`, ordered registered `tasks`, and one `emit` destination. Exactly one
  Source drives a run with `kind: changes | snapshot`; optional Sources use
  `current`, `record`, or `view`.
- Authors no longer configure Evaluation Basis modes, watermarks, predecessor
  policy, state-transition effects, or active/draft storage status. The runtime
  derives those details from Source and emission intent and persists them in the
  run audit.
- Every named record Source exposes both typed `.records` and escaped, unfenced
  `.rendered` row values; fencing them is the prompt author's decision, not the
  renderer's. Core values are limited to `entity` and
  `run.now|checkpoint|source_ids`. Source and Task names share one namespace;
  every declared value must contribute to a later Task or the final emission.
- `changes` consumes a bounded ready suffix and may chain a successor job.
  `snapshot` reads the complete matching scope through one exact checkpoint and
  fails rather than truncating when its record or token bound cannot hold the
  whole scope. `current` and `record` Sources are rechecked before commit. A
  `view` Source contributes its exact selected record IDs to provenance.
- Every successful changes run stores `source_hash` over only cursor-membership
  fields. Prompt, Task, emission, and limit changes continue at the same cursor;
  changing the driving collections, versions, types, statuses, kind, or keyed
  membership is rejected with guidance to use a new Pipeline identity or a
  snapshot. There is no authored definition-change switch.

### Registered Tasks are the computation seam

- Each Task call has `id`, `use`, optional typed per-run `input`, and validated
  static `with` configuration. Calls run in declaration order and may reference
  Sources and earlier Task results. There is no special final-LLM rule.
- `llm`, `search`, and `template` are built-in Task Adapters. Deployments may
  register additional async Tasks before compiling a catalog. Each registration
  supplies a strict `TaskConfigModel` and immutable implementation hash;
  `TASK_MODULES` loads the same registration code in API and worker processes.
- The generic `llm` Task declares its complete object-root JSON Schema inline
  in `output_schema`. Schema-capable provider Adapters use that exact contract
  as their primary structured-output mode, and schema failure still
  participates in the local correction attempt. Intermediate LLM values are
  therefore typed before a downstream Task consumes them.
- A Task registration declares Pydantic configuration plus input and output
  adapters. Workspace YAML may select it but cannot upload Python. A Task
  receives a constrained `TaskContext` with bounded completion, search, and
  rendering capabilities; it receives neither a database connection nor a
  canonical record writer.
- Task values carry transitive source IDs and a narrower set of directly citable
  IDs. A Task may narrow either set but cannot invent provenance or widen
  citation authority. Task implementation hashes and output hashes are recorded
  per run. Canonical run lineage remains conservative over every admitted
  source so erasure cannot leave an advanced cursor that skipped evidence.

### One emission vocabulary, inferred commit semantics

- `emit.from` is one exact typed reference to a Task result. Every proposed row
  uses the same fields: optional `key`, `text` and/or `content`, `citations`, and
  optional `retract`.
- An emission without `keys` appends unkeyed events. An emission with `keys`
  updates only emitted keys and leaves omissions unchanged. `complete: true`
  requires exactly one proposal for every declared key. `review: required`
  stages a bounded keyed emission as draft records; otherwise it commits active
  records immediately.
- The static collection, type, collection version, bounded key universe, review
  policy, and record cap are resolved at catalog load. This lets the private
  Evaluation Basis capture target heads before arbitrary Task computation.
- The private Candidate Set Module compiles and validates drafts, infers append,
  partial update, or complete replacement, calculates divergence, and feeds the
  same guarded canonical commit and Promotion machinery used previously.

## Authoring-surface revision — 2026-07-17

This entry deliberately amends the v3.2 YAML authoring surface (Sections covering scorers,
annotation processors, and derivation templates). No storage or wire-protocol change; the record
table, `scores`/`annotations` columns, and vector column are unchanged.

### One processor concept

- `conf/scorers.yaml` and `conf/annotation_processors.yaml` merged into `conf/processors.yaml`.
  A processor declares `kind: embedding | score | json` (what shape it writes) and, for score and
  json, `source: llm | client | constant` (who produces the value). The former annotation kinds
  `llm_json`/`client`/`constant`/`scorer` and the `mode: annotate` field are gone.
- Rationale: the storage special cases (flat `scores.*` map, vector column) are justified by rank
  expressions, accumulator triggers, and the HNSW index — but the authoring taxonomy was not.
  There were three ways to be a processor and two ways to produce a score. Scores and embeddings
  are now documented as storage projections of processor output, not separate processor
  categories.
- Every processor now declares an `input` scope, scores included; the "binding collection must be
  in input.collections" and "required processor must not narrow input.types" rules apply
  uniformly. The embedding and score annotation contracts are synthesized
  (`effective_output_schema`), never authored.
- Score-name registry: score processor names plus `score_fields` promotions share one collision
  checked namespace, capped at 8 names with at most 4 llm-source score processors. Promoted names
  are now first-class scores: valid in rank expressions, boosts, accumulator shorthand, and
  `CONTEXT_DOC_ORDER_SCORE`.
- `DefinitionCatalog.scorers`/`annotations` became `processors`, plus `score_names` and
  `score_owners` (score name → owning processor). `DefinitionSources` takes one `processors`
  tuple. Workspace catalog uploads accept `conf/processors.yaml` or `conf/processors/*` fragments.

### Derivation prompt names are author-declared (superseded 2026-07-18)

- The short-lived `input.as`/`state.as`/`context` prompt-binding design was
  replaced wholesale by named Sources and registered Tasks. No compatibility
  syntax remains. Its durable rationale—domain-named values, explicit core
  references, bounded reads, and compile-time reference validation—is carried
  forward by the Pipeline Interface above.

## M8 — 2026-07-17

### Workspace-owned definition packages

- Memseek is a service for many workspaces, so user definitions are not process-wide settings.
  The shipped catalog is a bootstrap fallback only; an authenticated workspace can install one
  immutable package through `POST /catalog` and inspect its identity with `GET /catalog`.
- The upload is a bounded map of relative YAML paths to source text. It is compiled through the
  same duplicate-key, schema, reference, graph, capability, and canonical-hash validator used by
  the filesystem catalog. The database stores package identity, catalog hash, and source files
  in `workspace_catalog`; it never stores an unvalidated partial catalog.
- Installation takes the workspace advisory lock and checks every existing public row's exact
  collection identity before replacement. This prevents a new package from reinterpreting old
  records. A compatible package replacement is atomic; incompatible replacement returns a
  machine-readable conflict.
- API reads and writes resolve the catalog after bearer authentication. Worker enrichment,
  projection, and derivation lanes resolve the package by claimed workspace. Unimplemented
  provider/backend execution remains outside package loading.
- API and worker startup therefore verify structural storage compatibility only; applying one
  process-wide processor/hash expectation to every tenant would make a valid custom package look
  like drift. Collection identity and processor metadata are rechecked at the workspace operation
  boundary where the selected catalog is available.
- YAML remains the reviewable interchange format, while `DefinitionSources` remains an optional
  Python generation path. The SDK exposes explicit resource operations: `catalog.publish` reads a
  directory and `catalog.publish_files` accepts generated path-to-text mappings. Both require the
  exact package reference; the client never guesses deployment intent from manifest contents.
  Both call the same service endpoint without coupling tenant definitions to server settings.
- The OpenAI-compatible adapter defaults to the current `max_completion_tokens` request field.
  Deployments targeting a legacy compatible server can explicitly select `max_tokens`; this is a
  process transport option and is not embedded in workspace definitions.
- The OpenAI-compatible Adapter declares native JSON Schema and JSON-object capabilities. Native
  schema output is selected by default; deployments configure `json_object` or `none` before
  startup for weaker compatible endpoints. Capability selection is never changed in response to a
  rejected request, and every result remains locally schema-validated.

## M0 — 2026-07-15

### Packaging and supported Python

- The distribution uses the `src/` layout and `uv_build` through PEP 517.
- Runtime support is intentionally `>=3.14,<3.15`; `.python-version` selects 3.14.6 and both Ruff
  and `ty` analyze as Python 3.14. This keeps the foundation on the latest stable Python release
  series rather than carrying compatibility branches for older interpreters.
- `uv.lock` is committed. Runtime dependencies live in `project.dependencies`; pytest, Ruff, and
  `ty` use the PEP 735 `dev` dependency group.
- Ruff owns formatting and a curated correctness-oriented rule set. `ty` analyzes `src/` and
  `tests/` for all platforms, treats warnings as failures, and enables the stricter possibly
  missing import/attribute and missing generic type-argument rules.
- `examples/reference.py` is a byte-for-byte copy of `spec/reference.py`. It is intentionally
  excluded from Ruff and `ty`; `make test` checks parity and executes it separately.

### Definition assets and identity

- Definition files remain external deployment assets and are resolved from the working directory.
  Wheels contain Python code, not an implicit replacement catalog.
- Combined Appendix A blocks are split by resource boundary: the eight general collections live in
  `collections/core.yaml`, calendar lives in `collections/calendar.yaml`, and each shipped view and
  artifact has its own file.
- The optional A7 deployment overlay is shipped as
  `conf/search_profile_overrides.example.yaml`; it is not loaded unless an operator copies/selects
  it with `SEARCH_PROFILE_OVERRIDES_FILE`.
- There are no shipped standalone trigger definitions. The four package trigger names are the
  normalized `.default` identities of the derivation-local triggers, and `triggers/` is retained as
  the extension directory.
- Canonical hashes use compact, sorted, finite JSON. Versioned semantic hashes omit only `active`;
  the whole-catalog hash includes active choices and deployment profile bindings.
- The shipped `skill` processor is an ordinary two-Task Pipeline with a complete reviewed keyed
  emission. Its shape uses the same Source, Task, citation, and Promotion contracts as every
  other Pipeline; the loader has no skill-specific computation branch.

### PostgreSQL and process lifecycle

- Alembic owns schema history and uses its standard `alembic_version` table. SQLAlchemy is present
  only for migrations; application and worker database access remains raw async psycopg.
- The first Alembic revision executes the immutable `migrations/001_init.sql` input byte-for-byte
  after checking its pinned SHA-256 digest. Online upgrades use a transaction-scoped PostgreSQL
  advisory lock to serialize concurrent migrators.
- Pools are created with `open=False`. API and worker lifespans explicitly open, wait for, verify,
  and close them; connections set the session timezone to UTC.
- Advisory-lock values use domain-separated SHA-256 digests converted to signed PostgreSQL
  `bigint`. Entity locks are acquired in sorted order where more than one entity is involved.
- The M0 worker does not claim jobs because no job kind has an M0 runtime handler. The queue
  primitives are independently available and tested for later workers.

### Authentication and queue safety

- Workspace bearer keys contain 32 random bytes encoded for one-time disclosure. Only lowercase
  SHA-256 digests are stored or cached, and comparisons use constant-time digest comparison.
- The authentication cache is process-local, bounded to 1,024 entries, and expires after 60
  seconds. Entries contain only a key digest and workspace ID; revocation may therefore take at
  most one cache TTL to be observed by a warm process.
- Job lease comparisons use PostgreSQL wall-clock time, not a worker clock. Every mutation after a
  claim is fenced by both job ID and claim token, and heartbeat/completion also require an
  unexpired lease.
- A not-ready release refunds the claim attempt. A normal failure schedules bounded retry or moves
  the job to dead state at the configured maximum; an expired final attempt is reaped to dead
  before new claims.

### Local verification

- Compose exposes only an isolated `memseek_test` PostgreSQL 16 + pgvector service, on host port
  55432 by default, with database storage in tmpfs.
- `make test` refuses a database whose final URL path does not contain `test`. The default service
  is started and removed automatically; `TEST_DATABASE_URL` selects an operator-provided test
  database and suppresses Compose startup.
- Fake LLM mode is forced for pytest. No M0 validation or test contacts an LLM or search provider.

## M1 — 2026-07-15

### Canonical ingest and client outputs

- `records.py` is the public semantic insertion boundary. It resolves the exact collection
  version/hash, constructs projected text before schema validation, validates provenance under the
  shared workspace lock, and calls readiness/current-projection hooks in the same transaction.
- `canonical_records.py` is the single physical record-write boundary for public targets, client
  audit runs, enrichment audit runs, and relation rows. It owns storage-shaped finite-JSON,
  namespace, provenance-shape, depth, content, and per-annotation-entry bounds; callers retain
  semantic preparation and the workspace/job/entity/record lock order.
- Dedupe equality compares canonical immutable storage plus every explicitly supplied optional
  value. A server-generated `occurred_at` is intentionally ignored on an otherwise exact retry.
- Client scorer values are clamped and mirrored into their annotation object. Client values are
  accepted only for processors declared `kind: client`; a missing required client scorer is a
  request error because no later worker is authorized to invent it.
- A public-only collection may require a client scorer, but a derivation may not target such a
  collection because derivation output has no trusted client-value channel. This is rejected while
  loading the derivation graph instead of leaving its outputs permanently unready.
- Client annotations and scores receive the same per-target `_system/run` audit records as worker
  annotations. Exact dedupe retries do not create new runs or outbox work.

### Enrichment and provider execution

- The worker runs one required enrichment unit at a time. A raw-record unit is bounded by
  `ENRICH_BATCH`; a derivation-output unit is exactly one `run_id` group. Provider calls occur
  outside row-locking transactions and finalization rechecks write-once keys.
- Readiness for a derivation `run_id` group is computed before any sibling is updated and applied
  all-or-none. A sibling with no required processors therefore cannot become visible before a
  blocked sibling from the same output group.
- Embeddings use batches of at most 64. LLM scorers and generic JSON annotations use deterministic
  token-packed sub-batches after exact middle truncation. Required failures terminate with a
  schema-valid default (or NULL embedding) and compact diagnostics so an unready row cannot become
  a permanent barrier.
- Model resolution, fallback, retry, effective parameters, usage estimates, and hashes are
  recorded per actual attempt. `LLM_FAKE=1` substitutes the deterministic provider while preserving
  configured model names as `fake:model`; the OpenAI-compatible adapter keeps trusted system and
  untrusted user messages separate.
- Annotation names are write-once semantic identities. Changing behavior requires a new processor
  name and explicit backfill/replacement rather than mutation of historical annotations.
- An optional processor without a default records a failed audit run and a hash-bound terminal
  marker when its bounded attempt cannot produce valid output. This preserves the absent optional
  annotation without repeatedly selecting the same row and starving later best-effort work.
- The OpenAI-compatible HTTP client is reused across calls and explicitly closed with the worker
  runtime. Embedding shape and finiteness failures are response failures inside the bounded retry
  loop, so every malformed attempt remains visible in run audit metadata.

### Readiness, projection, and relations

- `on_records_ready_tx()` is the only post-readiness seam. It rejects unready input, enqueues a
  no-dedupe projection job, refreshes keyed current candidates, reaches the trigger barrier, and
  optionally coalesces contradiction work. M1's general trigger hook is deliberately a no-op until
  its execution milestone.
- Projection jobs contain record IDs and last-known collections only. Every attempt reloads
  PostgreSQL, recomputes keyed currentness against ready and unready versions, routes by the active
  collection search profile, and translates a missing upsert target into a delete. The built-in
  PostgreSQL projection adapter is an idempotent no-op.
- Stored public rows are resolved by the full collection name/version/semantic-hash identity.
  Startup requires an explicit migration for a missing or changed definition, and projection
  rechecks the identity before backend I/O so a row is never reinterpreted through drifted schema.
- `CONTRADICTION_CHECK=1` adds one immutable built-in `contradiction_relation` definition to the
  catalog. Ready public stimuli coalesce per entity. Successful/noop relation runs advance a
  sequence watermark and cite their predecessor run as a control dependency; this preserves the
  erasure/rebuild chain while excluding that predecessor from semantic depth.
- Canonical transactions that can interact with a relation lane lock its active job row in UUID
  order before entity and record locks. Relation completion and any coalesced successor enqueue
  occur in one fenced transaction, preventing both lock inversion and a lost ready stimulus.
- The worker claims only projection jobs and the explicitly configured relation derivation. Other
  derive/cron work remains untouched for later milestones. Queue completion, retry, heartbeat, and
  not-ready release retain the M0 claim-token fence.
- Structured completion logs expose only operational identifiers, hashed entities, timings,
  provider/backend names, bounded counts, token usage, and error classes. Record content, prompts,
  model responses, exception messages, and secrets stay in neither worker nor run-completion logs.

## M2 — 2026-07-16

### Read-view request and response shape

- Multi-value read parameters (`collections`, `types`) are one comma-separated query value.
  Read query and body models are strict: unknown parameters, blank values, and out-of-range
  limits are `422 request_schema`. `GET /records/{id}` parses its path segment manually so a
  malformed UUID returns the standard error envelope as `422 invalid_id`.
- Page limits default to their caps (timeline/history 100, delta 500). `truncated: true` means a
  byte-bound stop under `MAX_RESPONSE_BYTES`; a page that merely filled its row limit signals
  continuation only through a non-null `next_before_seq`/`next_cursor`. Byte accounting uses the
  exact serializer options of the HTTP layer with a worst-case cursor placeholder, so a bounded
  response can never exceed the configured limit.
- Timeline rows are compact: middle-truncated 500-character text plus identity, readiness,
  tombstone, and run fields. Dereference returns everything persisted except the raw embedding
  vector; the specification exposes only the embedding space. `citations` on read rows are
  `derived_from` minus the row's own `run_id`.
- A read-access touch failure is logged as `reads.touch_failed` and never fails the read;
  `last_accessed` is retention metadata, not response content.

### Document and freshness

- Retraction entries count toward `MAX_DOCUMENT_RECORDS` because the bound covers the complete
  latest-per-key current-state set, tombstoned or not.
- The read-trigger SWR enqueue is deferred to the M5 trigger milestone: M2 validates
  `max_staleness` and reports freshness through `freshness.request_revalidation()`, an explicit
  no-op seam mirroring the M1 trigger barrier. The M5 read-trigger work replaces the seam body
  with cooldown-aware derive enqueue/coalescing.
- Freshness dirtiness probes the Pipeline's driving Source scope without a readiness filter; the
  first matching row above its cursor sets `dirty`, and unreadiness sets `pending_unready`.
  (M5 write/accumulator trigger evaluation will differ: those fire only on ready rows.)
- A dead-lettered derive job is reported as `job:"dead"` with its stored error kind until a
  later successful or noop run record completes at or after `dead_at`, even when a newer active
  job coexists with it. Active jobs map to `running` (unexpired lease), `enqueued`
  (`run_after` due), or `queued` (`run_after` in the future); PostgreSQL evaluates the time
  predicates.

### Delta scope hash and cursors

- The wire-stable scope hash is `sha256(canonical-json({"entity", "collections", "status",
  "include_system"}))` where `collections` is the sorted unique list or `null` when the filter
  is omitted. Equivalent filter orderings therefore hash identically.
- `POST /cursor` uses one conditional upsert; an empty `RETURNING` re-reads the row to
  distinguish `409 cursor_scope_mismatch` from `409 cursor_regression`. Re-posting the current
  position is an idempotent success, and `force: true` is the only path that changes a stored
  scope hash or lowers a position.

### Repairs while closing the M2 gate

- `alembic/env.py` now calls `fileConfig(..., disable_existing_loggers=False)`. Migrations run
  inside processes whose `memseek.*` loggers already exist; the default silently disabled
  them for the rest of the process (observed as empty structured logs in any process that
  migrates before serving, and as failing log-assertion tests).
- A `test_canonical_records` parametrization argument was renamed from `settings` to
  `bounded_settings`; pytest 9 rejects a function-scoped parameter shadowing the session-scoped
  `settings` fixture that the autouse migration fixture requests.

## M3 — 2026-07-16

### Canonical search and ranking ownership

- Candidate backends are intentionally recall-only channels. The core engine always reloads
  candidate IDs from PostgreSQL, reapplies scope and typed field predicates, recomputes
  similarity/text signals from canonical data, and applies one canonical rank expression or
  structured ordering before returning hits.
- Current-version filtering for keyed rows excludes a ready row when any newer row exists in the
  same `(workspace, entity, collection, key, status)` lane, including an unready replacement. This
  prevents search from surfacing stale keyed values while enrichment is pending.
- Query embeddings are generated only when at least one source uses vector or hybrid mode. Text,
  recent, and structured-only requests never call the embedder.

### Structured fields, views, and projection shape

- Portable typed field predicates and ordering are validated against declared fields on every
  collection version in scope. Annotation-backed fields are allowed for predicates/order only when
  the underlying annotation processor is required in each scoped collection version.
- Named views are immutable versioned SearchSpec templates. Request parameters are type-checked,
  rendered through the exact-reference template resolver, then revalidated as SearchSpec before
  execution.
- `GET /views` publishes view name/version/hash, parameter schema, referenced collections, required
  capabilities, and resolved profile names so operators can audit runtime routing and compatibility.

### Multi-source fusion and API boundaries

- Multi-source search uses weighted reciprocal-rank fusion with deterministic tie-break rules.
  Optional post-fusion boost expressions are restricted to scorer/age/const leaves to keep boost
  backend-independent.
- `/search` and `/views/{name}/query` responses are bounded by `MAX_RESPONSE_BYTES`; rendered
  snippets are separately bounded by `SEARCH_RENDER_TOKENS`. Over-limit responses return 409 rather
  than partial JSON payloads.

## M4 — 2026-07-16

### Bounded derivation runner

- A claimed non-relation derive job with a changes Source consumes the maximal ready prefix above
  its successful cursor. An unready first matching row refunds the claim and is released without a
  run row; later ready rows never jump over that barrier. Empty input with `allow_empty: false` is an
  audited noop and does not call a model.
- Guarded current and record Sources are packed completely before the driving Source and consume
  the shared visible-record budget first. Sources and search select complete deterministic rows;
  search fan-out executes concurrently but applies token, visible-ID, and total-retrieval limits in
  declared item order.
- Tracked Task values carry transitive source IDs through typed inputs, templates, intermediate
  JSON, and search results. Direct emission citations must be unique UUIDs authorized by the
  producing Task; an LLM Task authorizes only full handles visible in its prompt.
- Run and output UUIDs are allocated before commit. A successful/noop run cites model-visible
  sources plus its predecessor checkpoint, while each output cites its generating run and direct
  evidence. Workspace, claim-token, entity, cursor, current-Source, target-head, and source-parent
  checks all happen before the transaction can commit; stale work is rejected and audited as a
  failed attempt.
- Every model attempt, correction call, retrieval trace, usage count, config hash, contract hash,
  and bounded error classification is stored in the system run envelope. Failure audit uses the
  same workspace/job/entity lock order and never writes output rows.
- M4 exposes one authenticated manual enqueue route and dispatches configured non-relation derive
  jobs from the worker. Trigger evaluation, cooldowns, read/write/accumulator scheduling, and cron
  scans were intentionally left to M5; review/promotion and erasure remain later milestone work.

## M5 — 2026-07-16

### Transactional triggers and cron

- Trigger truth lives in PostgreSQL. The readiness hook and successful derive commit call the same
  evaluator, which uses the Pipeline cursor and canonical driving Source scope; no external search
  backend participates in trigger matching.
- Trigger stimuli are monotonic JSON boolean reason keys on one active derive mailbox. Cooldown
  computes the earliest permitted `run_after` from the latest successful/noop run, while manual
  enqueue remains an explicit cooldown bypass.
- Trigger and manual enqueue paths use PostgreSQL `clock_timestamp()` when no explicit schedule is
  supplied. Queue visibility therefore shares the same wall clock as `SKIP LOCKED` claims and does
  not lose an immediately runnable job to small application/database clock skew.
- Read-trigger SWR enqueueing stays on the document connection and returns current state without
  running a processor. The freshness entry is updated to reflect the newly queued mailbox.
- Cron schedules are persisted as deduplicated `cron_scan` jobs. A scheduler catches up due UTC
  buckets from the latest persisted checkpoint, and the worker chains lexical pages with a cursor
  after each 500-entity page. Dirty scans use the exact input scope and watermark.

### Job operations

- Job status exposes timing, lease, attempts, reason keys, bounded run IDs, and the last error but
  never payload contents. Retrying is limited to dead jobs and refuses a newer active derive job
  for the same workspace/derivation/entity partition.
- Cron scan completion is claim-token fenced in the same transaction as derive enqueue and cursor
  chaining. Stale claims therefore cannot mark a scan done or create an unowned successor.

## M6 — 2026-07-16

### Context and artifact boundaries

- `/context` remains a bounded convenience assembler over canonical document, named-view search,
  recent, and optional delta sections. It deduplicates record IDs before packing and wraps the
  rendering only in the element the request declared; general prompt composition belongs to
  artifact definitions.
- Artifact rendering is deterministic and provider-free. Every block records its exact scope,
  maximum observed sequence, input IDs, definition references, readiness, and truncation state;
  the response includes a stable content hash and package binding.
- Snapshots use ordinary materialization runs and records, so provenance, readiness, projection,
  document history, and future erasure all share the canonical storage path. Reviewed snapshots
  are draft rows until explicit promotion.

### Review and tool contracts

- `/runs` returns bounded persisted summaries and `/runs/{id}` pages outputs in the run envelope's
  declared `output_ids` order, reporting erased IDs explicitly rather than silently reordering.
- `/tools` exposes only read-oriented JSON Schema contracts. Mutation, arbitrary derivation,
  promotion, and erasure remain outside the default agent-tool surface.

## M7 erasure and projection repair — 2026-07-16

- Erasure is implemented as one canonical transaction. It takes the exclusive workspace lock,
  expands record seeds through `run_id` and recursive `derived_from` descendants, bounds the
  closure at 10,000 rows, fences active derive jobs, and acquires entity locks in sorted order
  before locking and deleting record rows.
- The operation captures original `(id, collection)` pairs before deletion and writes one
  claim-fenced `index_delete` job. Keyed rows call the existing current-state refresh helper so a
  surviving predecessor is projected again. A ready `_system/erasure` audit contains only counts,
  collection/ID hashes, and job identity; it never stores record text or prompts.
- The Turbopuffer adapter is deliberately recall-only. It uses deterministic hashed namespaces,
  bounded HTTP retries, explicit projection schema, and canonical reload/recheck in the search
  engine. Projection writes remain worker-owned and idempotent.
- `reindex --since-seq` and `--reset` are planning operations that enqueue ordinary projection
  jobs rather than bypassing lease fencing or mutating canonical data. Reset requires explicit
  confirmation outside a test database. External orphan enumeration and the final walkthrough/
  agent-loop examples remain follow-up M7 hardening.

### Python-authored definitions and importance semantics

- YAML is the primary reviewable, deterministic deployment authoring format: an application owns
  its collection, processor, derivation, view, artifact, and package files. `DefinitionSources` is
  an optional generation path for applications that need dynamic registration; it accepts Pydantic
  models or JSON-compatible mappings and compiles them through the same catalog validator by way
  of an isolated temporary source layout. This preserves one implementation for schema checks,
  references, automatic-cycle checks, semantic hashes, and deployment bindings;
  `DefinitionCatalog` is still immutable after compilation.
- `importance` is intentionally a numeric scorer (the compact per-record annotation form), not a
  derive processor. Enrichment writes its result to `scores.importance`; profile is the separate
  cross-record derive processor. The profile accumulator trigger reads only ready score values
  above its watermark and enqueues a claim-fenced successor job when the threshold is reached.

## Public typed relations — 2026-07-18

- This decision supersedes M1's built-in contradiction relation processor and separate worker
  lane. A relation is an ordinary public event whose structural fields are declared by a collection
  JSON Schema and whose semantic `type` is selected by the authoring derivation.
- The shipped `relations@1` collection requires UUID subject/object endpoints, explanation, and
  bounded confidence. The shipped `contradiction` Pipeline uses named `changes` and `current`
  Sources; its Task prompt owns the meaning of contradiction.
- Generic event emission is validated against its resolved target collection schema before commit.
  Citations still must be UUIDs available to the producing Task, so relation evidence uses the
  existing provenance and erasure contract without a relation-specific Adapter.
- Relation outputs use the normal derivation job, run audit, public readiness, enrichment,
  projection, search, delta, trigger, and erasure implementations. The special definition family,
  deployment settings, readiness enqueue, job-claim partition, worker counter, and `relations.py`
  implementation are deleted.

## Extended trigger condition surface — 2026-07-19

This entry amends the v3.2 trigger schema (spec §11) with six additional
conditions and one pacing modifier. The mailbox, watermark, readiness,
cooldown, and PostgreSQL-truth invariants are unchanged; every new condition
compiles to the same `enqueue_derive_tx()` coalescing path and the
`trigger:<name>:<condition>` reason vocabulary.

### New conditions

- **`quiet`** — a consumable scope plus `after_s`. Each matching ready arrival
  extends the shared job deadline to `now + after_s`; the run fires once the
  burst settles. Implemented as an `extend` coalescing mode on the mailbox
  (`greatest` instead of `least` on `run_after`), not as a new job kind.
- **`at`** — an observational scope plus a declared filterable datetime
  `field` and `offset_s`. The earliest deadline newer than the last successful
  completion becomes `run_after`; a deadline is handled once a successful run
  completes at or after it, so post-run re-evaluation chains future deadlines.
  Deadlines deliberately key off completion time, not the watermark, because a
  `changes` cursor may consume future-dated records long before they are due.
- **`changed`** — a consumable keyed scope with optional `keys` and
  `transitions: [added, changed, removed]`. Fires only when a keyed head's
  content differs from its previous head (lateral previous-head comparison);
  byte-identical rewrites are `unchanged`. The field is named `transitions`
  because unquoted `on` is a YAML 1.1 boolean.
- **`census`** — an observational scope plus integer `threshold`. Counts the
  entity's current non-tombstone records (latest head per key) and fires only
  when the floor is met AND the driving source is dirty. The dirtiness guard
  makes a standing census loop-free by construction. This is the deliberate
  substitute for a named-view trigger: views may require the external index,
  which is never trigger truth.
- **`lifecycle`** — `first_record` (watermark is zero and driver data exists)
  and/or `total_records: N` (total driver-scope history reached N and new data
  arrived). Both are gated on driver dirtiness to stay loop-free.
- **`retraction`** — a consumable keyed scope matching ready tombstone rows
  above the watermark. Erasure restores prior heads without new rows and is
  intentionally not a retraction stimulus.

### Accumulator generalization

`aggregate` now accepts `sum | count | avg | max | min | distinct_count`, the
metric accepts an explicit `{scorer: name, aggregate: …}` form alongside the
annotation form, and `comparison: gte | lte` selects the threshold direction.
The condition never fires on zero matching rows or a null aggregate, so `lte`
cannot loop on an empty backlog. A `gte` threshold must remain positive; an
`lte` threshold may be any finite number.

### Pacing modifier

`debounce_s` complements `cooldown_s`: cooldown is a trailing-edge rate limit
against the last success, debounce a leading-edge settle window that extends
while arrival stimuli keep firing. It applies to arrival conditions only —
never `read`, `cron`, or `at`; `quiet` carries its own `after_s`.

### Validation and graph rules

Consumable scopes (`write`, `quiet`, `changed`, `retraction`) keep the
subset-of-driving-source rule; observational scopes (`at`, `census`) may name
any collections and resolve omitted versions to the active version. The
structured `where` grammar is accepted on every scoped condition under the
existing required-annotation restriction. All scoped conditions contribute
edges to the automatic dependency cycle check; census and lifecycle also
contribute guarded driver edges like accumulator and read.

## Author-owned prompt fencing — 2026-08-04

### The engine escapes; the author fences

Escaping untrusted text and fencing it were one operation, and both belonged to
the renderer. They are now separated by who is accountable for each.

Escaping stays unconditional and unconfigurable: `&`, `<`, and `>` become
`&`, `<`, and `>` on every untrusted value, so record content can
never close or forge an element. That is a property of the substrate and no
definition can waive it.

Fencing — the element, its tag, and any sentence introducing it to a model — is
prompt composition, and prompt composition belongs to the definition author. The
renderer no longer contributes any of it. An artifact template, a derivation
prompt, and a package's MCP `instructions` are the complete text a model reads;
a render is that text with escaped values substituted and nothing added. The
previous behaviour put an engine-authored English sentence and a `<records>`
element into the middle of a template someone else wrote, invisible in the YAML
they were reviewing.

### Where a declaration replaces a template

Three surfaces render rows with no author template in between: a named view's
`rendered` field, `POST /search`, and `GET /context`. They accept an explicit
declaration instead — `fence: {tag, preamble}` in a search spec, `fence_tag` and
`fence_preamble` as `/context` query parameters. `tag` defaults to `records` and
always carries `untrusted="true"`, so the machine-readable marker cannot drift
from the escaping it pairs with. `preamble` has no default: structure may be
implied, prose never is. Absent a declaration the rows come back bare.

An artifact block takes no `fence` field. Its rows are always framed by the
template that consumes them, and a second declaration would only create two
places to look.

### Engine-composed prompts still fence themselves

The LLM scorer, the search reranker, `/answer`, and the derivation correction
suffix compose their own prompts *and* send the system message that gives a
fence its meaning. Owning both halves is what makes a fence sound, so these
declare their own outright and accept no configuration.

Because an author may now choose any tag, the trusted system messages name the
`untrusted="true"` attribute rather than one fixed element. The derivation
message additionally states that retrieved record rows are data, so an author
who leaves rows unwrapped changes their prompt's wording and not the boundary.

### Escaping now actually happens on derivation scalars

`derive/provenance.py` wrapped untrusted scalars in `<data untrusted="true">`
without escaping them, against the specification. With the wrapper gone,
escaping is the only remaining guarantee and is applied. A value already
rendered as rows is marked `pre_escaped` and substituted as-is, so markup a
renderer meant to emit survives.

## Interpretable retrieval scores — 2026-08-06

### Rank is authoritative; score is query-relative

Native retrieval utilities do not share a unit: a rank expression may sum
several signals, RRF uses reciprocal positions, a reranker emits judgments,
and a graph boost adds another term. Exposing every one of those values as the
same bare `score` made values above one appear invalid and encouraged callers
to read them as probabilities.

Every ranked hit now exposes an explicit one-based `rank`. Its public `score`
is a monotonic min-max projection onto `[0,1]` using the complete final ranked
candidate pool before the response is sliced to `k`; a completely tied pool
maps every tie to one. The engine's native finite utility remains available as
`rank_score` for diagnostics. Response-level `ranking` metadata identifies
rank-expression versus RRF ordering and states that the public score is
query-relative and uncalibrated. Consequently, scores preserve order but are
not relevance probabilities and must not be compared across queries, views,
or rank-definition versions.

### Structured order is not scored retrieval

A structured listing has deterministic `order_by` positions but no relevance
function. It now returns `rank`, `score: null`, no `rank_score`, and
`ranking: {kind: structured, scored: false}` instead of manufacturing a zero
that looked like a failed relevance judgment.
