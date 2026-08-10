---
title: Implementing L0–L3 agent memory on memseek
eyebrow: Design plan
---

# Implementing L0–L3 agent memory on memseek — detailed plan

This plan re-expresses the TencentDB Agent Memory design — the `L0 → L1 → L2 → L3`
hierarchy (raw conversation, atomic memory, scene, persona) plus its parallel Skill
extraction pipeline — as memseek definitions. It reads that design as a specification and
asks one question of each mechanism: **can a workspace author build it in YAML today, with
no change to the engine?**

The answer is *almost entirely yes*, and the reason is that both systems are built on the
same spine: an immutable evidence layer, a revisable derived layer, and a traceability
contract between them. Where memseek differs, it differs by being stricter — citations are
validated against a read receipt rather than trusted, and derived state is superseded
rather than deleted. Four things cannot be expressed in YAML today; they are named exactly
in the [gap tables](#7-gap-analysis).

**Update: the catalog sketched here has since been built and run.** See
[The L0–L3 agent memory example](agent-memory-example.md) for the shipped
`examples/agent_memory_catalog/` and the walkthrough that exercises it. Every gap table below
survived contact with the implementation; that page's *seven things the live runs taught*
records the further constraints that only appeared once real runs went through the engine —
among them that provenance flows from sources into prompts and never from one Task's output
into another's, and that a `foreach` fan-out is capped at five items.

## 1. The one structural insight

The source design's four layers are three distinct storage decisions, not four:

> **L0 and L1 are immutable events. L2 and L3 are keyed heads. Traceability between them
> is citation provenance.**

That is exactly memseek's `mode: event` / `mode: keyed` split, plus the `citations`
member of the record draft vocabulary. Read the two hierarchies side by side:

| Source design | Semantic operation | memseek |
|---|---|---|
| L0 raw conversation | capture evidence | `mode: event`, depth 0, never revised |
| L1 atomic memory | extract atomic claims | `mode: event`, depth 1, `citations` → L0 |
| L2 scene | consolidate a situation | `mode: keyed`, one head per section |
| L3 persona | infer stable patterns | `mode: keyed`, one head per trait slot |
| `source_message_ids` | traceability | `citations`, validated against the Evaluation Basis |
| dedup: store/update/merge/skip | model revision | emission intent + keyed supersession |
| Skill pipeline | procedure extraction | `review: required` emission + Promotion |

Two consequences are worth stating up front, because they remove work rather than adding
it:

- **"Write L0 first, then attempt higher-level extraction"** is not a discipline the author
  must maintain. Public ingest commits the canonical batch and its ready-transition outbox
  effects in one transaction (`src/memseek/records.py`); a derivation cannot observe a
  message that is not durably stored. If L1 extraction fails, the L0 records exist, and the
  pipeline cursor has not advanced.
- **`source_message_ids` is stronger here.** The source design asks the model to return
  message IDs and trusts them. memseek captures the read receipt *before* Tasks execute and
  validates every emitted citation against it, rejecting the whole Candidate Set if the
  model invents a UUID (`src/memseek/derive/candidates.py`). The traceability claim at the
  bottom of the source design — `L3 → L2 → L1 → L0` — is enforced rather than intended.

## 2. Five design moves that avoid core changes

### 2.1 One record type, one declared `kind` field

`emit` declares exactly one `type` per pipeline (`src/memseek/derive/schema.py:356`), so a
single extraction pass cannot emit `persona`, `episodic`, and `instruction` as three record
types. Emit `type: memory` and declare the trichotomy as a filterable content field, the
way `examples/gbrain_catalog/collections/atoms.yaml` already declares `kind`:

```yaml
fields:
  memory_kind: {path: content.memory_kind, type: string, filter: true, project: true}
```

The alternative — three pipelines over the same messages, each extracting one kind — costs
three LLM passes over identical evidence and is not recommended. (The shipped
`contradiction` / `belief_conflict` pair pays that cost only because polarity genuinely
needs two detectors.)

### 2.2 Priority is a content field; the *trigger* reads a score

The source design's `priority` is an LLM-assigned number used for two unrelated jobs:
ranking at recall time, and deciding when downstream synthesis should run. In memseek these
are different mechanisms:

- **Ranking and filtering** — `content.priority` declared with `filter: true, sort: true`.
  A structured view can then answer *"every instruction at priority ≥ 90, sharpest first"*
  with no relevance guessing.
- **Firing a trigger** — `trigger.accumulator` reads a processor score or an annotation
  path, never a content path (`AccumulatorMetric`, `src/memseek/derive/schema.py:152-174`),
  and a derived record draft has no way to carry a score (`RecordDraft` is
  `key`/`text`/`content`/`citations`/`retract`). So either declare an `llm` `score`
  processor named `priority` over the memories collection — which also makes priority
  usable inside `conf/rank_default.yaml` as `[score, priority]` — or use a write trigger
  with a declared-field filter, which needs no second model call:

```yaml
trigger:
  write:
    collections: [memories]
    types: [memory]
    where: {priority: {gte: 90}}   # WriteCondition.where filters declared fields
```

The magic `-1` "strict global instruction" value does not survive a `[0, 100]` scale and
should not be smuggled through one. Model it as what it is: a separate `scope: global`
field, or a `directives` slot on the persona document.

### 2.3 Scene identity: bounded index first, entity per scene second

This is the plan's only real fork, and it is worth stating plainly. A scene name is chosen
at runtime by a model; `emit.keys` is statically declared and capped at 50
(`src/memseek/derive/schema.py:374-375`). Two expressible designs:

**(a) One bounded scene index per agent.** Exactly the shipped
`examples/gbrain_catalog/derivations/consolidate.yaml` pattern: one keyed record
(`key: scene_index`) whose content is an array of ≤ N scenes, each with a title, state,
constraints, and its own citations. Merging, superseding, and dropping scenes all happen
inside one keyed successor, so the operation is a normal Divergence with a normal diff.
Works today, no application changes, and the scene *set* is capped by `maxItems`.

**(b) One entity per scene.** `entity: scene.billing-api-migration`, with the scene's
sections as static keys (`overview`, `status`, `constraints`, `open_questions`, `history`)
— structurally identical to how the shipped catalog gives each skill its own entity. This
scales without a cap, and cold-start is already handled: `trigger.lifecycle.first_record`
fires a pipeline on an entity's first matching record. The cost is that **something must
route** — a pipeline run emits into its own entity, so the application (not YAML) has to
ingest a scene-scoped stub record once it sees a new `scene_name` on an L1 memory. That is
an integration loop, not an engine change, but it is real work and it moves one decision
out of the catalog.

Recommendation: build (a), because it is pure YAML and the recall path is identical; treat
(b) as the growth path once scene count per agent exceeds the index budget. The general
version of this problem — runtime ontology growth — is already analysed in
[Synthesis, differentiation, and error-driven revision](synthesis-and-differentiation.md),
§8, and this plan deliberately does not re-propose it.

### 2.4 Dedup is a three-Task pipeline, and "delete" becomes "supersede"

Sections 3.8 and 3.9 of the source design — embed each candidate, retrieve `topK` similar
existing memories, then have a model choose `store` / `update` / `merge` / `skip` — fit in
one derivation, because a `search` Task accepts `foreach` over a prior Task's output
(`derivations/reflection.yaml` already does this):

```yaml
tasks:
  - id: extracted        # use: llm      → scene_name + atomic claims
  - id: candidates       # use: search   → foreach "{{extracted.memories}}", hybrid, k: 8
  - id: result           # use: llm      → decisions, then emit
```

The four actions map onto emission intent, with one honest asymmetry:

| Source action | memseek |
|---|---|
| `store` | emit a new event into `memories` |
| `skip` | emit nothing for that candidate |
| `merge` | emit the consolidated claim into the keyed index, citing both predecessors |
| `update` | keyed successor at the index slot; `retract: true` to empty it |

What does **not** map is `memoryStore.delete(decision.target_ids)`. A superseded atom stays
queryable at depth 1 forever, and the merged claim lives at the keyed layer. This is the
substrate being deliberately stricter, and it changes one thing about recall: **supersession
must be resolved by keyed heads, not by edges.** A `duplicate_of` edge in `relations` can
record the judgment for audit, but `where` filters only declared fields on the record being
searched, so no view can exclude "atoms that some edge superseded" (see
[gap 3](#requires-extending-memseek)). Read the index; drill to the atoms.

### 2.5 First/incremental persona modes collapse into one definition

The source design carries an explicit `mode: "first" | "incremental"` flag and a checkpoint
object. In memseek a `current` source is a guarded read that renders empty when no head
exists, so one prompt covers both cases — `derivations/profile.yaml` is already written
this way. The five persona-trigger conditions of §5.1 map onto trigger stanzas one-for-one:

| Source trigger reason | memseek trigger |
|---|---|
| explicit request | manual enqueue (`POST /processors/{name}/run`) or `read: true` |
| cold start (scenes exist, no persona) | `lifecycle: {first_record: true}` |
| enough new memories accumulated | `accumulator: {metric: priority, threshold: N}` |
| important scenes changed | `changed: {collections: [scenes], transitions: [added, changed]}` |
| checkpoint or time threshold | `cron: {expr: "...", entities: dirty}` |

`checkpoint.memories_since_last_persona` has no author-visible counter — cursors and
receipts are engine-owned — and `accumulator` is the substitute. Note `cooldown_s` and
`debounce_s` exist for the "do not regenerate after every scene update" requirement.

## 3. Catalog sketch: `examples/agent_memory_catalog/`

```text
examples/agent_memory_catalog/
├── collections/  messages.yaml  memories.yaml  scenes.yaml  persona.yaml
├── conf/         models.yaml  processors.yaml  search_profiles.yaml  rank_default.yaml
├── derivations/  l1_extract.yaml  scene_synthesis.yaml  persona.yaml  skill_extract.yaml
├── views/        memory_recall.yaml  standing_instructions.yaml
│                 scene_navigation.yaml  session_window.yaml
├── artifacts/    agent_context.yaml  maintained_skill.yaml
├── mcp/          agent_memory.yaml
└── packages/     agent_memory.yaml
```

Entity naming follows the shipped convention of one explicit dotted identifier per scope:
`agent.alice` for the memory owner, `skill.diagnose-duplicate-payment-charges` for a
maintained skill, and `scene.billing-api-migration` only under design (b) above.

### L0 — `collections/messages.yaml`

One record per message. `sessionKey` becomes the entity (the memory scope); `sessionId`,
`role`, and message order become declared fields, so `session_window` can return an exact
ordered slice for the "restore omitted context" operation.

```yaml
collections:
  - name: messages
    version: 1
    active: true
    mode: event
    schema:
      type: object
      required: [text, role, session_id, ordinal]
      properties:
        text: {type: string, minLength: 1}
        role: {type: string, enum: [user, assistant, tool]}
        session_id: {type: string, minLength: 1, maxLength: 128}
        ordinal: {type: integer, minimum: 0}
        tool_name: {type: string, maxLength: 64}
      additionalProperties: false
    fields:
      role: {path: content.role, type: string, filter: true, project: true}
      session_id: {path: content.session_id, type: string, filter: true, project: true}
      ordinal: {path: content.ordinal, type: integer, filter: true, sort: true, project: true}
    required_processors: [embedding_v1]
    search_profile: pg_default
```

The client supplies `occurred_at` per message, which matters: `derivations/profile.yaml`
already instructs the model to use `occurred_at` for domain chronology so a late-ingested
old message cannot override newer evidence. The source design has no equivalent guard.

### L1 — `collections/memories.yaml`

```yaml
fields:
  memory_kind: {path: content.memory_kind, type: string, filter: true, project: true}
  priority: {path: content.priority, type: number, filter: true, sort: true, project: true}
  scene_name: {path: content.scene_name, type: string, filter: true, project: true}
```

`memory_kind` is `persona | episodic | instruction`; `priority` is `0–100`; `scene_name`
carries the segmentation decision of §3.3. The episodic timing metadata of §3.4
(`activity_start_time` / `activity_end_time`) are ordinary content properties, and can be
declared `datetime` fields if a view needs to range over them. Everything the source design
calls an "operational field" added at persistence time — `id`, `timestamps`, `createdAt`,
`updatedAt`, `sessionKey` — is already a column on the canonical record.

### L1 — `derivations/l1_extract.yaml` (shape only)

```yaml
name: l1_extract
trigger:
  write: {collections: [messages], types: [message], statuses: [active]}
  debounce_s: 30
sources:
  new_messages:                 # the unprocessed suffix after the pipeline cursor
    kind: changes
    collections: [messages]
    types: [message]
    max_records: 40
    max_tokens: 20000
    allow_empty: false
  background:                   # §3.2 "background messages", a bounded prior slice
    kind: view
    view: session_window@1
    params: {entity: "{{entity}}", recent: 12}
    max_tokens: 6000
  current_index:                # §3.8 existing memories, for merge decisions
    kind: current
    collections: [memories_index]
    keys: [active_memories]
    max_records: 1
    max_tokens: 8000
tasks:
  - id: extracted    # llm    → {scene_name, memories:[{text, memory_kind, priority, citations}]}
  - id: candidates   # search → foreach "{{extracted.memories}}", conflict recall
  - id: result       # llm    → dedup decisions → record drafts
emit:
  from: "{{result.records}}"
  collection: memories
  type: memory
```

Three notes on fidelity. The `changes` source *is* `maxMessagesPerExtraction` plus the
processed-watermark bookkeeping, and its cursor only advances on a committed run.
`previousSceneName` is a `current` read, not a parameter the caller threads through. And
`limits.max_llm_calls` bounds the run in a way the source design's `maxIterations: 16`
does not — an extraction cannot quietly cost eight model calls.

### L2 and L3

`scenes` and `persona` are keyed collections. Under design (a), `scenes` holds one
`scene_index` record per agent whose content is the array of scenes with per-scene
citations, exactly like `takes` in the gbrain catalog. `persona` declares the L3 sections as
static keys — `archetype`, `role`, `technical_preferences`, `interaction_protocol`,
`decision_pattern` — with `complete: true` if you want every render to be a full document
rather than a patch. The Markdown shape of §4.2 and §5.3 is an **artifact template**
concern, not a storage concern: keep the sections as separate heads so they diff, and let
`artifacts/agent_context.yaml` assemble the document.

### Views — the recall ladder

| View | Mode | Answers |
|---|---|---|
| `memory_recall` | multi-source hybrid + RRF over `memories`, `scenes`, `persona` | §7 "what is relevant to this request?" |
| `standing_instructions` | `structured`, `where: {memory_kind: {eq: instruction}, priority: {gte: 90}}`, `order_by: priority desc` | "every hard constraint, no relevance guessing" |
| `scene_navigation` | `structured` over `scenes`, ordered by recency | §4.4 the compact scene map |
| `session_window` | `structured` over `messages`, `where: {session_id: ...}`, `order_by: ordinal asc` | §2 "what was literally said" |

`standing_instructions` is the sharpest single argument for this substrate over the source
design: a rule at priority 100 should never be subject to vector recall at all, and
`mode: structured` forbids `rank` outright.

### The recall flow of §7, as one artifact

`artifacts/agent_context.yaml` is `kind: prompt, lifecycle: live` with four blocks —
`persona` (document), `scenes` (view), `instructions` (view), `memory` (view) — each with
its own `max_tokens` budget, plus a `snapshot` stanza. The source design's L3→L2→L1→L0
walk becomes: render the artifact for the prompt, then drill through `citations` via
`GET /records/{id}` and `GET /document/history` when a claim needs to be audited back to
the message that produced it. `GET /context` does the same under one token budget.

## 4. The Skill pipeline

The source design's Skill pipeline (§8–§12) is a tool-using review agent: it reads a
truncated transcript, lists recent skills, and calls `skill_create` / `skill_update` /
`skill_patch` / `skill_files_write` for up to 16 iterations. memseek already ships the
declarative equivalent, and the mapping is close enough that most of this phase is
configuration rather than authoring:

| Source concept | memseek |
|---|---|
| execution trace as input | `transcripts` / `outcomes` records, cited |
| head-tail transcript truncation | `truncate_middle` (`src/memseek/render.py:37-49`), already applied at render |
| "recently touched skills" in the prompt | a `structured` view over `skills`, ordered by recency, as a `view` source |
| no-op / create / update / patch | emit nothing / new entity / keyed successor / partial keyed update |
| `SKILL.md` sections | static keys: `triggers`, `procedure`, `validation`, `escalation` |
| trigger conditions *and* exclusions | two required sections, both cited — the negative boundary of §9.3 becomes a slot the model must fill |
| validation criteria | a `validation` key, `complete: true` so it can never be silently dropped |
| `version: 1.0.0` → v2 → v3 | keyed successors with receipts; version history is native and diffable |
| review before it takes effect | `review: required` + `POST /promote`, as `derivations/skill.yaml` already does |
| `resources/*.sql`, `*.ts` | one keyed slot per resource; an arbitrary file tree has no equivalent |

Two things do not survive the translation, and both are choices rather than oversights.
There is no agentic loop: a Task cannot write canonical state, so "the extractor behaves
like a controlled editor of the Skill repository" becomes "the extractor proposes a complete
candidate and a human promotes it." And a skill's identity is an entity, so *creating* a new
skill means the application chooses `skill.<slug>` and ingests the first evidence record
under it — the same routing requirement as scene design (b).

In exchange, memseek adds the loop the source design lacks entirely: `artifacts/skill.yaml`
declares a Learning Target, a render registers an Artifact Use, a thumbs-down or task
failure arrives as a `learning_signal` naming the exact promoted heads that were in force,
and that signal is ordinary evidence for the next candidate. §13's claim that L0–L3 "support"
skill extraction is, here, a wired path rather than a diagram.

## 5. What the source design does not have

Worth recording, because these are the properties that justify the port rather than a direct
implementation of the original:

- **Erasure.** `POST /erase` walks the provenance closure and fences index deletes. Deleting
  an L0 conversation in the source design leaves every L1 claim derived from it standing.
- **Stale-citation repair.** A `stale_citations` source kind exists specifically for
  rebuilding derived state whose evidence was erased or superseded.
- **Audited runs.** Every derivation run persists its receipt, its Divergence, and its token
  spend (`GET /runs`). The source design's `applySceneOperations` leaves no artifact.
- **Bounded cost by declaration.** `limits` is per-pipeline and enforced; head-tail
  truncation, `max_records`, and `max_tokens` are contract, not convention.
- **Schema evolution.** A collection contract hash pins how stored rows are read, so
  changing the L1 shape does not strand existing memories.

## 6. What this costs in files

| Phase | Files | Blocked on |
|---|---|---|
| 0 — decide scene identity | none | the one decision in §2.3 |
| 1 — L0 capture | `collections/messages.yaml`, `conf/*`, an example ingest script | — |
| 2 — L1 extraction + recall | `collections/memories.yaml`, `derivations/l1_extract.yaml`, 2 views | phase 1 |
| 3 — L2 scenes | `collections/scenes.yaml`, `derivations/scene_synthesis.yaml`, `views/scene_navigation.yaml` | phase 2, decision 0 |
| 4 — L3 persona | `collections/persona.yaml`, `derivations/persona.yaml` | phase 3 |
| 5 — skills | `derivations/skill_extract.yaml` (adapt shipped `skill`), extra keyed slots | phase 1 |
| 6 — surface | `artifacts/agent_context.yaml`, `mcp/`, `packages/` | phases 2–5 |

Verification at each phase is the existing loop: `uv run memseek catalog-check --workspace W
--dir examples/agent_memory_catalog --package agent_memory@0.1.0` reports what publishing
would do before it does it, and `make check` plus a smoke script in the style of
`examples/gbrain_showcase.py` exercises the pipeline end to end against the deterministic
fake provider.

## 7. Gap analysis

### Works today, no engine change

- L0 capture with role, session, ordering, timestamps, and tool records; write-L0-first
  ordering guaranteed transactionally
- L1 atomic claims with a three-way `kind`, numeric priority, scene segmentation, and
  preserved uncertainty (a prompt constraint, as in the shipped catalog)
- `source_message_ids` → `citations`, validated against the read receipt rather than trusted
- Dedup candidate retrieval: per-candidate `topK` with BM25 + vector + RRF, inside the same
  bounded run
- `store` / `skip` / `merge` / `update` as emission intent; `retract` for an intentionally
  empty slot
- L2 scene documents as independently keyed blocks with per-scene citations; any index is
  navigation metadata, not the L2 data model
- L3 persona with first *and* incremental modes in one definition; all five trigger
  conditions declarable; `cooldown_s` / `debounce_s` for regeneration control
- The full recall ladder L3→L2→L1→L0, under one token budget via `GET /context`
- Skill artifacts with trigger conditions, exclusions, procedure, validation, escalation;
  native version history; explicit review gate; "recently touched skills" as a view
- Head-tail transcript truncation
- The storage split (append-only durable history + retrieval index) — PostgreSQL plus
  pgvector or Turbopuffer, with reindex planning

### Works today, but differs

| Source design | Here | Why |
|---|---|---|
| `delete(target_ids)` on merge | superseded atoms stay queryable | events have no successor; the merged claim lives at the keyed layer |
| priority is one number | content field for ranking, processor score for accumulation | `RecordDraft` carries no scores; accumulators read scores/annotations |
| `priority: -1` for global instructions | a separate field or persona slot | a magic value inside a `[0, 100]` scale is not author-legible |
| three memory types as three types | one `type: memory` + `memory_kind` field | `emit` declares one static type |
| individual scene files | up to 15 bounded dynamic keyed blocks | preserves independent update and merge history without an application-created entity |
| Skill Review Agent with mutation tools | bounded pipeline + explicit Promotion | Tasks cannot write canonical state |
| `resources/` file tree | one keyed slot per resource | records, not files |
| background + new messages in one window | a `view` source plus the `changes` source | one driving source per pipeline |
| `checkpoint.memories_since_last_persona` | `accumulator` threshold | cursors and receipts are engine-owned |
| one extractor that also dedups | two model passes: read, then decide | one prompt asked to do both collapses the conversation into a summary |
| `topK` retrieval per candidate | up to five recall queries covering the candidates | `MAX_FOREACH_ITEMS = 5` bounds a fan-out |

### Requires extending memseek

1. ~~**Dynamic keyed slots.**~~ **Fixed for bounded collections.** `dynamic_keys: true`
   with `max_active_keys` creates independently named keyed blocks while capturing every
   current head before a run and enforcing the live-block limit at commit time. The example
   uses this for its 15-scene L2 collection.
2. **Cross-entity emission.** A run emits into its own entity, so routing an L1 memory into
   a `scene.*` or `skill.*` scope requires an application write. No engine change is needed
   to *work*, but scene/skill creation cannot live entirely in the catalog.
3. **Exclusion by relation.** `where` filters declared fields on the record being searched,
   so "exclude memories carrying a standing `superseded_by` edge" is not expressible. Any
   supersession that must affect *retrieval* has to be expressed as a keyed head, not an
   edge.
4. ~~**Cited synthesis over author-named collections.**~~ **Fixed.** `POST /answer` used to
   resolve a hardcoded collection vocabulary, so a catalog naming its collections for its own
   domain got `422 answer_unavailable`. Collections now declare
   [`answerable: true`](collections.md#answerable-default-false) and the endpoint takes an
   `entities` scope, with the reasoning recorded in the repository's `DECISIONS.md`.
5. **Accumulating a content value.** `trigger.accumulator` cannot sum or count a content
   path. Firing on *arrival* of a high-priority record works today via
   `trigger.write.where`; firing on *accumulated content priority* requires a score
   processor, and therefore an extra model call per record.

### Out of scope by architecture

- Storing tool calls, model responses, or trace spans as first-class memory. An Artifact Use
  deliberately has no column able to hold a render, a request parameter, a model response, or
  a span; `execution_refs` on a learning signal is informational and no processor may depend
  on fetching it.
- A model deleting canonical rows. Erasure is an explicit, audited, bounded operation.
- Iterative agentic authoring of the memory store itself.

## 8. The scene representation

**Resolved.** `examples/agent_memory_catalog/` keeps one keyed record per scene block in the
agent’s own entity. `dynamic_keys: true` is bounded to 15 live blocks and captures every current
head before the model runs, so an update or merge is compare-and-set safe. This follows the
upstream representation directly: a scene index is navigation metadata and never the record that
contains all scene content. It also preserves prompt budget, because each scene is rendered as
its own compact record.
