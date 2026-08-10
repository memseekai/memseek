---
title: Schema evolution plan
eyebrow: Design plan
---

# Making definition change a first-class operation

## Implementation status

Every phase below is implemented. This document remains the design record: it
states the problem each phase solved, the shape chosen, and the decisions locked
along the way. For how to *use* any of it, see
[Changing definitions](changing-definitions.md).

- [x] **Phase 1 — publish preflight.** `POST /catalog?dry_run=true`,
  `GET /catalog/compatibility`, `memseek catalog-check`, `catalog.check()` in the
  SDK, and the same report on a refused publish under `compatibility`. The
  classifier lives in `definitions/compat.py` and is the single authority the
  preflight, the publish gate, and the hash-rewrite command all share.
- [x] **Phase 2 — collection identity split.** `contract_hash` (mode, schema,
  `text_projection`, `fields`, `required_processors`) is what records persist;
  `active`, `optional_processors`, `search_profile`, and `allowed_search_profiles`
  moved out. A closed allowlist of provably additive schema edits publishes in
  place, with bounded verification against real rows where subsumption cannot be
  shown structurally. `memseek migrate-collection-hashes` moves pre-split
  workspaces forward; a publish heals them automatically.
- [x] **Phase 3 — backfill lane.** The `annotation_backfill` job kind plus a
  durable `backfill` handle: `POST /backfill`, `GET /backfill`,
  `GET /backfill/{id}`, `POST /backfill/{id}/cancel`, `memseek backfill`, and
  `client.backfill.*`. Write-once, budgeted, resumable, cancellable, one live
  backfill per target.
- [x] **Phase 4 — annotation supersession.** `supersedes:` on a processor, with
  loader-validated linear same-kind acyclic chains. Declared fields, the canonical
  recheck, and index projection all prefer the newest annotation present. A field
  repointed along a chain is an additive publish.
- [x] **Phase 5 — migration derivations.** The deterministic `map_records` Task
  (`keep`/`set`/`drop`/`carry_key`) turns a snapshot pipeline into a
  copy-forward-with-lineage migration. No new persistence concept was needed.
- [x] **Phase 6 — online re-embed.** The `record_embedding` staging table,
  `memseek reembed --space` / `--cutover`, coverage-gated promotion, and
  reversibility via the outgoing space staged on the way out.
- [x] **Phase 7 — rebind and prune.** `POST /derivations/{name}/rebind` with
  `reset`/`carry` and an audit row, plus `memseek catalog-prune` reporting
  reference counts per definition.
## The problem this solved

Memseek's *safety* story was already complete: it reliably refused to reinterpret
a stored row. Its *evolution* story was not. Every non-trivial change was a manual
act of catalog archaeology, and four of them had no path at all.

Closing that gap without weakening the guarantee that produced it is what the
phases below do.

## What was already right, and stays

These were not up for renegotiation; every phase preserves them.

1. **Records are immutable.** No migration rewrites content, annotations, or
   provenance in place. Moving data forward means *emitting* new rows with
   lineage, not mutating old ones.
2. **A stored row is never reinterpreted.** The identity check that produces
   `409 catalog_incompatible` is the product's most valuable refusal.
3. **Annotation names are write-once semantic identities.** A changed prompt
   yields a new name, never a mutated value.
4. **Nothing partial is ever installed.** Publishing is atomic; validation is
   total.
5. **Canonical PostgreSQL is the source of truth.** External indexes are
   derived and rebuildable.

## The gaps, ranked by cost to users

Stated as they were before this work, because each phase is named by the gap it
closes.

