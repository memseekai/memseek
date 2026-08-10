---
title: "Python SDK vision"
eyebrow: Proposed — not yet built
---

!!! warning "Status: proposal"
    This page describes a **proposed** high-level Python surface, not shipped
    API. Today's client is [`MemseekClient`](sdk.md). Nothing here exists yet;
    it is here to be argued with before any of it is written.

The goal in one sentence: **the ease of a toy memory library, with none of
Memseek's substance hidden.** Line one is delightful to write. Line two answers
a question a blob-and-vector memory structurally can't — *why do you believe
that? how did this change? show me the proposal before it goes live?* And every
call hands you a **receipt** for exactly what it gathered: which records, how
many tokens, which definitions, how fresh.

This surface covers the **whole** API — ingest with provenance and client
scores, current state, search, views, context assembly, artifacts, review and
promotion, replay, history, provenance, retraction, and erasure. Where a
behavior depends on catalog configuration, the dependency is stated, not
assumed.

## Five principles this design refuses to break

1. **Construction does no I/O.** A `Memseek` is just your URL and key held in an
   object. It opens no connection, publishes nothing, checks nothing. The
   *first `await`* does real work. That is why there is no `.open()` — an object
   that reads like configuration should behave like configuration.
2. **No hidden writes.** Connecting never installs a catalog; no read ever
   mutates state. Publishing definitions is an explicit, atomic, reviewable act.
3. **An entity is a scope, not a container.** A handle for `account:acme` does
   not "hold everything about Acme." It fills in *whose* memory, and you still
   say *what* you want. There is no unscoped "give me the entity" firehose.
4. **Every result is a receipt.** A briefing tells you which records composed
   it; a search hit carries its source and score; current state reports its own
   freshness. What was gathered is always an attribute, never a mystery.
5. **The catalog lives in the workspace, not your source.** Memseek is
   multi-tenant: each workspace carries its own catalog, served over the API. A
   client may have authored that catalog — or may be a pure consumer that never
   saw the YAML. Either way it learns the types from the live workspace it is
   connected to, never by assuming files on your disk.

## Getting a client

```python
from memseek import Memseek

mem = Memseek(url="http://localhost:8000", api_key=key)   # explicit
mem = Memseek.from_env()                                  # or from MEMSEEK_URL / MEMSEEK_API_KEY
```

Both lines are pure configuration — no network call has happened yet. In a
long-lived app, manage the connection with a context manager; in a script, just
use it and let it close on exit:

```python
async with Memseek.from_env() as mem:
    ...                      # connection pool opened lazily, closed on exit
```

If no catalog is published for the workspace, the first call fails with a
message that says exactly what to do — `publish a catalog first:
mem.publish("./my-memory")` — rather than inventing one for you.

`await mem.healthy()` wraps the unauthenticated liveness check for probes.

## The entity handle

