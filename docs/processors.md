---
title: "Processors"
eyebrow: Enrichment capabilities
---

A **processor** is a small, named capability that enriches one record at a
time — "give this record an importance score", "classify this record's
sentiment", "compute this record's embedding". Collections bind processors to
run on every incoming record; triggers and rank expressions then use the
values the processors wrote.

This page is only about **enrichment processors**. A **derivation** combines
several existing records into new cited records; its manual HTTP route happens
to live under `/processors/...` for historical reasons. The
[Glossary](glossary.md#enrichment-processor) explains why these are different
kinds of work.

There is exactly **one kind of thing to define**. Every processor writes its
result onto the record as an annotation object (`annotations.<name>`), and
two shapes additionally project into dedicated storage so the rest of the
system can use them cheaply:

- a **score** is also mirrored into the flat numeric map `scores.<name>`,
  where rank expressions and accumulator triggers can read it;
- an **embedding** is also written to the record's vector column, where
  semantic search can find it.

So `scores.*` and the vector are not different kinds of processor — they are
*copies of processor output, put somewhere fast to reach*. The annotation
remains the original.

A processor's **name is a promise**. Once other definitions depend on
`importance` meaning "1–10 long-term significance", you can tune its prompt or
move it to a different model behind the same name — but if you change what the
number *means*, introduce a new name instead.

All processors live in **one file**, `conf/processors.yaml`. Any model they
call is referenced by a named alias from `conf/models.yaml` — provider model
names never appear here (see [Model aliases](models.md)).

## The two axes: `kind` and `source`

Every processor answers two questions:

- **`kind`** — *what shape does it write?* `embedding`, `score` (one
  number), or `json` (a structured object).
- **`source`** — *who produces the value?* `llm` (a model call), `judgment`
  (a [judgment model](models.md#judgment-models) answering typed questions),
  `client` (the caller supplies it at ingest time), or `constant` (a fixed
  configured value). The `embedding` kind has no `source`: it always uses the
  `embed` model alias.

Common fields for every processor:

- **`name`** (required) — the processor identity. Lowercase letters, digits,
  and underscores, up to 32 characters. The annotation is stored and queried
  as `annotations.<name>`.
- **`input`** (required) — which records this processor may run on.
  `collections` is a required, non-empty list; `types` optionally narrows it
  to specific record types. Every collection that *binds* this processor must
  appear in `input.collections`.

### `kind: embedding`

Computes the record's vector for semantic search, using the `embed` alias.
No prompt, no schema, no source — the whole definition is scope:

```yaml
processors:
  - name: embedding_v1
    kind: embedding
    input: {collections: [main, profiles, reflections]}
```

The annotation it writes records the embedding space
(`annotations.embedding_v1 = {"space": ...}`); the vector itself goes to the
record's dedicated vector column.

### `kind: score`

Attaches **one number** to each record. The number lands in two places:
`annotations.<name> = {"value": N}` and, projected flat, `scores.<name>`.
That flat value is a reusable signal: rank expressions can weight results by
it, and accumulator triggers can fire a derivation when enough of it piles
up.

> "Whenever a customer event arrives, rate how much it matters for the
> long-term relationship, from 1 to 10. If the model call fails, assume a
> middling 5."

```yaml
processors:
  - name: importance
    kind: score
    source: llm
    input: {collections: [customer_events]}
    scale: [1, 10]
    default: 5
    render: true
    model: cheap
    prompt: |
      Rate the long-term significance of each record from 1 to 10.
      Return one number per record in input order.
```

Score fields:

- **`scale`** (required) — two numbers, low then high, e.g. `[1, 10]` or
  `[0, 1]`. Every value (including `default` and `value`) must fall inside
  it; out-of-scale values are clamped.
- **`default`** — the fallback used when the model call fails or returns
  something unusable. **Required** for `source: llm`, forbidden otherwise.
- **`value`** — the fixed number a `constant` score emits. **Required** for
  `source: constant`, forbidden otherwise. Useful to give a whole collection
  a baseline weight.
- **`render`** (default `false`) — when `true`, the score is shown in
  rendered search output, so prompts built from search results can see it.
- **`model`** / **`prompt`** — the alias and rating instructions.
  **Required** for `source: llm`, forbidden for `client` and `constant`.

A `source: client` score is supplied by the caller when ingesting the record
(for example, your app already knows a priority). It declares only `name`,
`kind`, `source`, `input`, and `scale`.

### `kind: json`

Writes a **structured value** (a JSON object) onto each record as
`annotations.<name>`. Collections can declare typed fields over its leaves,
derivation triggers can filter on them, and numeric leaves can be promoted
into flat scores.

> "For every customer event, classify the sentiment as positive, neutral, or
> negative, with a confidence between 0 and 1. If classification hasn't
> happened yet, treat it as neutral. Also expose the confidence as a score so
> ranking can use it."

```yaml
processors:
  - name: customer_sentiment
    kind: json
    source: llm
    input:
      collections: [customer_events]
      types: [event, note]
    model: cheap
    prompt: Classify the sentiment of each record and give your confidence.
    output_schema:
      type: object
      required: [label, confidence]
      properties:
        label: {type: string, enum: [negative, neutral, positive]}
        confidence: {type: number, minimum: 0, maximum: 1}
    default_output: {label: neutral, confidence: 0}
    score_fields:
      sentiment_confidence: confidence
```

JSON fields:

- **`output_schema`** (required) — a JSON Schema (root must be an object)
  that the produced value must satisfy. This is the contract that lets
  collections declare typed fields over `annotations.<name>...`.
- **`default_output`** (optional; required for `source: constant`) — the
  value assumed before the processor has run (or if it fails). It must
  itself validate against `output_schema`.
- **`model`** / **`prompt`** — **required** for `source: llm`, forbidden for
  `client` and `constant`.
- **`score_fields`** (optional) — promotes numeric leaves of the annotation
  into flat, rankable scores. Each entry maps a new score name to a dotted
  path inside the output (here, `sentiment_confidence: confidence` makes
  `scores.sentiment_confidence` available everywhere a score is accepted —
  ranking, boosts, accumulator triggers). The path must point at a number in
  the output schema, and the name must not collide with any other score
  name.

### `source: judgment`

A judgment processor asks a judgment model such as TypeSafe's Jev a fixed set
of questions about each record. It takes no prompt and parses no text: the
answers come back typed. It runs on exactly the records an `llm` processor
with the same `input` would run on.

`questions:` uses the vocabulary of the Jev HTTP API, so examples from
TypeSafe's documentation can be copied as they are. The key is the answer id.

| `type` | `criteria` | answer |
| --- | --- | --- |
| `noul` | optional `{true, false}` descriptions | `noul` in [0, 1] |
| `choice` | map from option to a description or `null`, 2 to 255 options | `choice`, `probabilities`, `confidence` |
| `score` | ordered list of 2 to 10 level descriptions | `score` in [0, n-1], `probabilities` (one per level), `confidence` |

`instructions` and each criterion accept a string, an object, or a list. A
processor declares 1 to 64 questions. Instructions are static: they take no
templates, so a changed question always shows up as a changed processor hash.

Each record is judged on its own, all of its processor's questions in one
call. Instructions point into the state with backtick paths. The state is:

```json
{"record": {"collection": "main", "type": "chat", "key": null,
            "occurred_at": "2026-09-28T10:02:00Z",
            "text": "<the record text, middle-truncated to SCORER_TEXT_CHARS>",
            "content": {"...": "the other content fields"}}}
```

Annotations and internal metadata are left out.

A **score** judgment processor has exactly one `noul` or `score` question,
and requires `scale` and `default`. The answer maps linearly onto the scale: a
`noul` from [0, 1], a `score` from [0, n-1]. A `choice` has no order, so it
cannot be a score; promote one of its probabilities through a json processor
instead.

```yaml
- name: importance
  kind: score
  source: judgment
  model: jev
  input: {collections: [main]}
  scale: [1, 10]
  default: 5
  questions:
    importance:
      type: score
      instructions: "How significant is `record.text` for future decisions?"
      criteria: ["Routine", "Mildly useful", "Worth remembering", "Changes decisions"]
```

A score of 2.4 on those 4 levels becomes 1 + 2.4 / 3 × 9 = 8.2. The
annotation keeps the raw answer beside the value, so the probabilities stay
auditable: `{"value": 8.2, "judgment": {"importance": {"score": 2.4,
"probabilities": [...], "confidence": 0.8}}}`.

A **json** judgment processor stores the answer map itself. It forbids
`output_schema`, because the schema is synthesized from the questions, and
`score_fields` paths are checked against that synthesized schema. They must
end at a number: `noul`, `score`, `confidence`, or `probabilities.<option>`.

```yaml
- name: intent_v1
  kind: json
  source: judgment
  model: jev
  input: {collections: [main], types: [chat, message]}
  questions:
    actionable:
      type: noul
      instructions: "Does `record.text` ask someone to do something?"
    topic:
      type: choice
      instructions: "What is `record.text` mainly about?"
      criteria: {bug: "Something is broken", feature: null, other: null}
  score_fields:
    actionable_p: actionable.noul
    topic_bug: topic.probabilities.bug
```

When a judgment fails after its retries, a score processor writes its
`default` and a json processor writes its `default_output`, or a terminal
failure marker when it has none, exactly as `llm` processors do. A judgment
annotation can drive an accumulator trigger like any other:
`metric: {annotation: intent_v1, path: actionable.noul}`.

Judgment models are jagged: they are fast and cheap, but a probability is a
calibrated guess, not a fact.

- Batch independent questions about one state into one processor.
- Keep arithmetic, dates, and lookups in code; ask the model only what needs
  judgment.
- Gate actions on `confidence`, and calibrate thresholds against the pinned
  version before relying on them.

### Score limits

A catalog may declare at most **8 score names** in total (score processors
plus `score_fields` promotions), of which at most **4** may come from
`source: llm` score processors. Judgment score processors do not count
toward that 4. Scores are meant to be a few strong signals, not a feature
store.

## Binding processors to collections

Defining a processor does not run it anywhere. The *collection* decides which
processors run on its records, and whether the record should wait for them:

```yaml
# In the collection definition:
required_processors: [embedding_v1, importance]   # record isn't ready until these finish
optional_processors: [customer_sentiment]          # fills in later, doesn't block
```

Rules of thumb:

- The embedding processor is almost always **required** — without it the
  record cannot be found by semantic search.
- A score used by an accumulator trigger must come from a **required**
  processor on every collection the trigger watches, so the sum is
  well-defined.
- A field or trigger filter built on an annotation should use a **required**
  processor; optional annotations may not exist yet when the filter runs.
- Everything else can usually be **optional**, keeping ingestion fast.

The loader checks the whole chain: the processor's input scope must include
the collection, its output schema must support any fields declared over it,
its model alias must exist, a *required* processor must not narrow
`input.types` (that would leave excluded records permanently unready), a
required `json` processor must declare a `default_output` so a failure cannot
block readiness forever, and a server-side worker must actually be able to
produce required output (a derivation's output collection cannot *require* a
`source: client` score, since no client is present when the worker writes
rows).

## Changing a processor: `supersedes`

A record annotated under one prompt keeps that value forever — nothing is ever
silently recomputed. So improving a processor means publishing it under a **new
name**, not editing the old one.

That would normally leave every reader to cope with the split: older records
carry the old annotation, newer ones the new. `supersedes` is what saves them
from having to know:

```yaml
processors:
  - name: sentiment_v1        # keep it: history references it
    kind: json
    # ... unchanged ...

  - name: sentiment_v2
    kind: json
    source: llm
    input: {collections: [customer_events]}
    model: cheap
    prompt: |
      ...the better prompt...
    output_schema: {type: object, required: [label], properties: {label: {type: string}}}
    default_output: {label: neutral}
    supersedes: sentiment_v1
```

It is purely a **reading** preference. Both annotations stay on the record,
separately auditable, and neither is ever rewritten. What it changes:

- a declared field over `annotations.sentiment_v2.label` also answers for records
  that only carry `sentiment_v1`, preferring the newer value when both exist;
- every path that reads that field follows the same preference — the database,
  the re-check after a search, and any external index — so they cannot disagree
  with each other;
- filtering or sorting on such a field is allowed as long as *any* name in the
  chain is required, because then the value is guaranteed to exist;
- pointing an existing field at the new name is an *additive* publish, not a
  version bump.

The loader keeps a chain well formed: the target must exist, be the same `kind`,
and the chain must be linear (two processors cannot supersede one name) and
acyclic. Declaring a supersession never changes a collection's record contract, so
it cannot strand a record.

To apply the new processor to records you already have, run a
[backfill](changing-definitions.md#apply-a-processor-to-records-you-already-have).

## What is *not* a processor

Spotting beliefs that contradict each other looks like enrichment, but it isn't:
it compares several records against each other, so it is a
[derivation](derivations.md), written in ordinary YAML like any other.

That is deliberate. There is no hidden built-in for it and no special
processing lane — its inputs, model, prompt, budgets, trigger, and output
collection are all visible and all yours to change. See
[Contradiction detection](contradiction-detection.md).

The same test applies to anything you are unsure about: **one record in, one
annotation out** is a processor. **Many records in, new records out** is a
derivation.
