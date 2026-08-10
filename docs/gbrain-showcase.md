---
title: The gbrain showcase
eyebrow: Tutorial — a graph-aware memory you can talk to
---

`examples/gbrain_showcase.py` is a live terminal walkthrough of a *self-wiring
knowledge brain* built entirely on Memseek. It seeds a tiny corpus of
interlinked pages, watches Memseek wire them into a cited graph, extract a
declared-fact index, distill patterns, concepts, and consolidated takes, turn a
conversation into durable memory atoms, and then lets you search and ask cited
questions of the result — all from the plain SDK, with **no graph-specific HTTP
endpoint** and no API surface of its own.

It is the executable counterpart to the animated
`marketing/public/showcase/gbrain/index.html`
product tour, and it re-expresses the distinctive capabilities of
[Garry Tan's gbrain](https://github.com/garrytan/gbrain) on Memseek's
immutable-record substrate. This page explains what it does, sketches how it is
implemented in YAML, and shows what you can do with it. For the
parameter-by-parameter reading of every catalog file, see
[The gbrain catalog](gbrain-catalog.md); for the design rationale and the
phase-by-phase build log, read [the implementation plan](gbrain-plan.md).

> Everything the showcase exercises ships as an **opt-in example catalog** —
> `examples/gbrain_catalog/`, published as `gbrain@0.13.0`. The default Memseek
> catalog exposes none of these surfaces; a workspace opts in by publishing the
> package, which the script does for you on startup.

## What it demonstrates

Run against a real provider, one run seeds an isolated entity and reveals, in
order:

1. **A self-wiring graph.** Five markdown pages are written; Memseek extracts
   typed, cited edges between them with **zero LLM calls** and exposes the walk
   through a named `graph_query` view.
2. **Structurally isolated pages.** One page is a deliberate orphan — it links
   to nothing and nothing links to it — so `orphan_pages` has a real isolated
   page to report.
3. **A deterministic fact index.** Every page's `## Facts` section is folded
   into one bounded, current `page_facts` array — also with no model calls.
4. **Cited memory atoms.** A conversation transcript is distilled into small,
   durable atoms (`fact | preference | commitment | decision | outcome |
   relationship`), each citing the transcript UUID that supports it.
5. **Patterns, concepts, and takes.** New edges and atoms drive a bounded
   "dream cycle": recurring `patterns`, a compact `concept_index`, and a
   consolidated `take_index` — each entry cited, each index size-capped.
6. **Hybrid, graph-boosted retrieval.** A single search spans pages, facts,
   atoms, patterns, concepts, and takes, with an optional graph-distance boost
   anchored at a seed page.
7. **Cited synthesis.** `POST /answer` produces a bounded answer with visible
   citations, an optional query rewrite, anchored graph context, gaps, and a
   saved provenance record.

Every surface is **bounded** (each derivation caps Tasks, tokens, rows, and
wall-clock) and **cited** (nothing derived is kept unless it names the evidence
it rests on).

## How it's implemented — the example catalog

The whole brain is YAML. The script writes records and reads views through the
ordinary SDK; the behaviour lives in `examples/gbrain_catalog/`.

### Two collections carry the graph

- **`pages`** (`collections/pages.yaml`) — a **keyed** collection of markdown
  pages (`title`, `body`, `type`, plus a searchable `text` projection). Keyed
  means each page key has one current version; rewriting a page supersedes it
  rather than appending.
- **`edges`** (`collections/edges.yaml`) — an **event** collection, one record
  per directed, typed edge. The predicate is a closed enum: `works_at`,
  `invested_in`, `founded`, `advises`, `attended`, `mentions`, `image_of`,
  `wikilink_basename`. A new `edges` collection is used rather than overloading
  the semantic `relations` collection — zero-LLM structural links are a
  different concept from model-inferred relations.

Traversability is **not** something these files opt into. The graph reads
`content->>'subject'`, `'object'`, and `'predicate'` directly, and requires only
that collections literally named `pages` and `edges` exist in the active
catalog — the names and those content keys are a runtime contract, not an
authoring choice. Declaring `subject`, `object`, and `predicate` as filterable
`fields` buys something else: ordinary `where` filters in searches and views,
which nothing in this catalog uses yet.

### Deterministic derivations — no model calls

Two write-triggered derivations run on **every page write** with
`max_llm_calls: 0`:

- **`link_extraction.yaml`** uses the `extract_relations` Task to resolve
  markdown links, bare `dir/slug` references, and `[[wikilinks]]`, classify them
  into typed predicates, and emit `edges`. This is gbrain's headline
  "self-wiring graph on every save" reproduced without a model in the loop.
- **`fact_extraction.yaml`** uses the `extract_facts` Task to fold every page's
  `## Facts` heading into one bounded, current `page_facts` index (`keys:
  [page_facts]`, `complete: true`), capped at 100 facts of 80 chars each.

Because they are deterministic, the graph, the orphan report, and the fact index
are fully alive even under `LLM_FAKE=1`.

### The dream cycle — bounded, cited LLM derivations

Write signals cascade through five model-backed derivations, each declaring
tight `limits` and requiring citations on everything it emits:

| Derivation | Triggered by | Emits |
|---|---|---|
| `atom_extraction` | new `transcripts` | append-only cited `atoms` |
| `pattern_detection` | new `edges` / `atoms` | append-only cited `patterns` (≥2 citations each) |
| `concept_synthesis` | new `atoms` / `patterns` | one bounded, current `concept_index` (≤12 themes) |
| `consolidate` | new `atoms` / `patterns` / `facts` | one bounded, current `take_index` (≤12 conclusions) |
| `enrich_thin` | a thin, un-enriched `page` | rewrites that one page in place, cites its sources |

The concept and take indexes are **static-key replacements** (`keys:
[concept_index]` / `[take_index]`, `max_records: 1`): each run replaces the
single current index rather than appending, and returns `{"records":[]}` when no
change is justified. `enrich_thin` is guarded with `ignore_own_outputs: true`
and a `where: {gbrain_enriched: {exists: false}}` clause so it never loops on
its own rewrites.

### Views, not endpoints

The graph is **never** reached through a bespoke route. Three named views
(declared in `views/`) carry every read-side capability through Memseek's
generic view contract, queried at `POST /views/<name>/query`:

- **`graph_query`** (`kind: graph`) — bounded traversal from a seed page, by
  direction, depth, and predicate filter; returns paths and cited edges.
- **`orphan_pages`** (`kind: graph_orphans`) — current pages with no live
  incoming or outgoing edge.
- **`gbrain_search`** (`kind: search`) — hybrid retrieval across the memory
  collections.

The two graph views bind `edges` (and `pages` for orphan reporting) explicitly.
Those names and gbrain's predicate enum belong to this example catalog; the
runtime supports other edge collections, field mappings, and predicate
vocabularies through the general [Graph data](graph-data.md) contract.

Cited synthesis rides the general `POST /answer` capability (rewrite, anchored
graph boost, gaps, `save: true`), and a `gbrain_context` artifact renders a
bounded per-entity dossier (pages + concepts + takes).

One asymmetry is worth knowing: `save: true` writes its synthesis under the
literal entity **`answer`**, keyed by a hash of the question and window — not
under the run's isolated entity. That is deliberate de-duplication (re-asking a
question supersedes the previous answer rather than piling up copies), but it
means saved answers are the one surface that *does* carry across runs. It is
also why `repair_synthesis` pairs `stale_citations` with
`cron: {entities: any}`.