| # | Gap | Cost at the time |
| --- | --- | --- |
| 1 | **Every** collection edit except `active:` is breaking — including provably additive ones like a new optional property, a new optional processor, or a wider search-profile allowlist | Version churn for changes that reinterpret nothing, and a permanent multi-version catalog |
| 2 | There is **no** way to apply new enrichment to records that already exist. The optional-processor sweep is catch-up within one contract, not backfill | A new scorer or extractor can only ever reach rows written after it, so improving enrichment means abandoning history |
| 3 | A publish gives no preview — you learn what breaks by breaking it | Every schema change is trial-and-error against production data |
| 4 | A changed processor prompt diverges silently and permanently | Two records' `importance` can mean different things with nothing marking the boundary |
| 5 | No way to move rows into a new version | Old and new versions live forever, and every consumer has to know both |
| 6 | No re-embed path | The embedding model is effectively frozen for the life of a workspace |
| 7 | A `changes` cursor cannot be rebound | Widening a derivation's source means abandoning its name |
| 8 | Nothing tells you when an old definition is finally safe to delete | Catalogs only ever grow |

Gaps 1 and 2 compounded: because binding a processor required a version bump, and
a new version starts empty, "improve enrichment" and "keep your history" were
mutually exclusive. That pair was the product problem worth solving first, and it
is what phases 2 and 3 targeted.

Phases are ordered so each one is independently shippable and useful, and so the
cheapest, highest-leverage work lands first.

---

## Phase 1 — Publish preflight

**Closes gaps 3 and 4. Size: S. No schema change. No behavior change.**

A read-only compatibility report, available before anything is installed.

```
POST /catalog?dry_run=true      → 200 with a change report, never installs
GET  /catalog/compatibility     → the same report for the currently installed package
```

```bash
uv run memseek catalog-check --workspace acme --dir ./catalog --package acme@1.4.0
```

The report classifies every definition in the incoming catalog against what the
workspace actually stores:

```json
{
  "verdict": "reinterpreting",
  "changes": [
    {
      "family": "collection",
      "name": "customer_events",
      "version": 1,
      "class": "reinterpreting",
      "stored_hash": "9f2c…",
      "incoming_hash": "41ab…",
      "differing_fields": ["schema", "fields"],
      "detail": "schema.required gained ['channel']",
      "required_action": "add customer_events version 2 with this change and keep version 1 in the package"
    },
    {
      "family": "processor",
      "name": "sentiment_v1",
      "class": "reinterpreting",
      "detail": "annotations already written keep their value and config hash; they are never recomputed",
      "required_action": "publish under a new processor name"
    },
    {
      "family": "collection",
      "name": "pages",
      "version": 3,
      "class": "invisible",
      "differing_fields": ["active"]
    }
  ]
}
```

Three pieces of work:

1. **A classifier** — `src/memseek/definitions/compat.py`, pure and unit
   testable: given two compiled catalogs, return per-definition
   `invisible | additive | reinterpreting` plus the differing field paths. The
   field-level diff comes from the same normalized `_dump` the hash is computed
   over, so classification and enforcement can never disagree.
2. **A counter** — the row and annotation counts, from the queries the publish
   gate already runs.
3. **Reuse at the gate** — `409 catalog_incompatible` carries the same report as
   its payload instead of one opaque sentence. This alone converts the most
   common failure from a puzzle into an instruction.

**Done when** a publish that would fail can be predicted exactly, with row
counts and a named action per definition, without touching the workspace; and
when the 409 body is the same structure.

Phase 1 also gives gap 4 its only realistic answer short of a backfill lane: a
changed processor prompt is invisible at runtime, but the preflight can name it,
count the annotated rows that will keep the old vintage, and refuse to let it pass
unremarked.

## Phase 2 — Split the collection identity

**Closes gap 1, and unblocks gap 2. Size: M. One data migration.**

Today `collection_hash` covers everything in the collection block, so edits that
reinterpret nothing still force a version bump. Two components fix that.

### 2a. Bindings leave the persisted identity

**Record contract** — what determines how a stored row is *read*, and therefore
stays in the persisted identity:

`name`, `version`, `mode`, `schema`, `text_projection`, `fields`,
`required_processors`

**Bindings** — what determines what *else happens* to a row, and moves out of
the persisted identity:

`optional_processors`, `search_profile`, `allowed_search_profiles`

