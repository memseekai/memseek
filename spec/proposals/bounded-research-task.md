# Proposal: Bounded `research` Task for deep-research derivations

- **Status:** Draft for review
- **Date:** 2026-07-24
- **Author:** jdc
- **Affects:** spec §10 (Pipelines/Tasks), non-goals (§ lines 21, 318), `DECISIONS.md`
- **Type:** Design proposal — no code, storage, or wire changes in this document

---

## Summary

Add a new **installed Task Adapter, `use: research`**, that lets a derivation run a
**bounded, sandboxed, read-only tool loop** in which the model itself decides what to
consult next — search, re-search, read a named view, follow a graph edge — until it has
enough cited evidence, then emits records through the ordinary `emit` boundary.

It is **not a new engine.** It occupies one Task slot inside an otherwise ordinary
pipeline. Everything around it — driving source, `emit`, Candidate Set, commit, triggers,
erasure, budgets, provenance/citation machinery — is unchanged.

---

## Motivation

Today a derivation is a **statically author-wired DAG**: the author declares every
retrieval up front and the model only fills prompt slots. That is sufficient for
`harvest` / `profile` / `reflection`, but it cannot express a **deep-research**
derivation, where good output requires the model to *decide what to look up based on what
it just learned* and iterate.

The framing "we need more than a simple prompt" is already half-true: derivations are
multi-step pipelines, not prompts (`reflection.yaml` is `llm → search(foreach) → llm`).
The missing capability is specifically **model-chosen, multi-hop retrieval** under
provenance guarantees.

---

## Background: why this fits without weakening guarantees

- **The Task sandbox already has the capabilities.** A Task's `TaskContext` already
  exposes `search`, `traverse` (graph edges), `answer`, and `complete_json`
  (`src/memseek/derive/runner.py:810-886`). Deployments already add trusted Python Tasks
  via the `register_task` seam (`src/memseek/derive/tasks.py:141`); YAML references them by
  name and a Task never receives a DB connection or record writer.
- **A bounded research flow already exists.** `src/memseek/answer.py` (`POST /answer`)
  does rewrite → search → traverse → one cited call, and explicitly "is not a second
  pipeline runner." This proposal generalizes that shape into a reusable Task.
- **Provenance does not depend on static control flow.** It depends on: (a) a closed
  bounded *scope*, (b) hard budget caps, (c) a complete recorded *trace*, and (d) citation
  authority enforced at `emit`. A `search` Task already introduces record IDs
  *dynamically* and folds them into `model_visible_ids` + citation visibility
  (`src/memseek/derive/provenance.py:242-283`). A research loop is the same accounting with
  the *order and selection* of retrievals chosen by the model.

### The one honest trade

A statically-inspectable **plan** becomes a dynamically-inspectable **trace** plus a
statically-bounded **scope and budget**. The spec already abandoned deterministic LLM
replay (line 355), so this is consistent — but the blanket "no tool loop" non-goal
(lines 21, 318) must be narrowed to say so.

---

## Proposal

### Authoring shape

```yaml
- id: investigate
  use: research
  with:
    objective: |                     # trusted instruction; {{...}} refs allowed
      Research what {{entity}} has been avoiding and why, using memories,
      prior reflections, and the relations graph. Cite decisive UUIDs.
    tools:                           # declared allowlist — the closed scope
      search:
        scope: {collections: [main, reflections], types: [event, chat, observation, reflection]}
        modes: [hybrid, vector]
        max_k: 12
      views: [agent_relevant_memory] # names must resolve at catalog load
      traverse:                      # optional; omit to forbid graph hops
        collections: [relations]
        max_depth: 2
    max_steps: 6                     # reasoning turns; must be <= limits.max_llm_calls
    output_schema: { ... }           # required object-root JSON Schema, same as `llm` Task
    max_output_tokens: 1600
```

Emission is unchanged: `emit.from: "{{investigate.records}}"`, exactly like a terminal
`llm` Task.

### Step / action protocol

Each loop step is **one LLM call** returning JSON validated locally (Draft 2020-12) — the
same provider-neutral structured-output path `llm` uses (`runner.py:321-419`), **not**
provider-native tool-calling. The step returns either:

- an **action** — `{tool, args}` selecting one declared read tool; the runner executes it
  via `TaskContext.search` / `execute_view` / `traverse` and feeds the fenced, untrusted
  result back into the next step; or
- a **final** — the answer object validated against `output_schema`, carrying `citations`.

### Termination (all reuse existing budgets)

The loop ends on the first of: model emits *final*; `max_steps` reached;
`max_llm_calls` / `max_total_tokens` / `max_wall_s` exhausted; retrieval capacity
exhausted. On budget exhaustion the runner forces one *conclude-now* final call, or fails
the attempt with `budget` — reusing existing checks in `_execute_tasks` / `_call_json`.

---

## Guarantees preserved (by construction, no new mechanism)

