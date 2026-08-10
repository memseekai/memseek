---
title: "CRM profile walkthrough"
eyebrow: End-to-end Pipeline example
---

This walkthrough builds the checked-in
`examples/crm_profile_catalog` from one product question:

> What does Avery do, what have they committed to, how do they prefer to
> communicate, what are their goals, and which CRM events prove each answer?

The example demonstrates two complementary Pipelines:

- `crm_profile` incrementally maintains live profile slots from new events;
- `crm_profile_rebuild` independently reconstructs the complete profile from
  a bounded snapshot and stages it for review.

Both use the same general authoring Interface:

```text
named Sources → registered Tasks → emit typed record drafts
```

The cursor, checkpoint receipt, expected active heads, Candidate Set, and
guarded commit stay behind that Interface.

## The catalog at a glance

```text
crm_events ── required enrichment ──> ready evidence
    │                                      │
    │                                accumulator trigger
    │                                      v
    ├────────> crm_profile ───────────> active user_profiles
    │              ^                            │
    │              ├── playbook record          ├── document reads
    │              └── crm_history view         ├── search
    │                                           └── profile brief
    │
    └────────> crm_profile_rebuild ───> draft proposal
                                                │
                                          explicit approval
                                                │
                                                v
                                         active successors
```

The package is `crm_user_profile@2.0.0`. The major version marks the breaking
move from the former transition-oriented derivation syntax to the Pipeline
Interface; there is deliberately no compatibility layer. The package contains:

- `crm_events@1`, `user_profiles@1`, and `playbooks@1` collections;
- `crm_embedding`, `importance`, and `deal_signals` per-record processors;
- `crm_profile` and `crm_profile_rebuild` Pipelines;
- the `crm_history@1` view;
- live `crm_profile_brief@1` and reviewed
  `crm_profile_candidate@1` artifacts; and
- the inline `crm_profile.default` trigger.

## 1. Preserve CRM evidence

The source collection is append-only:

```yaml
collections:
  - name: crm_events
    version: 1
    active: true
    mode: event
    schema:
      type: object
      required: [text, source, event_kind]
      properties:
        text: {type: string}
        source: {type: string, enum: [salesforce, hubspot, support, product]}
        event_kind: {type: string, enum: [role, commitment, preference, interaction]}
        account_id: {type: string}
      additionalProperties: false
```

`mode: event` preserves every arrival instead of replacing an earlier event.
Each event receives a canonical UUID; later profile records cite those UUIDs.
The schema prevents provider and event-kind spelling drift.

The collection also declares queryable fields and enrichment:

```yaml
    fields:
      source:
        path: content.source
        type: string
        filter: true
        project: true
      event_kind:
        path: content.event_kind
        type: string
        filter: true
        project: true
      account_id:
        path: content.account_id
        type: string
        filter: true
        project: true
    required_processors: [crm_embedding, importance]
    optional_processors: [deal_signals]
    search_profile: pg_default
```

The embedding and importance score form the readiness barrier. An event does
not trigger profile work or appear in normal search until both required
processors finish. `deal_signals` is optional and can arrive later.

## 2. Give profile state bounded slots

The maintained profile and account playbook are keyed collections:

```yaml
  - name: user_profiles
    version: 1
    active: true
    mode: keyed
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
        tombstone: {type: boolean}
      additionalProperties: false
    required_processors: [crm_embedding]
    search_profile: pg_default

  - name: playbooks
    version: 1
    active: true
    mode: keyed
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
        tombstone: {type: boolean}
      additionalProperties: false
    required_processors: [crm_embedding]
    search_profile: pg_default
```

For `user_profiles`, the application uses six keys — five durable factual slots
plus a synthesized summary:

- `role`;
- `commitments`;
- `preferences`;
- `open_threads`;
- `goals`; and
- `summary` — one sentence over the whole profile, regenerated each run.

A later active record for the same entity/key becomes current. The earlier row
remains immutable history. A retraction is another successor containing a
tombstone, not a destructive delete.

## 3. Enrich and retrieve supporting evidence

