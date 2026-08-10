---
title: Selective context disclosure & the agent filesystem
eyebrow: Design plan
---

# Selective context disclosure and an agent-facing filesystem

This is a planning document, not shipped behaviour. It answers two questions
against the current codebase (v3.2 substrate, milestones M0–M8 plus the
2026-07 amendments in `DECISIONS.md`):

1. **Selective context disclosure** — how to expose a *controlled, principal-
   specific subset* of a workspace's memory to an agent, enforced by the
   server rather than merely requested by the caller.
2. **An agent filesystem** — how to present that memory to an agent through a
   filesystem mental model (paths, `ls`, `read`, `stat`, history, `write`,
   `rm`), mapping onto the substrate's `(entity, collection, key)` namespace.

The short version: **most of the *filesystem* already exists as read
endpoints and needs only a translation layer; almost none of the *enforced
disclosure* exists yet and is the real work.** The two features compose into
one model — *the filesystem is the projection; disclosure is the policy that
decides which projection a principal sees.*

---

## 1. Where the codebase stands today

### 1.1 The only trust boundary is the workspace

Authentication resolves a bearer key to a single workspace string and nothing
finer — see `authenticate_api_key()` in
`src/memseek/auth.py`. There is no sub-workspace
principal, no scoped key, no per-collection or per-entity grant. A confirming
grep across `src/`, `docs/`, and `spec/` finds no `principal`, `scoped-key`,
`grant`, `clearance`, `disclosure`, `redact`, `sensitivity`, or `audience`
concept anywhere (only unrelated log-field redaction in
`src/memseek/logging.py`).

**Consequence:** any holder of the workspace key can read everything in the
workspace. Everything below is about carving *inside* that boundary.

### 1.2 `scope` is query-shaping, not authorization

Every read takes a caller-supplied scope and turns it into SQL predicates —
`scope_conditions()` in `src/memseek/search/scope.py`,
`ContextQuery` in `src/memseek/views/context.py`,
the delta scope hash, timeline/document filters. These *narrow* a query but the
caller chooses them freely and can always widen them. The only involuntary
restriction is that `_system` records are hidden by default (spec §6.1;
`row.collection <> '_system'` in `scope_conditions`).

**Consequence:** scope is a convenience, not a fence. Selective disclosure
needs a *ceiling* scope the caller cannot widen.

### 1.3 Disclosure-adjacent primitives that already work for us

- **Named views** (`views/agent_memory.yaml`,
  `agent_relevant_memory`, etc.) are immutable, versioned `SearchSpec`
  templates. The author fixes collections, types, and filters; the caller
  supplies only declared, type-checked parameters. This is a *curated*
  disclosure surface — but only advisory today, because the same key can still
  call raw `/search`.
- **Provenance narrowing at the derivation seam.** Task values carry
  "transitive source IDs" and a narrower set of "directly citable IDs"; a Task
  *may narrow either set but cannot widen citation authority* (DECISIONS.md,
  "Registered Tasks are the computation seam"; `src/memseek/derive/provenance.py`).
  A Pipeline can therefore emit a genuinely sanitized projection into a
  separate collection — an *enforced* disclosure mechanism that exists now.
- **Rendering + fencing** in `src/memseek/render.py`:
  deterministic projection, one untrusted-data fence (`fence_records`),
  fence-closing-character escaping, and per-score `render` gating
  (`definition.render and name in record.scores`). Field selection already
  happens before this module. This is the natural home for field-level
  redaction.
- **Bounded context assembler** `/context`
  (`src/memseek/views/context.py`) —
  deduplicated, budgeted, fenced. Already a disclosure envelope; it just lacks
  a policy input.

### 1.4 The filesystem is *latent* in the data model

Records are `(entity, collection, key)` triples. Keys are slash-friendly
strings up to 128 chars — the shipped gbrain example already uses
`people/maya`, `companies/acme`, `profiles/role`
(`examples/gbrain_showcase.py`). Collection +
key is a two-level namespace, and existing reads already implement most POSIX
verbs:

