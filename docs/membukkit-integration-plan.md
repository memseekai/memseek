---
title: MemBukkit as a Memseek search method
eyebrow: Revalidated integration plan
---

# MemBukkit as a Memseek search method

> Rechecked on 2026-08-18 against Memseek's current tree and local MemBukkit
> commit `af1bf323a80901f58928189c16caa372191a1219` (2026-08-14).
> This document is an implementation plan, not a claim that the method already
> ships.

## 1. Outcome

The goal is deliberately narrow:

> A collection can opt into `membukkit@1` in a search profile, callers use
> the ordinary Memseek `/search` or named-view surface, canonical records
> remain in Memseek, and every response says which buckets were opened and how
> much of the eligible bank was scanned.

The intended authoring shape is:

```yaml
profiles:
  pg_membukkit:
    backend: pg
    method:
      name: membukkit
      version: 1
      partition:
        by: [collection, entity]
        buckets: 24
        seed: 0
        min_records: 48
        refit_growth: 0.50
      routing:
        scan_budget: 0.30
        candidate_cap: 100
        cold_start: standard
```

`backend` answers where the search projection lives. `method`
answers how candidate IDs are selected. Keeping those as separate axes is the
central change in this revision.

The default method is `standard`, so every existing profile and request
retains today's behavior. `membukkit@1` is opt-in and definition-hashed.

### Done means

This integration is done only when:

1. `standard` and `membukkit@1` are two real Search Method
   Adapters behind one Interface;
2. `membukkit@1` runs over Memseek's canonical records without a second
   memory store;
3. a named view can compose a verbatim source and an atomic-memory source with
   the existing multi-source RRF;
4. the response carries a batch-level Retrieval Receipt;
5. cold start, partial coverage, stale partitions, and fallback are explicit in
   that receipt rather than silent;
6. the MemBukkit frozen harness can compare the reference implementation,
   Memseek `standard`, and Memseek `membukkit@1` on identical
   inputs; and
7. the public statement “available as a search method in Memseek” is published
   only after the release gate in §9 passes.

## 2. Recheck: what changed from the previous plan

The earlier plan found the right capability but put several pieces on the wrong
seams.

| Previous assumption | Recheck | Plan correction |
|---|---|---|
| A new `SearchBackend` is sufficient | Runtime registration is split across the descriptor registry, the engine's private `_BACKENDS` map, projection factories, a Pydantic literal, and Settings validation. | Deepen one registry Module before adding the method. |
| Bucket routing is a backend capability | `pg` and `turbopuffer` are physical search backends. MemBukkit is a retrieval policy that already runs over both in its own repository. | Add `method` beside `backend` on a search profile. |
| Bucket assignment is a new Processor kind | A Processor is a per-record, write-once annotation. KMeans is corpus-level state, and refitting changes assignments. | Keep partitions and assignments in rebuildable search-projection tables. Do not change `ProcessorDefinition.kind`. |
| `CandidateHit.diagnostics` can carry the trace to the response | The engine keeps candidate IDs and drops the diagnostics. The response reports only `candidate_count`. | Return `CandidateBatch(hits, receipt)` and thread the receipt through the engine. |
| Add `bucketed` to `SearchCapability` to select it | Capabilities validate what a backend can do; they do not select a profile or implementation. | The profile selects the method. A view may later require a receipt capability, but that is validation, not routing. |
| A cross-encoder sidecar is “another model provider” | The current LLM Interface supports only completion and embedding. Reranking is a direct `llm_judge` branch using the fixed `cheap` alias. | If needed, add a separate Reranker Module with `llm_judge` and `cross_encoder_http` Adapters. |
| Cross-encoder work is the first cheap win | MemBukkit's current published ablation says plain cosine (83.4%) statistically tied the shipped hybrid reranking result (82.0%) on the cited run. | Prove bucket gating and dual lanes first. Reranking is gated on incremental evidence. |
| Port the benchmark harness into Memseek | MemBukkit already owns datasets, judges, recipes, tolerances, and output artifacts. A second copy will drift. | Add a thin Memseek system Adapter to the existing harness. |
| The query router is a small named-view block | A view executes exactly one strict query. Routing to another view needs reference validation, parameter compatibility, cycle checks, and diagnostics. | Defer routing until the fixed-policy method works and has a baseline. |
| The 92.6% result demonstrates only the search algorithm | The frozen recipe also pins the reader, distiller, official judge, OpenAI embeddings, dual lanes, aggregation routing, and deep/full-scan policy. | Never attach 92.6% to a partial Memseek port. Measure each layer and name the tested recipe. |
| Memseek has no license and targets exactly Python 3.14.6 | Memseek now has an Apache-2.0 `LICENSE` and declares `>=3.14,<3.15`. | Remove those stale blockers. Preserve attribution for copied Apache-2.0 code. |