The complete processor file is under
`examples/crm_profile_catalog/conf/processors.yaml`. Its three roles are:

| Processor | Kind | Purpose |
| --- | --- | --- |
| `crm_embedding` | embedding | Semantic search over evidence, profile, and playbook records. |
| `importance` | score | Numeric trigger signal for meaningful CRM activity. |
| `deal_signals` | JSON | Optional structured stage/risk metadata. |

The `crm_history@1` view searches older evidence for commitments and next
steps. The live Pipeline uses it as one named Source. This is useful when the
incremental batch alone does not contain enough historical context, while the
view's exact selected canonical IDs still become provenance.

## 4. Maintain the live profile incrementally

The checked-in file is
`examples/crm_profile_catalog/derivations/crm_profile.yaml`.

### Trigger and named Sources

```yaml
name: crm_profile
trigger:
  accumulator:
    metric: importance
    threshold: 9
  cooldown_s: 1

sources:
  new_crm_events:
    kind: changes
    collections: [crm_events]
    types: [crm_event]
    statuses: [active]
    keyed: false
    max_records: 100
    max_tokens: 12000
    allow_empty: false

  current_profile:
    kind: current
    collections: [user_profiles]
    types: [profile]
    statuses: [active]
    keys: [role, commitments, preferences, open_threads, goals, summary]
    max_records: 20
    max_tokens: 6000

  account_playbook:
    kind: record
    collection: playbooks
    key: playbook
    type: playbook
    max_tokens: 1500

  commitment_history:
    kind: view
    view: crm_history
    params: {entity: "{{entity}}", query: "commitments and next steps"}
    max_tokens: 3000
```

This one block expresses four different read intentions:

| Source | Why it exists | Runtime behavior |
| --- | --- | --- |
| `new_crm_events` | Drive work from evidence not processed before. | Reads the next ready suffix after the Pipeline cursor. |
| `current_profile` | Preserve or revise the existing slots intelligently. | Reads latest keyed rows and guards their identities through commit. |
| `account_playbook` | Apply one account-specific instruction record. | Reads and guards exactly one keyed slot. |
| `commitment_history` | Bring older relevant evidence into the computation. | Runs a bounded named view and tracks selected canonical IDs. |

Exactly one Source drives the run: `new_crm_events` has `kind: changes`.
Every other Source supports the Task without advancing the cursor.

There is no author-facing watermark, predecessor, state binding, or expected
head declaration. The runtime infers and audits those details.

The changes cursor is protected by an internal Source-membership hash. Updating
the prompt, swapping a registered Task, expanding emission keys, or changing
budgets can continue from the same cursor and remains visible in run hashes.
Changing which collections, versions, types, statuses, or keyed shape belong to
`new_crm_events` is rejected; deploy a new Pipeline identity or use an explicit
snapshot so “already consumed” never changes meaning silently.

### One general Task

```yaml
model: strong
limits:
  max_tasks: 1
  max_llm_calls: 2
  max_retrieved_records: 0
  max_visible_records: 100
  max_total_tokens: 20000
  max_wall_s: 60

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
        Maintain a precise, durable CRM profile for {{entity}}.

        CURRENT PROFILE:
        {{current_profile.rendered}}

        ACCOUNT PLAYBOOK:
        {{account_playbook.rendered}}

        EARLIER COMMITMENT HISTORY:
        {{commitment_history.rendered}}

        NEW CRM EVENTS:
        {{new_crm_events.rendered}}

        Emit role, commitments, preferences, open_threads, or goals, plus a single
        summary — one sentence over the whole profile. Accumulate newly-evidenced
        items into the list-like slots rather than replacing them. Every record must
        cite visible CRM event UUIDs. Return only:
        {"records":[{"key":"role","text":"...","citations":["uuid"]}]}
```

`use: llm` selects the process-installed built-in Task Adapter. The Task
receives escaped rendered Sources — the `<records untrusted="true">` elements in
the prompt above are the author's, not the runtime's — makes a bounded JSON
completion, and produces a typed value named `result`.

