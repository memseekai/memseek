---
title: The gbrain catalog, parameter by parameter
eyebrow: Reading a real catalog end to end
---

[The gbrain showcase](gbrain-showcase.md) describes what
`examples/gbrain_showcase.py` *does*. This page explains how its catalog is
built: every load-bearing parameter in `examples/gbrain_catalog/`, what it
enables, and where it shows up at run time.

Read it after the authoring pages ([Collections](collections.md),
[Pipelines](derivations.md), [Triggers](triggers.md),
[Views & search](views-search.md), [Packages](packages.md)) as a worked example
of all of them at once — or read it first and follow the links when a parameter
raises a question.

The showcase script writes **pages** and **transcripts**, and reads **views**.
That is all it does. Every behaviour a reader would call a feature — a
self-wiring graph, a fact index, cited atoms, patterns, concepts, takes, an
orphan report, a dossier, an MCP surface, a retention job — is a declaration in
the catalog. Roughly 200 lines of SDK calls sit on top of 1,100 lines of YAML.

| Directory | Files | The question it answers |
|---|---|---|
| `collections/` | 9 | What may be stored, and does a second write append or replace? |
| `conf/` | 4 | Which models, which enrichment, how is relevance scored? |
| `derivations/` | 8 | When does compute run, on what, bounded how, emitting what? |
| `views/` | 3 | What can be read, with which typed parameters? |
| `artifacts/` | 1 | How is memory assembled into a prompt? |
| `mcp/` | 1 | Which of the above an agent may call. |
| `packages/` | 1 | The exact version set, plus one operational policy. |

## 1. Collections: the storage contracts

| Collection | `mode` | Written by | Why that mode |
|---|---|---|---|
| `pages` | `keyed` | your application | one current version per page key; a rewrite supersedes |
| `edges` | `event` | `link_extraction` | one row per directed link; history is the point |
| `transcripts` | `event` | your application | a conversation is append-only |
| `atoms` | `event` | `atom_extraction` | each atom is a separate durable claim |
| `patterns` | `event` | `pattern_detection` | observations accumulate |
| `facts` | `keyed` | `fact_extraction` | one slot, `page_facts` |
| `concepts` | `keyed` | `concept_synthesis` | one slot, `concept_index` |
| `takes` | `keyed` | `consolidate` | one slot, `take_index` |
| `syntheses` | `keyed` | `POST /answer` | key is a hash of the question; re-asking supersedes |

### `mode` is the one irreversible choice

