---
title: Glossary
eyebrow: Shared language
---

This page gives each term in Memseek exactly one meaning. Read it after
[Core concepts](concepts.md) and keep it open while you work through the
authoring and API guides. Where a name appears literally in configuration or
JSON, it is shown in `code style`.

## The pairs people mix up

If you only skim one thing, make it this table. Every row is a pair of terms
that sound related but are not.

| These two | Are not the same | Because |
| --- | --- | --- |
| `active` on a **definition** | `status: active` on a **record** | One picks the default version of a contract. The other means a record is live rather than a draft. |
| **Processor** | **Derivation** | A processor enriches one record as it arrives. A derivation reads many records and writes new ones. |
| **Definition version** | **Record successor** | A new version changes a contract. A successor changes a stored value. Data changing never needs a new version. |
| **Ready** | **Active** | Ready means enrichment finished. Active means not a draft. A record can be one without the other. |
| **Draft** | **Tombstone** | A draft proposes a value that isn't live yet. A tombstone withdraws one that was. |

## Start with the data

### Workspace

A **workspace** is an isolated memory area with its own key, definitions,
records, background work, and search results. A key grants access to one
workspace only — never to every workspace on a server.

### Catalog

A **catalog** is the complete set of YAML definitions describing one memory
system: what can be stored, how records get enriched, what automated work may
run, and what callers may read. Publishing a catalog validates all of it
together, then puts it into effect for a workspace.

### Package

A **package** is the release manifest inside a catalog. It lists the exact
versions of the collections, views, artifacts, and other definitions that are
meant to work together. If the catalog is the source tree, the package is the
versioned release you install. Package versions look like
`customer_memory@1.2.0`.

### Collection

A **collection** is a named contract for one kind of record — much like a
database table with extra rules. It supplies the schema, the search setup, and
the enrichment that records must have. For example `customer_events` holds
things that happened, while `customer_profiles` holds current facts.

### Record

A **record** is one stored memory: an event, observation, note, profile value,
or concluded fact. It belongs to exactly one collection and one entity. Records
are never edited — correcting a fact writes another record.

### Entity

An **entity** is whoever or whatever the memory is about. It is an identifier
you choose, such as `customer:acme`, `user-42`, or `project-apollo`. Searches,
document reads, and automated reasoning usually work one entity at a time.

### Event and current fact

An **event** is something that happened and stays part of history — "the
customer requested a refund." A **current fact** is the latest answer to a named
question, such as a customer's role or preferences. A collection declares which
of these it holds with `mode: event`, `mode: keyed`, or `mode: mixed`.

### Key, successor, and tombstone

A **key** names one current-fact slot. In a keyed collection, the newest active
record for an entity-and-key pair is the current value; an older one is its
**predecessor** and a newer one its **successor**.

A **tombstone** is a successor that deliberately empties the slot. It removes
the value from current-state reads without erasing any history.

### Ready, draft, and active

Three states that are easy to confuse:

- **Ready** — the record has finished all *required* enrichment, so it may now
  appear in search and set off automated reasoning.
- **Draft** — a proposed record awaiting review. It does not replace a current
  active value.
- **Active** — a live record that ordinary reads may use. For a keyed slot, the
  newest active record is the current one.