The Pipeline is not limited to LLMs. `use` could select `search`, `template`,
or a deployment-installed typed Task. The runtime cares only that each Task
registration is known, hashed, bounded through its context, and returns a
JSON-compatible value.

### One constrained emission

```yaml
emit:
  from: "{{result.records}}"
  collection: user_profiles
  type: profile
  keys: [role, commitments, preferences, open_threads, goals, summary]
```

This is the whole write declaration:

- `from` must be one exact typed Task-result reference;
- the collection and type are fixed before any Task runs;
- `keys` is both the allowed output shape and bounded concurrency scope; and
- because `complete` is absent, Tasks may emit any subset.

If the Task returns only `role`, only that slot gets a successor.
`commitments`, `preferences`, `open_threads`, and `goals` remain unchanged.
If it returns no records, the run is an audited no-op.

The Task uses one unified draft vocabulary:

```json
{
  "records": [
    {
      "key": "commitments",
      "text": "Avery committed to deliver Northstar by September 30.",
      "citations": ["crm-event-uuid"]
    },
    {
      "key": "open_threads",
      "retract": true,
      "citations": ["closing-event-uuid"]
    }
  ]
}
```

There is no separate `updates`, `events`, `set`, or `put` output language.
Events omit `key`; keyed values include it; `retract: true` expresses absence.

## 5. What happens on real arrivals

The SDK example ingests four events for `contact:avery-chen`:

1. Salesforce reports Avery's VP of Product role.
2. HubSpot records a Northstar beta commitment.
3. Support records a preference for concise written updates.
4. Product telemetry records a dashboard view.

Required processors enrich each event. Once the ready importance total above
the cursor reaches the trigger threshold, one entity-scoped job runs.

The Pipeline can reasonably emit three profile drafts—role, commitment, and
preference—while ignoring the low-value dashboard interaction. Each emitted
record:

- has a new immutable UUID;
- is validated as `user_profiles@1` content;
- stores type `profile` and the entity;
- cites the generating run plus direct event UUIDs;
- waits for its required `crm_embedding`; and
- becomes current only when the complete sibling output group is ready.

Now imagine a fifth event changes the Northstar deadline. The next run receives
only records after its prior cursor but also sees the current five profile
slots. It can emit only a new `commitments` record. The first commitment stays
in history and the other slots do not churn.

### Concurrent work is rejected safely

Before the Task starts, the runtime privately captures:

- the prior successful cursor;
- the exact current-profile and playbook record IDs; and
- the active head—or absence—for all five allowed output keys.

Before commit it reloads those assumptions under the normal locks. If another
worker changed `goals`, even when this Task did not emit `goals`, the run is
stale because its declared target state changed during arbitrary computation.
Nothing is partially written; a retry starts from a fresh receipt.

## 6. Reconstruct independently from a snapshot

Incremental maintenance is efficient, but it does not prove that current state
can be reconstructed. The second checked-in Pipeline does:

```yaml
name: crm_profile_rebuild
sources:
  crm_corpus:
    kind: snapshot
    collections: [crm_events]
    types: [crm_event]
    statuses: [active]
    keyed: false
    max_records: 200
    max_tokens: 24000
    allow_empty: true

  account_playbook:
    kind: record
    collection: playbooks
    key: playbook
    type: playbook
    max_tokens: 1500

model: strong
limits:
  max_tasks: 1
  max_llm_calls: 2
  max_retrieved_records: 0
  max_visible_records: 220
  max_total_tokens: 36000
  max_wall_s: 90

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
        Reconstruct the complete CRM profile for {{entity}}
        through checkpoint {{run.checkpoint}}.

        ACCOUNT PLAYBOOK:
        {{account_playbook.rendered}}

        COMPLETE BOUNDED CRM CORPUS:
        {{crm_corpus.rendered}}

        Return exactly role, commitments, preferences, open_threads, and
        goals. Use a cited value when supported. Use retract with an empty
        citations list when the complete corpus supports no current value.

emit:
  from: "{{result.records}}"
  collection: user_profiles
  type: profile
  keys: [role, commitments, preferences, open_threads, goals]
  complete: true
  review: required
```

