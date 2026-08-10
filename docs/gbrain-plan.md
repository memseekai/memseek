---
title: The gbrain capability plan
eyebrow: Design plan
---

# Implementing gbrain on memseek — detailed plan

This plan re-expresses [Garry Tan's gbrain](https://github.com/garrytan/gbrain) as memseek
definitions and a small number of new Task adapters / engine stages. It is **not** a port of
gbrain's ~250K LOC of TypeScript. It reproduces gbrain's *distinctive* capabilities on memseek's
immutable-record substrate.

## Implementation status

- [x] **Phase 1 — knowledge graph:** `pages`/`edges`, deterministic link extraction, bounded graph
  traversal, the `graph_query` named view, and edge traversal indexes are implemented. Graph remains
  available only through the generic named-view route; there is no graph-specific endpoint.
- [x] **Phase 2 — answer:** `POST /answer`, citation-visible synthesis, optional anchored graph
  context, gaps, `save:true` provenance writes, the `syntheses` collection, SDK support, and tool
  discovery are implemented.
- [x] **Phase 3 — retrieval rewrite and reranking:** opt-in `rewrite:true` query rewriting inside
  `POST /answer`, bounded `llm_judge` reranking, and anchored graph-distance boosts in `SearchSpec`
  are implemented. External rerank providers remain future work.
- [x] **Phase 4 — dream cycle:** bounded write-triggered slices are implemented: page writes
  deterministically rebuild a `facts` index, transcript writes produce cited `atoms`, new graph/atom
  signals can produce cited `patterns`, new atoms/patterns update one bounded `concept_index` array,
  new atoms/patterns/fact-index changes update one bounded `take_index` array, and thin pages are
  directly and cautiously enriched in place. `orphan_pages` reports current isolated pages through
  the generic named-view route. The gbrain package also declares a trusted daily retention job that
  permanently purges current page tombstones after 30 server-recorded days, in batches of 25 pages,
  through canonical erase semantics. **Phase 5b's explicit gbrain MCP declaration, compiler,
  authenticated discovery, remote Streamable HTTP endpoint, and local stdio server are
  implemented.**
- [x] **Interactive showcases:** `examples/gbrain_showcase.py` is a
  live terminal walkthrough that seeds an isolated entity and exercises the implemented graph,
  answer, retrieval, atom, pattern, fact-index, and concept-index surfaces through the existing
  SDK/routes. It first publishes the self-contained
  `example catalog` as `gbrain@0.13.0`, and adds no API surface.
  `marketing/public/showcase/gbrain/index.html` is
  the animated, static product tour, published with the marketing site at `/showcase/gbrain/`. `examples/pydantic_ai_mcp_showcase.py` is the companion animated Pydantic AI v2
  client: it displays any selected package's declared MCP allowlist, connects through the shipped
  stdio bridge, and traces only real declared-tool calls.

## Design decisions (baked in)

1. **Pages collection.** gbrain's system of record is markdown files in git; memseek's is immutable
   records. We add a keyed `pages` collection (markdown body + frontmatter fields + wikilink
   resolution) so the brain has a gbrain-shaped page surface, but **memseek records remain
   canonical** and **git-source sync is out of scope**.
2. **Synchronous `/answer`.** gbrain's `think` returns a synthesized answer inline. We expose that
   capability as bounded synchronous `POST /answer`, rather than only the async enqueue-and-poll
   flow.
3. **Respect memseek's design priorities** (from project memory): YAML-author clarity first;
   explicit `entity.id`-style naming; no backward compatibility burden; one concept per definition.
   → We introduce a **new `edges` collection** for zero-LLM graph edges rather than overloading the
   existing semantic-`relations` collection.
4. **Graph traversal is a named view.** It is defined, versioned, discovered, and invoked through
   memseek's existing view contract. There is no graph-specific HTTP endpoint or tool; a caller
   queries `graph_query` through `POST /views/graph_query/query` like every other view.
5. **gbrain is an example catalog, not part of the default catalog.** All gbrain collections,
   derivations, views, artifact, package, model/processor/rank configuration, and its explicit
   MCP declaration live in `examples/gbrain_catalog`. A workspace opts
   in by publishing `gbrain@0.13.0`; the default catalog exposes none of those surfaces.

## What already exists (reuse, don't rebuild)

- Hybrid RRF search, portable rank AST, named views, search profiles — `src/memseek/search/`
- Cited synthesis and provenance primitives — `derivations/reflection.yaml`
- Contradiction / reconcile / worldview loops — `derivations/`
- Durable job queue + worker + search fan-out (`foreach`)
- Task adapter extension point — `register_task` in `src/memseek/derive/tasks.py`
- Workspace catalog upload (schema-pack analog) — `POST /catalog`, `src/memseek/workspace_catalog.py`
- Deterministic artifact rendering — `src/memseek/artifacts.py`

---

## Phase 1 — Self-wiring knowledge graph (headline feature) — implemented

gbrain extracts typed edges on **every page write with zero LLM calls** (`inferLinkType` in
`src/core/link-extraction.ts`): markdown links, bare `dir/slug` refs, and `[[wikilinks]]` are
resolved and classified by verb regex + page-role priors into
`works_at | invested_in | founded | advises | attended | mentions | image_of | wikilink_basename`.

### 1a. New `edges` collection — `examples/gbrain_catalog/collections/edges.yaml`

Event-mode records, one per directed typed edge. Fields declared filterable for traversal:

```yaml
collections:
  - name: edges
    version: 1
    active: true
    mode: event
    schema:
      type: object
      required: [text, subject, object, predicate, link_source]
      properties:
        text: {type: string}
        subject: {type: string}          # page key (slug) or entity id
        object: {type: string}
        predicate:
          enum: [works_at, invested_in, founded, advises, attended,
                 mentions, image_of, wikilink_basename]
        link_source: {enum: [markdown, wikilink-resolved, bare-slug]}
        context: {type: string}          # excerpt around the match (provenance)
        confidence: {type: number, minimum: 0, maximum: 1}
      additionalProperties: false
    fields:
      subject:   {path: content.subject, type: string, filter: true, project: true}
      object:    {path: content.object,  type: string, filter: true, project: true}
      predicate: {path: content.predicate, type: string, filter: true, project: true}
    required_processors: [embedding_v1]   # optional; enables semantic edge search
    search_profile: pg_default
```

Keep `collections/relations.yaml` untouched — it stays the
*semantic* relation collection (contradictions). `edges` is the *structural* graph. This is the
MECE split gbrain itself uses (`links` table vs. semantic relations).

### 1b. New pure-function Task adapter — `use: extract_relations`

Port `extractPageLinks` + `inferLinkType` verbatim as deterministic Python. No LLM, no DB.

- File: `src/memseek/derive/tasks_graph.py` (new module).
- Config model `ExtractRelationsConfig`: `dir_pattern` (entity dir whitelist), predicate regex
  overrides, `emit_mentions` (default false → typed edges only, matching gbrain's default).
- Handler: takes rendered source records / page bodies from `input`, runs the regex passes
  (markdown-link pass, bare-slug pass, wikilink pass), classifies each with the ported
  `infer_predicate(page_type, context, global_context, target)`, returns a list of edge dicts
  ready for `emit` into `edges`.
- Register with `register_task("extract_relations", implementation_hash=<sha256>, ...)` and add the
  module to `TASK_MODULES` (`src/memseek/config.py` → loaded at API +
  worker startup). This is the sanctioned adapter-extension path; no engine changes.

Ported verb rules (from `inferLinkType`):
`meeting → attended`, `media/image → mentions/image_of`, then
`FOUNDED_RE→founded`, `INVESTED_RE→invested_in`, `ADVISES_RE→advises`, `WORKS_AT_RE→works_at`;
person→company role priors (`PARTNER_ROLE→invested_in`, `ADVISOR_ROLE→advises`,
`EMPLOYEE_ROLE→works_at`); else `mentions`.

### 1c. Write-triggered derivation — `examples/gbrain_catalog/derivations/link_extraction.yaml`

Fires on every write to `pages` (and optionally `main`), zero-LLM, emits into `edges`.

```yaml
name: link_extraction
trigger:
  write: {collections: [pages], statuses: [active]}
sources:
  changed_pages:
    kind: changes
    collections: [pages]
    keyed: true
    max_records: 50
model: none            # NEW: allow model-less pipelines (see Cross-cutting)
limits: {max_tasks: 1, max_llm_calls: 0, max_wall_s: 30}
tasks:
  - id: edges
    use: extract_relations
    with:
      emit_mentions: false
emit:
  from: "{{edges}}"
  collection: edges
  type: edge                      # predicate remains structured edge content
```

`emit.type` is a static record type in memseek, so `content.predicate` is the typed graph
relation. The record type is always `edge`; it is not dynamically templated from the predicate.

Cross-cutting change required: the pipeline schema currently expects `model`; add support for a
model-less pipeline (`max_llm_calls: 0`) so a purely deterministic extraction derivation is legal.
Confirm in `src/memseek/derive/schema.py`.

### 1d. Graph traversal primitive + `use: graph` Task adapter — multi-hop traversal

gbrain's `traverse_graph` / `graph-query`. No home in memseek today.

- Add one workspace-scoped traversal primitive, then expose it through both the named-view executor
  and the `use: graph` derivation task. The task keeps graph traversal available to `answer`; it does
  not create a separate read surface.
- Extend the `TaskContext` protocol (`tasks.py:89`) with a bounded
  `traverse(seed, *, predicates, direction, depth, limit)` capability, and implement it on
  `_RuntimeTaskContext` (`runner.py`).
- Implementation: a **recursive CTE** over the `edges` projection, workspace-scoped, starting from
  seed subject(s), following `subject → object` (and/or reverse for backlinks), filtered by
  `predicate`, bounded by `depth` and a hard row cap. Returns nodes + paths with the edge records as
  citations. Reuses the same canonical-reload + scope-recheck discipline the search engine uses.
- `use: graph` has an empty static `GraphTaskConfig`; its templated **input** is
  `{seed, predicates, direction, depth, limit}`. This follows the task runtime's existing typed
  input-rendering path instead of introducing a second configuration-templating mechanism.

### 1e. Graph named view + backlinks

- Add `examples/gbrain_catalog/views/graph_query.yaml` as a parameterized, versioned **graph-kind view**. Its parameters
  are `seed` (required), `predicates` (optional string array), `direction` (`out`, `in`, or `both`,
  default `out`), `depth` (bounded integer, default `1`), and `limit` (bounded integer). It returns
  `nodes`, ordered `paths`, and cited edge records.
- Extend the named-view definition/executor only as needed to support `kind: graph`: retain the
  existing `POST /views/{view_name}/query` route, typed parameter validation, versioning, catalog
  listing, response-size bound, and workspace authorization. Search-kind views continue to execute
  their `SearchSpec`; graph-kind views call the traversal primitive from 1d.
- `GET /views` advertises `graph_query` automatically. The existing generic view capability/tool
  invokes that route; do not add a graph-specific entry to `/tools`.
- **Backlinks** are `graph_query` with `direction: in` and `depth: 1`, equivalent to gbrain's
  `get_backlinks`.

### Phase 1 migrations / tests
- Add an Alembic revision for partial expression btrees over active, ready `edges` on
  `(workspace, content.subject, content.object)` and the reverse object/subject order. Those
  indexes serve every recursive hop; predicate filtering remains bounded within the hop.
- Tests: golden-corpus extraction determinism (same input → same edges), predicate-classification
  table tests ported from gbrain fixtures, recursive-CTE depth/cap bounds, scope isolation.

---

## Phase 2 — `answer`: synthesis with citations + gap analysis — implemented

gbrain `think` = INTENT → GATHER (hybrid + takes + graph) → SYNTHESIZE → **gaps**. The first
memseek slice exposes that capability as `answer`, using one bounded hybrid retrieval and an
optional two-hop graph walk before a schema-constrained model call. It deliberately does not add
generic request variables to declarative pipelines: the existing pipeline model has a stream driver
rather than caller-supplied inputs, and widening that model is a separate architectural decision.

### 2a. `examples/gbrain_catalog/collections/syntheses.yaml`

Persisted answers use the keyed `syntheses` collection. Its key is a stable hash of the question
and optional scope, its content retains the question, anchor, and gaps, and its canonical
`derived_from` parents are exactly the cited record IDs. The collection uses the normal embedding
processor and canonical write path; no answer-specific storage mechanism is introduced.

### 2b. Synchronous `POST /answer`

- `POST /answer` `{question, anchor?, since?, until?, rewrite?, save?}` returns `{answer,
  retrieval_query, citations[], gaps[], input_ids[], model_usage, saved_id}` in one call.
- Retrieval is limited to 20 hybrid-search hits from the available memory/page collections,
  including `atoms` and the page `facts` index when the gbrain package is installed. When
  `anchor` is present, graph traversal is `both`, depth 2, and at most 10 paths; otherwise no graph
  lookup is made.
- The normal path has one answer call plus at most one JSON-schema correction. With `rewrite:true`,
  a cheap model first produces one retrieval query (also with at most one correction). The request
  remains capped at four model calls, 60,000 tokens, and 150 seconds, each additionally clipped by
  deployment-wide limits.
- Every model citation must be a UUID literally visible in the fenced search or graph evidence.
  Invalid or invented citations fail the request rather than being saved.
- With `save:true`, `syntheses` is written through `insert_public_records`; citations become
  canonical provenance parents. With `save:false`, the endpoint is read-only.

### Phase 2 tests

- Stubbed-provider coverage asserts real citations, populated gaps, bound enforcement, and
  `save:true` provenance.

---

## Phase 3 — Retrieval quality (rerank / expansion / graph signals)

memseek has RRF + rank AST. This phase adds bounded answer-query rewriting, `llm_judge` reranking,
and anchored graph-distance boosting; multi-query expansion and external rerank providers remain open.

### 3a. Query expansion / rewrite — implemented for `answer`

- `POST /answer` accepts `rewrite: true`. A cheap, schema-constrained call rewrites the question
  into one retrieval query before the existing hybrid search. It preserves the original question for
  synthesis, exposes `retrieval_query` in the response, and shares the answer request's wall-clock,
  token, and model-call budget.
- This is intentionally a single-query rewrite, not multi-query expansion: the current search
  primitive has one query vector per request. General multi-query fusion would be an engine design
  change and remains out of scope for this incremental slice.
- Plain `/search` remains deterministic and model-free; an opt-in rewrite there is deferred.

### 3b. Reranker stage — `llm_judge` implemented

- `SearchSpec.rerank` now supports `none` (the default) and `llm_judge`, with `{top_n}`.
  The stage runs after canonical reload and survivor filtering, before a source's final ordering;
  it therefore never judges stale, cross-workspace, or predicate-failing candidates.
- `llm_judge` uses the configured `cheap` alias by default, limits each source to the top 20 compact
  candidates, fences every record as untrusted data, and requires a finite `[0, 1]` score for
  exactly each UUID it received. Invalid judgments fail the opted-in request rather than silently
  changing the base order. The response reports the applied rerank metadata and judged count.
- `zeroentropy` and other external cross-encoders are deferred. They need a credential, timeout,
  retry, and deployment policy analogous to a new search backend; that is intentionally not folded
  into this bounded initial stage.

### 3c. Graph-signal boost (gbrain `graph-signals`) — implemented

- `SearchSpec.graph_boost` accepts `{anchor, depth, weight, limit}`. It traverses the existing
  workspace-scoped `edges` graph in both directions after ordinary source ranking/fusion, then adds
  `weight / (distance + 1)` for a result whose `key` or `entity` matches a reachable node.
- Graph-boosted searches retain up to 100 pre-boost candidates per source before the final `k`
  cutoff, so an anchor-connected record can actually be promoted rather than only reordered inside
  the already-returned set. The response reports the applied signal and matched-record count.
- This is deliberately a post-fusion opt-in signal, not a new rank-AST operator. Degree-based and
  learned graph features need a concrete evaluation policy before they should become portable rank
  semantics.

---

## Phase 4 — Dream cycle (autonomous overnight enrichment)

**Correction on the "66 cron jobs."** That number is Garry's *personal* production deployment
(many sources × cycles × integrations). The actual definition in gbrain is a single command,
`gbrain dream`, a thin alias over `runCycle` (`src/core/cycle.ts`). `runCycle` executes a
**fixed-order 22-phase pipeline** (`ALL_PHASES`), each phase tagged `source` / `global` / `mixed`
scope (`PHASE_SCOPE`). `gbrain autopilot` is the scheduler: it runs the *source-scoped* phases once
per source and the *global* phases exactly once in a separate `autopilot-global-maintenance` job
(to avoid running the heavy global passes N times concurrently). So the real unit to reproduce is
**these 22 phases**, not 66 cron entries.

### Full phase enumeration (verbatim from `ALL_PHASES`, in execution order)

Scope key: **S** = per-source, **G** = global, **M** = mixed. Mapping: ✅ maps to existing
memseek machinery · 🔨 new memseek derivation/adapter · ⛔ out of scope for this build.

| # | Phase | Scope | What gbrain does | memseek mapping |
|---|-------|-------|------------------|-----------------|
| 1 | `lint` | S | Validate page/frontmatter, dry-fix issues | 🔨 lint derivation/doctor (catalog load already validates *definitions*; page-content lint is new) |
| 2 | `backlinks` | S | Rebuild inbound-link index | ✅ reverse traversal over `edges` (Phase 1d) — recompute on demand |
| 3 | `sync` | S | Sync markdown files ↔ DB (opt. `git pull`) | ⛔ git sync out of scope; with `pages` (Phase 5a) this is the ingest path, not a cron phase |
| 4 | `synthesize` | M | Synthesize pages from transcripts/inputs | ✅ derivation in the `reflection.yaml` pattern |
| 5 | `extract` | S | Link + timeline extraction/materialization | 🔨 Phase 1c `link_extraction.yaml` (+ a timeline extractor task) |
| 6 | `extract_facts` | S | Reconcile DB fact index from each page's `## Facts` fence | ✅ deterministic `fact_extraction` rebuilds one bounded, keyed entity fact-index array |
| 7 | `extract_atoms` | S | Pack-gated Haiku extraction of "atoms" from transcripts | ✅ initial source-scoped `atom_extraction` derivation → cited `atoms`; package selection is the pack gate |
| 8 | `resolve_symbol_edges` | G | Code-intelligence symbol resolution within files | ⛔ code graph — out of scope |
| 9 | `patterns` | M | Detect recurring patterns across the graph | ✅ bounded append-only `patterns` derivation over new `edges` / `atoms` + current page facts |
| 10 | `synthesize_concepts` | G | Aggregate atoms → tier-promoted concept pages | ✅ bounded `concept_synthesis` maintains one cited `concept_index` array; pack-gated |
| 11 | `recompute_emotional_weight` | S | Recompute an emotional/importance weight per page | ✅ a `score` processor / recompute derivation (memseek already has score processors + `importance`) |
| 12 | `consolidate` | S | Cluster unconsolidated facts → synthesize one "take" per cluster | ✅ bounded `consolidate` maintains one cited `take_index` array; non-gradeable and pack-gated |
| 13 | `propose_takes` | S | LLM proposes gradeable claims to a review queue | ⛔ "takes/calibration" subsystem — out of scope (could map to a reviewed-artifact + judge derivation later) |
| 14 | `grade_takes` | G | Judge model verdicts takes against retrieved evidence | ⛔ out of scope (as above) |
| 15 | `calibration_profile` | G | Aggregate resolved takes → narrative pattern statements + bias tags | ⛔ out of scope (Hindsight calibration wave) |
| 16 | `conversation_facts_backfill` | S | Opt-in bulk fact extraction for long conversation pages | 🔨 opt-in extraction derivation (default off) |
| 17 | `enrich_thin` | S | Develop thin stub pages into full entries | ✅ bounded direct replacement of one thin keyed page; no invented facts |
| 18 | `skillopt` | G | Self-evolving skills optimizer (opt-in, default off) | ⛔ out of scope; memseek has a reviewed skill lifecycle (`skill.yaml`) but not the optimizer loop |
| 19 | `embed` | G | Embed new/changed content | ✅ memseek embeds at ingest via `required_processors`; the backfill = existing enrichment sweep + `memseek reindex` |
| 20 | `orphans` | G | Detect orphan pages (no in/out edges) | ✅ bounded `orphan_pages` named view; current page provenance makes superseded-source edges stale |
| 21 | `purge` | G | Purge soft-deleted pages past retention | ✅ catalog-declared scheduled tombstone purge using canonical `erase` |
| 22 | `schema-suggest` | S | Passive suggestion of new page types/schema | ⛔ agent-writable schema mutation — out of scope (memseek catalog is workspace-uploaded) |

**Plus** memseek's own already-built loops that belong in the nightly set but have no gbrain-phase
equivalent by name: **contradiction sweep** (`contradiction.yaml`),
**self-contradiction / reconcile** (`belief_conflict.yaml`,
`reconcile.yaml`), and **citation repair**. The first bounded slice
now repairs saved gbrain `syntheses` when a directly cited keyed record is superseded or retracted.

### In-scope dream cycle for this build

The realistic memseek "dream cycle" (dropping the ⛔ rows) is roughly:

```
extract (links/timeline) → extract_facts → extract_atoms → patterns →
synthesize / synthesize_concepts → recompute_weight → consolidate →
enrich_thin → contradiction sweep → reconcile → citation repair →
backlinks recompute → orphans report → embed backfill → purge
```

#### Implemented initial slices: `extract_facts`, `extract_atoms`, `patterns`, `synthesize_concepts`, `consolidate`, `enrich_thin`, `orphan_pages`, saved-synthesis citation repair, and pages retention purge

`examples/gbrain_catalog/collections/facts.yaml` stores one keyed `page_facts` record per entity. Its `facts` array is a
complete materialized index of `{page_key, text}` entries, rather than independently keyed facts.
`examples/gbrain_catalog/derivations/fact_extraction.yaml` uses a normal page-write `changes` cursor plus the existing
guarded `current` source to read at most 64 current pages. The pure `extract_facts` Task parses only
ordered or unordered list items under a `## Facts` heading, ignores fenced code, and replaces the
static index record. Removed page facts therefore disappear in the next replacement without dynamic
keys, tombstones, a special endpoint, or a scheduler. The index exposes an explicit truncation flag
if its 100-fact output bound is reached; entries are capped at 80 characters to keep canonical
records within the existing content bound.

`examples/gbrain_catalog/collections/atoms.yaml` defines append-only, embedded atomic memories with a constrained
`kind` (`fact`, `preference`, `commitment`, `decision`, `outcome`, or `relationship`) and an
evidence-grounded confidence. `examples/gbrain_catalog/derivations/atom_extraction.yaml` was introduced in gbrain 0.4.0 and
is included in the current gbrain package. It fires on ready transcript writes, consumes at most
five transcript records, makes at most one cheap-model attempt plus one schema correction, emits at
most 20 cited atoms, and does not add a new endpoint or scheduler. `/answer` includes atoms in its
existing hybrid evidence scope when the collection is present.

This intentionally starts with write-triggered extraction rather than an `entities: any` nightly
snapshot: repeatedly re-emitting atoms or patterns without a consolidation/retention policy would
create duplicates. `pattern_detection` therefore remains write-triggered for now; the cron cadence
and repeated-run retention policy remain later work.

`examples/gbrain_catalog/collections/patterns.yaml` is the first append-only dream-cycle output above atoms and facts. Its
write-triggered `pattern_detection` derivation consumes at most 20 new ready `edges` or `atoms` and
the one current `page_facts` index. It emits at most three short, cited patterns only when a model
can connect at least two visible records; otherwise it emits no records. This makes the layer useful
to `/answer` immediately while deliberately leaving periodic cron orchestration and repeated-run
retention to later slices.

`examples/gbrain_catalog/collections/concepts.yaml` adds no dynamically keyed records. Instead,
`concept_synthesis` maintains one static-key `concept_index` record whose bounded `concepts` array
contains title, concise statement, and confidence. It is triggered by newly ready atoms or
patterns, reads the current fact and concept arrays, and may emit no replacement when there is no
justified change. Each replacement is cited and retains at most 12 concepts. This keeps concept
maintenance bounded, auditable, and simple without inventing dynamic concept-key machinery.

`examples/gbrain_catalog/collections/takes.yaml` is the deliberately smaller consolidation
surface, not the later proposal/grading subsystem. `consolidate` watches newly ready atoms,
patterns, or the materialized page-fact index, then maintains one static-key `take_index` record.
Its `takes` array holds at most 12 evidence-backed conclusions with an exact citation list per
entry; the record-level citations cover the complete replacement. The derivation can retain and
merge supported takes or emit no replacement when no cluster supports a conclusion. This gives
`/answer` and generic search a compact interpretation layer without dynamic keys, review queues,
or a new route.

`examples/gbrain_catalog/derivations/enrich_thin.yaml` directly replaces a live thin page when the
page can be expanded solely from visible page, fact, concept, and take evidence. The model must
preserve the page key, title, type, and existing content fields, set `gbrain_enriched: true`, and
return no record for substantive or unsupported stubs. The bounded `emit.driver_key: true` mode
carries exactly one keyed driver record's captured key to one keyed output, with the normal
active-head receipt and provenance checks still enforced. The trigger
also ignores its own output and the successor scan does the same, so a direct replacement cannot
retrigger itself.

`examples/gbrain_catalog/views/orphan_pages.yaml` is a bounded `kind: graph_orphans` named
view, reached only through `POST /views/orphan_pages/query`. It reports current ready `pages` whose
key has no incoming or outgoing current edge. Because edges are append-only events, the report only
counts an edge when it directly cites the present source-page head; a link from a superseded page
cannot keep either endpoint falsely connected. The report is read-only and adds neither an orphan
endpoint nor a scheduler.

`repair_synthesis` is a single hourly `entities: any` derivation. Its narrow `stale_citations`
driver reads only the current keyed `syntheses` record whose direct non-system citation has a newer
ready version in the same canonical slot (a ready tombstone counts as newer); the cron scan uses
that same selector before enqueueing work, so unrelated entities do not receive noop repair runs.
The saved-answer
schema is now `syntheses@2`: it records `question`, `anchor`, `since`, `until`, and `rewrite`, so
the trusted replay task can call the existing bounded answer flow with exactly the original scope.
It never writes directly and adds no route. The normal keyed emission receipt prevents overwriting a
newer saved answer; fresh answer citations remain the only output citations. When a retraction
leaves no current evidence, the repaired answer may carry only gaps and no factual citation. Physical
`/erase` already cascades through provenance, so there is no surviving synthesis record to replay in
that case. The current package release is `gbrain@0.13.0`.

`gbrain@0.13.0` declares `purge_pages`, a trusted daily worker-only retention policy for
`pages@1`. It chooses only **current active keyed tombstones** whose immutable server-side
`created_at` is at least 30 days old; client-provided `occurred_at` cannot make a page eligible
early. Each run selects at most 25 page slots, then passes every historical version in those slots
to the existing canonical erase transaction. That removes the retained page content, tombstone,
and any provenance descendants, emits the normal content-free erasure audit record, and queues
the normal projection deletion repair. Missed retention ticks do not catch up, so downtime cannot
turn the batch limit into a large destructive backlog. It is declarative package metadata plus an
internal worker job—not an API endpoint—and the default catalog has no such policy.

### How to wire it in memseek

- Each record-producing phase is a **cron-triggered derivation** (memseek already supports `cron`
  triggers + persisted `cron_scan` jobs + lexical paging in the worker — M5). `purge_pages` is the
  one destructive exception: a package-declared retention policy schedules an internal
  `retention_purge` worker job, so it can call canonical erase without invalidating a derivation's
  own active job. No new scheduler or HTTP route is needed.
- Reproduce gbrain's **source-vs-global split** with two trigger cadences: per-entity/per-collection
  crons for `source` phases, and a single workspace-wide cron for remaining `global` phases
  (`embed` backfill, `purge`) so heavy passes run once. `orphan_pages` is already a live bounded
  report, so it needs no scheduled materialization.
- Reproduce gbrain's **staleness gating** (`LINK_EXTRACTOR_VERSION_TS` → re-extract pages stale
  since the extractor version bumped) with a definition-hash / version watermark on `edges`; memseek
  freshness already compares input watermarks to derivation run watermarks.
- **Ordering matters** and is load-bearing in gbrain (e.g. `extract_facts` before `patterns`;
  `consolidate` before `embed` so new takes embed same-cycle). memseek derivations don't share a
  single ordered driver — encode dependencies via trigger conditions (`changed` / `census` /
  `quiet`) or a small ordered orchestrator derivation, and document the intended order.

Deliverables: one cron trigger per in-scope phase (or `triggers/dream_cycle.yaml` grouping them),
the new derivations marked 🔨 above, and an ops doc describing phase order + the source/global split.

---

## Phase 5 — Pages, wikilinks, declarative MCP server, CLI

### 5a. `pages` import ergonomics — partially implemented

- The keyed `pages` collection (`key: slug`, `type`, `title`, `body`, and embedding) and its
  Phase 1 write trigger are already implemented.
- `POST /records` is the current page ingest path. A dedicated SDK helper or `POST /pages` remains
  optional ergonomics work, not a prerequisite for graph extraction.
- **Wikilink resolution — implemented:** `extract_relations` now receives a bounded current-page
  index alongside changed pages. Direct `[[dir/slug]]` links retain their normal structural
  classification; bare `[[name]]` links resolve deterministically against normalized page titles and
  terminal slugs. Ambiguous names deliberately fan out to one `wikilink_basename` edge per matching
  page, rather than guessing a winner. This is extractor behavior only: it adds no resolver route or
  page-specific endpoint.

`gbrain@0.5.0` introduced the deterministic global-basename resolver; the current
`gbrain@0.13.0` package adds patterns, bounded concepts, consolidated takes, direct thin-page
enrichment, an orphan-page view, bounded saved-answer citation repair, and an explicit MCP
allowlist while preserving the existing graph-view and record-ingest interfaces.

### 5b. Declared MCP interface and server — implemented

MCP is a **separate, explicit package interface**, not an automatic projection of every HTTP route,
view, or artifact. A package binds one exact versioned declaration:

```yaml
# packages/gbrain.yaml
mcp: gbrain@1
```

The declaration lives at
`examples/gbrain_catalog/mcp/gbrain.yaml`. It is an
ordered allowlist: gbrain exposes only `answer`, `search_memory`, `explore_graph`,
`find_orphan_pages`, `context`, and `record`. Adding a view or artifact to the catalog does not
make it an agent capability; it must be deliberately added to this file and the MCP interface
version must be bumped.

- **Named search methods, not raw search.** `search_memory` targets the new explicit
  `gbrain_search@1` `kind: search` view. It can retrieve only the bounded gbrain memory corpus
  (`pages`, `atoms`, `facts`, `patterns`, `concepts`, and `takes`). It does not expose a generic
  `/search` escape hatch, generated answers, raw transcripts, or structural edges. Graph evidence
  is obtained through the separate `explore_graph` view.
- **Views and artifacts are exact bindings.** An MCP `kind: view` tool names one exact
  `view: name@version`; an `artifact` tool names one exact `artifact: name@version`. Their input
  schemas are compiled from the referenced definition's typed parameters, including descriptions,
  enums, and bounds. The MCP YAML supplies the tool name and purpose, rather than copying or
  weakening the parameter contract.
- **Built-ins are opt-in capabilities.** `answer` and `record` have no arbitrary route template.
  The HTTP server and stdio adapter map them to the existing bounded answer and canonical-record reads. MCP
  `answer` must force `save: false`; a write-capable answer is a future, separately declared
  capability.
- **Discovery and transport remain generic.** The catalog compiler validates the package-to-MCP
  reference and every view/artifact reference. Authenticated `GET /tools` publishes the selected
  package's declared tool list, exact bindings, generated JSON Schemas, and interface/catalog
  hashes. The authenticated `/mcp` endpoint serves that declaration remotely; `memseek mcp` is the
  local stdio adapter. Both invoke the existing generic routes (`/answer`, `/records/{id}`,
  `/views/{name@version}/query`, and `/artifacts/{name@version}/render`). No per-view, graph, or
  artifact endpoint is introduced.

The versioned `mcp/*.yaml` definition family is part of the package compiler and catalog hash.
Package closure rejects undeclared or unpinned view/artifact targets. Parameter descriptions and
constraints generate the same JSON Schemas used by runtime validation. Both MCP transports refresh
the authenticated declaration before listing/calling tools and never follow an arbitrary endpoint
from discovery. Tests cover compiler closure, generated schemas, default/gbrain workspace discovery,
HTTP authentication/negotiation, and exact-reference dispatch.

The animated `pydantic_ai_mcp_showcase.py` runs its Pydantic AI/FastMCP client in an isolated
environment and launches the MCP SDK 2.x `memseek mcp` command from the project environment with
explicit workspace credentials. This avoids a package conflict while proving legacy-client
negotiation, and makes no new route or gbrain-only tool contract.

### 5c. CLI verbs (optional, gbrain is CLI-first)
- Add `memseek search`, `memseek answer`, `memseek view graph_query`, `memseek ingest` to
  `src/memseek/cli.py` wrapping `MemseekClient`. Purely ergonomic; the HTTP
  API is the real interface. The CLI uses the same named-view route; it has no `memseek graph`
  special case.

---

## Cross-cutting work

- [x] **Model-less pipelines** (`max_llm_calls: 0`) support the deterministic extractor.
- [x] **`TaskContext` graph capability** and graph-kind named views use the generic
  `POST /views/{view_name}/query` route; no `/graph` route or graph-specific tool exists.
- [x] **`POST /answer`** and its discovery contract are implemented. `POST /pages` remains an
  optional Phase 5 convenience surface; page ingest currently uses `POST /records`.
- [x] **Migrations and package manifest:** edge traversal indexes and
  `gbrain package` are implemented. `syntheses`,
  `atoms`, `concepts`, `takes`, and the keyed `facts` index use the canonical record shape and need no
  bespoke storage migration.
- [x] **Package-declared MCP surface:** `mcp/*.yaml` definitions bind exactly to packages, validate
  exact package-listed targets, derive schemas from typed parameters, and expose only declared tools
  through authenticated discovery, remote Streamable HTTP, and the stdio adapter.
- [x] **Tests:** deterministic extraction, traversal bounds/scope, answer citation/gap behavior,
  rewrite bounds, reranker ordering/validation, graph-boost promotion, saved-answer provenance,
  bounded cited atom extraction, deterministic fact-index replacement, MCP closure/schema
  validation, declared-only discovery, HTTP auth/protocol compatibility, and stdio dispatch are
  covered.

## Out of scope (gbrain features not carried over)

Git-source sync, voice (Twilio/OpenAI Realtime), email/calendar webhooks, OAuth multi-user,
S3/Supabase attachments beyond what memseek has, the admin SPA, eval harnesses (LongMemEval /
BrainBench / cross-modal), gradeable-take calibration/skillopt/brainstorm/autopilot. Any of these can be a
follow-up if wanted.

## Suggested remaining order & rough effort

1. Phase 4 (dream cycle) is complete for the scoped build: citation repair and the bounded
   pages-only retention purge now cover the remaining scheduled maintenance work. The ⛔ phases
   (gradeable takes/calibration, skillopt, code intel, schema-suggest, git sync) remain dropped.
2. Decide whether the optional convenience CLI verbs (`search`, `answer`, named `view`, ingest) or a
   page-import helper justify separate work. `memseek mcp` itself is complete.