`keyed` versus `event` is the whole architecture of this example. `pages` being
`keyed` is what makes `enrich_thin` a *rewrite* instead of a duplicate. `edges`
being `event` is what lets `link_extraction` re-run on every save with no
diffing step — and it is why the orphan report has to define edge *liveness*
(see [§6](#6-views-reads-without-endpoints)).

### The schema is the real bound, not the prompt

Compare the two halves of the concept index. The storage contract:

```yaml
# collections/concepts.yaml
concepts:
  type: array
  maxItems: 12
  items:
    required: [title, text, confidence]
    properties:
      title: {type: string, minLength: 1, maxLength: 80}
      text:  {type: string, minLength: 1, maxLength: 280}
```

And the prompt's opinion:

```yaml
# derivations/concept_synthesis.yaml
Keep at most 12 concepts; use truncated and omitted_concepts only when
more supported concepts exist.
```

The prose is a hint. `maxItems: 12` and `maxLength: 280` are the enforcement,
checked against the destination collection version after the model returns and
before anything commits. A model that returns thirty concepts fails the run; it
does not write thirty concepts. That is the difference between a bounded system
and a prompt asking politely for brevity.

`truncated` and `omitted_concepts` — with `omitted_facts` and `omitted_takes`
alongside them — are the honesty half of the same idea. When a cap bites, the
record says so instead of quietly shortening.

Three collections declare `tombstone: {type: boolean}`. You never write it: a
Pipeline emits `retract: true`, or the API insert passes `tombstone=true`, and
the runtime writes the canonical tombstone content. Declaring the property is
what makes that path legal and visible. `pages` declares it because it is the
collection the package's retention policy erases from.

### `fields`: what becomes queryable

```yaml
# collections/edges.yaml
fields:
  subject:   {path: content.subject,   type: string, filter: true, project: true}
  predicate: {path: content.predicate, type: string, filter: true, project: true}
```

`path` says where the value lives, `filter` enables `where`, `sort` enables
`order_by`, and `project` enables returning the value with hits. Undeclared
content is still stored — it just cannot be queried by name.

Note what these declarations do *not* do. Graph traversal does not use them: it
reads `content->>'subject'` as raw JSON and requires only that collections
literally named `pages` and `edges` exist in the active catalog. Those names and
those content keys are a runtime contract. What the `fields` block buys is
ordinary `where` filtering in searches and views.

### `required_processors`: the readiness gate

```yaml
required_processors: [embedding_v1]
```

This one line explains most of the showcase script's shape. A record is *ready*
only once its required processors succeed, and readiness gates **both** search
visibility **and** trigger eligibility. That is why the script polls
`record["ready"]` after writing pages, and why it waits for graph citations to
appear rather than sleeping a fixed interval: the derivation cascade cannot
start until the worker has embedded the pages.

It is also the source of the showcase's one operational rule — the API and the
worker must run in the same provider mode, because the worker embeds records and
the API embeds queries.

### `syntheses@1` plus `syntheses@2`: a versioning lesson

`collections/syntheses.yaml` ships version 1 as `active: false` and version 2 as
`active: true`. Version 2 adds `rewrite` **to `required`**, plus optional
`since` and `until`.

This is not decorative. `POST /answer` always writes `rewrite` into the saved
record, and version 1 sets `additionalProperties: false` — so a version-1-only
catalog physically cannot save an answer. The `repair_synthesis` Task then reads
the record back with `rewrite` required and extra properties forbidden. Adding a
required property means a new version; keeping the old one present but inactive
is what stops anything that pinned `syntheses@1` from breaking. The example is
[the versioning rule](collections.md#when-to-create-a-new-version), executed.

## 2. `conf/`: models, enrichment, ranking

**`conf/models.yaml`** is the only file in the catalog permitted to name a
provider model. Everything else refers to `cheap`, `strong`, or `embed`. The
`embed` alias is required by name, must have exactly one target, and may never
be used for completions. `defaults.derivation` is the fallback when a Task and
its Pipeline both omit `model:`; `defaults.fold` is validated but not yet
consumed by any runtime path, so declare it and expect nothing from it.

**`conf/processors.yaml`** declares `embedding_v1` over all nine collections. A
processor's `input.collections` must be a superset of every collection that
names it in `required_processors`, and the loader checks this. `importance` is a
`source: constant` score of 5 over `syntheses` only, and the file says why in a
comment: the generic document renderer orders records by that score, and keeping
it here lets the package stand alone instead of importing the default memory
catalog's processor.

**`conf/rank_default.yaml`** — read the `hybrid` variant aloud:

> A record's score is the sum of three terms, each weighted 1.0: its normalized
> best-of(vector similarity, text match); its normalized importance score; and a
> decay curve over hours since `last_accessed`, with a 24-hour midpoint.

A missing score resolves to `0.0`, and `importance` is attached only to
`syntheses`, which no gbrain search includes in scope — so in practice that
middle term contributes nothing to gbrain retrieval. It earns its place through
the document renderer, and the file is inherited whole.

## 3. Deterministic Pipelines: zero model calls

Two Pipelines run on **every page write**. They are the part that stays fully
alive under `LLM_FAKE=1`.

### The shared trigger and source pattern

```yaml
trigger:
  write:
    collections: [pages]
    statuses: [active]
    keyed: true          # only keyed rows: a page write, not an event
sources:
  changed_pages:         # the driver: what is new since my last successful run
    kind: changes
    max_records: 50
    max_tokens: 24000
  current_pages:         # context: every current page, guarded
    kind: current
    max_records: 64
    max_tokens: 40000
```

Two mechanisms are doing real work.

**The subset rule.** A consuming trigger's scope must be a subset of the
driving Source's scope. Without it you could arm a trigger on rows the driver
never consumes, and those rows would sit above a cursor forever, re-firing. Note
that an omitted `types` means *any type*, which is broader than a driver that
names types — so declare `types` on both sides even when the collection only
ever receives one.

**`changes` versus `current`.** `changes` advances a cursor and consumes a ready
prefix; if matching rows remain, commit queues a successor run. `current` is a
*guarded* read: it is reloaded before commit and the run is rejected as stale if
the set changed while Tasks ran. `link_extraction` needs both — `changed_pages`
is what to scan, and `current_pages` is the title and basename index that lets
`[[Acme]]` resolve to `companies/acme`.

The cursor also stores a hash of exactly the fields that decide Source
membership: kind, collections and versions, types, statuses, keyed shape. Edit a
prompt, a limit, or `max_records` and the cursor continues. Change *which
records belong* and the change is rejected — use a new Pipeline identity.

### `link_extraction`: the self-wiring graph

```yaml
model: null
limits: {max_tasks: 1, max_llm_calls: 0, max_wall_s: 30}
tasks:
  - id: edges
    use: extract_relations
    input:
      records: "{{changed_pages.records}}"
      known_pages: "{{current_pages.records}}"
    with:
      emit_mentions: false
emit: {from: "{{edges}}", collection: edges, type: edge}
```

- **`max_llm_calls: 0`** is a guarantee, not a hope. It also bounds calls an
  installed Task might make through its constrained context.
- **`.records` versus `.rendered`** is the distinction to internalize.
  `.records` is the typed record list, validated against the Task's registered
  input type; `.rendered` is escaped prompt rows, which your prompt wraps in
  whatever element you want. Deterministic Tasks always take `.records`.
- **`with:`** is static configuration, validated against the Task's
  configuration model when the catalog compiles. `emit_mentions: false` drops
  every `mentions` edge — the low-confidence fallback emitted when no predicate
  pattern matches — which is what keeps the demo graph readable. The other
  available knobs are `dir_pattern` (which directory prefixes count as entity
  references), `predicate_regex_overrides`, and `context_chars`.
- **Classification** runs a fixed precedence — `founded`, `invested_in`,
  `advises`, `works_at` — then role-based inference, then `mentions`; page type
  wins outright for images, meetings, and media. Code fences are blanked in
  place first, so a link inside backticks never becomes an edge.
- **`emit` with no `keys`** appends event records. Each edge cites the page it
  came from, so it can later be proven stale.

The seed page `people/maya` contains
`Maya founded [Acme](companies/acme) after investing in companies/orbit.` That
yields `people/maya founded companies/acme` from the markdown link and
`people/maya invested_in companies/orbit` from the bare slug. Two typed, cited
edges, one save, no model.

### `fact_extraction`: a complete keyed replacement

```yaml
tasks:
  - id: fact_index
    use: extract_facts
    with: {heading: Facts, max_facts: 100, max_fact_chars: 80}
emit:
  from: "{{fact_index}}"
  collection: facts
  type: page_facts
  keys: [page_facts]
  complete: true
  max_records: 1
```

The bounds are enforced twice: the Task truncates to `max_fact_chars`, and the
collection schema independently caps `maxLength: 80`. The parser reads only
bullets under a `## Facts` heading, ignores fenced code, joins indented
continuation lines, and stops at the next heading. It deliberately does not
infer facts from prose.

The emission triple is the part worth memorizing:

| Declaration | Required Task result | Runtime behaviour |
|---|---|---|
| no `keys` | up to `max_records` unkeyed drafts | append event records |
| `keys` | any unique subset of them | update those slots, leave omissions |
| `keys` + `complete: true` | exactly one draft per declared key | complete replacement |
| `driver_key: true` | exactly one draft, key omitted | rewrite the driving record's slot |

So `keys: [page_facts]` plus `complete: true` plus `max_records: 1` reads as:
there is exactly one current fact index for this entity, and this run replaces
it whole. Delete a page's Facts section and its facts disappear on the next
write, with no separate deletion path. Because the emission is complete, an
absent key may commit with an empty citation list — the complete bounded Source
receipt *is* the provenance for absence.

## 4. Model-backed Pipelines: bounded and cited

Five Pipelines call a model. They differ in emission shape, not in structure.

| Pipeline | Fires on | Driver `max_records` | Emits | Shape |
|---|---|---|---|---|
| `atom_extraction` | new `transcripts` | 5 | `atoms` | event append, ≤20 |
| `pattern_detection` | new `edges` / `atoms` | 20 | `patterns` | event append, ≤3 |
| `concept_synthesis` | new `atoms` / `patterns` | 20 | `concepts` | static-key replace, 1 |
| `consolidate` | new `atoms` / `patterns` / `facts` | 20 | `takes` | static-key replace, 1 |
| `enrich_thin` | a thin, un-enriched `page` | 1 | `pages` | `driver_key` rewrite, 1 |

### `limits`: six independent ceilings

```yaml
limits:
  max_tasks: 1               # declared Task count, up to 20
  max_llm_calls: 2           # includes the one JSON-correction retry
  max_retrieved_records: 0   # no search Task may expose records
  max_visible_records: 22    # union of driving, current, record, view, searched
  max_total_tokens: 36000    # prompt plus completion for the whole run
  max_wall_s: 90             # Task execution wall time
```

With one Task, `max_llm_calls: 2` is exact: one attempt plus one correction.
`max_visible_records: 22` is arithmetic against the Sources — twenty new
signals, one fact index, one current concept index. And
`max_retrieved_records: 0` is a positive statement: this Pipeline may not go
looking for more evidence, it reasons over exactly what its named Sources
supplied.

### The citation contract, mechanically

```yaml
citations:
  type: array
  minItems: 1          # pattern_detection raises this to 2
  uniqueItems: true
  items: {type: string, format: uuid}
```

Two enforcements stack. The schema requires citations to exist; then the runtime
checks that every UUID was **literally present in the rendered prompt** —
Source UUIDs in the prompt are the Task's citation authority, and a Task may
narrow that set but never widen it. A fabricated UUID fails the run.

`pattern_detection`'s `minItems: 2` is the clean example of encoding a
definition in a schema. "A pattern must connect at least two distinct records"
is not left to the prose; the output is unsatisfiable without two citations.

This is also exactly why `LLM_FAKE=1` leaves atoms, patterns, concepts, and
takes empty. The deterministic fake provider cannot produce real citation
UUIDs, so the contract rejects its output. That is the contract working.

### Static-key replacement: read, modify, write

`concept_synthesis` and `consolidate` share one shape:

```yaml
sources:
  new_signals:      {kind: changes, ...}
  current_concepts: {kind: current, keys: [concept_index], max_records: 1}
tasks:
  - id: concept_index
    use: llm
    with:
      output_schema:
        properties:
          records:
            maxItems: 1
            items:
              properties:
                key: {type: string, const: concept_index}
emit:
  collection: concepts
  type: concept_index
  keys: [concept_index]
  max_records: 1
```

The prompt receives its own current output and is told to *maintain* it:
incorporate new signals, retain still-supported entries, merge overlap, and cite
existing-index UUIDs only when preserving something still supported.
`key: {const: concept_index}` makes the slot un-mistakeable at the schema level.
And `{"records":[]}` is a first-class answer — "no justified change" costs one
cheap call and writes nothing.

`current` being a *guarded* Source is what makes this safe under concurrency: if
the index changes while the model is thinking, the commit is rejected instead of
clobbering it. `consolidate` adds a second citation layer, where each take
carries its own citations supporting *that claim* while the record's outer
citations must cover the whole index.

### `enrich_thin`: a driver-key rewrite

```yaml
trigger:
  write:
    types: [page]
    keyed: true
    ignore_own_outputs: true                   # brake 1
    where:
      gbrain_enriched: {exists: false}         # brake 2
tasks:
  - with:
      output_schema:
        content:
          required: [title, body, type, gbrain_enriched]
          properties:
            gbrain_enriched: {const: true}     # brake 3
          additionalProperties: true           # preserve unknown fields
emit:
  collection: pages
  driver_key: true
  max_records: 1
```

`driver_key: true` means "rewrite whichever slot the single driving record
occupies" — the key is not knowable when the catalog is authored. It is
deliberately hemmed in: it requires `max_records: 1`, forbids static `keys`,
forbids `complete: true`, and still captures the same head preconditions as any
other keyed emission.

`ignore_own_outputs: true` is legal *only* because of `driver_key`. It compiles
into a predicate excluding rows whose producing run belongs to this same
processor, and it is also what lets the catalog's cycle detector accept a
Pipeline that writes into its own trigger collection — automatic cycles are
otherwise rejected when the catalog graph loads.

`where: {gbrain_enriched: {exists: false}}` requires that field to be declared
`filter: true` on `pages`, which it is. Trigger predicates and collection field
declarations are one mechanism, not two.

One asymmetry to understand before copying this pattern: `where` is a **trigger**
predicate, and Sources have no equivalent — a record Source declares
collections, versions, types, statuses, and keyed shape, and nothing else. So the
`changes` Source still walks the page cursor one record per run, and an
already-enriched or already-substantive page can arrive as `changed_page` and
spend a call returning `{"records":[]}`. That is bounded rather than free: each
page is consumed exactly once, `max_records: 1` keeps every run cheap, and the
prompt's own guard is what makes the empty answer correct. The lesson to carry
is that a trigger `where` narrows *when you are woken*, never *what you then
read*.

### `repair_synthesis`: provenance repair

```yaml
trigger:
  cron: {expr: "17 * * * *", entities: any}
sources:
  stale_syntheses:
    kind: stale_citations
    keyed: true          # mandatory for this driver
    max_records: 1
```

`stale_citations` selects current, ready, non-tombstone keyed rows whose cited
keyed parents now have a newer ready version — including a tombstone. It has no
cursor: membership is recomputed each run, and the same record reappears until
its citations are current, so the run's own output is what removes it from the
set. `entities: any` matters because this driver *is* its own entity selector,
so the hourly scan enqueues work only for entities that actually have stale
citations instead of one no-op run per entity. `max_records: 1` drains a backlog
as many cheap bounded runs rather than one oversized one.

## 5. Why the cascade terminates

```text
page write ──> link_extraction (0 LLM) ──> edges ─────────┐
           └──> fact_extraction (0 LLM) ──> page_facts ──┐ │
                                                         │ ├──> pattern_detection ──> patterns ─┐
transcript ──> atom_extraction ──> atoms ────────────────┼─┘                                    │
                                       ├─────────────────┴───────────────> concept_synthesis ───┤
                                       └─────────────────────────────────> consolidate ─────────┘
page write ──> enrich_thin ──> page rewrite ──┘  (re-enters the top, once)
```

Three brakes, all declarative:

1. **Scope.** `concept_synthesis` triggers on `[atoms, patterns]`, not
   `concepts`. `consolidate` triggers on `[atoms, patterns, facts]`, not
   `takes`. Neither can retrigger itself.
2. **`ignore_own_outputs` plus `where`.** The `enrich_thin` rewrite re-enters
   `link_extraction` and `fact_extraction` — intended, since the enriched body
   should be re-scanned — but cannot re-enter `enrich_thin`.
3. **`{"records":[]}`.** Every model-backed Pipeline is both instructed and
   schema-permitted to return nothing, so a second lap costs one cheap call and
   writes nothing.

The load-time cycle checker enforces the first. The other two are the author's
job.

## 6. Views: reads without endpoints

`POST /views/<name>/query` is the only route. Three views, three kinds.

### `graph_query`: the shape is fixed, the bounds are yours

This is the most misread file in the catalog. A `kind: graph` view must declare
**exactly** `seed`, `predicates`, `direction`, `depth`, and `limit`, with
exactly those types, required-ness, and defaults. The loader rejects anything
else. A graph view cannot be reshaped.

The separate projection binding says which canonical edge collection supplies
the graph. gbrain uses the conventional role field names, so the defaults are
enough:

```yaml
kind: graph
graph: {edges: edges}
```

Those collection and field names are not runtime constants. Other catalogs can
map their own vocabulary; see [Graph data](graph-data.md).

What the author does own is everything an agent sees:

```yaml
predicates:
  type: string_array
  item_enum: [works_at, invested_in, founded, advises, attended, mentions,
              image_of, wikilink_basename]
  max_items: 8
  description: Optional edge predicates to follow…; omit for every predicate.
depth: {minimum: 1, maximum: 4}
limit: {minimum: 1, maximum: 100}
```

`description`, `item_enum`, and the numeric bounds are the **only** source for
the generated [MCP](mcp.md) JSON Schema, so a bound missing here is a bound the
agent never sees. Get them wrong in the other direction and you advertise a
range the runtime rejects: these maxima must not exceed the deployment's
`MAX_GRAPH_DEPTH` (default 4) and `MAX_GRAPH_PATHS` (default 100), or a
schema-legal request fails with `422 graph_depth` or `422 graph_limit`.

The response carries `nodes`, ordered `paths`, and cited edge records. Global
depth and path ceilings remain server settings, so no definition can turn a read
into an unbounded recursion.

### `orphan_pages`: one parameter, one subtle rule

`kind: graph_orphans` is more constrained still — exactly `limit`, integer,
optional, default 50. Its projection adds `nodes: pages`. The subtlety lives in the runtime: because edges are
*event* records, an edge counts as live only while it still directly cites the
**current** head of its source page. Without that rule, a stale page revision's
edges would keep an isolated page looking connected forever. This is the cost of
`mode: event` on `edges`, paid once, in the right place.

### `gbrain_search`: the query language

```yaml
query:
  q: "{{query}}"
  mode: hybrid
  scope:
    collections: [pages, atoms, facts, patterns, concepts, takes]
    status: active
    versions: current
  k: "{{limit}}"
  include: [text, collection, collection_version, entity, type, key, scores, occurred_at]
  render: true
```

`versions: current` is why a search returns today's concept index rather than
every revision of it. `render: true` adds the token-bounded, prompt-ready block
— turn it on for any view that feeds a model. And `syntheses` is deliberately
*out* of scope: saved answers are not evidence for the next answer.

The showcase's own `search` command bypasses this view and calls `/search`
directly, because it wants one thing a view cannot declare — `graph_boost`:

```python
graph_boost={"anchor": "people/maya", "depth": 2, "weight": 0.15, "limit": 12}
```

That runs a bounded `direction: both` traversal from the anchor and adds
`weight / (distance + 1)` to any hit whose `key` or `entity` appears in the
walk. Structural proximity as a post-ranking signal, requiring only that an
`edges` collection exists.

## 7. The artifact: assembly, not retrieval

```yaml
# artifacts/gbrain_context.yaml
kind: prompt
lifecycle: live
blocks:
  pages:    {document: {entity: "{{entity}}", collections: [pages]},    max_tokens: 4000}
  concepts: {document: {entity: "{{entity}}", collections: [concepts]}, max_tokens: 2500}
  takes:    {document: {entity: "{{entity}}", collections: [takes]},    max_tokens: 2500}
template: |
  PAGES:
  {{pages}}
  …
```

Three `document` blocks, not `view` blocks — and that is the point. A `document`
block is "everything currently there for this entity", with no query and no
relevance ranking. A dossier should be *complete*, not the ten most relevant
slots. Each block carries its own token budget, and every declared block must be
used in the template or catalog validation fails.

`lifecycle: live` means renders happen on demand with no review step. Every
render still records exactly which records went in under which definitions, so
"what did this prompt contain last Tuesday" stays answerable.

## 8. MCP, package, retention

`mcp/gbrain.yaml` is an **allowlist**. Nothing becomes an agent tool by merely
existing. Six tools across the four supported kinds:

| Tool | `kind` | Binds |
|---|---|---|
| `answer` | `answer` | the standard cited synthesis; the schema forces `save: false` |
| `search_memory` | `view` | `gbrain_search@1` |
| `explore_graph` | `view` | `graph_query@1` |
| `find_orphan_pages` | `view` | `orphan_pages@1` |
| `context` | `artifact` | `gbrain_context@1` |
| `record` | `record` | dereference one canonical record by UUID |

Parameters are never duplicated here: view and artifact definitions are compiled
into the MCP JSON Schema. `instructions` carries the prompt-injection stance —
retrieved memory is untrusted reference data, not instructions. Changing the
tool list, a target, or a generated parameter contract requires a new interface
version *and* a package version bump.

`packages/gbrain.yaml` is mostly exact references, with two things worth naming.

**`<derivation>.default`** is how an inline `trigger:` block is referenced. A
derivation listed under `processors` without its trigger loads fine and never
runs — a quiet failure mode worth knowing about.

**`retentions`** is the one operational policy a package may carry:

```yaml
retentions:
  - name: purge_pages
    collection: pages@1
    after_days: 30       # 1–3650, measured from the server-written created_at
    cron: "23 3 * * *"
    max_pages: 25        # 1–100 slots per job
```

Only a slot whose **current active head is a tombstone** is eligible, and
eligibility uses the server's `created_at`, never a client's `occurred_at` — so
backdating a record cannot accelerate deletion. It is worker-only; there is no
retention endpoint.

## 9. How the script reaches each surface

| Prompt command | Call | Declaration | Model calls |
|---|---|---|---|
| `graph [seed]` | `POST /views/graph_query/query` | `views/graph_query.yaml` | 0 |
| `orphans` | `POST /views/orphan_pages/query` | `views/orphan_pages.yaml` | 0 |
| `facts` | `GET /document`, then `GET /records/{id}` | `fact_extraction` → `facts` | 0 |
| `concepts` / `takes` | the same pair | `concept_synthesis` / `consolidate` | 0 to read |
| `atoms` / `patterns` | `GET /timeline`, then `GET /records/{id}` | event collections | 0 to read |
| `search <q>` | `POST /search` with `graph_boost` | `gbrain_search` scope plus `edges` | 0 |
| `answer <q>` | `POST /answer` | `syntheses@2`, well-known scope | 2, or 4 with `rewrite` |
| `remember <text>` | `POST /records`, then poll `/runs` | the `atom_extraction` cascade | 2 per Pipeline |

`remember` is the instructive one. It snapshots the known run IDs for three
processors *before* writing the transcript, then waits for a *new* run of each.
That is how you observe a cascade without a subscription API: the run audit is
the observability surface, and a `succeeded` run with `outputs: []` is a
legitimate result meaning "nothing was justified."

## The idea to take away

A distinctive, graph-shaped memory product can be **entirely a catalog**: keyed
pages and typed edges, deterministic and model-backed Pipelines, named views, an
artifact, an MCP surface, and a retention job — all declared, all bounded, all
cited, adding not one line of graph-specific HTTP surface. Your application
writes pages and transcripts and reads views.

## Next

- Run it: [The gbrain showcase](gbrain-showcase.md)
- Deploy your own: [Authoring a workspace catalog](authoring-definitions.md)