| Filesystem concept | Substrate primitive | Endpoint today |
| --- | --- | --- |
| mount / root | entity | `?entity=` |
| directory | collection (+ key path segments) | — |
| file path | `collection/key` (`/`-delimited key) | — |
| `ls <dir>` | current keyed rows in a collection | `GET /document?collections=` |
| `ls -R` | current state, all collections | `GET /document` |
| `cat <path>` | rendered current record text | dereference of current row |
| `stat <path>` | id, seq, ready, occurred_at, citations, scores | `GET /records/{id}` |
| open by inode | dereference by UUID | `GET /records/{id}` |
| `git log <path>` | key version history, newest first | `GET /document/history` |
| `tail -f` | activity stream / replay | `GET /timeline`, `/delta` + `/cursor` |
| `find` / `grep` | retrieval | `POST /search`, `/views/{name}/query` |
| write / append | ingest evidence (append-only) | `POST /records` |
| `rm` | tombstone (retract key) | keyed tombstone via Pipeline |
| `rm -rf` (cascade) | provenance-closure delete | `POST /erase` |
| symlink / computed file | named view / artifact | `/views/*`, `/artifacts/*` |
| `.snapshot/` | materialized artifact snapshots | `POST /artifacts/{name}/snapshot` |

The `/tools` surface (`src/memseek/tools.py`) already
frames a read-only agent tool contract (`memseek_search`, `memseek_answer`,
`memseek_dereference`, `memseek_context`, `memseek_view`, `memseek_artifact`)
with the untrusted-data warning baked into every description. A filesystem is a
natural sibling set of tools on the same pattern.

**Architectural constraint to respect (spec §4, lines 406/691):** *logical
collections and packages never imply physical namespaces.* So the filesystem
must be a **logical projection/adapter**, never new physical storage. Good news
— that is exactly what the table above already is.

---

## 2. Ask 1 — Selective context disclosure

**Definition adopted here:** the ability to expose a controlled subset of a
workspace's memory to a given principal (agent, tenant-of-tenant, session),
sliced by entity, collection, key-prefix, type, sensitivity, field, provenance
authority, status/freshness, and token budget; **enforced at the server** and
**auditable**. A principal must not be able to widen its own ceiling.

The plan is layered so we can ship value without the full build.

### Level 0 — Possible *today*, by composition (no code changes)

These work now but give **advisory or write-time** enforcement only:

- **Curated view catalog.** Give an agent integration a documented list of
  named views and treat raw `/search`/`/document` as off-limits. The view
  author fixes the scope. *Caveat: not enforced — the same workspace key still
  reaches everything. Use only when the agent is trusted and curation is about
  focus, not security.*
- **Entity partitioning.** Put a principal's disclosable memory under its own
  entity id (explicit `entity.id`-style naming, per our design priorities) and
  hand the agent only that entity. *Same caveat — enforced only if the key is
  also scoped (Level 1).*
- **Sanitizing derivations (genuinely enforced, today).** Author a Pipeline
  that reads sensitive collections and emits a redacted, citation-narrowed
  projection into a dedicated `disclosable/*` collection, then expose *only*
  that collection. Because Tasks can narrow provenance and citation authority
  but never widen it, the projection cannot leak a source it didn't render.
  This is the one Level-0 path that is safe against a hostile reader — at the
  cost of materializing a second copy and a derivation lag.

**Recommendation:** use sanitizing derivations for the genuinely-sensitive case
right now; treat curation/partitioning as ergonomics, not security, until
Level 1 lands.

### Level 1 — Scoped disclosure keys + ceiling scope (the core build)

This is the smallest change that makes disclosure *real*. Two pieces:

**(a) Principals below the workspace.** Extend auth so a workspace can mint a
child credential bound to a **disclosure policy**. `authenticate_api_key()`
returns not a `str` workspace but a `Principal(workspace, policy)`; the full
workspace key resolves to an unrestricted policy for backward-compatibility-free
continuity (our design priority is *no backward compat*, so the root key simply
maps to the all-permissive policy — no dual code paths).

A policy is small and YAML-authored (design priority: YAML-author clarity):

```yaml
# a disclosure grant, mintable by the workspace root
name: assistant-readonly
entities: ["cust:*"]            # entity globs; "*" only for root
collections: [pages, facts, profiles]   # allow-list
types: [page, fact]             # optional type allow-list
status: active                  # never expose draft/all
max_depth: 2                    # cap provenance depth
redact_fields: [profiles.ssn, profiles.dob]   # dotted field paths
tools: [memseek_search, memseek_view, memseek_fs_read]  # allowed tool subset
views: [agent_relevant_memory]  # allow-listed named views
token_ceiling: 8000             # hard cap on any single disclosure
```

**(b) A ceiling scope intersected into every read.** Add one choke-point
helper — call it `apply_policy(scope, policy)` — that intersects the caller's
requested scope with the policy and is invoked by *every* read path:

- `scope_conditions()` in `search/scope.py` (search, views, `/context`
  candidates, graph boost);
- `/document`, `/document/history`, `/timeline`, `/delta`
  (`src/memseek/views/`);
- **`GET /records/{id}` dereference** — must re-check the fetched row against
  the policy so a principal can't escape the ceiling with a known UUID.
  Disclosure-safe behaviour: an out-of-policy id returns **404, not 403**, so
  existence is never confirmed.
- artifact/view execution (`src/memseek/artifacts.py`).

Because the machinery is *intersection with existing predicates*, this reuses
`scope_conditions` almost verbatim; the work is threading `policy` from the auth
dependency to each read and adding the intersection + the dereference recheck.

**Enforcement invariant to test:** for any endpoint and any caller-supplied
scope, the returned record set ⊆ (policy-permitted set). Add an adversarial
test that tries to widen collections/entities/status/depth past the policy and
asserts empty/404.

### Level 2 — Sensitivity labels + field-level redaction

Row- and field-level control, layered on Level 1:

- **Row labels.** Two options: (1) a reserved `content` label the insert
  validator understands, or (2) reuse the existing *classification annotation*
  the spec already names (spec §1 lists `classification` among annotation kinds)
  and filter on it as an annotation-backed field. Option (2) composes with the
  existing required-annotation predicate rules and needs no new storage column.
  Policy carries a `clearance`; reads drop rows whose label exceeds it.
- **Field redaction** in `render_record()` / the pre-render projection
  (`src/memseek/render.py`): drop or mask the dotted
  paths in `policy.redact_fields`, mirroring how per-score `render` gating
  already works. Redaction must happen before fencing and before token
  counting so a redacted field never consumes budget or leaks via length.

### Level 3 — Disclosure audit

Reuse the `_system` audit pattern (`_system/run`, `_system/erasure`): write a
trigger-silent `_system/disclosure` record (or a dedicated log sink) capturing
principal, resolved scope, record-id hashes, and token count per disclosure.
`last_accessed` touch already exists in `/context` and reads
(`src/memseek/views/context.py`); this makes
"who saw what" first-class and queryable, closing the loop for compliance.

---

## 3. Ask 2 — A filesystem interface for the agent

**Definition adopted here:** an honest filesystem *view* over the substrate —
honest because it must not hide the substrate's three defining truths:
append-only/event-sourced writes, eventual readiness, and successor-versioning
(no in-place mutation). The mapping in §1.4 is the contract; this section is how
to expose it.

### 3.1 Path grammar

```
mem://<entity>/<collection>/<key...>[@<selector>]
```

- `<key...>` may contain `/`; the collection is always the first segment after
  the entity, so `mem://cust:acme/profiles/team/lead` is
  entity `cust:acme`, collection `profiles`, key `team/lead`.
- Optional `@selector` picks a version: `@current` (default), `@seq:44`,
  `@run:<uuid>`, `@at:2026-07-01T00:00:00Z` (point-in-time — dovetails with the
  [generative-agents Day-3 provenance/point-in-time idea](generative-agents-example.md)
  in memory). Point-in-time read = history filtered by `occurred_at`/`seq`.

### 3.2 Recommended shape: a thin adapter + tool surface (no storage change)

**Option A (recommended): SDK adapter + `/tools` entries.** Add a
`MemseekFilesystem` adapter in `src/memseek/sdk.py` that
translates path ops into existing endpoints, and register matching tool schemas
in `src/memseek/tools.py` so any MCP/agent harness gets
filesystem tools for free (same pattern as `memseek_dereference` today):

| Tool / method | Translates to |
| --- | --- |
| `fs_ls(path)` | `/document` (dirs = collections + key prefixes; group + count) |
| `fs_read(path[@sel])` | current/selected row → `render_record` |
| `fs_stat(path)` | `/records/{id}` metadata (id, seq, **ready**, citations, scores, depth) |
| `fs_history(path)` | `/document/history` |
| `fs_find(glob \| query)` | `/search` or a named view |
| `fs_write(path, text, content, cite)` | `POST /records` (see §3.3) |
| `fs_rm(path)` | keyed tombstone (via a shipped `retract` Pipeline) |

Optionally add a small convenience HTTP facade `GET /fs/ls|read|stat` in
`src/memseek/api.py` for non-SDK callers; it is pure
translation over the routes above and stores nothing new — honouring "logical
collections never imply physical namespaces."

