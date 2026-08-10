---
title: Synthesis, differentiation, and error-driven revision
eyebrow: Design proposal
---

# Synthesis, differentiation, and error-driven revision

This is a design proposal, not shipped behaviour. It examines the catalog in
`collections/`, `derivations/`, and `views/` against one framing of general
memory — an immutable **trace** layer and a revisable **model** layer, related
by two operations, *synthesis* (compress particulars into a model) and
*differentiation* (split a model that incompatible evidence has broken) — and
proposes four changes.

The short version: **the trace/model separation is already the substrate's
spine, and synthesis is the only operation it can perform.** Every shipped
derivation compresses. Nothing splits, nothing compares a trace against a
model, and model revision is triggered by volume rather than by error. Three of
the four proposals below are YAML-only; the fourth needs a design decision about
who owns the model's ontology.

---

## 1. The two layers already exist, enforced by storage

Trace versus model is `mode: event` versus `mode: keyed`. This is not a
convention the catalog follows — it is a property the engine enforces.

| | collection | mode | `depth` | revisable |
|---|---|---|---|---|
| Trace | `main`, `transcripts`, `calendar_events` | event | 0 | never |
| Reflection | `reflections` | event | n | never |
| Model | `profiles`, `worldview`, `skills`, `plans` | keyed | n | superseded |
| Relation | `relations` | event | n | never |

An event record has no key and no successor, so *"Sam requested a detailed
answer on Tuesday"* cannot be edited. A keyed record has exactly one head,
resolved by `seq desc` in the `record_keyed_current` index
(`migrations/001_init.sql:57-59`), and revision writes a **new successor row**
while every predecessor stays byte-identical. *"Sam prefers short answers"* is a
key whose head moves.

The rule *keep what happened separate from what you currently believe it means*
therefore needs no adoption. It is already load-bearing.

Two further points are already settled and are **not** re-proposed here:

- **Contradiction is not a special object.** The *Public typed relations*
  decision (`DECISIONS.md`, 2026-07-18) deleted a built-in contradiction
  processor, its worker lane, its job-claim partition, and `relations.py`,
  replacing all of it with an ordinary event whose semantic `type` is chosen by
  the authoring derivation. [Contradiction detection](contradiction-detection.md)
  already notes that another derivation could target the same collection with a
  type such as `supports`.
- **Reflection is not a special object.** It is `type: reflection` in an
  ordinary event collection.

---

## 2. Every shipped derivation is a synthesis

Read the shipped `emit` blocks as operations and the pattern is uniform:

| derivation | inputs | output | operation |
|---|---|---|---|
| `harvest` | ≤5 transcripts | ≤50 observations | synthesis |
| `reflection` | ≤100 events | ≤5 insights | synthesis |
| `worldview` | ≤50 reflections | ≤5 keyed convictions | synthesis |
| `profile` | ≤200 events | ≤5 keyed facts | synthesis |
| `skill` | ≤100 evidence records | exactly 3 keyed sections | synthesis |
| `contradiction` | changed + current heads | ≤5 edges | detection |
| `belief_conflict` | changed + current convictions | ≤5 edges | detection |
| `reconcile` | conflict edges + convictions | ≤3 reflections | synthesis |

No shipped gbrain derivation performs differentiation. `contradiction` and
`belief_conflict` detect that a split may be required and emit an *edge*;
nothing consumes that edge to split anything. `reconcile` — the escalation that
is supposed to close the loop — responds to accumulated conflict by writing a
*reflection*, which is another synthesis. The cycle *compress → encounter
residual → split → recompress* is therefore missing from that catalog.

The structural reason is explicit in the emission contract
(`src/memseek/derive/schema.py:344-351`):

> `dynamic_keys: true` provides a bounded independent-key mode. It requires
> `max_active_keys`, captures every current target head, and rejects a commit
> that would exceed the declared live-key bound.

**The key space is the model's ontology, and it is bounded by the author.**
`dynamic_keys` permits runtime growth only up to `max_active_keys`; static
`emit.keys` and `driver_key` remain available for their narrower cases.