The `active: true` flag on a *definition* means something different: it picks
the default version of that collection, view, or artifact. See
[Version and current](#version-and-current) below.

### Annotation and score

An **annotation** is extra information a processor attaches to a record after
it is stored, under that processor's name. A **score** is a number copied out of
an annotation into a place that is cheap to read, so ranking and triggers can
use it without loading everything.

### Document

A **document** is the "what do we currently believe about this entity?" read.
It returns current keyed values, anything withdrawn, and how fresh the reasoning
behind them is — as opposed to search, which finds relevant records.

## Define how memory works

### Schema and field

A collection's **schema** validates a record's structured content. A declared
**field** gives one value inside that content a stable, typed name that search,
filtering, and sorting can use.

This distinction matters: a value is **not** automatically searchable or
filterable just because it exists in the content. You have to declare it.

### Model alias

A **model alias** is a stable name for a provider model, such as `strong`,
`cheap`, or `embed`. Your definitions refer to aliases rather than vendor model
names, so a deployment can change providers — or upgrade a model — without
anyone rewriting the design.

### Enrichment processor

An **enrichment processor**, usually just **processor**, performs one small
operation on each incoming record: creating an embedding for meaning-based
search, producing a numeric score, or attaching structured data. Required
processors are the barrier a record must clear to become ready; optional ones
add information later without holding anything up.

One naming wart worth knowing: the route `POST /processors/{name}/run` manually
runs a **derivation**, not a processor. It is a historical name, and the API
guide flags it where the route appears.

### Derivation (also called a pipeline)

A **derivation** is a declared, bounded piece of automated reasoning that reads
existing evidence and writes new, cited records — for example turning recent
customer events into an updated profile.

Some configuration and runtime output calls the same thing a **pipeline**. In
these guides *derivation* is the term used throughout; *pipeline* appears only
where it is literally the name in YAML or in a runtime response.

### Source and Task

Inside a derivation, a **source** selects a bounded set of input records — new
events, or current profile slots. A **task** does one step with that input: a
model call, a search, or a deterministic transformation. When the tasks finish,
the derivation emits validated records.

### Trigger

A **trigger** says *when* a derivation should be queued: after a matching write,
once enough score has accumulated, when things go quiet, or on a schedule. A
trigger schedules work — it never runs the derivation inline with the write that
set it off.

### Job, run, and backfill

A **job** is queued work waiting for a worker. A **run** is one recorded attempt
at executing it. A **backfill** is a resumable job that applies one enrichment
processor to records that already existed before that processor was added; it
leaves annotations that are already present alone.

## Read and use memory

### Search and SearchSpec

**Search** finds relevant records. A **SearchSpec** is the typed request
describing one search: its query, scope, filters, ranking, requested values, and
size limit.

Once a search is part of your product rather than a one-off, use a named view
instead of rebuilding the same request in every caller.

### View

A **view** is a saved, versioned read with typed parameters. It usually wraps a
search — "relevant history for this customer and task." The caller supplies
values like the entity and the task; your definition owns the scope, the
filters, and the ranking, and the caller cannot widen any of them.

### Structural graph

A **structural graph** is a bounded way of reading ordinary records as
connections between things. A view maps three declared fields onto the roles
"from", "to", and "what kind". It does not create a separate graph database, and
every path returned cites the exact records it walked through. See
[Graph data](graph-data.md).

### Search profile and projection

A **search profile** declares how a collection can be searched — by keyword, by
meaning, by exact filtering, or a combination.

A **projection** is a rebuildable external copy of records that makes search
faster. The database remains the source of truth: whatever a projection
proposes is always re-checked against the real records before it reaches you,
which is why a projection can be rebuilt or thrown away at any time.

### Artifact and render

An **artifact** is a saved recipe that combines current records and views into
bounded text — a prompt, a briefing, a profile, a policy. A **render** is the
resulting text, plus the information needed to audit how it was assembled.
Rendering is deterministic: the same inputs always produce the same text.

### Artifact use and feedback

An **artifact use** is a small handle created when a rendered artifact is handed
to an agent or application. You store that handle next to whatever your system
produced with it.

Later, **feedback** on that handle writes a learning-signal record. It never
changes an artifact directly. This is what makes a real outcome traceable back
to the memory and the prompt that influenced it.

### MCP interface

An **MCP interface** is an explicit allowlist, owned by your package, of the
tools an AI agent may call. A view, artifact, or endpoint is invisible to an
agent unless the interface names it. `GET /tools` publishes that allowlist, and
`POST /mcp` serves it remotely over Streamable HTTP; `memseek mcp` serves the
same contract locally over stdio.

## Trust, history, and change

### Provenance and citation

**Provenance** is the trail from a concluded record or a rendered prompt back to
the records that informed it. A **citation** is one explicit reference in that
trail. Together they let you inspect the evidence rather than take a generated
conclusion on faith.

### Version and current

Definitions and data each have their own history:

- A **definition version** (`customer_events@2`) changes the contract for future
  records. `active: true` picks which version is the default.
- A **record successor** changes the stored value of one keyed slot. The newest
  active successor is the **current** value.

Changing a record's value never requires a new collection version. Changing what
the collection's contract *means* may. See
[Core concepts: versioning](concepts.md#versioning-which-latest-is-which).

### Watermark and cursor

A **watermark** records how far incremental reasoning has safely read, so it
cannot double-count records. A **cursor** records how far an external consumer
has applied a stream of changes. Both are progress bookmarks, not versions of
anything.

### Erasure

**Erasure** permanently removes an entity or specific records, along with the
bounded set of records concluded from them. It is not a soft delete and cannot
be undone through the API.

## Where each term shows up

Start with [Getting started](getting-started.md) to run the service, then
[Core concepts](concepts.md) for the data model. If you are designing a memory
system, continue with [Catalog layout](catalog-layout.md). If you are
integrating one, read the [Python SDK](sdk.md) or the
[HTTP API guide](api-surface.md). The deeper authoring pages link back here
whenever a term first matters.