### One package, one MCP surface, one retention job

`packages/gbrain.yaml` binds it all together: the nine collections, the ten
processors and their triggers, the three views, the artifact, the search
profile, an MCP declaration (`mcp/gbrain.yaml`), and a trusted daily
`purge_pages` retention job that permanently erases page tombstones after 30
days, 25 pages at a time. The MCP surface exposes six read-only tools —
`answer`, `search_memory`, `explore_graph`, `find_orphan_pages`, `context`, and
`record` — and is what `examples/pydantic_ai_mcp_showcase.py` connects to.

## What you can do with it

After the opening tour, an interactive prompt (`gbrain ▸`) accepts:

```text
  graph [seed]       traverse the named graph_query view (default: people/maya)
  orphans            list pages with no incoming or outgoing edge
  facts              show the deterministic, current page_facts array
  concepts           show the bounded, current concept_index array
  takes              show the bounded, current take_index array
  search <query>     hybrid retrieval across all memory collections + graph boost
  answer <question>  cited synthesis with query rewrite, graph context, saved result
  remember <text>    store a transcript line and watch atom extraction run
  atoms              list the append-only cited atoms
  patterns           list the append-only cited recurring patterns
  status             redisplay every surface at once
  help / quit
```

`remember` is the most instructive: storing one transcript line triggers atom
extraction, which in turn can drive a fresh concept-index and take-index update
— you watch the cascade land, run by run, each output cited. `answer`
demonstrates that the same evidence the graph and search expose is what the
synthesis is grounded in.

