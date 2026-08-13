# First-class usage and cost tracking

## Summary

Build cost tracking as a content-free, append-only usage ledger rather than deriving dollars from
existing run records. Every billable call made by Memseek records its physical usage once, then
links that usage to the logical operation that caused it: an answer, search, derivation,
annotation/enrichment batch, backfill, re-embed, or projection job. This avoids double-counting
batched embeddings and scorers while making it possible to answer:

- What did this question cost, including its query embedding, rerank, rewrite, and answer calls?
- What did it cost to produce this memory, including the emitting derivation and enrichment of the
  emitted record?
- What did an entity, processor, derivation, provider, model, workspace, or service cost over a
  period?
- Which quantities are measured, which dollars are estimated, and which charges were imported?

The first release measures and reports rather than blocking execution. It supports soft alerts
through persisted alert events and structured logs. Raw usage remains reportable when no price is
known. Direct estimates are available immediately; imported/shared service charges add provisional
fully loaded views without rewriting the causal direct estimate.

## Semantics and invariants

### Four separate concepts

1. **Usage event** — one physical, potentially billable attempt or service operation. Examples:
   completion input/output tokens, embedding input tokens, a Turbopuffer query, indexed bytes, or
   a storage-month charge imported from an invoice. Retries are separate usage events when their
   usage is known; failed attempts with no reported quantity remain visible as unpriced attempts.
2. **Logical operation** — one user-meaningful unit such as `answer`, `search`, `derive`,
   `annotate`, `backfill`, `reembed`, or `projection`. Nested work shares the parent operation ID,
   so an answer owns its internal search and a derivation owns searches performed by its Tasks.
3. **Direct estimate** — usage priced with the exact immutable rate revision effective when the
   event occurred. It never changes retroactively. An event without a matching rate has usage but
   `estimated_cost: null` and is reported as incomplete, never silently treated as zero.
4. **Imported/shared charge and allocation** — a period-level actual or estimate imported by an
   operator, then provisionally allocated by an explicit rule. It is displayed separately from
   direct cost. Both period-level and optional fully loaded per-operation views are exposed;
   allocations may move when charges or rules are revised and are never labeled final.

All money uses an ISO-4217 currency and integer minor units; rates use fixed-precision decimal
units rather than floats. V1 supports USD rate cards and rejects cross-currency totals rather than
inventing an exchange rate. Every response returns `usage`, `direct_estimate`, `allocated_cost`,
`fully_loaded_cost`, and completeness/status fields separately.

### Attribution rules

- Workspace is mandatory on every internally measured event. The deployment-wide operator report
  sums workspace events and separately reports unallocated service charges.
- A logical operation may name zero, one, or many entities. One entity receives the operation's
  full direct cost; explicitly multi-entity operations split it equally using deterministic minor-
  unit remainder assignment. Unscoped operations remain workspace-only. Entity rollups therefore
  reconcile to workspace totals.
- A memory record's production cost is its share of the emitting derivation operation plus its own
  annotation/embedding operations. It does not recursively charge cited ancestors, preventing
  common evidence from being counted once per descendant. Batched processor calls are allocated
  across target records by the meter's causal quantity (embedding/input tokens when available,
  otherwise equal share), with deterministic remainder handling.
- An answer operation begins before rewrite/search and ends after optional save. Its direct cost
  includes query embedding, optional reranking, optional rewrite, and answer synthesis. Saving the
  answer adds no provider cost unless the resulting record later receives separately recorded
  enrichment.
- Catalog/model aliases are labels; pricing binds to the resolved provider connection and model.
  Fallback and retry attempts therefore use the rate for what actually ran.
- `LLM_FAKE=1` records usage with `environment: fake` and zero direct cost, keeping tests useful
  without mixing fake estimates into production totals.
- Entity erasure intentionally retains content-free cost rows and their entity attribution, as
  selected for this feature. The API/docs must call this out explicitly because it differs from
  canonical provenance erasure. Usage rows contain no prompts, responses, record text, API keys,
  or vendor invoice documents.

## Storage and configuration

Add an Alembic migration with the following normalized operational tables rather than canonical
`record` rows:

- `cost_operation`: UUID, workspace, kind, status, parent operation, correlation fields
  (`request_id`, `job_id`, `run_id`, `backfill_id`, or artifact-use ID where applicable), named
  component, entity array, start/end timestamps, and bounded metadata. Correlation IDs are unique
  within a workspace where retries can revisit a completion path.
- `usage_event`: immutable event UUID/idempotency key, operation/workspace, meter, provider,
  resolved model/service, measured quantities as a bounded JSON object, measurement source
  (`provider`, `estimated`, `imported`), outcome, occurrence time, and the applied rate revision plus
  direct cost when priced. Index by workspace/time, operation, entity attribution, meter,
  provider/model, and correlation IDs.
- `usage_allocation`: immutable per-target splits from a usage event to an entity and/or record.
  Allocated shares must sum exactly to the source event quantity and direct cost.
- `rate_card_revision`: scoped to deployment or workspace, immutable effective interval, meter,
  provider/model match pattern, unit prices, currency, source (`yaml` or `api`), and supersession
  link. Overlapping matches at the same specificity are rejected. Precedence is exact model over
  provider wildcard over meter default, and workspace API overlay over deployment YAML baseline.
- `shared_charge_revision` and `allocation_rule_revision`: operator-managed, append-only revisions
  for service/vendor charges and proportional allocation rules. V1 allocates a charge to
  workspaces, then optionally to operations, by one declared driver: direct cost, a named usage
  quantity, operation count, or equal share. Zero-driver periods remain explicitly unallocated.
- `cost_alert_policy` and `cost_alert_event`: versioned workspace or deployment thresholds and
  idempotent threshold crossings. Policies select a rolling/calendar window, scope, metric
  (`direct_estimate`, usage quantity, allocated cost, or fully loaded cost), threshold, and
  cooldown. Events retain observed value/status and acknowledgment time.

Foreign keys to canonical records, runs, jobs, or workspaces should use `ON DELETE SET NULL` where
needed so permanent financial history survives erasure and cleanup. Store copied content-free
correlation identities needed for reporting after the source object disappears. Do not add an
automatic retention job in V1; detail and rollups remain until an explicitly designed financial
retention/export feature is introduced.

Extend `conf/models.yaml` with an optional `pricing` section defining the deployment baseline rate
card for resolved model targets and embedding models. Add an operator-owned
`conf/costs.yaml` for non-model meter rates, allocation defaults, and deployment alert defaults.
Both are strictly validated and hashed at startup. Database API revisions overlay those defaults
from their `effective_from`; they never mutate uploaded catalog YAML. Rate snapshots are copied
onto usage events, so a catalog publish or overlay change affects only later usage.

Provide built-in meter contracts initially for:

- `llm.completion`: `input_tokens`, `output_tokens` and optional provider-supported token classes
  such as cached input or reasoning tokens. Unknown classes stay in raw usage and are marked
  unpriced until the rate schema supports them.
- `llm.embedding`: `input_tokens` and `items`.
- `search.turbopuffer.query`, `search.turbopuffer.write`, and
  `search.turbopuffer.delete`: requests plus measurable rows/bytes. Do not claim these equal a
  vendor invoice; price them only when the configured vendor contract exposes a compatible unit.
- Generic imported meters for database, storage, network, and other infrastructure charges.
  Memseek does not fabricate per-query PostgreSQL/storage cost from wall time. Those enter as
  shared period charges unless an adapter supplies a trustworthy native quantity.

## Runtime integration

Introduce a request/task-local `CostContext` containing the operation ID, workspace, component,
and entity targets. HTTP endpoints create root operations; workers create or resume root operations
from durable job IDs; nested answer/search/Task calls inherit the context automatically. Standalone
search creates its own operation, while search inside answer or derive becomes a child span under
the same root cost operation.

Instrument the physical seams, not their callers:

- The LLM runtime persists one usage event for every completion or embedding attempt with known
  usage, including correction calls, fallbacks, and retries. Expand provider-neutral usage to
  retain extra token classes without coupling core to OpenAI response shapes.
- Search records query-embedding and rerank model usage through that same seam. The Turbopuffer
  adapter emits request/row/byte meters for candidate queries and projection writes/deletes.
- Enrichment keeps one event per actual shared provider batch. Its existing per-record `_system`
  annotation runs reference the operation/event allocation but do not duplicate usage or dollars.
