---
title: Implementing qmd on memseek
eyebrow: Design plan
---

# Implementing qmd on memseek — detailed plan

This plan re-expresses [Tobi Lütke's qmd](https://github.com/tobi/qmd) as memseek definitions.
qmd is a local search engine for markdown: it indexes files by path and glob into SQLite, chunks
them, and answers queries through a three-stage hybrid pipeline — parallel BM25 and vector
retrieval, Reciprocal Rank Fusion, then LLM re-ranking — with local GGUF models, an MCP server,
and a benchmark harness.

It is a good test of memseek precisely because it is *not* a memory system. qmd has no
provenance, no derived state, no notion of a belief that supersedes another. It is a retrieval
engine and nothing else, which means it exercises exactly the part of memseek that competes with
purpose-built search infrastructure, with none of the substrate's usual advantages in play.

The answer is more favorable than expected. qmd's retrieval architecture maps onto memseek's
existing multi-source search almost one-for-one, because memseek already has weighted
Reciprocal Rank Fusion, native LLM re-ranking, PostgreSQL full-text ranking, pgvector HNSW,
named parameterized views, and a declarative MCP surface. Three things provably cannot be
expressed in YAML today, and they are named exactly in the [gap tables](#5-gap-analysis).

**This document is a design, not an implementation.** Nothing described here has been built.

## 1. The one structural insight

qmd's schema makes a choice that is easy to miss and carries the entire mapping:

> **BM25 runs over whole documents. Vectors run over chunks.**

Its `documents_fts` FTS5 index covers full document text, while `content_vectors` holds ~900-token
chunks with their own embeddings. RRF then fuses a document-ranked list with a chunk-ranked list,
folding chunk hits back onto their parent document.

That is a *multi-source query*, which memseek already has as a first-class concept: one source in
`mode: text` over a document collection, one source in `mode: vector` over a chunk collection,
merged with `fuse: {kind: rrf, rank_constant: 60}` and per-source `weight`, then re-ranked with
`rerank: {backend: llm_judge}`. No new engine stage is required to express qmd's pipeline shape.

## 2. Two design moves that avoid core changes

### Chunks are records

memseek stores exactly one embedding per record and has no chunk table. The naive reading is that
chunk-level retrieval is therefore impossible without schema work. It is not.

Make the chunk a record. A `chunks` collection with `required_processors: [embedding_v1]` gets one
vector per chunk from the existing embedding processor, with no changes anywhere. Each chunk
carries `derived_from: [<doc record id>]`, which is a client-suppliable field on ingest — see the
ingest item model in `src/memseek/records.py` (line 57), validated for
duplicate parents, `MAX_DERIVED_FROM`, provenance depth, and cycles.

So chunk-level vector search arrives with a real provenance edge back to its document, and
`POST /erase` on a document reaches its chunks through the ordinary closure. qmd has no equivalent
— deleting a document there is a cache eviction, not a provenance operation.

The corollary matters: the `docs` collection must declare **no** `required_processors`. The whole
document is the BM25 unit, not a vector unit, and running the embedding processor over it would
push a long file through `truncate_middle` into a single lossy vector. Skipping it also means
document rows are `ready` on insert, so text search sees them immediately.

### Prefix matching without a prefix operator

`PREDICATE_OPERATORS` in `src/memseek/search/spec.py` (line 35)
is exactly:

```python
{"eq", "in", "gt", "gte", "lt", "lte", "exists", "contains_any", "contains_all"}
```

There is no `prefix`, `like`, or `glob`. That appears to rule out `qmd ls notes/work` and
collection-scoped search.

It does not, given one modelling choice: have each document declare a `path_prefixes` array field
holding every ancestor path plus its own.

```yaml
# docs/api/auth.md becomes:
path_prefixes: ["docs", "docs/api", "docs/api/auth.md"]
```

`{path_prefixes: {contains_any: ["docs/api"]}}` is then a true prefix query against a declared,
filterable, indexed field. This covers `ls`, subtree-scoped search, and the context tree.

What it does not cover is genuine glob — `journals/2025-05*.md`. That stays client-side: resolve
the subtree with a prefix query, then `fnmatch` the returned paths. Acceptable for a CLI, and the
[gap table](#requires-extending-memseek) records the operator that would fix it properly.

## 3. Sketch: `examples/qmd_catalog/`

Self-contained, following the [gbrain catalog](gbrain-catalog.md) precedent of shipping its own
`conf/rank_default.yaml` and `conf/search_profiles.yaml` rather than inheriting the repository's
bootstrap catalog.

### Collections

| Collection | Mode | Key | Purpose |
|---|---|---|---|
| `docs@1` | `keyed` | `<source>/<relpath>` | One record per indexed file. The BM25 unit. |
| `chunks@1` | `keyed` | `<source>/<relpath>#<seq>` | ~900-token chunks. The vector unit. |
| `contexts@1` | `keyed` | the path prefix | qmd's context tree. |

`docs` content carries `text` (the full body), `title`, `path`, `qmd_uri`, `source`, `ext`,
`content_hash`, `docid`, `bytes`, `lines`, and `path_prefixes`. Declared `fields:` expose `path`,
`source`, `ext`, `docid`, `content_hash`, and `title` as filterable and projectable scalars, plus
`path_prefixes: {type: [string], filter: true, project: true}` for the prefix trick.

`chunks` content carries `text`, `path`, `seq`, `start_char`, `heading_path`, `doc_key`, and
`path_prefixes` — `heading_path` so a hit can be reported as
`ranking.md › Fusion › Position bonuses` rather than a bare offset.

Because both collections are `keyed`, re-indexing a changed file writes a new version of the same
key and the old one becomes non-current. `GET /document/history` then yields per-path version
history for free, and a deleted file is a `retract: true` tombstone rather than a silent row
disappearance. qmd has neither.

### Views — the qmd command surface

| View | Shape | qmd command |
|---|---|---|
| `qmd_query@1` | Multi-source: `lexical` (`mode: text`, `[docs]`) + `semantic` (`mode: vector`, `[chunks]`); `fuse: {kind: rrf, rank_constant: 60}`; `rerank: {backend: llm_judge, top_n: 20}` | `qmd query` |
| `qmd_search@1` | `mode: text` over `[docs]` | `qmd search` |
| `qmd_vsearch@1` | `mode: vector` over `[chunks]` | `qmd vsearch` |
| `qmd_get@1` | `mode: structured`, `where: {path: {eq: "{{path}}"}}` | `qmd get` |
| `qmd_multi_get@1` | `mode: structured`, `where: {path: {in: "{{paths}}"}}` | `qmd multi-get` |
| `qmd_ls@1` | `mode: structured`, `where: {path_prefixes: {contains_any: ["{{prefix}}"]}}`, `order_by: [{field: path, direction: asc}]` | `qmd ls` |
| `qmd_context@1` | `mode: structured` over `[contexts]`, same prefix predicate | `qmd context list` |

An `mcp/qmd.yaml` declaration exposes these against the four available tool kinds — `view`,
`artifact`, `answer`, and `record`, per
`src/memseek/definitions/models.py` (line 713) — which
covers qmd's MCP surface (`query`, `get`, `multi_get`, `status`) and adds cited answers, which qmd
has no equivalent for. A `qmd_dossier@1` prompt artifact composes the resolved context tree with
top hits, mirroring qmd's habit of returning path context alongside results so a client model can
disambiguate. The package manifest binds exact versions and declares a `retentions:` entry
standing in for `qmd cleanup`.

### Three constraints worth recording now

1. **A `cheap` model alias is mandatory.** `llm_judge` re-ranking is rejected without one — see
   `src/memseek/search/engine.py` (line 417). Easy to omit in
   a catalog that otherwise needs no generative model.
2. **`conf/rank_default.yaml` must declare exactly `hybrid`, `vector`, `text`, and `recent`.**
   qmd's should be *relevance-only* — `[normalize, [text_match]]`, `[normalize, [similarity]]`,
   and their `max` for hybrid. This is a deliberate contrast with the
   [gbrain catalog](gbrain-catalog.md), whose ranks blend importance and recency decay. qmd has no
   salience term at all: a note is not more relevant for being recent or important, only for
   matching. Encoding that as a rank expression rather than as engine behavior is the point.
3. **One open question.** Whether an array-typed view parameter templates cleanly into
   `{path: {in: "{{paths}}"}}`. `_EXACT_TEMPLATE_RE` in
   `src/memseek/search/spec.py` (line 38) permits an exact
   `{{param}}` substitution to resolve to a non-string, which suggests yes, but it needs verifying
   against a real view load. If it does not hold, `multi-get` degrades to N `qmd_get` calls.

## 4. Sketch: `examples/qmd_cli.py`

A `MemseekClient` driver in the style of `examples/gbrain_showcase.py` — runnable instructions in
the module docstring, `NO_COLOR`-aware styling, publishes its own catalog before writing records.

Its config file mirrors qmd's `index.yml` field-for-field (`path`, `pattern`, `ignore`, `update`,
`includeByDefault`, `context`, `global_context`, `editor_uri`) so the two are legible side by
side, and backs the `collection` and `context` subcommands.

`update` walks the glob, honors `ignore` plus qmd's built-ins, runs the collection's `update`
command, skips files whose content hash is unchanged, then ingests each document with its chunks.
Files that have vanished are retracted.

The one component worth specifying precisely is the **chunker**, a faithful port of qmd's
deterministic algorithm: ~900-token target, 15% overlap, a 200-token lookback window, and
squared-distance decay over boundary scores of H1 = 100, H2–H6 = 90 → 50, code fence = 80,
horizontal rule = 60, blank line = 20, list item = 5, line break = 1. It is pure, portable, and
unit-testable with no database, which makes it the natural first piece to build.

`search` and `vsearch` map straight onto their views. `query` is the exception: it must expand the
query client-side, issue one search per variant, and fuse the results itself with the original
weighted `2.0`. The reason is in the gap table below, and it is the single most interesting finding
in this exercise.

The remainder — `get` with `#docid` and `:from:count` line ranges, `multi-get`, `ls`, `status`,
`doctor`, `cleanup`, `bench` reporting precision@k, recall, MRR and F1 against a fixture in qmd's
JSON format, `--explain`, `--format`, and OSC 8 editor hyperlinks — is ordinary client code over
surfaces that already exist.

## 5. Gap analysis

### Works today, no core change

| qmd feature | memseek mechanism |
|---|---|
| Weighted RRF fusion | Native `sources[].weight` + `fuse: {kind: rrf, rank_constant: 60}` |
| LLM re-ranking | Native `rerank: {backend: llm_judge}` — `RerankSpec`, `spec.py` line 120 |
| Vector search | pgvector HNSW cosine — `migrations/001_init.sql` lines 24 and 77 |
| Incremental re-index by content hash | `dedupe_key` + keyed versions; `/document/history` adds per-path version history qmd lacks |
| Chunk → document provenance | Client-supplied `derived_from` — `records.py` line 57 |
| `get` by path or docid | `mode: structured` with `where: {path: {eq}}` |
| `ls` and subtree-scoped search | `path_prefixes` ancestor array + `contains_any` |
| Line ranges (`file.md:50 -l 100`) | Client-side slicing of returned text |
| Context tree | `contexts` collection; longest prefix selected client-side |
| MCP server | `mcp/*.yaml` + authenticated `/mcp` Streamable HTTP or `memseek mcp` stdio |
| `cleanup` | `POST /erase` + package `retentions:` |
| `bench`, `doctor` | Pure example code |
| SDK usage | `MemseekClient` |

### Works today, but differs

| qmd feature | memseek today | Delta |
|---|---|---|
| BM25 (SQLite FTS5) | `websearch_to_tsquery` + `ts_rank_cd` over a GIN index — `search/pg.py` lines 93–102, `001_init.sql` line 79 | The same recall channel with different scores. `ts_rank_cd` is coverage density, not BM25 with `k1`/`b` tuning |
| Chunking | One vector per record; chunk-as-record recovers the behavior | Results are right, but chunking lives in *application code* rather than declared YAML — see the `chunk` Task below |
| Offline operation | `openai_compat` pointed at Ollama or llama.cpp | Genuinely offline in server mode; no in-process GGUF, so no single-binary story |
| `--explain` | `include: [scores]`, per-hit `source_ranks`, `GET /rank/schema` | Coarser than qmd's per-backend breakdown |

### Requires extending memseek

| qmd feature | Blocker | Change needed |
|---|---|---|
| Expansion fusing 3 queries × 2 backends into 6 lists | `SearchSource` (`spec.py` lines 177–186) has no `q` field. All sources share one top-level `q`, which is *required* when any source uses text, vector, or hybrid | A per-source `q` override, or an `expand: {model, variants}` block on `SearchSpec`. Until then fan-out and fusion happen in the client, which weakens the canonical-ranking guarantee: memseek no longer computed the ranking it is returning |
| Glob path filtering | No `prefix`/`like`/`glob` in `PREDICATE_OPERATORS` (`spec.py` line 35) | Add a `prefix` operator in `spec.py` and `search/pg.py`. Small and contained |
| Declarative chunking | No chunking primitive | Register a deterministic `chunk` Task adapter beside `llm`, `search`, and `template` (`derive/tasks.py` line 215), so a `derivations/chunk_docs.yaml` emits chunk records. No schema change — chunks stay ordinary records with one vector each. This is the highest-value item on the list |
| AST-aware chunking for code | — | The same `chunk` Task with `with: {strategy: auto}`, plus a tree-sitter dependency |
| `--candidate-limit 40` | `rerank.top_n` is capped at 20 (`spec.py` line 124) | Raise the cap |
| Cross-encoder re-ranker | Only `llm_judge` exists | A `cross_encoder` rerank backend |
| Position-aware blending (75/25 → 60/40 → 40/60) and top-rank bonuses | `boost` is post-fusion but not rank-position-aware | Rank-position terms in fusion and blending |
| True BM25 scores | PostgreSQL has no native BM25 | ParadeDB / `pg_search`, or computed tf-idf, plus a `bm25` operator in `search/rank.py` |
| 768-dim local embedding models | `vector(1536)` is hardcoded — `001_init.sql` line 24 | A configurable dimension and a migration. The existing `embedding_space` column already anticipates multiple spaces, so the model is half-built |
| Search-time `llm_cache` | No read-through cache for expansion or re-ranking | Minor; a cache table |

### Out of scope by architecture

qmd is a single local binary over SQLite. memseek is a service over PostgreSQL. `qmd init` and a
project-local `.qmd/index.sqlite` have no analog beyond "use a separate workspace", and should not
get one. The same goes for automatic GGUF downloads from HuggingFace and GPU backend detection:
these are properties of shipping an on-device binary, not gaps in a substrate.

## 6. The other direction

A comparison that only counts qmd's features would be dishonest, because most of what memseek
brings is absent from qmd by design:

- **Provenance.** Every chunk cites its document. Erasure follows the closure.
- **History.** `GET /document/history` returns every version of a path, including tombstones. In
  qmd, re-indexing destroys the prior state.
- **Retention as policy.** A declared `retentions:` block, rather than a `cleanup` command someone
  has to remember to run.
- **Cited answers.** A first-class `answer` tool kind whose claims carry record IDs.
- **Audit.** Every derivation writes a `_system/run` record with config hashes, model usage, and
  input IDs.
- **Multi-tenancy.** Workspaces with per-workspace catalogs and bearer authentication.
- **Derived state.** Triggers that build new records from ingested ones — the entire capability
  qmd does not attempt, and the reason a qmd built this way could grow a fact index or a concept
  graph without changing its retrieval path.

The honest summary: memseek can host qmd's retrieval architecture today with two modelling tricks
and no core changes, will rank differently because PostgreSQL full-text is not BM25, cannot fuse an
expanded query server-side until `SearchSource` accepts its own `q`, and brings a substrate qmd
deliberately does without.

## 7. If we build it

A follow-up session would create `examples/qmd_catalog/**` (three collections, seven views, one
artifact, an MCP declaration, a package manifest, and the four `conf/` files), `examples/qmd_cli.py`,
a small markdown corpus with a `bench.json` fixture in qmd's format, and
`tests/test_qmd_chunker.py`. Two shared files would be touched additively: `tests/conftest.py` for
a `qmd_settings` fixture copying the `gbrain_settings` pattern, and `tests/test_definitions.py` for
a self-contained-package assertion mirroring
`test_gbrain_catalog_is_a_separate_self_contained_package`.

Nothing under `src/` would change. The extensions in the
[third gap table](#requires-extending-memseek) are separate proposals, and the `chunk` Task adapter
is the one worth doing first — it turns the only piece of this design that lives in application
code into declared YAML, which is the whole claim memseek makes about itself.