Three declarations make this a rebuild:

1. `kind: snapshot` selects every matching CRM event through one exact
   `run.checkpoint`.
2. `complete: true` requires exactly one record or retraction for every key.
3. `review: required` stages the results instead of changing live state.

The Pipeline deliberately has no `current_profile` Source. Its answer must be
independent of current derived state. Nevertheless, the runtime captures all
five active target heads before the Task begins. That hidden guard makes later
Promotion compare-and-set rather than blind overwrite.

### Snapshot is complete or fails

If 201 matching CRM events exist, `max_records: 200` fails the run. If all
records do not fit the Source token or run visible-record bounds, the run also
fails. It never labels a truncated sample “complete.”

When raw history becomes too large, good options include:

- narrow the snapshot scope by a durable domain partition;
- raise bounds only when the model and service budgets genuinely allow it; or
- derive a compacted evidence collection first, then rebuild from that smaller
  typed collection.

## 7. Inspect divergence before accepting a rebuild

Running `crm_profile_rebuild` writes five draft rows and one audited run. The
run includes a private Candidate Set divergence report such as:

```json
[
  {
    "collection": "user_profiles",
    "key": "role",
    "change": "unchanged",
    "active_record_id": "...",
    "candidate_record_id": "..."
  },
  {
    "collection": "user_profiles",
    "key": "preferences",
    "change": "changed",
    "active_record_id": "...",
    "candidate_record_id": "..."
  },
  {
    "collection": "user_profiles",
    "key": "goals",
    "change": "added",
    "active_record_id": null,
    "candidate_record_id": "..."
  }
]
```

The four classifications are `added`, `changed`, `removed`, and `unchanged`.
They describe content difference, not quality. A reviewer still decides
whether the reconstructed values are acceptable.

The reviewed artifact links the complete candidate Pipeline to the expected
profile key contract:

```yaml
artifacts:
  - name: crm_profile_candidate
    version: 1
    active: true
    kind: profile
    lifecycle: reviewed
    parameters:
      entity: {type: string, required: true}
    blocks:
      profile:
        document:
          entity: "{{entity}}"
          collections: [user_profiles]
          status: active
        max_tokens: 2500
    template: |
      REVIEWED CRM PROFILE
      {{profile}}
    candidate_processor: crm_profile_rebuild
    complete_keys: [role, commitments, preferences, open_threads, goals]
```

Catalog compilation verifies that the candidate Pipeline has a complete,
reviewed keyed emission to the artifact's document collection and kind.

## 8. Promotion changes records, not definitions

Promotion accepts the source run's ready draft records. It does not mutate:

- the Pipeline YAML;
- the artifact YAML;
- the original draft rows;
- the snapshot receipt; or
- the divergence report.

It creates one Promotion run and copies all five drafts into new active
successor records atomically.

If an incremental run changed any one of the five active heads after the
rebuild started, Promotion returns `409 promotion_stale` and writes none of
them. Generate a new candidate against the newer state. This is what permits a
review window lasting minutes or days without risking an old snapshot
overwriting newer live work.

## 9. Replace the LLM with a typed domain Task

The data and commit model does not depend on an LLM. A deployment could install
a deterministic CRM normalization Task:

```python
import hashlib
from typing import Any

from memseek.derive import TaskConfigModel, TaskContext, TaskResult, register_task


class CRMNormalizeOptions(TaskConfigModel):
    ignore_interactions: bool = True


async def normalize_crm(
    context: TaskContext,
    value: list[dict[str, Any]],
    config: CRMNormalizeOptions,
) -> TaskResult[dict[str, Any]]:
    records = [
        row
        for row in value
        if not config.ignore_interactions
        or row["content"].get("event_kind") != "interaction"
    ]
    return TaskResult({"records": build_profile_drafts(records)})


register_task(
    "normalize_crm",
    implementation_hash=hashlib.sha256(b"normalize_crm:v1").hexdigest(),
    config_model=CRMNormalizeOptions,
    input_type=list[dict[str, Any]],
    output_type=dict[str, Any],
    handler=normalize_crm,
)
```