- Derivation success and failed-run persistence attach the `cost_operation_id`; every attempt spent
  before a retry remains charged to the operation/job. Reprocessing after a crash uses an
  idempotency key derived from the durable job, job attempt, logical call ordinal, provider target,
  and provider attempt. This prevents a database retry from double-writing an observed call while
  honestly retaining a newly issued external retry.
- Re-embed and backfill create durable operations tied to their handles/batches. Projection jobs
  use the durable job ID. Pure PostgreSQL search/read work records an operation with zero direct
  provider usage, which is useful for request counts and later allocation but is not assigned a
  fictional direct dollar cost.

Provider calls cannot be atomically committed with PostgreSQL. Persist events immediately after a
response, before business output commit. If that write temporarily fails, enqueue a bounded,
content-free usage envelope in a dedicated durable outbox/retry path and emit a high-severity
`cost.meter_write_failed` log. Operation responses expose `cost_status: pending` until reconciliation;
financial reports show missing/pending event counts. Never fail a successful model-backed user
operation merely because metering storage is temporarily unavailable.

After each committed usage event or imported allocation revision, evaluate matching soft-alert
policies transactionally. Insert one event per policy/window/threshold crossing, emit structured
`cost.alert.triggered` logs, and expose active/acknowledged state through the API. Alerts never
reject, cancel, or defer provider work in V1.

## API, SDK, and authorization

### Workspace surface

- `GET /costs/summary`: required `start`/`end`; optional groupings and filters for entity,
  operation kind, component, provider/model, meter, and estimate status. Bounded grouping choices
  prevent arbitrary SQL dimensions. Returns raw quantities, direct estimates, provisional
  allocations, fully loaded totals, currency, completeness, and pagination metadata.
- `GET /costs/operations`: paginated operation drill-down; filters include period, entity, kind,
  correlation ID, and status.
- `GET /costs/operations/{id}`: event/record allocation detail. Existing `/answer` adds
  `cost_operation_id`, `direct_estimate`, and `cost_status`; run details add the same linkage.
- `GET /records/{id}/cost`: creation-plus-enrichment production cost for one workspace-owned
  memory record, with component breakdown and no recursive lineage.
- CRUD-by-revision endpoints for workspace rate overlays and alert policies, plus
  `GET /costs/alerts` and `POST /costs/alerts/{id}/acknowledge`. Mutation creates a new revision;
  history is not edited in place.
- Add matching typed SDK helpers under `client.costs`, and document how to join operation IDs to
  existing answer/run/job/artifact-use handles. Cost administration is not exposed as an MCP tool
  by default.

### Operator surface

Add `/admin/costs/*` for cross-workspace summaries, deployment rate revisions, shared-charge
imports, allocation-rule revisions, recomputation, and deployment alert policies. Authenticate it
with a separate deployment operator bearer secret configured independently of workspace keys;
compare only its stored/configured hash, never log it, never accept it on workspace or MCP routes,
and return no canonical content. Require an idempotency key for charge imports and all revision
creation.

The operator surface reports both aggregate and per-workspace allocation, while a workspace sees
only its own allocated share. A shared charge import includes vendor, external reference, period,
currency, amount, estimate/actual classification, and meter; it never stores an invoice body.
Re-importing the same idempotency key is an exact replay or a conflict. Revisions recompute
provisional allocations without changing immutable direct estimates.

CLI commands mirror the operator workflows for self-hosted deployments:
`cost-rates`, `cost-import`, `cost-allocate`, and `cost-report`. JSON output stays machine-readable.

## Reporting behavior

Rollups are computed from the detailed ledger in V1 and are bounded to a maximum report interval
and fixed grouping cardinality. Add daily materialized rollups only after query-volume evidence
shows they are needed; if added, they remain rebuildable projections rather than financial truth.

Each report includes:

- period and timezone (UTC storage; caller-selected IANA timezone for calendar buckets), currency,
  and grouping keys;
- measured quantities separated by meter/unit;
- direct estimated minor units and counts of priced, unpriced, estimated-usage, pending, and failed
  events;
- allocated shared minor units, unallocated remainder, allocation revision, and `provisional: true`;
- fully loaded total only when its components share one currency, plus an explicit completeness
  flag;
- operation counts and cost percentiles where meaningful, calculated once per operation rather
  than per usage event.