Every record in Memseek belongs to one **entity** — the customer, user, or
agent the memory is about. It is the first concept
[Core concepts](concepts.md#entity-whose-memory-it-is) teaches, so the SDK uses
the same word:

```python
acme = mem.entity("account:acme")     # the id string and its format are yours to choose
```

`mem.entity(...)` does no I/O — it just holds the entity id for the calls that
follow, so you never repeat it. Remember principle 3: `acme` is a *scope*. Every
call on it still names a collection, a view, or an artifact. One level further,
a collection handle scopes both at once:

```python
profiles = acme.collection("profiles")     # entity + collection; also no I/O
```

## The client learns your types from the workspace

The ergonomic Memseek can offer that a schemaless memory can't: **the client
knows your types because the workspace publishes them.** The catalog is served
over HTTP — you do not need the source, or even to have authored it:

| Endpoint | What it exposes |
| --- | --- |
| `GET /collections` | each collection's JSON `schema` (enums, required fields, types), its `mode`, declared filter `fields` |
| `GET /processors` | each derivation's `emit` — including the keyed `keys` it owns |
| `GET /artifacts` | every artifact's `kind` and typed `parameters` |
| `GET /views` | every view's typed `parameters` |
| `GET /triggers` | when reasoning runs — including whether `read` (freshness) triggers exist |
| `GET /catalog` | the selected package and the catalog hash that stamps all of the above |

These also back plain discovery — `mem.collections()`, `mem.views()`,
`mem.artifacts()`, `mem.triggers()`, `mem.catalog()` — so a client can list
what a workspace offers instead of hard-coding it. (`GET /rank/schema` and
`GET /tools` serve query builders and agent integrations; the facade leaves
them to those tools.)

### Types at runtime — the default, and the multi-tenant answer

On first use the client fetches the connected workspace's catalog and builds
Pydantic models from it — no source access, no build step, no assumption about
files on your disk:

```python
mem = Memseek(url=URL, api_key=tenant_key)     # one workspace = one catalog

CrmEvent = mem.model("crm_events")             # a Pydantic model built from the LIVE schema
await acme.add(CrmEvent(
    text="Renewal call: CFO wants SSO before signing.",
    event_kind="commitment",                   # enum values come from the workspace's schema
    source="salesforce",
), type="crm_event")
```

Multi-tenant is exactly why this is the default. A backend serving many
workspaces builds a client per tenant key, and each validates against *its own*
live catalog — different tenants, different collections, all discovered, none
assumed:

```python
async def handle(request):
    async with Memseek(url=URL, api_key=request.tenant_key) as mem:
        acme = mem.entity(request.entity_id)
        await acme.add(mem.model("crm_events")(...), type="crm_event")
```

The catalog is cached per workspace and keyed by its hash; the client refetches
only when the hash moves. The stringly form validates the same way:

```python
await acme.record("crm_events", type="crm_event",
                  text="…", event_kind="commitment", source="salesforce")
# an unknown field or bad enum fails locally, against the live schema, with a path
```

One boundary stated plainly: a model describes the collection's **content**
schema. The record *envelope* — `type`, `key`, `occurred_at`, and so on — is
not part of that schema (`type` is chosen at write time, not declared), so it
travels as explicit arguments to `add(...)`, never guessed.

### Types as a file — only if you own the catalog

If you *are* the team that authored the catalog and want static autocomplete
and mypy in your own code, generate a typed module — from a workspace key or
from the YAML you wrote:

```console
memseek types --workspace $KEY        --out memory.py   # from a live workspace you own
memseek types --catalog   ./my-memory --out memory.py   # or from catalog source
```

```python
from memory import CrmEvent, ProfileSlot        # identical shapes, now statically typed
```

`ProfileSlot` is `Literal["role", "commitments", "preferences", "open_threads",
"goals"]`, read from the derivation that owns those keys — the same mechanism
generates a skill collection's section slots or a prompt collection's single
`body` slot, long-form bodies included. Slot enums are **per collection**
(`ProfileSlot`, `SkillSlot`), because a key is only meaningful next to its
collection. A keyed collection no derivation owns keeps open string keys.

Codegen is an **owner convenience, not a requirement**. In a multi-tenant
service where each workspace differs, you skip it and introspect at runtime — a
static `memory.py` can only describe one catalog, and you have N. The generated
module embeds the catalog hash, so if a workspace drifts from it the next call
warns rather than lying.

## Writing memory

The full ingest contract, nothing hidden — every field of the write surface has
a place:

```python
result = await acme.add(
    CrmEvent(text="…", event_kind="commitment", source="salesforce"),
    type="crm_event",                          # required: the record's type label
    occurred_at=call_time,                     # when it happened (storage time is automatic)
    because=[transcript_id],                   # optional provenance: what this was derived from
    scores={"priority": 8},                    # values for client-sourced score processors
    dedupe="crm:avery:sso:2026-07-19",         # your idempotency key, when you have one
)
result.id, result.ready    # durable UUID; ready=False while required enrichment runs

await acme.add_many([...])                     # one atomic batch; may mix collections
```

Three deliberate choices worth noticing:

- **`type` is explicit.** The catalog does not declare a collection's types —
  they are write-time vocabulary — so the SDK asks rather than guesses.
- **Dedupe is opt-in.** An earlier draft derived a content-hash dedupe key by
  default; that silently swallows *legitimately repeated* events ("user
  clicked again") that have no natural key. The default now matches the API:
  no key, every write lands. Pass `dedupe=` when you own an idempotency key,
  or `dedupe="content"` to explicitly opt into content-hash semantics.
- **`because=` is client provenance.** Your application can declare what a
  record was derived from at ingest time — the same `derived_from` citations
  derivations use — so even hand-written conclusions are traceable.

### Keyed writes and retraction

Keys apply to `keyed` and `mixed` collections only (an event collection has no
key — that is why `CrmEvent` carried none). Write a slot through the collection
handle, or with `key=` on `add`:

```python
profiles = acme.collection("profiles")
await profiles.set("role", text="SVP Product")           # a successor for profiles/role

await profiles.retract("open_threads", because=[evidence_id])
```

`retract` writes the canonical tombstone: the slot's current value is
withdrawn, history keeps every prior version. Note the signature — **even
forgetting cites evidence**: the API requires a retraction to name at least one
parent record, and the SDK surfaces that rather than hiding it.

Drafts are the same write with `status="draft"` — stored, versioned, invisible
to normal reads until promoted or explicitly requested.

## Reading — three verbs, one axis each

### `current` — the current state of a collection

A direct lookup of the current value in each slot — deduped, bounded, no
ranking. You **name the collection**; there is no unscoped form:

```python
profile = await acme.current("profiles")
profile["commitments"].text        # "Committed to SSO rollout before renewal"
profile["commitments"].citations   # the evidence behind it

skill = await acme.current("skills")       # a different collection = a different read
skill["steps"].text                        # slots hold whole bodies, not only short facts
```

A slot value is whatever its collection allows — a one-line fact, a structured
object, a skill section, a rendered `body`. Reading several collections in one
bounded call is allowed; because keys are collection-scoped, a key shared by
two scoped collections must be disambiguated instead of silently merged:

```python
state = await acme.current("profiles", "plans")
state["risks"]                      # fine while unique across both
state.only("plans")["today"]        # explicit when the same key exists in both
```

`current(..., status="draft")` reads staged drafts — the read a review screen
needs. If the result would exceed the response bound, the call raises
`DocumentTooLarge` (never returns a silent partial); narrow the collections.

### `search` — ranked retrieval across collections

Search is multi-collection *by nature* — ranking across drawers is the point —
so it takes a **list**, and it lives on both the entity and the workspace:

```python
hits = await acme.search(                          # scoped to this entity
    "SSO commitments and renewal risks",
    collections=["crm_events", "reflections"],
    mode="hybrid",                                 # hybrid | vector | text | recent | structured
    k=12,
    where={"event_kind": {"in": ["commitment"]}},  # typed filters over declared fields
)
for hit in hits:
    hit.text, hit.collection, hit.score, hit.cite

await mem.search("renewal risk", collections=["crm_events"],   # across entities…
                 entities=["account:acme", "account:globex"])  # …or omit entities for all
```

The full [SearchSpec](views-search.md) passes through — `where`, `order_by`,
`fields`, `annotations`, `include`, custom `rank` — so nothing expressible in
the query language is out of reach from the SDK. `current` and the slot reads
are inherently per-entity; **search is the one read that belongs on both `acme`
and `mem`.**

### `view` — a saved, typed search

A view is a search you've named, typed, and made reusable — the home for
weighted multi-source fusion and capability requirements:

```python
memories = await acme.view("relevant_memory", task="prepare the renewal")
```

Reach for `search()` when you're writing a query once; graduate it into a
`view()` when it's worth naming or needs fusion.

### The stream, and single records

```python
async for entry in acme.timeline(collections=["crm_events"]):   # newest first, auto-paginated
    entry.text, entry.when, entry.ready

record = await mem.record(record_id)      # full dereference of one known UUID
record = await citation.load()            # every Citation can dereference itself
```

`timeline` is the raw activity stream — successor versions not collapsed,
readiness visible. `mem.record(...)` returns everything canonical about one
row: content, scores, annotations, provenance parents, hashes.

## Keys are collection-scoped

A key names a slot *within a collection* — `role` in `profiles` and `role` in
`skills` are different slots, and the API requires the collection wherever a
key appears. The SDK threads that through so a bare key is never accepted:

```python
profile = await acme.current("profiles")
profile["commitments"].why()          # provenance for profiles/commitments
profile["commitments"].history()      # the successor chain for that slot

profiles = acme.collection("profiles")
profiles.history("role")              # for a slot you haven't read (or a retracted one)
```

A `Belief` carries its own `(collection, key)`, so provenance and history hang
off it unambiguously; the collection handle covers the rest.

## The verbs nobody else can offer

Each is one line, and each maps to a Memseek capability a blob store lacks.

```python
profile = await acme.current("profiles")

# "Why do you believe that?" — walk provenance, to a bounded depth
why = await profile["commitments"].why(depth=3)
why.evidence           # [Memory("Renewal call: CFO wants SSO…", source="salesforce", when="May")]
why.chain              # the citation graph, depth-tagged; every node dereferenceable

# "How did this belief change?" — the successor chain
async for version in profile["commitments"].history():
    print(version.when, "·", version.text)

# Human-in-the-loop as an object
proposal = await acme.propose("crm_profile_rebuild")      # enqueue + wait for the run
proposal.changes       # [Change(key="goals", kind="added", text="Expand to EMEA"), …]
proposal.diff()        # printable before/after per key
await proposal.approve()   # promote atomically; raises StaleProposal if state moved

# Provenance-aware erasure — whole entity, or specific records
report = await acme.erase()                        # everything about this entity
report = await mem.erase(records=[record_id])      # or a targeted selection
report.removed, report.dependents_removed          # the closure is visible, not hidden
```

`propose` lives on the entity handle like every other entity-scoped call. If
the job dead-letters, the raised `JobFailed` carries the job and a `retry()`
that maps to the API's retry route. A `StaleProposal` on `approve()` means live
state moved during review — re-propose rather than force. `why(depth=)` bounds
the walk; provenance chains are finite but can be deep.

## Assembling context for a model

Two calls turn memory into prompt input, and they answer different needs:

```python
# One-call assembly: current state + relevant search + recent records, budgeted
ctx = await acme.context("prepare the renewal", budget=8_000)
ctx.rendered            # one fenced, deduplicated, token-bounded block
ctx.tokens              # 3,200 — never 101,000
ctx.input_records       # every record id that went in

# A defined, versioned briefing — when the composition itself is a contract
brief = await acme.brief("what should I know before the Acme renewal?")
brief.text; brief.citations; brief.tokens; brief.artifact   # "crm_profile_brief@1 · hash"
```

`context()` is the shipped convenience assembler (`GET /context`) — zero
authoring, good defaults, one bounded block. `brief()` renders an
[artifact](artifacts.md): reviewable composition, per-block budgets, and a
persisted manifest. Start with `context()`; graduate to an artifact when the
prompt's structure deserves version control.

The artifact-kind verbs — `brief()` (prompt), `skill()`, `profile()`,
`policy()` — resolve by rule, not magic: each picks the workspace's active
artifact of that kind **when exactly one exists**, and raises
`AmbiguousArtifact` naming the candidates otherwise; `using="name"` always
works. The query string binds to the artifact's single required string
parameter besides `entity`; artifacts with richer signatures take keyword
arguments matching their declared parameters.

Snapshots follow the artifact surface: `acme.snapshot("crm_profile_brief", …)`
persists a render as a keyed record, and `acme.materialized("crm_profile_brief")`
reads the current stored snapshot without re-rendering.

## Replay — feeding another system

Integrations that maintain a cache or projection replay every canonical change
in order. The facade wraps `/delta` + `/cursor` as an iterator with an explicit
commit, because "durably applied" is the consumer's call, not the SDK's:

```python
async with mem.follow(consumer="crm-cache", entity="account:acme") as stream:
    async for batch in stream:
        apply_to_cache(batch.records)      # tombstones included — caches must see removals
        await batch.commit()               # advance the cursor only after durable apply
```

The stream carries the scope hash the API uses to prevent a consumer silently
reusing a position with a different scope; changing the scope raises instead of
drifting.

## What each call gathers

The receipt principle made concrete: you always know what a call fetched, how
it was bounded, and where it came from.

| Call | Gathers | The result tells you |
| --- | --- | --- |
| first typed call | the **catalog** — schemas, emit keys, artifact/view params, hash; cached by hash | drives typing; `mem.catalog()` |
| `acme.add(...)` | nothing; sends one record | `.id`, `.ready` |
| `acme.current("profiles")` | the current head of each slot, bounded (`DocumentTooLarge`, never a silent partial) | slots' `.text`/`.content`/`.citations`; `.fresh` + `.freshness`; `.tokens` |
| `acme.search(...)` | ranked candidates, **re-checked against canonical rows** — the index only nominates | per hit: `.collection`, `.score`, `.cite`, `.when` |
| `acme.view(name, …)` | exactly what the view's contract declares | hits, plus which view answered |
| `acme.context(q, budget=)` | current state + relevant search + recent records, deduplicated and budget-packed | `.rendered`, `.tokens`, `.truncated`, `.input_records` |
| `acme.brief(q)` | the artifact's declared blocks, each within its token budget | `.input_records`, `.tokens`, `.truncated`, `.artifact`, `.citations` |
| `belief.why(depth=)` | the citation graph behind one slot, to the requested depth | `.evidence`, `.chain`, `.depth` |
| `belief.history()` | the successor chain for one collection/key slot | each version's `.when`, `.text`, `.evidence` |
| `acme.timeline()` | the raw stream, paginated | per entry: `.when`, `.ready`, `.tombstone` |
| `mem.follow(...)` | every canonical change in sequence order, ready or not, tombstones included | `.records`, explicit `.commit()` |
| `acme.propose(name)` | the run receipt after the job completes | `.changes`, `.diff()`, divergence |
| `acme.erase()` / `mem.erase(records=)` | the provenance closure swept | `.removed`, `.dependents_removed` |

The through-line: a `brief` isn't an opaque string — it's text plus the exact
list of records that produced it, the definitions in force, and its token cost.
"What's in this prompt?" is `brief.input_records`, not a guess.

## Staying honest about time

A derivation is eventual: after `add(...)`, a derived profile lags until its
trigger fires and the worker runs. The design surfaces this instead of faking a
synchronous "process now":

```python
await acme.settled(timeout=30)               # wait until this entity's pipeline is idle
brief = await acme.brief(query, fresh=True)  # revalidate first, then render
```

Stated precisely, because vague freshness is worse than none:

- `settled()` waits until the entity has **no unready records and no queued or
  running derive jobs** — "everything already in flight has landed." It does
  not conjure work that no trigger scheduled.
- `.fresh` / `fresh=True` ride the catalog's **`read` triggers**: freshness is
  reported per read-triggered derivation, and `fresh=True` enqueues
  revalidation then waits for it. **In a catalog with no `read` trigger there
  is nothing to revalidate through** — `.freshness` is empty and `fresh=True`
  raises `NoReadTrigger` rather than silently doing nothing. The escape hatch
  is explicit: `await acme.propose("profile")` runs the pipeline now.

## Errors read like English

Every failure is a typed exception carrying the structured payload — you catch
the *situation*, not a status code:

```python
from memseek import DedupeConflict, StaleProposal, DocumentTooLarge, JobFailed

try:
    await proposal.approve()
except StaleProposal:
    ...                        # live state moved during review: re-propose

except DedupeConflict:         # same dedupe key, different payload
except CatalogIncompatible:    # a publish would strand existing records' contracts
except DocumentTooLarge:       # narrow the collections; never a partial document
except SchemaMismatch:         # a local validation failure, with the schema path
except JobFailed as e:         # dead-lettered job; e.retry() maps to the API's retry
except NoReadTrigger:          # fresh=True in a catalog with no read trigger
except AmbiguousArtifact:      # brief() with several prompt artifacts; pass using=
```

All inherit `MemseekError` (with `.status_code`, `.code`, `.raw`), so coarse
handling stays one `except` wide.

## The result models

Every result is a real Pydantic model — introspectable, autocompleted,
type-checked — and each keeps a `.raw` dict so a new server field never breaks
your code:

```python
class Citation(BaseModel):
    source: str                     # the channel you recorded under
    when: datetime                  # occurred_at of the evidence
    record_id: UUID
    snippet: str
    async def load(self) -> Record: ...        # dereference to the full canonical row

class Belief(BaseModel):            # the current value in one slot
    collection: str                 # keys are collection-scoped, so a belief carries its collection
    key: str                        # the slot; its value may be a fact, a skill section, or a rendered body
    text: str
    content: dict[str, Any]
    cited_at: datetime | None
    citations: list[Citation]
    ready: bool
    async def why(self, depth: int = 3) -> Provenance: ...
    async def history(self) -> AsyncIterator[BeliefVersion]: ...

class Current(BaseModel):           # what acme.current("profiles") returns
    entity: str
    beliefs: dict[tuple[str, str], Belief]     # (collection, key) — no cross-collection merging
    retractions: list[str]
    fresh: bool                     # trivially True when no read-triggered derivation feeds this
    freshness: list[Freshness]
    tokens: int
    raw: dict[str, Any]
    def __getitem__(self, key: str) -> Belief: ...       # by key when unambiguous
    def only(self, collection: str) -> Current: ...      # narrow when it is not

class Hit(BaseModel):               # one search/view result
    text: str
    collection: str
    rank: int                        # authoritative one-based order
    score: float | None              # query-relative 0-1; null when structured
    rank_score: float | None         # native diagnostic utility
    when: datetime
    cite: Citation
    raw: dict[str, Any]

class Brief(BaseModel):
    text: str
    citations: list[Citation]
    input_records: list[UUID]       # every record that composed it
    tokens: int
    truncated: bool
    artifact: str                   # "name@version · hash"
    raw: dict[str, Any]
    def as_prompt(self) -> str: ...
```

## Every call lowers onto an endpoint you already have

Nothing here is invented behavior — the facade is a readability layer over
receipts the service already returns:

| Friendly call | Lowers to |
| --- | --- |
| `mem.entity(e)` / `acme.collection(c)` | in-memory scope; no I/O |
| `mem.model(c)` / `memseek types` / discovery | `GET /collections` + `/processors` + `/artifacts` + `/views` + `/triggers` + `/catalog` |
| `acme.add(...)` / `col.set(...)` / `col.retract(...)` | `POST /records` (schema check client-side; envelope: `type`, `key`, `occurred_at`, `derived_from`, `scores`, `status`, `tombstone`) |
| `acme.current(...)` | `GET /document` |
| `acme.search(...)` / `mem.search(...)` | `POST /search` (full SearchSpec passthrough) |
| `acme.view(name, …)` | `POST /views/{name}/query` |
| `acme.context(q, budget=)` | `GET /context` |
| `acme.brief(q)` / `skill()` / `profile()` / `policy()` | `POST /artifacts/{name}/render` |
| `acme.snapshot(...)` / `acme.materialized(...)` | `POST /artifacts/{name}/snapshot` / `GET /artifacts/{name}` |
| `belief.why()` | `GET /records/{id}` walk over `citations` |
| `belief.history()` / `col.history(key)` | `GET /document/history?collection=c&key=k` |
| `acme.timeline()` | `GET /timeline` (auto-pagination) |
| `mem.record(id)` / `citation.load()` | `GET /records/{id}` |
| `mem.follow(consumer=…)` | `GET /delta` + `POST /cursor` |
| `acme.propose(...)` / `proposal.approve()` / `JobFailed.retry()` | `/processors/{name}/run` → `/jobs/{id}` → `/runs/{id}` → `POST /promote`, `/jobs/{id}/retry` |
| `acme.runs(...)` | `GET /runs` (auto-pagination) |
| `acme.settled()` | freshness + job state |
| `acme.erase()` / `mem.erase(records=)` | `POST /erase` (entity or explicit ids) |
| `mem.publish(dir)` / `mem.catalog()` / `mem.healthy()` | `POST /catalog` / `GET /catalog` / `GET /health` |

## Where it deliberately stops

Three seams the facade names rather than papers over:

- **A synthesized free-text "answer" is not a substrate feature.** Memseek
  retrieves and bounds; it does not compose prose on read. There is no
  `acme.answer(q)` hiding a model call — `context()`/`brief()` give you cited
  input and you complete it, or you pass your own `llm=`.
- **`add(...)` needs a collection and a type.** The friendly
  `source="salesforce"` is a schema field on a record that still lands in a
  typed collection you defined. The SDK is not an ETL layer and ships no
  connectors.
- **Derivation is eventual, and the API says so** (`current(...).fresh`,
  `settled()`, `NoReadTrigger`), rather than pretending a write is instantly
  reflected.

## Two audiences, and the graduation path

The facade serves two roles, and it must not blur them:

- **Consumers** connect to a workspace someone else provisioned. They introspect
  the live catalog at runtime and use it — there is nothing to author, publish,
  or eject. This is the multi-tenant runtime path above.
- **Owners** author the catalog. For them the facade *uses* and *seeds* memory;
  it never becomes a second way to *define* it. When defaults stop fitting, pull
  the catalog into explicit, reviewable YAML — the real source of truth — and
  keep going:

```python
await mem.eject_catalog("./my-memory")     # OWNERS only: write the catalog out as YAML you own
```

Instant start for a newcomer who owns their catalog; explicit catalog-as-code
the moment it matters; and for a pure consumer, none of this applies — the
catalog was never theirs to eject. No lock-in, no shadow schema.

## What building this needs

Four honest layers, each usable alone:

1. **`MemseekClient`** — today's raw HTTP client (dict returns). The transport.
2. **A typed client** — `wait_for_run`, typed result models, typed errors,
   pagination. The `Citation`/`Current`/`Hit`/`Proposal` models live here.
3. **A catalog introspector** — fetch the catalog endpoints and build Pydantic
   models **at runtime, per workspace** (cached by catalog hash); optionally
   emit them as a file (`memseek types`) for owners who want static types. Pure
   client work; the endpoints already return everything it needs.
4. **`Memseek`** — this facade, built on layers 2 and 3.

The sequence is: type the client, add the introspector, then build the friendly
surface. A first prototype — `Memseek`, `entity()`, `add()`, `current()`,
`search()`, `context()`, `why()`, `brief()`, plus `memseek types` — can run
against the shipped `core.yaml` catalog (it already has `profiles`,
`reflections`, `skills`, and matching artifacts), on the fake provider, with no
new YAML.