Piping anything to stdin runs a short **scripted tour** (`graph`, `orphans`,
`facts`, `concepts`, `takes`, a search, and — with a real model — an answer)
and exits, which doubles as a quick smoke walkthrough once a stack and worker
are running.

## Run it yourself

Start PostgreSQL, the API, and the worker (see the
[programmer quickstart](getting-started.md)), then:

```console
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
export OPENAI_API_KEY=sk-...     # the variable conf/models.yaml names in api_key_env
make database
uv run memseek migrate
uv run uvicorn memseek.api:app &                 # terminal A
uv run memseek worker &                          # terminal B
uv run python examples/gbrain_showcase.py        # terminal C
```

Set `MEMSEEK_API_KEY` to use an existing workspace, or `DATABASE_URL` to have
the script create a fresh disposable one. On startup it publishes
`examples/gbrain_catalog` as `gbrain@0.13.0` into the workspace and verifies the
package activated before writing anything; each run seeds a **freshly isolated
entity**, so repeated runs never collide — with the single exception of saved
`answer` syntheses, which are keyed by question under a shared entity.

Prefer `DATABASE_URL` when you are also **editing** the catalog. Publishing
checks every existing record's stored `(collection, version, definition hash)`
against the incoming catalog, so changing a collection file in place makes the
upload incompatible with a workspace that already holds records of that
collection — a deliberate `409 catalog_incompatible`. A fresh disposable
workspace has nothing to migrate.

**For the full experience, use a real provider.** `answer` and atom extraction
need a model that can produce citation UUIDs. Point the aliases in
`examples/gbrain_catalog/conf/models.yaml` at models your account can call, as
in the [real-LLM skill maintenance walkthrough](skill-maintenance.md).

**Or run it offline** with `LLM_FAKE=1`. The deterministic surfaces — the graph,
the orphan report, the fact index, and hybrid retrieval — are fully alive, so
the run still demonstrates real structure. The model-backed surfaces (atoms,
patterns, concepts, takes, and `answer`) return empty rather than inventing
uncited content: that is the citation contract doing its job, not a bug.

> **One rule:** the API and the worker must run in the **same** provider mode.
> Records are embedded by the worker and queries by the API; mixing a real
> provider on one side with `LLM_FAKE=1` on the other makes search meaningless.

## The one idea to take away

The showcase is a demonstration that a distinctive, graph-shaped memory product
can be **entirely a catalog** on top of Memseek: keyed pages and typed edges,
deterministic and model-backed derivations, named views, an artifact, an MCP
surface, and a retention job — all declared in YAML, all bounded, all cited,
adding not a single line of graph-specific HTTP surface. Your application writes
pages and transcripts and reads views; Memseek is the memory.