**Option B (stretch/demo): a real virtual filesystem.** A FUSE/9p mount in an
SDK extra that presents `mem://` as an OS directory. Great for the showcase and
for "give the agent a working directory," heavier to maintain. Keep it out of
core; it consumes the same adapter as Option A.

### 3.3 Making writes honest

`POST /records` is an **append** boundary (spec §6.1): public keyed inserts cite
the current same-key parent and become a *successor*, not an overwrite; event
collections just append. So the FS write verb must not pretend to be `pwrite`:

- On an **event** collection, `fs_write` = append evidence.
- On a **keyed** collection, `fs_write` = propose a new version of that path
  (successor row), optionally `ready:false` until enrichment — surface that in
  `fs_stat` like a file still syncing.
- `fs_rm` = tombstone (the key's current row becomes a retraction; history is
  preserved). True deletion is only `/erase`, which cascades the provenance
  closure — expose it, if at all, as an explicit `fs_destroy`, never as `rm`.

Surfacing these truths (readiness in `stat`, history as first-class, retract ≠
destroy) is what separates an honest adapter from a leaky abstraction.

---

## 4. How the two features compose

The unifying idea:

> **The filesystem is the projection. Selective disclosure is the policy that
> decides which projection a principal sees.**

The FS adapter resolves every path *through the principal's ceiling scope*
(Level 1). Concretely:

- `fs_ls` lists only disclosed collections/keys — undisclosed paths simply do
  not appear (they were never in the ceiling scope).
- `fs_read`/`fs_stat` on an out-of-policy path returns **ENOENT**, not EACCES —
  identical to the 404-not-403 dereference rule, so the filesystem never
  confirms the existence of what the principal may not see.
- `fs_read` renders through the redaction step (Level 2), so a disclosed file
  can still have masked fields.
- Every `fs_read` writes a disclosure audit record (Level 3).

This means the filesystem needs no access logic of its own; it inherits it from
the disclosure layer. Build Level 1 first and the filesystem becomes safe by
construction.

---

## 5. Complex greps the agent can issue

A `grep` over the filesystem is content matching over the disclosed record set.
The substrate already ships two of the three engines you need; only true
regex/substring matching is missing.

### 5.1 Three engines behind one `fs_grep` verb

| Mode | Engine | Status |
| --- | --- | --- |
| `word` / phrase | existing `record_fts` GIN on `to_tsvector('english', content->>'text')` | **exists** |
| `semantic` (match by meaning) | existing hybrid search (HNSW `record_vec` + fts, RRF fusion) | **exists** |
| `fixed` (substring) / `regex` | **new**: `pg_trgm` extension + GIN trigram index on `content->>'text'`; recall via `LIKE`/trigram `%`, exact verify via `~` / `~*` / `ILIKE` | **build** |

Word and semantic grep are `POST /search` today. The `fixed`/`regex` engine is
the genuine gap: `to_tsvector` is token-based and cannot express `grep -E`. Add
it as a **new PostgreSQL recall channel** that fits the existing "backends are
recall-only, core rechecks canonical" architecture
(`src/memseek/search/scope.py`): the trigram
index generates candidate ids, the canonical core reloads and re-applies the
regex with `~`. Because the external index (Turbopuffer) cannot do regex,
**regex grep is a Postgres-only path** — state that limitation rather than
silently degrading it.

### 5.2 Structured / boolean grep is mostly here already

Typed field predicates already exist in the `where` grammar
(`src/memseek/search/spec.py`): `eq`, `in`,
`gt/gte/lt/lte`, `exists`, `contains_any`, `contains_all`, capped at 100 values,
with multiple fields ANDed. The clean extension is **one new `match` (regex)
operator** for string/content fields, threaded through the existing
`_validate_predicates()` and `pushdown_predicate()` seams — no new endpoint, and
it composes with the field filters agents already have. (There is no `OR` today;
callers express alternation with a multi-source search or a regex alternation.)

### 5.3 `grep -r dir/` needs a key-prefix scope

Scope has `collections`, `entities`, `types`, `status`, `keyed`, time, and
depth — but **no key-prefix**, so `grep -r pages/team/` cannot be expressed.
Add `key_prefix` (or `key_glob`) to `SearchScope` and one clause to
`scope_conditions()` (`row.key like %s`, trigram-assisted). Entity + collection
already scope; key-prefix completes the "recursive from a directory" model.