Deploy that module to both processes and configure, for example,
`TASK_MODULES=["acme_crm.tasks"]`; API and worker startup import the same
registry before catalog compilation.

The Pipeline Task call becomes:

```yaml
tasks:
  - id: result
    use: normalize_crm
    input: "{{new_crm_events.records}}"
    with:
      ignore_interactions: true
```

`input` carries typed per-run data. `with` is static configuration validated at
catalog load. The handler receives no database connection or record writer;
it returns ordinary JSON drafts that pass through the same citation, schema,
key, target-head, lineage, and commit Modules as LLM output.

This enables:

- deterministic normalization before or instead of model reasoning;
- domain-specific merge and confidence logic;
- calling a trusted external CRM client behind a bounded Adapter;
- combining several models or algorithms inside one installed Task; and
- unit-testing business computation without constructing worker jobs.

Workspace catalog uploads cannot install this Python. The operator registers
trusted Tasks in the process before compiling catalogs, and every run records
the Task implementation hash and output hash.

## 10. Run the checked-in example

Start the API and worker with the example environment, create a workspace, and
export its key. Then run:

```console
uv run python examples/sdk_crm_profile.py
```

The script:

1. publishes `crm_user_profile@2.0.0`;
2. ingests the four synthetic CRM events;
3. waits for the triggered live profile;
4. prints the Pipeline run audit;
5. searches for the Northstar commitment; and
6. renders the profile brief.

Set this flag to also generate a reviewed rebuild:

```console
MEMSEEK_RUN_REBUILD=1 uv run python examples/sdk_crm_profile.py
```

The script intentionally leaves the candidate in draft. To accept it as well:

```console
MEMSEEK_RUN_REBUILD=1 MEMSEEK_PROMOTE_REBUILD=1 \
  uv run python examples/sdk_crm_profile.py
```

The core SDK flow is:

```python
queued = await client.run_processor(
    "crm_profile_rebuild",
    entity="contact:avery-chen",
)
job = await wait_for_job(client, queued["job_id"])
run_id = job["successful_run_id"]

candidate = await client.run(run_id)
content = candidate["run"]["content"]
print(content["basis"])
print(content["candidate_set"]["divergence"])

await wait_for_candidate_ready(client, run_id)
await client.promote(
    entity="contact:avery-chen",
    source_run_id=run_id,
    artifact="crm_profile_candidate",
)
```

Evaluation Basis and Candidate Set appear here because this is an operator
audit. They are not values the Pipeline author has to construct or pass between
Tasks.

## 11. What this architecture enables

The two Pipelines are examples of a much broader space:

| Need | Source and emission intent |
| --- | --- |
| Cheap ongoing maintenance | `changes` Source + keyed subset emission. |
| Add a newly introduced profile slot | Include the key in the live Pipeline; optionally run a reviewed snapshot backfill. |
| Independent correctness check | `snapshot` Source + complete reviewed emission; inspect divergence only. |
| Repair or consolidation | Snapshot complete candidate, review, then promote atomically. |
| Append observations or relations | `changes` Source + emission without keys. |
| Deterministic migration | Snapshot Source + installed typed Task + complete reviewed emission. |
| Model-assisted workflow | Chain `llm`, `search`, and another `llm` Task before emission. |
| External enrichment | Installed Task behind constrained input/output typing, still no direct canonical write. |

The important separation is:

- Tasks own **computation**;
- Sources own **declared reads**;
- emission owns **allowed output shape**; and
- Memseek owns **canonical state transitions**.

That keeps the authoring model general and lean while preserving the system's
distinctive guarantees: immutable history, typed records, exact provenance,
bounded execution, concurrent-write rejection, divergence, and stale-safe
Promotion.

## Next

- Full Pipeline reference: [Pipelines & triggers](derivations.md)
- Hidden runtime semantics:
  [Runtime receipts and Candidate Sets](evaluation-bases.md)
- Deploy this as a workspace package:
  [SDK CRM user-profile quickstart](sdk-user-profile-quickstart.md)