Evidence in the current Memseek tree:

- the backend Interface and unused per-hit diagnostics:
  [`search/registry.py`](../src/memseek/search/registry.py);
- the hard-wired runtime adapters and dropped diagnostics:
  [`search/engine.py`](../src/memseek/search/engine.py);
- strict search-profile and Processor definitions:
  [`definitions/models.py`](../src/memseek/definitions/models.py);
- the second backend factory:
  [`projections.py`](../src/memseek/projections.py);
- canonical scope recheck:
  [`search/scope.py`](../src/memseek/search/scope.py); and
- the existing immutable/projection decisions:
  [`DECISIONS.md`](../DECISIONS.md).

Evidence in MemBukkit:

- [method description](https://github.com/memseekai/membukkit/blob/main/docs/METHOD.md);
- [frozen benchmark recipes](https://github.com/memseekai/membukkit/blob/main/src/membukkit/bench/recipes.py);
- [candidate generation and reranking](https://github.com/memseekai/membukkit/blob/main/src/membukkit/pipeline.py); and
- [topic routing](https://github.com/memseekai/membukkit/blob/main/src/membukkit/retrieval/buckets.py).

## 3. Architecture decision

### 3.1 Search Profile selects two axes

A Search Profile becomes the one declared seam:

```text
Search Profile
├── backend: pg | turbopuffer
└── method: standard | membukkit@1
```

The backend owns physical candidate/index operations. The method owns retrieval
policy. A runtime Search Method Adapter is registered for a supported
`(method, backend)` pair.

The first pairs are:

| Method | Backend | Status |
|---|---|---|
| `standard` | `pg` | Existing behavior, migrated behind the registry |
| `standard` | `turbopuffer` | Existing behavior, migrated behind the registry |
| `membukkit@1` | `pg` | First implementation slice |
| `membukkit@1` | `turbopuffer` | Deferred until PostgreSQL proves value |

This makes the Search Method seam real: `standard` and
`membukkit@1` are two Adapters. It also preserves Locality: adding a
method/backend pair happens in one registry Module instead of coordinated edits
across loader, engine, worker, projections, settings, and diagnostics.

### 3.2 One registry Module

The registry Interface owns:

- the public method/backend names;
- supported Search Modes and method-specific options;
- strict option validation;
- runtime construction;
- availability checks;
- projection construction;
- standardized error translation; and
- the data needed by `GET /rank/schema`.

The current private `_BACKENDS` map and the separate projection factory
go away. Their deletion should remove duplicated registration complexity rather
than move it into callers—the deletion test for a deeper Module.

### 3.3 Candidate Batch and Retrieval Receipt

The candidate Interface returns a batch, not a bare list:

```python
CandidateBatch(
    hits=(CandidateHit(...), ...),
    receipt=RetrievalReceipt(
        method="membukkit",
        method_version=1,
        method_hash="...",
        partition_generation="...",
        eligible_records=1_240,
        opened_records=389,
        returned_candidates=100,
        scan_fraction=0.3137,
        opened_buckets=(...),
        state="ready",
        degraded_reason=None,
    ),
)
```

The receipt is batch-level because opened buckets and scan fraction describe the
candidate operation, not one hit.

The engine remains authoritative:

```text
Search Method proposes IDs + receipt
        ↓
canonical PostgreSQL reload
        ↓
scope/readiness/tombstone/current recheck
        ↓
exact signals and final ranking
        ↓
hits + retrieval receipt
```

The receipt must not imply that every proposed ID survived or became a hit.

### 3.4 Bucket state is a rebuildable projection

Do not put `bucket_id` in canonical record content, a write-once
annotation, or an Artifact. Store it in dedicated derived state:

- `search_partition` — scope identity, method/profile hash, embedding
  space, generation, configured `k`, counts, build state, timestamps;
- `search_bucket` — one centroid and current size per bucket; and
- `search_bucket_member` — record-to-bucket assignment for one
  generation.

At minimum, a partition identity includes:

```text
workspace
search-profile definition hash
method name + version
collection + collection version
entity
embedding space
seed + bucket count
generation
```

The state is disposable and reconstructable from canonical ready records.
Deleting the projection never deletes memory.

### 3.5 Staged fit and activation

Refitting follows the same shape as staged re-embedding:

```text
fit a staged generation
    → assign every eligible row
    → verify dimensions and complete coverage
    → atomically activate
    → retain the prior generation until activation succeeds
```

New ready records use the active centroids for cheap nearest-centroid assignment.
A growth threshold schedules a refit; it does not refit in the write path.
Deletes update membership/counts and may schedule maintenance.

A new claim-fenced `search_partition_refit` job is real worker work,
not just “call `jobs.py`.” It requires a JobKind, migration constraint,
claim/dispatch/heartbeat handling, retry classification, results, and lifecycle
tests.

### 3.6 Honest scope for version 1

MemBukkit's bank has one owner and one denominator. Memseek permits arbitrary
workspace, collection, entity, type, version, and field scopes. Precomputing every
possible combination is impossible, and using a workspace-wide denominator for
an entity query would produce a dishonest scan fraction and poor recall.

Therefore `membukkit@1` starts with these explicit constraints per
source:

- `mode: vector`;
- exactly one collection;
- exactly one collection version;
- exactly one entity;
- `status: active`;
- `versions: current`; and
- the active catalog embedding space.

Unsupported shapes fail at resolution with a precise error. They do not silently
fall back.

This is intentionally narrow. It gives every partition one stable eligible bank
and makes the scan fraction meaningful. Bounded collection fan-out, hybrid lexical
admission, and history scopes are later extensions.

### 3.7 Cold start is explicit

Before `min_records` or while the first partition builds, the profile's
declared `cold_start` policy applies:

- `standard` — run the existing vector candidate path and return
  `state: cold_start` with `degraded_reason`; or
- `error` — return a typed unavailable error.

No undeclared fallback is allowed. Records written after the active generation
are either assigned immediately or admitted through a bounded
`unassigned` lane reported in the receipt.

## 4. Mapping MemBukkit's method onto Memseek

### 4.1 Reuse Memseek's embedding space

Do not import the MemBukkit bi-encoder into the first slice. The current 92.6%
recipe itself uses `openai:text-embedding-3-large@1536`, and Memseek
already declares one embedding space and guards vector comparability.

The fit worker reads canonical vectors from that active space. The API process
routes a query over roughly 24 centroids with a small pure-Python cosine loop;
NumPy/scikit-learn are confined to an optional fit-worker extra. Torch never
enters Memseek's core dependencies.

If a later recipe needs another dimension, use Memseek's staged embedding-space
migration. Do not smuggle a second vector space into a search option.

### 4.2 Bucket gate first

For one resolved source:

1. load the active partition and exact eligible count;
2. compare the query vector with bucket centroids;
3. open buckets in descending similarity until their member counts meet the
   declared scan budget;
4. always report overshoot caused by opening a whole bucket;
5. select at most `candidate_cap` IDs from the opened region by exact
   vector similarity; and
6. let the canonical engine reload, recheck, and rank them.

The receipt distinguishes:

- `eligible_records` — denominator in the active partition;
- `opened_records` — members of opened buckets before the cap;
- `returned_candidates` — bounded IDs proposed to the engine; and
- `final_hits` — already represented by the response's hits, not by the
  method receipt.

That is more honest than one overloaded `n_scanned`.

### 4.3 Dual lanes are a named view, not hidden method behavior

Memseek already has the right composition Interface: a multi-source SearchSpec
with per-source ranking and weighted RRF.

An agent-memory package can declare:

```yaml
views:
  - name: agent_memory
    version: 1
    active: true
    parameters:
      entity: {type: string, required: true}
      query: {type: string, required: true}
    query:
      q: "{{query}}"
      sources:
        - name: verbatim
          mode: vector
          scope:
            collections: [messages]
            collection_versions: {messages: [1]}
            entities: ["{{entity}}"]
          k: 10
        - name: atomic
          mode: vector
          scope:
            collections: [memories]
            collection_versions: {memories: [1]}
            entities: ["{{entity}}"]
          k: 10
      fuse: {kind: rrf, rank_constant: 60}
      k: 20
      render: true
```

Each source gets its own partition and receipt. The response preserves both
source receipts and the final RRF ranking metadata. This provides MemBukkit's
verbatim/atomic union without hardcoding those collection names into the engine.

### 4.4 Query policy comes after the fixed method

The headline MemBukkit recipes widen `top_k` and sometimes use a full
scan for aggregation, broad-context, or deeper reasoning questions. That policy
matters, but it is not required to prove bucket-gated search.

After the fixed method passes its gate, design a separate Query Policy Module
that selects declared variants such as:

```yaml
policies:
  default: {scan_budget: 0.30, source_k: 10}
  aggregation: {scan_budget: 1.00, source_k: 50}
  deep: {scan_budget: 1.00, source_k: 60}
```

The design must settle reference validation, classification ownership,
localization, cycle behavior, and selected-policy diagnostics. Do not add a loose
`query_router` dict to `ViewDefinition`.

### 4.5 Cross-encoder is evidence-gated

The first slice uses the existing exact cosine/rank-expression path. If the
controlled ablation shows an incremental win, introduce:

```text
Reranker Interface
├── llm_judge Adapter
└── cross_encoder_http Adapter
```

Use a discriminated rerank definition so each Adapter owns its fields and bounds.
The HTTP Adapter names a declared endpoint/model, has bounded concurrency and
timeouts, validates that every input ID is returned exactly once, and exposes
model identity in the response.

Do not load torch in the API process, and do not pretend a cross-encoder is an
`LLMProvider` when that Interface supports only completion and embedding.

## 5. Benchmark strategy

### 5.1 One harness

Keep the benchmark knowledge in MemBukkit. Add a Memseek HTTP system Adapter to
its existing harness rather than porting:

- dataset loaders;
- official and Mem0 judges;
- frozen recipes;
- tolerance checks;
- distillation caches; or
- result formats.

The Adapter should:

1. publish a minimal Memseek package for the benchmark;
2. write the exact same verbatim and atomic rows used by the reference run;
3. wait for embeddings and search partitions to become ready;
4. call the named view;
5. translate hits into the harness's evidence shape; and
6. persist the Retrieval Receipt beside every question result.

Writing the same prepared rows isolates retrieval. An end-to-end comparison using
Memseek Pipelines is a separate experiment because changing distillation changes
the corpus.

### 5.2 Three comparable systems

Every ablation run uses the same rows, embedding target, reader, judge, and query
set:

| System | Purpose |
|---|---|
| MemBukkit reference | Detect drift from the source method |
| Memseek `standard` | Establish the current search baseline |
| Memseek `membukkit@1` | Measure the integration |

Then layer optional changes one at a time:

1. bucket gate only;
2. dual lanes;
3. query-depth policy;
4. cross-encoder; and
5. combined recipe.

### 5.3 Claims discipline

The 92.6% LongMemEval-S number belongs to the exact frozen MemBukkit recipe. A
Memseek result may cite it only when Memseek runs the same effective corpus,
embedding, retrieval policy, reader, distiller, and official judge inside the
recipe's declared tolerance.

Before that, publish narrower statements such as:

- “`membukkit@1` opened 31% of the eligible Memseek bank”;
- “answer accuracy changed from X to Y under recipe Z”; or
- “dual lanes added N points over the atomic-only ablation.”

## 6. Implementation sequence

### Phase 0 — contract, registry, and baseline

**Changes**

- Add Search Method and Retrieval Receipt to domain vocabulary.
- Add strict `method` configuration to `SearchProfileDefinition`;
  omitted means `standard`.
- Replace scattered descriptor/runtime/projection selection with one registry
  Module.
- Return `CandidateBatch` and surface its receipt without changing
  standard search behavior.
- Add the thin Memseek Adapter to the MemBukkit harness and record a baseline.

**Exit**

- A fixture method can be registered once and exercised through catalog load,
  projection dispatch, `/search`, and `GET /rank/schema`.
- Existing response and ranking tests for profiles without `method` are
  unchanged except for an additive standard-method receipt if explicitly chosen.
- The baseline artifacts exist before bucket code lands.

### Phase 1 — PostgreSQL bucket lifecycle

**Changes**

- Add partition/bucket/member tables.
- Add the claim-fenced refit job and staged activation.
- Extend projection execution with the database/profile context required by a
  local projection Adapter.
- Assign new ready rows against active centroids.
- Handle deletion, current keyed successors, embedding-space mismatch, retries,
  and refit rollback.

**Exit**

- A partition never becomes active before complete verified assignment.
- Failure leaves the previous generation active.
- Replaying projection/refit work is idempotent.
- Workspace, entity, collection, version, and embedding-space isolation are
  proven in integration tests.

### Phase 2 — usable `membukkit@1` vertical slice

**Changes**

- Implement budgeted centroid routing and PostgreSQL candidate selection for the
  constrained v1 source shape.
- Include whole-bucket overshoot and cold/unassigned state in the receipt.
- Add one package/view using the existing `messages` and
  `memories` domain collections.
- Document profile opt-in and operator fit/refit commands.

**Exit**

- The named view returns canonical hits plus separate verbatim/atomic receipts.
- The standard profile remains one binding edit away.
- Missing or incomplete partition state never masquerades as a successful
  Membukkit search.

### Phase 3 — ablation and query policy

**Changes**

- Run standard, bucket-only, dual-lane, and dynamic-depth comparisons.
- Add a Query Policy Module only if dynamic depth produces a measured gain.

**Exit**

- The chosen fixed and routed policies are recorded as frozen recipe inputs.
- Unsupported languages or query classes have explicit behavior.
- The selected policy and rationale appear in the response.

### Phase 4 — optional reranker

**Changes**

- Add a Reranker Interface.
- Preserve `llm_judge` as one Adapter.
- Add `cross_encoder_http` only if its ablation clears the gate.

**Exit**

- Quality gain exceeds the predeclared tolerance or the Adapter is dropped.
- No torch dependency enters Memseek core or the API event loop.

### Phase 5 — Turbopuffer parity

**Changes**

- Implement the `(membukkit@1, turbopuffer)` Adapter.
- Store partition metadata and bucket attributes through the existing durable
  projection lane.
- Re-run canonical-recheck and receipt parity tests against PostgreSQL.

**Exit**

- Backend choice changes storage/latency, not method semantics or receipt fields.

## 7. Test surface

### Definition and registry

- unknown method/version/backend pair;
- method-specific unknown options;
- omitted method preserves standard behavior;
- method/profile definition hash changes on every behavior-changing option;
- one registration drives loader, engine, projections, availability, and schema
  diagnostics; and
- collection `allowed_search_profiles` still gates deployment overrides.

### Partition lifecycle

- deterministic fit for seed and input order;
- `k_eff` when the bank is smaller than configured buckets;
- staged coverage verification and atomic activation;
- no activation on dimension/space mismatch;
- idempotent upsert/delete/retry;
- new-record nearest-centroid assignment;
- unassigned admission during races;
- growth-triggered refit;
- prior generation survives fit failure;
- keyed current successor removes the old current member;
- erasure removes membership; and
- workspace/entity/collection/version isolation.

### Search contract

- exact v1 scope validation;
- whole-bucket budget and overshoot;
- deterministic centroid and candidate ties;
- candidate cap does not alter reported opened count;
- canonical readiness, tombstone, status, current, and typed-filter recheck;
- no cross-workspace or cross-entity candidates;
- cold-start standard/error behavior;
- receipt survives single- and multi-source search; and
- final ranking metadata remains authoritative.

### Benchmark

- MemBukkit reference parity smoke test;
- Memseek standard baseline;
- bucket-only;
- atomic-only, verbatim-only, and union;
- fixed versus dynamic depth;
- cosine versus cross-encoder if Phase 4 is attempted; and
- complete-run guard before any frozen tolerance check.

## 8. Risks and controls

| Risk | Control |
|---|---|
| Partition explosion from arbitrary scopes | Constrain v1 to one explicit collection/version/entity; widen only with a bounded fan-out design. |
| Search profile changes strand partition state | Key state by profile definition hash; build a new generation and declare cold behavior. |
| New rows become unreachable before assignment | Include/report the bounded unassigned lane; never silently omit it. |
| A large bucket overshoots the budget | Open whole buckets, report exact overshoot, and keep eligible/opened/returned counts distinct. |
| Fit work blocks ordinary queues | One claim-fenced batch/generation lane with heartbeat, retry, and worker interleaving. |
| Method code drifts from MemBukkit | Port only the small pure routing core with attribution and keep parity fixtures against the source repository. |
| Quality claim confuses search with reader/distiller gains | Use one harness, frozen inputs, layer-by-layer ablations, and exact recipe names. |
| Cross-encoder adds operational weight without value | Require a measured incremental gain before implementing/keeping it. |
| Marketing says the method ships before it does | Treat the release gate below as a publication dependency. |

## 9. Release gate

Before calling the method available:

1. all Phase 0–2 tests pass;
2. the MemBukkit harness completes a non-lite comparison with complete-run
   guards;
3. the quality tolerance is declared before viewing results;
4. standard versus `membukkit@1` quality, latency, opened fraction,
   candidate count, and reader tokens are published together;
5. cold-start and degraded counts are zero or explicitly explained;
6. the docs show the exact supported scope and do not imply query-wide profile
   switching;
7. copied code preserves Apache-2.0 attribution; and
8. the implementation claim is synchronized in:
   - `marketing/src/pages/benchmarks.astro`,
   - `marketing/src/pages/membukkit.astro`, and
   - the generated/static homepage under `marketing/public/`.

Until then, those surfaces should say “planned for Memseek” rather than
“available in Memseek.”

## 10. Explicitly out of the critical path

These MemBukkit capabilities may be valuable, but none is required to make it a
Memseek Search Method:

- ingest parsers;
- `as_of`/bitemporal reads;
- automatic supersession inference;
- GUI;
- general cost accounting;
- `Memory.add/search/ask` facade;
- RAG query decomposition;
- bucket labeling; and
- model checkpoint/dimension migration.

Plan each separately after the search-method release gate. In particular,
`as_of` touches current/history semantics and deserves its own decision;
it should not delay candidate routing.

## 11. Recommended decisions

1. **Public name:** use `membukkit` with an explicit integer
   `version`. “CoreMem” remains the research-method description in
   benchmark artifacts.
2. **Selection:** operator-selected Search Profile first. Per-request profile
   selection is a separate feature and is not implied here.
3. **First backend:** PostgreSQL.
4. **First ranking:** Memseek's existing exact cosine/rank expression.
5. **First composition:** named-view dual sources over author-named collections.
6. **Cold start:** declared `standard` fallback with a non-empty degraded
   receipt for the reference package; deployments may choose `error`.
7. **Code sharing:** port only the small partition/routing core with attribution;
   do not add MemBukkit or torch as a Memseek runtime dependency.
8. **Benchmark ownership:** keep frozen recipes in MemBukkit and call Memseek over
   its public HTTP Interface.

That sequence delivers the product statement the repository already wants—a
real, selectable, auditable MemBukkit search method—while preserving Memseek's
canonical recheck, definition hashing, workspace isolation, and lean API runtime.