### 5.4 Flags map to SQL cleanly

| grep flag | Mapping |
| --- | --- |
| `-i` | `~*` / `ILIKE` |
| `-c` (count) | `count(*)` query |
| `-l` (files with matches) | `distinct (collection, key)` |
| `-v` (invert) | negated predicate |
| `--include` / `--exclude` | collection / type filters |
| context lines / snippet | `truncate_middle` snippet under `SEARCH_RENDER_TOKENS` (already in `render.py`) |
| `-r` | default (scoped) |

### 5.5 Two constraints that are easy to get wrong

- **grep must scan the *disclosed, redacted* projection, never raw `content`.**
  Otherwise `fs_grep --fixed 123-45-6789` confirms a value that field redaction
  (Level 2) was supposed to hide. The match corpus = exactly what the principal
  could `fs_read` after ceiling scope and redaction. This is the single most
  important disclosure interaction for grep.
- **Safety and boundedness.** Cap pattern length, run regex under a
  `statement_timeout`, and prefer trigram-index-backed patterns; arbitrary
  regex without a usable trigram prefix falls back to a bounded sequential scan.
  Responses stay under `MAX_RESPONSE_BYTES` with the existing `truncated` /
  cursor pagination. Guard against ReDoS-style catastrophic patterns via the
  timeout, since the pattern is caller-supplied.
- **Consistency.** `word`/`fixed`/`regex` grep runs over canonical PostgreSQL
  and is **read-your-writes**; `semantic` grep depends on the projection outbox
  and is **eventually consistent** (a just-written row may not be semantically
  searchable until its `index_upsert` job runs). Tell the agent which it got.

---

## 6. Multiple agents on the same "files"

This is mostly **already solved** by the substrate's append-only, event-sourced
design (spec §6.2, DECISIONS.md). The work is surfacing existing guarantees
through the filesystem verbs, not building concurrency control.

### 6.1 Reads are snapshot-consistent for free

Records are immutable — nothing is ever mutated in place, only superseded by a
successor. So concurrent readers never see a torn record, and a `@seq` / `@run`
/ `@at` selector (§3.1) **pins a stable multi-file view**: an agent can read
many paths at one logical instant, the filesystem analog of opening a consistent
snapshot. Point-in-time (`@at`) is just history filtered by `seq`/`occurred_at`.

### 6.2 Concurrent writes to the same key are already compare-and-swap

Per spec §6.2, every keyed write privately snapshots the active head of each
key it touches; commit and promotion compare those expected heads and reject a
stale candidate (`409 promotion_stale`, or a derive job retries), and conflicting
keyed commits for one entity serialize under the `(workspace, entity)` advisory
lock. That is optimistic concurrency control we can expose directly:

- `fs_write(path, …, expected_version=<seq|run>)` → **compare-and-set**; a 409
  if another agent moved the head first (the `O_EXCL` / test-and-set case).
- `fs_write(path, …)` with no expected version → **blind append**: the newest
  write becomes current, but — unlike a POSIX overwrite — **every version
  persists in history, so there is no lost update**. This is the honest default.
- `dedupe_key` collapses identical concurrent writes, so two agents issuing the
  same write do not double-insert.

### 6.3 A write conflict is a divergent head, not data loss — resolve by merge

Because both writes survive as successors, "conflict" means two heads disagree,
and resolution is already first-class: the `Divergence` classification
(added/changed/removed/unchanged, see `CONTEXT.md`) plus the
shipped `reconcile`,
`belief_conflict`, and
`contradiction` Pipelines. Two agents
disagree on `profiles/role` → a reconcile derivation emits a merged successor
citing both inputs. **This is git-style merge via a derivation, not a lock** —
and it is the recommended resolution path over pessimistic locking.

### 6.4 Independent tailing and in-progress isolation

- **N agents can watch the same files independently.** Each takes its own named
  `/delta` + `/cursor` consumer (monotonic, scope-hash-guarded), so watchers do
  not interfere; read-triggers/freshness can wake them on change.
- **Stage work invisibly with `status=draft`.** Draft rows are excluded from
  `status=active` reads until promotion, so an agent can prepare a change others
  don't see yet. Per-*agent* draft isolation layers on the Principal policy
  (Level 1) — each agent's drafts scoped to its principal.
- **Cooperative locks are available but discouraged.** A lease could be modelled
  as a TTL'd keyed record in a `_locks`-style collection, but append-only + CAS
  + reconcile make pessimistic locking unnecessary in the common case; treat it
  as an escape hatch, not the default.