That bound is defensible and should be kept. Unconstrained differentiation is
how a compressing memory degenerates: every residual becomes a new special case,
model complexity climbs without bound, and the result is an episodic database
with no abstraction. What follows considers a more explicitly reviewed way to
grow the ontology, not an open one.

---

## 3. What is missing: no comparison is trace-to-model

Every comparison the substrate performs is between two interpretations:

| comparison | compares | where |
|---|---|---|
| Divergence | candidate model ↔ current model | engine, after the model already decided to revise |
| `contradiction` | changed keyed fact ↔ current keyed fact | `derivations/contradiction.yaml` |
| `belief_conflict` | conviction ↔ conviction | `derivations/belief_conflict.yaml` |
| **trace ↔ model** | — | **nothing** |

Divergence is the clearest case: it is computed *after* Task execution, so it
describes the change rather than motivating it. Nothing anywhere asks *does this
new event fit what I currently believe?*

To be fair to the catalog, the comparison does happen — invisibly.
`derivations/profile.yaml` puts `new_events` (traces) and `current_profile`
(model) in one prompt and instructs the model to "emit only records whose
current value should change because of the new evidence." A trace-to-model fit
judgment occurs inside that call and produces no artifact. It cannot gate the
revision, because it happens *during* it; it cannot accumulate; it cannot be
queried or audited.

This is a recurring pattern worth naming: the catalog repeatedly computes the
interesting intermediate and discards it. `derivations/reflection.yaml`
generates exactly three high-level questions in its `qs` task, uses them to
drive search, and throws them away. Making these intermediates durable is the
cheapest available improvement.

---

## 4. Consequence: revision is volume-triggered, not error-triggered

| derivation | trigger |
|---|---|
| `profile` | `accumulator: {metric: importance, threshold: 100}` |
| `reflection` | `accumulator: {metric: importance, threshold: 150}` |
| `worldview` | `accumulator: {metric: count, threshold: 3}` |
| `reconcile` | `census: {threshold: 2}` over `self_contradiction` |

Only `reconcile` fires on anything resembling error. The others ask *has enough
happened?*, never *was any of it surprising?* A stream of perfectly
model-confirming traces triggers exactly as much revision as a stream of
model-breaking ones — the same `strong`-model spend, the same rewrite risk, no
information gained. A well-fitting model should be cheap and stable.

---

## 5. Proposal A — `supports` and `resists` edges

**Cost: two derivation files. No schema change, no code.**

`collections/relations.yaml` already requires exactly the fields needed:
`subject_id`, `object_id`, `explanation`, `confidence`. Subject becomes the
trace, object the model head, `confidence` the strength of the relationship.
The semantic `type` distinguishes polarity.

```yaml
name: model_fit_resists
trigger:
  write:
    collections: [main]
    types: [observation]
    statuses: [active]
  cooldown_s: 300
sources:
  new_traces:
    kind: changes
    collections: [main]
    types: [observation]
    statuses: [active]
    keyed: false
    max_records: 40
    max_tokens: 16000
    allow_empty: false
  current_model:
    kind: current
    collections: [profiles, worldview]
    statuses: [active]
    max_records: 40
    max_tokens: 12000
model: cheap
# tasks: one llm call emitting {text, citations:[trace_uuid, head_uuid], content:{...}}
emit:
  from: "{{result.records}}"
  collection: relations
  type: resists
```

Two design constraints, both deliberate:

- **Write `supports` too, not only `resists`.** The *ratio* is what selects
  between the three responses to residual evidence. Forty supports and one
  resist is an exception worth keeping; two supports and three resists is a
  model that needs splitting. Stored resistance alone cannot distinguish them.
  `supports` edges also serve as the confirmation-based reinforcement signal the
  substrate currently lacks — `last_accessed` bumps feed decay
  (`conf/rank_default.yaml`) but record retrieval, not confirmation.
- **Require `subject_id` to be a `depth: 0` record.** An edge whose subject is a
  derived reflection lets the model resist itself, which is the
  self-confirmation failure the `depth_lte: 3` guard in
  `derivations/reflection.yaml` already defends against. Traces resist;
  interpretations do not.