The default period is not implicit: callers must send `start` and `end`, preventing accidental
unbounded lifetime scans. Maximum range defaults to 400 days for detailed operation queries;
operator period summaries may span longer with coarser calendar buckets.

## Implementation sequence

1. **Ledger foundation** — add schemas, typed meter/rate models, price resolution, exact decimal
   arithmetic, operation context propagation, workspace summaries, and model provider
   instrumentation. Return operation IDs and direct estimates from answer/run detail.
2. **Complete internal attribution** — cover query embeddings, rerank, enrichment batch
   allocation, failed derivations/retries, backfill, re-embed, and Turbopuffer query/projection
   meters. Add record production-cost and entity/component reports.
3. **Hybrid configuration and alerts** — load/validate YAML baselines, add immutable workspace and
   deployment API overlays, evaluate soft thresholds, log crossings, and expose acknowledgment.
4. **Service-wide accounting** — add operator authentication, imported charge revisions,
   allocation rules, provisional per-period/per-operation allocation views, operator SDK/CLI, and
   reconciliation diagnostics.
5. **Documentation and operations** — update model/catalog, API, SDK, operations, erasure, and
   privacy guidance; add dashboards/examples showing question, memory, entity, period, and service
   reports without presenting estimates as invoices.

## Test plan and acceptance criteria

- Unit-test rate precedence/effective intervals, decimal rounding, currencies, raw unpriced usage,
  extra provider token classes, deterministic entity/record splits, and allocation remainders.
- Prove one batched embedding/scorer provider call creates one physical usage event even though it
  produces many annotation runs; per-record allocations sum exactly to the event and workspace
  totals do not multiply.
- Exercise an answer with query embedding, rerank, rewrite, correction, fallback, and save; its
  operation total must equal the sum of every physical attempt and must not include later saved-
  record enrichment until that separate operation completes.
- Exercise successful, failed, retried, timed-out, and lease-lost derivations. Costs already
  incurred remain visible, durable idempotency prevents duplicate ledger writes, and a genuinely
  repeated provider call is charged again.
- Verify search, backfill, re-embed, and Turbopuffer projection/query meters, including missing
  credentials, retryable errors, and requests whose provider reports no usage.
- Verify rate changes never reprice old direct events; imported charge or allocation revisions may
  change only the explicitly provisional allocated/fully-loaded views.
- Verify workspace reports reconcile to entity splits plus unattributed cost; operator reports
  reconcile across workspaces plus unallocated charges. Multi-currency requests fail clearly.
- Verify alert threshold/window/cooldown behavior, one idempotent crossing event, structured log
  output, acknowledgment, and that alerts never alter execution.
- Verify workspace keys cannot access `/admin`, the operator key cannot access workspace data
  routes as a tenant, MCP does not disclose cost administration, and all queries enforce workspace
  isolation and global response bounds.
- Verify prompts, responses, record text, API keys, and invoice documents never enter cost tables
  or logs. Entity erasure removes canonical provenance but deliberately retains selected
  content-free entity cost attribution and documents that behavior.
- Add migration upgrade/downgrade tests, API/SDK contract tests, fake-provider zero-cost fixtures,
  concurrency tests for duplicate idempotency keys, and an end-to-end reconciliation fixture whose
  expected usage and minor-unit totals are exact.

## Assumptions and chosen defaults

- V1 covers all service spend through trustworthy native meters plus imported shared charges; it
  does not pretend elapsed time is a database/cloud invoice.
- Both raw usage and estimates are first-class. Missing pricing yields an incomplete estimate, not
  zero cost.
- Measurement and soft alerts ship before hard budgets or admission control.
- YAML is the versioned deployment baseline; immutable database API revisions override matching
  defaults. A later UI may call the same APIs.
- Cross-workspace access uses a separate operator bearer key in V1. A multi-user RBAC/control plane
  is out of scope.
- Shared allocations and fully loaded operation costs are always provisional; there is no billing-
  period close/finalization workflow in V1.
- Detailed accounting has no automatic expiry. A future retention/export policy must preserve
  financial reconciliation and explicitly define its interaction with entity erasure.
- USD is the initially supported pricing/reporting currency. Imported other-currency charges may be
  stored but cannot be combined until an explicit exchange-rate design is added.