### 6.5 Composition with disclosure

N agents with *different* policies see the same underlying rows through
*different* redacted projections — one "file", many views. Crucially, CAS runs
against the **true server-side head**, not the caller's redacted view, so a
low-clearance agent that writes against a partial view still cannot corrupt
state: the server rechecks the real head and re-validates scope before commit.

---

## 7. Phased roadmap

| Phase | Deliverable | Touchpoints | Enforced? |
| --- | --- | --- | --- |
| **P0** | Sanitizing-derivation recipe + a curated view catalog for agents | `derivations/`, `views/`, docs | write-time only |
| **P1a** | `Principal`/policy model; root key ⇒ all-permissive | `auth.py`, `models.py`, `api.py` deps | — |
| **P1b** | `apply_policy()` ceiling intersection on every read + dereference recheck (404) | `search/scope.py`, `views/*`, `records.py`, `api.py` | **yes (row/scope)** |
| **P2** | Row sensitivity label (via classification annotation) + field redaction | `render.py`, insert validation, `definitions/models.py` | **yes (row/field)** |
| **P3** | `_system/disclosure` audit | reuse `on_records_ready`/audit path, `logging.py` | audit |
| **F1** | `MemseekFilesystem` adapter + `fs_*` tool schemas | `sdk.py`, `tools.py` | inherits P1 |
| **F2** | `GET /fs/*` convenience facade + `@selector` / point-in-time | `api.py`, `views/timeline.py` | inherits P1 |
| **G0** | `key_prefix`/`key_glob` scope for `grep -r dir/` | `search/spec.py`, `search/scope.py` | inherits P1 |
| **G1** | `fs_grep` fixed/regex engine: `pg_trgm` ext + trigram index + canonical recheck | migration, `search/`, `sdk.py`, `tools.py` | inherits P1 |
| **G2** | `match` (regex) operator in the `where` grammar | `search/spec.py`, `search/scope.py` | inherits P1 |
| **C1** | Surface concurrency: `@seq/@run/@at` selectors + `fs_write` CAS (`expected_version`, 409) | `sdk.py`, `api.py`, `records.py` | inherits P1 |
| **F3** *(stretch)* | FUSE/virtual mount SDK extra | new SDK extra | inherits P1 |

Ordering rationale: **P1b is the keystone** — it is what turns every existing
scope from advisory into enforced, and it is what makes the filesystem safe.
The filesystem (F1) is cheap once P1 exists because it is translation only.

---

## 8. Open decisions (need a human call)

1. **Policy authoring surface.** A new YAML resource compiled through the
   existing catalog validator (consistent with packages), or a runtime
   credential-mint API (`POST /keys` returning a scoped key once, like
   `create_workspace`)? Likely both: YAML declares *named* grants; the mint API
   issues a key bound to one. Recommendation: start with the mint API returning
   a key bound to an inline policy; add named YAML grants if reuse emerges.
2. **Row labels: reserved `content` field vs. classification annotation.** The
   annotation route reuses machinery and stays out of the public write schema,
   but adds enrichment latency before a row is filterable. Recommendation:
   annotation-backed, matching the spec's existing `classification` concept.
3. **404 vs 403 everywhere.** 404-not-403 is the disclosure-safe default and I
   recommend it uniformly; confirm it's acceptable for debuggability.
4. **Write semantics in the FS.** Expose only `append`/`new-version`, or also a
   convenience that hides successor-versioning? Recommendation: honest verbs
   only — the abstraction's value is that it *doesn't* lie.
5. **Docs split.** Per our "one topic per docs page" priority, when this
   graduates from plan to shipped docs it should become two pages
   (`selective-disclosure.md`, `agent-filesystem.md`) plus a short composition
   note; this planning file is the single-file exception.

---

## 9. What we can honestly say we have *now*

- A working, honest **filesystem read model** already exists across
  `/document`, `/document/history`, `/timeline`, `/records/{id}`, `/search`,
  `/views`, `/artifacts` — it needs a path-grammar adapter, not new storage.
- A working **write-time enforced disclosure** path exists via
  provenance-narrowing sanitizing derivations.
- **No enforced read-time selective disclosure exists** — the workspace key is
  all-or-nothing and `scope` is voluntary. That gap (P1) is the substantive
  build, and everything else layers cleanly on top of it.
