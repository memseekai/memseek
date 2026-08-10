---
title: "Contradiction detection"
eyebrow: An ordinary typed derivation
---

A memory system that accumulates beliefs will eventually hold two that disagree:
"prefers async updates" written in March, "asked for a weekly call" written in
June. Noticing that is valuable — and in Memseek it is not a feature you switch
on.

Contradiction detection is an ordinary [derivation](derivations.md) that you own
and can read: a prompt that compares current beliefs about an entity and writes
an ordinary record whenever two of them conflict.

It takes two definitions, both yours to edit:

- a **collection** defining what a "these two disagree" record looks like, and
- a **derivation** defining what gets compared, with which model, on what
  prompt, under what budget, and how often.

There is no environment flag to enable, no built-in processor, no reserved
record type, and no special processing lane. If you want it to compare
different things, or run less often, you edit the YAML like anything else.

## The typed public collection

`relations` is a normal event collection. Its JSON Schema requires a readable
summary, two UUID endpoints, an explanation, and a bounded confidence value:

```yaml
collections:
  - name: relations
    version: 1
    active: true
    mode: event
    schema:
      type: object
      required: [text, subject_id, object_id, explanation, confidence]
      properties:
        text: {type: string}
        subject_id: {type: string, format: uuid}
        object_id: {type: string, format: uuid}
        explanation: {type: string, minLength: 1}
        confidence: {type: number, minimum: 0, maximum: 1}
      additionalProperties: false
    fields:
      subject_id: {path: content.subject_id, type: string, filter: true, project: true}
      object_id: {path: content.object_id, type: string, filter: true, project: true}
      confidence: {path: content.confidence, type: number, filter: true, sort: true, project: true}
    required_processors: [embedding_v1]
    search_profile: pg_default
```

`relations` is only a collection name. The derivation chooses the semantic
record type `contradiction`; another derivation could target the same collection
with a user-chosen type such as `supports` or `duplicates`.

Because the collection is public, relation records work with ordinary record
reads, timeline, delta, search, declared-field filters, write triggers, and
erasure provenance. No `include_system` option is needed.

## The derivation

The detector declares two named sources over collections that contain keyed
facts:

```yaml
name: contradiction
trigger:
  write:
    collections: [profiles, skills, plans]
    statuses: [active]
sources:
  changed_keys:
    kind: changes
    collections: [profiles, skills, plans]
    statuses: [active]
    keyed: true
    max_records: 20
    max_tokens: 12000
    allow_empty: false
  current_keys:
    kind: current
    collections: [profiles, skills, plans]
    statuses: [active]
    max_records: 40
    max_tokens: 12000
model: cheap
tasks:
  - id: result
    use: llm
    with:
      output_schema:
        type: object
        required: [records]
        properties:
          records:
            type: array
            items:
              type: object
              required: [citations]
              properties:
                key: {type: string}
                text: {type: string}
                content: {type: object}
                citations:
                  type: array
                  items: {type: string, format: uuid}
                retract: {type: boolean}
              additionalProperties: false
        additionalProperties: false
      prompt: |
        Compare {{changed_keys.rendered}} with {{current_keys.rendered}}.
        Return only {"records":[...]} with direct UUID citations.
```

These are the important visible decisions:

- Only keyed records in `profiles`, `skills`, and `plans` are considered.
- At most 20 changed keys and 40 current keys enter one run.
- The model alias is `cheap`.
- The prompt, rather than Python code, defines “direct contradiction” and caps
  the requested result at five conflicts.

The `llm` task requires the model to return ordinary record drafts:

```json
{
  "records": [
    {
      "text": "The campaign status conflicts with the current commitment",
      "citations": ["<changed-key-uuid>", "<current-key-uuid>"],
      "content": {
        "subject_id": "<changed-key-uuid>",
        "object_id": "<current-key-uuid>",
        "explanation": "One says the campaign ended; the other says it is active.",
        "confidence": 0.9
      }
    }
  ]
}
```

`emit` picks that task's value and fixes where it goes. The task itself cannot
redirect its output somewhere else:

```yaml
emit:
  from: "{{result.records}}"
  collection: relations
  type: contradiction
```

## What Python does

The generic derivation runner performs four mechanical checks; none decides
what a contradiction means:

1. Resolve the declared sources and track which record UUIDs were visible to
   the task that produced the emitted value.
2. Require every `citations` UUID returned by the model to have been visible
   to that producing task.
3. Merge each record's top-level `text` with its `content` and validate the
   result against the target collection's JSON Schema.
4. Commit a normal derivation run plus emitted public relation records. Each record cites
   the run and the two evidence records, then follows the normal readiness and
   enrichment path.

The relevant Implementation is the generic emission compiler in
`src/memseek/derive/candidates.py`, called by the pipeline executor in
`src/memseek/derive/runner.py`. Scheduling uses the same trigger and worker
functions as `profile`, `reflection`, and every other pipeline.

## Example

Suppose the current keyed profile contains:

> `commitments` → “Deliver the Northstar beta by September 30.”

A keyed plan then says:

> `delivery_status` → “The Northstar beta moved to Q1 and will not ship in September.”

The write trigger schedules `contradiction`. Both UUIDs appear in its prompt.
If the model judges them directly incompatible, the derived public record has
this envelope:

```json
{
  "collection": "relations",
  "type": "contradiction",
  "key": null,
  "content": {
    "text": "Northstar delivery dates conflict",
    "subject_id": "<delivery-status-uuid>",
    "object_id": "<commitments-uuid>",
    "explanation": "Q1 is incompatible with the September 30 commitment.",
    "confidence": 0.9
  },
  "citations": ["<delivery-status-uuid>", "<commitments-uuid>"]
}
```

Detection does not update or retract either key. A separate application action
or derivation can react to `collections: [relations]` and
`types: [contradiction]`.

## Inspecting decisions

Use the normal catalog and run surfaces:

- `/processors` shows the normalized sources, tasks, trigger, limits, and emit
  boundary.
- `/triggers` shows exactly why and when the detector is scheduled.
- `/runs?processor=contradiction` shows source receipts, final citation
  visibility, model attempts, usage, output IDs, and the configuration snapshot.
- `/records/{id}` shows the public relation content and provenance.

Changing contradiction policy means editing YAML: narrow the sources, change
the prompt, choose another model alias, add a task of your own, or target a
different typed event collection. The engine remains unchanged.