**Known cost.** `emit` declares one `type`, so polarity requires two
derivations over the same evidence — two LLM passes. Emitting mixed-polarity
edges in one pass would require per-record type selection, which the emission
vocabulary deliberately does not allow. This duplication mirrors the existing
`contradiction` / `belief_conflict` pair and is accepted here rather than
worked around.

---

## 6. Proposal B — reflections must state testable expectations

**Cost: one `output_schema` change in `derivations/reflection.yaml`.**

A reflection that cannot fail is a summary. Today `reflections` stores
`{text, kind}`, so *"Sam is cautious"* is unfalsifiable and asking whether a
trace resists it is an open-ended judgment.

Adding a condition and an expectation narrows it to a checkable question — did
the condition obtain, and did the predicted behaviour occur?

```yaml
content:
  type: object
  required: [kind, condition, expectation]
  properties:
    kind: {const: reflection}
    condition: {type: string, minLength: 1}    # when this situation obtains
    expectation: {type: string, minLength: 1}  # this is what should happen
  additionalProperties: false
```

*"Sam is cautious"* becomes *condition:* a decision is hard to reverse;
*expectation:* Sam requests additional evidence. Proposal A's detector then has
something specific to test, which materially improves edge quality. This should
land **before** Proposal A, not after.

---

## 7. Proposal C — retarget revision triggers onto error

**Cost: trigger stanza edits.**

Once `resists` edges exist, `census` counts them, and `derivations/reconcile.yaml`
is the working template: an observational scope plus a threshold, firing only
when the driving source is also dirty, which makes a standing count loop-free by
construction. Observational scopes may name any collection, so this composes
without touching the driving-source subset rules.

```yaml
trigger:
  census:
    collections: [relations]
    types: [resists]
    statuses: [active]
    threshold: 3
```

Applied to `worldview` and `profile`, model revision becomes driven by
accumulated residual rather than by throughput. Retaining a low-frequency
volume trigger alongside it is reasonable — a model should still be refreshed
occasionally by confirming evidence — but error should be the primary stimulus.

---

## 8. Historical alternative — gated differentiation via `driver_key`

**Cost: one new keyed collection, two derivations, and a design decision.**

Before bounded `dynamic_keys` existed, this was the only proposal that added an
operation the substrate could not express. It remains a narrower alternative:
`driver_key` is the seam, and its constraints are exactly the ones that make it
safe (`src/memseek/derive/schema.py:368-373`,
`src/memseek/derive/candidates.py:222-229`):

- exactly one captured target key, taken from `basis.expected_heads`
- `max_records: 1`
- cannot combine with static `keys`, cannot be `complete`

Critically, **the model never chooses the key** — it comes from a record that
already exists. That gives a two-step split which grows the ontology one
auditable record at a time:

1. **Propose.** A detector reads accumulated `resists` edges against one head
   and emits a *context proposal* into a new keyed collection, where the key is
   the proposed slot name. Emitted with `review: required`, so activation is an
   explicit Promotion — exactly how `derivations/skill.yaml` already gates its
   writes.
2. **Specialise.** A `driver_key` pipeline, triggered by
   `changed: {transitions: [added]}` on that collection, writes the specialised
   head for each promoted proposal.

The result is runtime differentiation without an arbitrary dynamic-key write
surface: every ontology growth is itself a canonical record with provenance, a
review gate, and a diff.

**The decision this needs.** Should a model be able to grow its own ontology
without a human? Given the catalog's priority on author-legible YAML and
explicit naming, gating growth through Promotion is the recommended answer — it
keeps the key space reviewable and the worldview diffable. An ungated version is
possible but would make the ontology unauditable, which is the property the
rest of the design exists to protect.

**Open questions before implementing:**

- How does a `driver_key` run behave when the target head does not yet exist?
  `driver_keys` is derived from `basis.expected_heads`, so first-write semantics
  need confirming.
- The proposal's key becomes the belief's key, so proposal naming needs a
  convention that stays legible in a `keys` listing.
