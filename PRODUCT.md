# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary (for customer-facing surfaces):** technical prospects evaluating memseek as memory
infrastructure — engineering leaders, technical founders, and senior developers building
long-running AI agents. They arrive skeptical, having already tried a vector store or a
hand-rolled RAG stack and hit the wall where "storing text" stops behaving like memory. They
are deciding whether memseek can carry a *real product*, not just a demo.

**Secondary:** developers authoring memseek catalogs (YAML) and integrating the SDK, HTTP API,
or MCP surface.

## Product Purpose

memseek is **memory for AI agents**. It turns the records an application already has — CRM
entries, calls, email, transcripts, documents, pages — into the context an agent actually needs:
the few facts that matter right now, current, dated, and traceable to their source. Nothing is
ever overwritten, so what changed and why is always recoverable.

Success is an agent that recalls the *right* thing, quotes the *current* thing, and can show
where it got it.

## Positioning

Not a database, not a vector store, not another RAG wrapper. The mechanism a neighboring product
could not truthfully copy:

- **Context selection over context volume.** Retrieval finds what looks similar; memseek resolves
  what is true *now*, across sources and across time.
- **Memory systems are declared, not built.** Collections, derivations, triggers, views,
  artifacts, packages, retention, and an MCP surface are all YAML over immutable records. Whole
  knowledge products ship as a catalog, with no bespoke services or graph database.
- **Provenance is structural, not bolted on.** Every record names its parents (`derived_from`);
  every derived belief cites the evidence it stands on, or it is rejected.

## Operating Context

Developers author a catalog of YAML definitions, publish it as a versioned package into a
workspace, and run the API plus a worker against PostgreSQL. Applications write records and read
views through the Python SDK, the HTTP API, or the compiled MCP tool surface. Enrichment is
asynchronous and worker-owned; records become `ready` once required processors have run.

## Capabilities and Constraints

Confirmed, present in the repository:

- **Records.** Immutable and append-only. `keyed` collections hold one current version per key
  (rewrites supersede); `event`/`mixed` collections append. Every record carries `derived_from`
  provenance and a computed lineage `depth`.
- **Derivations.** Write-triggered, source-scoped pipelines declared in YAML. Every one declares
  hard limits (`max_tasks`, `max_llm_calls`, `max_retrieved_records`, `max_visible_records`,
  `max_total_tokens`, `max_wall_s`). Emission can be append-only or a bounded static-key
  replacement, and can require citations on every emitted record.
- **Deterministic task adapters.** `extract_relations` and `extract_facts` run with
  `max_llm_calls: 0` — structural extraction with no model in the loop.
- **Named views.** Kinds include `search`, `graph`, and `graph_orphans`, all queried through the
  generic `POST /views/<name>/query` route. There is deliberately **no graph-specific endpoint**.
- **Retrieval.** Hybrid (semantic + keyword) search with a portable rank AST, search profiles,
  and optional anchored graph-distance boosts.
- **Synthesis.** `POST /answer` returns a bounded, citation-visible answer with optional query
  rewrite, anchored graph context, declared gaps, and `save: true` provenance writes.
- **Artifacts.** Deterministic prompt assembly from token-budgeted named blocks, each render
  recording an exact input manifest and content hash.
- **Feedback loop.** A render can be bound to an **artifact use**: a small handle carrying artifact
  identity, content hash, and the exact promoted version of the maintained value it declared as its
  learning target. The application stores one ID beside its own result and later submits the
  outcomes worth learning from, which land as ordinary `learning_signals` records that a reviewed
  derivation consumes. Deliberately *not* observability: a use holds no prompt, response, tool call,
  token count, or span, expires on a retention setting, and never promotes anything on its own.
  OpenTelemetry correlation is a bounded scalar attribute map and an optional dependency.
- **Governance.** Point-in-time belief reconstruction via version history, contradiction
  detection, recursive-closure erase (right to be forgotten), and scheduled retention jobs.