`required_processors` deliberately stays in the contract: it determines
readiness, and readiness gates visibility. `optional_processors` does not gate
anything, and `search_profile` only routes — projection already re-resolves
routing from the active collection on every attempt.

### 2b. Provably additive schema edits are accepted in place

The schema stays in the record contract, because it determines validity. But some
schema edits cannot possibly reinterpret a stored row, and the publish gate can
prove it structurally rather than trusting the author.

Accept an in-place edit to `(name, version)` when the incoming definition differs
from the stored one *only* by changes on this allowlist:

- a new property in `properties` that is **not** added to `required`;
- a new entry in `fields` whose path resolves to such a property;
- `additionalProperties` going from `false` to `true`.

The predicate to enforce is exactly: *every value that validated under the old
schema also validates under the new one.* Each item above satisfies it by
construction, which is why the list is short and closed — anything not on it
(a new `required` entry, a narrowed type, a tightened `enum`, a changed field
`path` or `type`, a removed property) keeps requiring a new version.

Accepting the edit means rewriting the stored `collection_hash` for that version's
rows, using the same batched command as 2a. A newly declared field is
`null`/absent on rows written before it — the preflight reports how many, and
Phase 3 is how you fill them.

This is the difference between "add a filterable `channel` field" being a
migration project and being a publish.

### Migration mechanics

The loader can compute both the old full hash and the new contract hash from the
same definition, which makes the rewrite deterministic:

```bash
uv run memseek migrate-collection-hashes --workspace acme   # omit for every workspace
```

For each stored `(collection, version, stored_hash)`: if `stored_hash` equals the
old-algorithm hash of a definition present in the catalog, rewrite it to that
definition's contract hash. Anything that does not match is reported and left
untouched — a workspace already drifted must be fixed before it can be migrated.
Ship it as an Alembic revision plus this idempotent, resumable, batched command,
guarded by the workspace lock.

**Done when** adding an optional processor, switching search profile, adding an
optional schema property, and declaring a field over it are all `additive`
changes on a collection with rows; `migrate collection-hashes` is idempotent and
resumable; the additive predicate has adversarial tests for every near-miss on the
allowlist; and the preflight classifier reports the new classes.

## Phase 3 — Backfill as a job lane

**Closes gap 2. Size: M. One new job kind.**

Promote backfill from an implicit sweep to a declared, fenced, observable
operation.

```
POST /backfill                    {"collection": ..., "version": ..., "processor": ..., "max_rows": ...}
GET  /backfill                    every recent backfill, newest first
GET  /backfill/{id}               state, cursor, and counters
POST /backfill/{id}/cancel        stop at the next batch boundary
```

```bash
uv run memseek backfill --workspace acme --collection customer_events \
    --version 1 --processor sentiment_v2
```

- New job kind `annotation_backfill`, alongside `derive`, `cron_scan`,
  `retention_purge`, `index_upsert`, `index_delete`. It carries
  `(collection, version, processor)` and a `seq` cursor, so it is resumable,
  claim-fenced, and cancellable — the same lease discipline as every other lane.
- **It can target any version, including a frozen one.** An explicit request
  names the version, so no catalog edit and no hash change is involved. This is
  the whole point: after Phase 2 the binding is not part of the identity, and
  after Phase 3 you do not even need the binding to reach old rows.
- **It can also fill a newly declared field**, by re-projecting rows whose
  content already holds the value under `additionalProperties: true` — the
  common case after a Phase 2b publish.
- Rate and cost bounds are explicit: `--max-rows`, `--max-cost`, and the
  existing per-pass batch limits. A backfill over a million rows is a budgeted
  operation, not a surprise invoice.
- The existing presence-only sweep stays exactly as it is for newly added
  optional processors. This adds a targeted path; it removes nothing.

**Done when** a new processor can be applied to every row of any collection
version — active or frozen — with progress, cost, cancellation, and a durable
audit record; and a field declared after ingestion can be populated for rows that
already carry the value.

## Phase 4 — Annotation supersession

**Closes gap 4 properly, and the readable half of gap 2. Size: S. No schema
change.**