- Does anything bound total key growth per entity? The static 50-key cap does
  not apply to `driver_key` emissions.

---

## 9. Distinctions this proposal deliberately preserves

The trace/model framing tends to over-unify in four places. Each of these is a
case where the substrate is already more precise than the framing, and should
stay that way.

**Temporal change is not contextual differentiation.** A context split means the
model was *always* wrong, both branches were always true, and both stay live —
two heads. A temporal change means the model was *right and is now wrong*, and
one branch is dead — one head, superseded. These are different storage
operations, and the catalog already draws the line in the
`belief_conflict` prompt: *"Do NOT flag a single belief simply being updated or
reversed over time (an earlier, superseded version of the same slot): that is a
belief revision, not a standing contradiction."* Keyed supersession already
implements differentiation-by-time natively. Only the contextual case is
missing, and only that case is proposed above.

**Forgetting is not lossy synthesis.** `src/memseek/erase.py` computes a
recursive descendant closure and deletes it, driven by privacy and correctness
rather than compression. It correctly erases derived records too, since a model
built on a deleted trace is a laundered copy of it. This is the one operation
that must violate trace immutability, and it is a legal requirement rather than
a design choice — it needs no place in a compression objective.

**This is not event sourcing.** The mapping is right about the invariant —
never mutate evidence, derive interpretation — and wrong about the mechanism.
Event sourcing rebuilds state by replaying a deterministic fold. Derivations are
LLM calls and cannot be replayed, which is precisely why the
[Evaluation Basis](evaluation-bases.md) exists: an immutable receipt of the
checkpoint, record IDs, guarded reads, and active heads, persisted and
re-checked before commit. The substrate trades **reproducible state for
reproducible provenance**. Models also re-enter the log — a reflection is a
canonical row that `worldview` reads as its driving source — so this is a
stratified derivation graph, not a projection. Note also that *projection*
already means external search-index sync here (`index_upsert` / `index_delete`
job kinds, `src/memseek/projections.py`); reusing the word in the
event-sourcing sense will confuse design discussion.

**There is no global objective to minimise.** A description-length objective
over model complexity, unexplained evidence, and contradiction is a useful
frame, but every pipeline is entity-scoped and bounded (`max_tasks`,
`max_llm_calls`, `max_total_tokens`, `max_wall_s`), and a global optimiser would
violate that. Three of its terms are already countable — `census` counts current
non-tombstone heads and is effectively a model-complexity meter; conflict and
resistance edges are countable once Proposal A lands; unexplained evidence is
traces with no `supports` edge. The honest implementation is **thresholds on the
residual**, which is what `census` already does.

---

## 10. Staging

| stage | change | cost |
|---|---|---|
| 1 | Proposal B — testable expectations in `reflections` | one `output_schema` |
| 2 | Proposal A — `supports` / `resists` derivations | two YAML files |
| 3 | Proposal C — `census` triggers on `resists` | trigger stanzas |
| 4 | Proposal D — gated differentiation | new collection + 2 derivations + decision |

Stages 1–3 are pure authoring and together convert the loop from volume-driven
to error-driven. Stage 4 adds the missing operation and is the only one
requiring a governance decision.

---

## 11. Related, not proposed here

Three adjacent gaps surfaced during this analysis and are recorded for
completeness:

- **Persist the reflection questions.** `derivations/reflection.yaml` already
  generates three open questions per run and discards them. A `questions`
  collection plus a second emission would make uncertainty explicit and
  inspectable for almost no cost.
- **Forward predictions.** Proposal B makes reflections retrospectively
  falsifiable. A forward version — the model emits dated predictions that are
  checked when due — would enable calibration. The `at` trigger condition
  (`DECISIONS.md`, extended trigger surface) is exactly the machinery for this
  and is currently unused.
- **Episodes.** A bounded event with cause, response, and outcome would make
  analogical retrieval real; `reflection`'s hybrid search task is already the
  retrieval half, with nothing structurally comparable to retrieve. `outcomes`
  is the nearest existing collection and carries no narrative structure.