- **MCP.** A package may declare a tool surface compiled from its own views, artifacts, and
  endpoints; served over a stdio bridge with authenticated discovery.
- **Offline mode.** `LLM_FAKE=1` keeps deterministic surfaces fully live while model-backed
  derivations emit nothing rather than invent uncited content. This is the citation contract
  working, not a degraded mode.
- **Constraint.** The API and worker must run in the same provider mode; mixing a real provider
  with `LLM_FAKE=1` makes search meaningless.

**The gbrain example catalog** (`examples/gbrain_catalog/`, published as `gbrain@0.13.0`) is a
complete worked instance: 9 collections, 8 derivations, 3 views, 1 artifact, 6 MCP tools, and 1
retention job. It re-expresses the distinctive capabilities of Garry Tan's open-source gbrain on
memseek's substrate. It is opt-in; the default catalog exposes none of its surfaces.

## Brand Commitments

- **Name:** memseek, set lowercase. Descriptor: "ai memory infrastructure".
- **Established visual world** (binding; incumbent authority is `marketing/index-v3.html`):
  dark teal-black ground `#070D10` with a light sage-paper counterpart `#EEF0EA`; amber
  `#F2A83B` as the single accent; green `#4FD68A` and rose `#F0688C` as semantic signals only.
  Iowan Old Style serif for headings with italic amber emphasis; monospace for labels, stamps,
  buttons, and code; system sans for body. A **ledger grammar**: numbered `entry NN` section
  stamps, cited sources, dated facts, "as of then". Film-grain overlay. Both light and dark
  themes are first-class, with an explicit toggle.
- **Voice:** concrete, evidence-first, anti-hype. Claims are demonstrated rather than asserted.
  "Shows its work" is the recurring promise.
- **Primary CTA:** "Start building".

## Evidence on Hand

Real and usable:

- `examples/gbrain_catalog/` — the actual YAML definitions quoted on customer-facing surfaces.
- `examples/gbrain_showcase.py` — a runnable interactive walkthrough seeding an isolated entity.
- `docs/gbrain-showcase.md`, `docs/gbrain-plan.md` — the written walkthrough and build log.
- `marketing/index-v3.html` — benchmark claims (LongMemEval-S, MuSiQue, 2WikiMultiHopQA, and a
  ~32× context-token reduction) with their exact figures; treat that file as the source of truth
  for any benchmark number reused elsewhere.
- The gbrain demo corpus is real and checked in: five pages (`people/maya`, `companies/acme`,
  `people/nora`, `companies/orbit`, and the deliberate orphan `notes/unfiled`), their declared
  facts, and one seed transcript.

Absences that must not be fabricated: **no customer names, logos, testimonials, case studies,
pricing, licensing, SLA, or uptime claims exist.** No captured real-provider run output for the
gbrain showcase exists yet — model-written answer, atom, concept, and take text shown on
customer surfaces is representative of the real corpus and must be presented as illustrative,
never as a captured benchmark result.

## Product Principles

1. **Show the work.** Every served fact stays attached to a dated, openable source. A conclusion
   that cannot cite its evidence is rejected, not softened.
2. **Declare, don't build.** Capability arrives as YAML over a generic substrate. Adding a
   feature must not add a bespoke endpoint.
3. **Bounded by construction.** Every derivation caps its tasks, tokens, rows, and wall-clock, so
   a runaway model is a config error rather than a bill.
4. **Deterministic before probabilistic.** Structure that can be extracted without a model is
   extracted without a model.
5. **Never invent.** With no evidence, the honest output is empty. Absence is reported, not
   filled.

## Accessibility & Inclusion

No product-specific standard has been established, but the incumbent customer-facing surface sets
the working bar and must not regress: skip link, visible `:focus-visible` outlines, full
`prefers-reduced-motion` support, and first-class light and dark themes.