Backfilling `sentiment_v2` leaves rows carrying `v1`, rows carrying `v2`, and
rows carrying both. Immutability is right; making every reader handle the
vintage boundary is not.

Add one declaration:

```yaml
processors:
  - name: sentiment_v2
    supersedes: sentiment_v1
```

`supersedes` is metadata about *reading*, never about writing. Both annotations
remain on the row, untouched and separately auditable. What it changes:

- a declared field may resolve through a supersession chain, preferring the
  newest present annotation;
- rendering and search projection follow the same preference;
- the preflight reports how many rows still hold only the superseded value —
  which is exactly the backfill progress metric.

Validation: no cycles, compatible output schemas, one chain per name.

**Done when** an author can change a processor's behavior and have readers
prefer the new value without every consumer learning both names.

## Phase 5 — Migration derivations

**Closes gap 5. Size: L. No new persistence concepts.**

The answer to "can old rows move into the new version" is: not by mutation, but
by *copy-forward with lineage* — which is what derivations already do.

```yaml
derivations:
  - name: customer_events_v1_to_v2
    kind: migration
    source:
      mode: snapshot
      collections: [customer_events]
      collection_versions: {customer_events: [1]}
    tasks:
      - use: memseek.tasks.map_content       # typed, deterministic, installed
        config:
          mapping:
            channel: {from: content.source, default: note}
    emit:
      collection: customer_events@2
      preserve: [entity, key, type, occurred_at]
```

Properties it inherits for free, which is the reason to build it this way:

- every emitted row carries `derived_from` back to its version-1 original, so
  the migration is auditable and erasable through the existing closure;
- it is bounded, resumable, and claim-fenced like any derivation;
- `occurred_at` is preserved, so history keeps its shape and ordering;
- a *reviewed* migration is the default: propose, inspect divergence, promote
  atomically. Deterministic mappings can opt into direct emission.

Two decisions to lock before building:

- **Retire or retain the source rows?** Recommend retain by default (the
  original is history) with an explicit opt-in `retire_source: tombstone` for
  keyed collections, so keyed reads converge on the new version instead of
  seeing two heads.
- **Deterministic-only, or LLM Tasks allowed?** Recommend allowing the full Task
  set — a migration that needs to re-extract a field from text is a real case —
  but defaulting `kind: migration` to reviewed emission so a model never
  silently rewrites a corpus.

**Done when** a workspace can move a whole collection to a new version with
provenance, review, bounded cost, and no mutation — and then legitimately retire
the old version.

## Phase 6 — Online re-embed

**Closes gap 6. Size: L. Real schema change.**

The blocker is structural: one `embedding` column and one active
`EMBEDDING_SPACE_ID` means two models can never coexist, so migration is
all-or-nothing and there is no path back.

Options, with a recommendation:

| Option | Verdict |
| --- | --- |
| Second nullable embedding column | Cheapest, but caps the design at exactly two spaces and bakes the limit into DDL |
| `record_embedding(record_id, space, embedding)` side table | **Recommended.** Makes multi-space explicit, allows one-column-per-space to disappear, keeps `record` narrow, and is the only shape that supports overlap-then-cut-over |
| Accept downtime and re-embed in place | Not viable for a hosted, multi-tenant service |

With the side table:

```bash
uv run memseek reembed --workspace acme --space default-v2 --model text-embedding-3-large
```

- writes into the new space while the old space keeps serving reads;
- a per-space `ready` watermark, so cut-over happens when coverage is complete
  rather than on a guess;
- vector search reads the active space and can be configured to union both
  during transition, accepting the recall cost knowingly;
- the old space is droppable afterwards, reclaiming the storage.

This phase also relaxes the fixed `EMBED_DIM = 1536`, since dimension becomes a
property of a space rather than of the table.

**Done when** a workspace can change embedding models with no loss of vector
recall at any point in the transition, and can roll back before cut-over.

## Phase 7 — Cursor rebinding and definition retirement

