---
title: Memseek vs. Cognee
eyebrow: Comparison
---

# Memseek vs. Cognee

A comparison of **Memseek** (this repository) and **[Cognee](https://docs.cognee.ai/)**,
two systems for giving AI agents durable memory. Both let an application ingest information,
enrich it with an LLM, and retrieve it later — but they make almost opposite bets about *what
memory is* and *what matters most about it*.

> Cognee facts here are drawn from its public docs, GitHub, and third-party write-ups
> (sources at the bottom). Memseek facts are drawn from this repository
> (`README.md`, `CONTEXT.md`, and the v3.2 spec). Cognee is a
> mature, widely adopted open-source project; Memseek is a spec-driven implementation currently at
> the M6 + M7-slice milestone. Weigh the analysis with that asymmetry in mind.

---

## One-line positioning

- **Cognee** — an open-source **AI memory platform** that turns raw documents into a
  **self-improving knowledge graph** (entity/relationship triplets) plus vectors, and exposes
  many retrieval modes including graph traversal. Optimizes for *recall quality and reasoning over
  connected knowledge*.
- **Memseek** — an async **agentic data substrate**: immutable, typed, versioned records with
  mandatory provenance, audited derivations, deterministic artifact rendering, and
  provenance-aware erasure. Optimizes for *correctness, auditability, and governance* — a durable
  control plane an application builds on.

Put crudely: **Cognee is a smart, evolving brain; Memseek is a system of record with a
memory-shaped API.**

---

## At a glance

| Dimension | Memseek | Cognee |
|---|---|---|
| **Core abstraction** | Immutable records in typed, versioned collections; keyed current state + event log | Knowledge graph of entity/relationship triplets + vectors |
| **Pipeline** | Ingest → enrich (processors) → derive (Pipelines/Tasks) → search / views / artifacts | `add` → `cognify` → `memify` → `search` (ECL) |
| **Authoring model** | Declarative **YAML** catalog (collections, processors, derivations, views, artifacts, triggers), strictly validated | **Code-first** Python tasks & pipelines; SDK defaults |
| **Canonical store** | PostgreSQL (canonical) + pgvector; optional Turbopuffer as a *disposable* projection | Postgres default; also Neo4j, Neptune, Kuzu (graph) |
| **Vector store** | pgvector; Turbopuffer optional | pgvector, LanceDB, Qdrant, Chroma, Weaviate, Milvus |
| **Retrieval** | Typed `SearchSpec`: text / hybrid / recent / structured, RRF fusion, named views; results are **re-ranked against canonical rows** | ~14 modes incl. graph traversal & chain-of-thought; **auto-routing** picks a strategy |
| **Memory evolution** | Explicit, audited **derivations**; accumulation via cited re-derivation; nothing mutates silently | **`memify`** self-improvement loop: prunes, reweights, adds derived facts automatically |
| **Provenance** | **Mandatory citations**; every derivation writes an audited `_system/run`; deterministic replay | Audit trails / traceability (less central to the model) |
| **Deletion / GDPR** | `POST /erase`: transitive provenance closure, job fencing, hash-only audit record | `Forget` / delete operation |
| **Determinism** | Strong: content hashes, deterministic artifact rendering, no LLM call in renderers | Lower by design; graph evolves and LLM-driven extraction dominates |
| **Multimodal** | Text + typed JSON content | Text, files, images, audio (30+ sources) |
| **Maturity** | Early; single-language, spec milestone implementation | Mature: ~29k★, v1.x, Cloud offering, Rust/TS clients |
| **License / model** | (in-repo; not stated as OSS) | Apache 2.0 + managed Cognee Cloud |

---

## Similarities

1. **Same job to be done.** Both give agents persistent, long-term memory that survives across
   sessions, beyond a single context window.
2. **Ingest → enrich → retrieve pipeline.** Both take raw input, run it through LLM-driven
   enrichment, and expose a retrieval surface. Memseek's `records → processors → derivations →
   search` mirrors Cognee's `add → cognify → memify → search`.
3. **Hybrid retrieval.** Neither relies on vector similarity alone; both blend semantic vectors
   with structured/relational signals.
4. **PostgreSQL + pgvector as the default backbone.** Both can run their whole stack on a single
   Postgres instance — no mandatory separate vector service.
5. **Pluggable LLM/embedding providers** and a self-hostable design.
6. **Explicit deletion.** Both treat "forget this" as a first-class operation, not an afterthought.
7. **Extensible processing.** Both let developers add custom processing steps (Memseek Tasks /
   Cognee tasks).

---

## Differentiators

### What Cognee does that Memseek does not
- **A real knowledge graph.** Cognee's center of gravity is entity/relationship extraction into a
  traversable graph, enabling multi-hop reasoning ("chain-of-thought over structure"). Memseek
  stores events and keyed state and has narrow relation derivations (e.g. `contradiction`), but it
  is **not** a general-purpose KG and does not do graph traversal.
- **Self-improving memory (`memify`).** Cognee automatically prunes stale nodes, reweights
  frequently used edges, and derives new facts from access patterns. Memseek deliberately does the
  opposite — nothing changes without an explicit, audited derivation.
- **Breadth of retrieval + auto-routing.** ~14 retrieval modes and a router that picks a strategy
  for you. Memseek gives fewer, strongly-typed modes and asks the author to declare intent.
- **Multimodal + many connectors.** Images, audio, 30+ data sources vs. Memseek's text/JSON.
- **Backend choice for graphs.** Neo4j, Neptune, Kuzu, and a wide vector-DB matrix.
- **Ecosystem maturity.** ~29k stars, managed Cloud, Rust/TypeScript clients, benchmark results,
  broad community.

### What Memseek does that Cognee does not (as centrally)
- **Immutability + versioning as the foundation.** Records are immutable; collections are versioned
  content schemas; there is no silent mutation and (by design) no backward-compat fudging.
- **Mandatory, machine-checked provenance.** Every derived record carries citations; every run
  writes an audited `_system/run` with config/contract hashes, read receipts, and divergence. You
  can always answer "why does memory say this, and from what?"
- **Determinism and reproducibility.** Content-hashed definitions, deterministic artifact rendering
  that makes **no LLM call** at render time, stable rendered-content hashes. Cognee's evolving,
  LLM-extracted graph is far less reproducible.
- **Provenance-aware erasure.** `/erase` computes a bounded transitive `derived_from` closure,
  fences active jobs, deletes canonical rows, refreshes projections, and writes a hash-only audit
  record — a genuinely compliance-grade delete, not just "remove the node."
- **Concurrency and consistency rigor.** Claim-token fencing, advisory locks, stale-commit
  rejection, scope-hashed replay cursors, all-or-nothing batches with idempotent dedupe.
- **Reviewed promotion.** Skill/artifact snapshots stay drafts until an explicit, atomic Promotion
  that re-checks preconditions — no self-deploying changes.
- **Declarative, reviewable catalog.** The whole behavior is YAML that can be diffed, hashed, and
  code-reviewed; the runtime cannot execute uploaded code.

### The core philosophical split
Cognee optimizes for **memory that gets smarter on its own** (accuracy, reasoning, autonomy).
Memseek optimizes for **memory you can trust and audit** (determinism, provenance, governance).
That single difference explains almost every other one in the table.

---

## SWOT — Memseek

### Strengths
- **Auditability & provenance are unmatched** here: mandatory citations, audited runs, deterministic
  replay. Strong fit for regulated/enterprise settings.
- **Governance-grade erasure** with transitive provenance closure — a real GDPR/right-to-be-forgotten
  story, not a best-effort delete.
- **Determinism** (content hashes, no-LLM artifact rendering) makes behavior testable and reproducible.
- **Declarative YAML catalog** is reviewable, hashable, and safe (no arbitrary code execution);
  aligns with the "YAML-author clarity first, explicit naming" design priorities.
- **Correctness engineering**: immutability, versioning, concurrency fencing, idempotent ingest,
  bounded/no-partial responses.
- **Clear ownership boundary** — it's a substrate, not a framework that colonizes the app.

### Weaknesses
- **No knowledge graph / multi-hop reasoning.** In a market increasingly sold on "graph memory,"
  the absence of entity-relationship traversal is a visible gap.
- **Early maturity**: milestone implementation, single language, no cloud offering, no public
  adoption/benchmarks, no Rust/TS clients.
- **Narrower ingestion**: text + JSON only; no image/audio, few connectors.
- **Higher upfront authoring cost**: you design collections/processors/derivations in YAML rather
  than calling `add`/`cognify` and getting a graph for free.
- **Determinism-first means less "magic"** — no self-improving loop; the app must drive evolution.
- **Operational footprint**: Postgres + worker + migrations + strict runtime is heavier than a
  `pip install` quickstart.

### Opportunities
- **Own the "compliance-grade / auditable memory" niche** — regulated industries (finance,
  healthcare, legal) where "why did the agent believe this?" and provable erasure are requirements,
  not nice-to-haves.
- **Position as a substrate under other memory tools** (including a KG built on top) rather than a
  competitor to all of them.
- **Deterministic artifact rendering** is a differentiated primitive for reproducible agent
  prompts/skills — lean into it.
- **CRM/domain-augmentation pattern** (app owns transactional truth, Memseek owns memory) is a
  concrete, sellable wedge.
- **Graph-shaped derivations** could be added on the existing relation/citation machinery to close
  the most obvious feature gap without abandoning the provenance model.

### Threats
- **Cognee's mindshare and momentum** (~29k★, Cloud, benchmarks) set market expectations Memseek
  isn't yet meeting.
- **"Graph memory" as the default narrative** could make a non-graph substrate look dated regardless
  of its correctness advantages.
- **Managed offerings** (Cognee Cloud and others) lower adoption friction; a self-hosted-only
  substrate raises it.
- **Convergence risk**: mature platforms can bolt on audit/provenance/governance faster than an
  early project can build graph reasoning + ecosystem + adoption.
- **Category confusion**: "data substrate" is a harder sell than "AI memory for agents"; positioning
  and docs must do heavy lifting.

---

## SWOT — Cognee (for balance)

- **Strengths** — knowledge-graph reasoning, self-improving `memify` loop, many retrieval modes,
  multimodal, broad backend/connector support, strong adoption and managed Cloud.
- **Weaknesses** — evolving LLM-extracted graph is less deterministic/reproducible; provenance and
  hard-delete are less central; documented gaps in incremental updates on unstructured data,
  terabyte-scale, and (historically) TS support.
- **Opportunities** — become the default agent-memory layer; move up into enterprise with stronger
  governance/audit.
- **Threats** — commoditization of "graph RAG"; substrates that emphasize auditability/compliance
  eroding its enterprise appeal; LLM-extraction accuracy/cost pressure.

---

## When to choose which

**Choose Memseek when** you need a system of record for agent memory: provable provenance, audited
derivations, deterministic/reproducible outputs, compliance-grade erasure, strict schemas and
versioning — e.g. regulated domains, agents whose decisions must be explained, or a durable
substrate other tools sit on top of.

**Choose Cognee when** you want to stand up rich, connected memory fast: automatic
entity/relationship graph, self-improving recall, graph-traversal reasoning, multimodal ingestion,
and a mature ecosystem with a managed option — e.g. knowledge assistants and agentic RAG where
recall quality and reasoning matter more than reproducibility.

**They can also compose:** Memseek as the canonical, audited substrate; a Cognee-style knowledge
graph as a derived projection over it.

---

## Sources
- [Cognee documentation](https://docs.cognee.ai/)
- [How Cognee Builds AI Memory for Agents](https://www.cognee.ai/blog/fundamentals/how-cognee-builds-ai-memory)
- [topoteretes/cognee (GitHub)](https://github.com/topoteretes/cognee)
- [From RAG to Graphs: How Cognee is Building Self-Improving AI Memory (Memgraph)](https://memgraph.com/blog/from-rag-to-graphs-cognee-ai-memory)
- [Cognee — AI Memory with Ontologies](https://www.cognee.ai/blog/deep-dives/grounding-ai-memory)
- Memseek `README.md`, `CONTEXT.md`, and `spec/memseek-spec-v3.2-agentic-data-substrate.md`