- **Bounded evidence.** Every tool result's newly-introduced IDs charge
  `max_retrieved_records` and the run-wide `max_visible_records` cap — the exact caps that
  bound `search` today (`runner.py:1586-1607`). Over-capacity tool calls return
  "budget exhausted, conclude."
- **Citation authority.** Tool results render with full UUID handles; the final answer's
  citations are validated against the union of everything the loop actually saw — the
  existing citation-visibility intersection. An invented or never-retrieved UUID cannot
  become evidence.
- **Evidence, not current-state guard.** Research retrievals have the same status as a
  `view` source (spec §10.2, line 1628): cited and audited, but not commit-verified like
  guarded `current` / `record` sources. This matches `search` / `answer` today.
- **Glass-box trace.** The full tool-call trajectory (tool, args hash, selected IDs,
  truncation) is recorded in the run's `task_trace` / `retrieval_trace`, extending the
  per-Task trace vocabulary already in `_run_content` (`runner.py:1118-1207`, spec §5.2
  line 873).
- **Erasure.** Erasing a researched input invalidates the run and downstream checkpoints
  through the existing `derived_from` walk — no research-specific path.

---

## What stays unchanged

Driving source, `emit` boundary, Candidate Set, transactional commit, triggers, cron,
erasure, depth/lineage rules, the visible-record budget, and the citation machinery. No
storage, migration, or wire-protocol change. The substrate is still **not an agent
runtime**: the Task is read-only, gets no writer or DB handle, and funnels into the single
`emit`; triggering, budgets, commit, and lineage remain substrate-owned.

---

## Spec and DECISIONS changes

1. **spec §10.4 (Built-in and installed Tasks)** — add a `research` Task subsection after
   `search`: the `with` contract above, the step/action protocol, termination rules, the
   tool set (`search`, `views`, `traverse`), the trace additions, and the "evidence not
   guard" statement.
2. **spec §10.1 (validation rules)** — add: declared `views` names and `search`/`traverse`
   scopes must resolve at startup via the existing reference resolver; `max_steps` ≤
   `limits.max_llm_calls`; researched records bounded by
   `max_retrieved_records` / `max_visible_records`; research tool scopes are read scopes
   and (like `search`) contribute **no** trigger/dependency-graph edges, so the acyclic
   graph is unaffected.
3. **spec non-goals (near lines 21, 318)** — narrow "no tool loop" to: "no
   *unbounded / autonomous* tool loop over the data plane; a single sandboxed, read-only,
   budget-bounded research Task that funnels into the one emit boundary is permitted."
4. **`DECISIONS.md`** — new dated entry **"Bounded research Task — 2026-07-24"**
   (newest-first): the decision, the non-goal amendment, the closed-scope + budget
   contract, the "evidence not guard" status, and the explicit unchanged-list above.

---

## Open questions

- **Action protocol surface** — per-step JSON "action | final" schema (recommended;
  provider-neutral) vs. provider-native tool-calling. Recommend the former, matching how
  `llm` avoids provider tool-calling.
- **Dedicated `max_tool_calls`?** Recommend *no* — reuse `max_steps` (≤ `max_llm_calls`)
  plus `max_retrieved_records`. Add one only if non-LLM tool calls need separate capping.
- **Allow `answer` as a nested tool?** Recommend *no* for v1 (answer is itself a research
  flow; nesting complicates budget accounting).
- **Naming** — `research` vs `agent` vs `investigate`. Recommend `research`: describes the
  bounded read-only intent without implying "agent runtime."

---

## Verification (design-stage)

No code, so verification is design-consistency review against a worked example:

1. **Worst-case run on paper** — `max_steps: 6`, `max_retrieved_records: 60`,
   `max_visible_records: 220`: confirm every termination path (final / steps / tokens /
   wall / capacity) is covered by an existing budget and the forced-conclude path still
   validates `output_schema`.
2. **Provenance walk** — a hypothetical emitted record has `derived_from` = run ID + cited
   IDs; every cited ID is in `model_visible_ids`; a never-retrieved UUID cannot be cited.
3. **Erasure closure** — erasing one researched input invalidates the run and downstream
   checkpoints via the existing `derived_from` walk.
4. **Non-goal coherence** — the amended non-goals and the new DECISIONS entry, read
   together, still assert "not an agent runtime."
5. **Worked example** — draft (uncommitted) `derivations/deep_reflection.yaml` reusing
   `views/agent_relevant_memory` and the `relations` graph; confirm it validates
   conceptually against the amended §10.1 rules.

---

## Follow-up (implementation, out of scope here)

Once approved: add the `research` adapter (e.g. `src/memseek/derive/tasks/research.py`)
registered in `tasks.py:215-232`, a strict `TaskConfigModel`, loader reference-validation,
budget/trace wiring, and tests — reusing `answer.py`'s building blocks and the
`TaskContext` methods verbatim.