**Closes gaps 7 and 8. Size: S each.**

**Cursor rebinding.** Widening a `changes` source was indistinguishable
from corrupting it. Make the intent declarable:

```
POST /derivations/{name}/rebind  {"entity": "...", "policy": "reset" | "carry"}
```

`reset` restarts the cursor from zero (correct when the widened scope must be
fully re-read); `carry` keeps the watermark and records the new source hash
(correct when the widening only adds rows that will arrive in future). Both write
an audit row naming the old and new source hashes. The default remains the
refusal — you have to say which one you mean.

**Retirement.** Give the catalog a way to shrink:

```bash
uv run memseek catalog prune --workspace acme --dry-run
```

Reports, per definition, whether anything still references it: rows for a
collection version, annotations for a processor, cursors and runs for a
derivation, uses and snapshots for an artifact. A definition with zero references
is safe to delete, and the command says so with the counts that prove it.
Optionally accept `retired: true` in YAML as an author's assertion, validated
against the same counts at publish time so the assertion cannot be wrong.

**Done when** an operator can widen a derivation source deliberately, and can
delete an old collection version with proof that nothing references it.

---

## Sequencing and dependencies

```
Phase 1  Preflight            ── independent, ship first
Phase 2  Identity split       ── needs Phase 1's classifier
Phase 3  Backfill lane        ── needs Phase 2
Phase 4  Supersession         ── needs Phase 3 to be useful
Phase 5  Migration derivations ── needs Phase 1; independent of 3–4
Phase 6  Online re-embed      ── independent; largest schema change
Phase 7  Rebind + prune       ── needs Phase 1's reference counting
```

Phases 1–4 are one coherent release and the reason to do this at all: *additive
changes stop being migrations, you can see what any change costs, and you can
apply new enrichment to old data.* Together they turn the two compounding gaps
into ordinary operations.

Phase 5 is what lets catalogs stop growing. Phase 6 was separable, and is the one
item whose cost was dominated by DDL rather than design.

## Decisions locked

These changed the shape of the work, so they were settled before implementation
rather than discovered mid-way. Each is the recommendation that shipped.

1. **Does `required_processors` stay in the record contract?** Recommend yes —
   it gates readiness, and readiness gates visibility.
2. **Does `search_profile` leave the identity for published packages too?**
   Recommend yes. Projection already re-resolves routing per attempt, so keeping
   it in the identity buys nothing and costs a version bump.
3. **Is the hash rewrite mandatory or opt-in?** Recommend mandatory, at a known
   revision, with the command idempotent and resumable — two hash algorithms
   coexisting indefinitely would be its own compatibility problem.
4. **Is the additive-schema allowlist closed?** Recommend yes, and kept
   deliberately short. The value is that acceptance is *provable*, so the
   temptation to add "probably fine" cases (a widened `enum`, a relaxed
   `maxLength`) should be resisted until someone asks for them with a real
   workload. Each addition needs its own subsumption argument.
5. **Does an accepted additive publish rewrite hashes eagerly or lazily?**
   Recommend eagerly, inside the publish transaction for small row counts and via
   a fenced background job above a threshold, with the publish blocked until it
   completes. Lazy rewriting would mean two identities live for the same version.
6. **Is backfill metered per workspace?** Recommend yes, with an explicit
   `--max-cost`, since a backfill is the easiest way for an author to spend real
   money by accident.

## Documentation deliverables

- [Changing definitions](changing-definitions.md) — rewritten around the shipped
  behavior: how to make each change, what the preflight reports, and the short
  list that still needs a new version by design.
- [Collections](collections.md) — the versioning checklist gains the record
  contract / bindings distinction after Phase 2.
- [Processors](processors.md) — `supersedes` after Phase 4.
- [Operations](operations.md) — preflight, backfill, reembed, prune commands and
  their troubleshooting rows.
- [Packages](packages.md) — what a preflight report means for publish workflow.
- `DECISIONS.md` — one entry per phase, recording the locked decisions above and
  the rationale, in the existing milestone format.
