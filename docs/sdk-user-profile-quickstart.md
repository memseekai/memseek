---
title: "SDK quickstart: CRM events to a cited user profile"
eyebrow: Existing guide
---

## The scenario

Your team is building a sales copilot. Account executives open a contact and
ask one question: *"What do I need to know about this person before I get on
the call?"* The raw material is scattered — Salesforce activity, HubSpot
touches, support tickets, product telemetry — and the naive implementations
all fail in familiar ways:

- **Stuff everything into the prompt** — blows the context window within
  weeks and buries the one commitment that matters under forty routine
  dashboard logins.
- **Vector-search the event log at question time** — retrieves *similar*
  events, not a coherent picture; the model re-derives the profile on every
  request, differently each time.
- **Maintain a profile table with app code + cron** — now you own scheduling,
  dedup, race conditions, and when the copilot says "Avery committed to an
  August launch," nobody can prove where that came from.

This guide builds the fourth option: a **self-maintaining, cited profile**.
Events flow in; an LLM scores each one for importance; when enough
importance accumulates for a contact, a derivation re-reads the new evidence
and patches five bounded profile slots — `role`, `commitments`, `preferences`,
`open_threads`, and `goals` — each citing the CRM events that support it, plus
a synthesized one-sentence `summary` slot that the brief renders first. Because
the derivation runs incrementally over the current profile, the list-like slots
*accumulate*: a preference learned this week is folded in alongside last week's,
not overwritten. Your app just reads the profile. When a sales lead asks "why
does it say that?", the answer is a record ID, not a shrug.

This is a real service walkthrough. The CRM events are synthetic, but the
importance scoring and profile derivation use real LLM/provider calls. An
application owns its catalog, publishes it to one workspace, ingests events,
and reads the resulting profile through the SDK.

The checked-in example has two parts:

- `examples/crm_profile_catalog/` is the application's complete workspace
  catalog — nine small YAML files, each shown and explained in
  [step 4](#4-read-the-catalog-file-by-file).
- `examples/sdk_crm_profile.py` is the client application.

The automatic path is:

```text
publish catalog ──> ingest CRM events ──> importance processor
                                                 │
                                       accumulated score >= 9
                                                 │
                                                 v
                                       crm_profile derivation
                                                 │
                                                 v
                               cited, keyed user_profiles records
```

## 1. Configure a real provider

Synchronize the locked environment, start PostgreSQL, and apply the schema:

```console
uv sync --frozen --all-groups
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
make database
uv run memseek migrate
```

Configure the OpenAI-compatible provider. Keep the key in the process
environment; it is not part of the uploaded catalog:

```console
export LLM_FAKE=0
export OPENAI_API_KEY='replace-with-your-real-key'
```

The base URL, the token-limit field, and which variable holds the key are all
declared by the provider entry in the catalog, so the environment supplies only
the secret itself.

The example catalog currently targets `gpt-5.4-nano-2026-03-17` for both
importance scoring and profile reasoning, and `text-embedding-3-small` as its
embedding model. Both live in
`examples/crm_profile_catalog/conf/models.yaml` (shown in step 4), so the
application can replace them without changing the service. See the current
[OpenAI model catalog](https://developers.openai.com/api/docs/models) and
[Chat Completions API](https://platform.openai.com/docs/api-reference/chat/create).

If another OpenAI-compatible server only accepts the legacy token-limit field,
set `token_limit_field: max_tokens` on its provider entry. Use model IDs exposed
by that server in the uploaded `conf/models.yaml`.

## 2. Start the API and worker

Start the API in terminal A, with the database variables above:

```console
uv run uvicorn memseek.api:app --host 127.0.0.1 --port 8000
```

Start the worker in terminal B with the same database and provider variables:

```console
uv run memseek worker
```

The API stores and retrieves workspace data. The worker makes the real provider
calls, commits importance scores, evaluates the trigger, claims the derivation
job, and materializes the profile. Both processes must be running.

## 3. Create a tenant workspace

In terminal C, create a workspace and retain its one-time bearer key:

```console
workspace_json="$(uv run memseek create-workspace crm-sdk-demo)"
export MEMSEEK_API_KEY="$(printf '%s' "$workspace_json" | \
  uv run python -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"
export MEMSEEK_BASE_URL=http://127.0.0.1:8000
```

Memseek stores only the key's SHA-256 digest. The example reads the secret
from the environment and never writes it to YAML or logs.

## 4. Read the catalog, file by file

The SDK publishes the whole directory, not a server-side settings catalog.
The entire memory design of the copilot is these nine files:

```text
examples/crm_profile_catalog/
├── collections/crm.yaml          # what gets stored
├── conf/
│   ├── models.yaml               # which models, behind stable aliases
│   └── processors.yaml           # embedding, scores, and JSON annotations
├── derivations/crm_profile.yaml  # how the profile maintains itself
├── derivations/crm_profile_rebuild.yaml # independent bounded reconstruction
├── views/crm_history.yaml        # "show me relevant past events"
├── artifacts/profile_brief.yaml  # the briefing handed to the copilot
├── artifacts/profile_candidate.yaml # reviewed replacement policy
└── packages/crm_user_profile.yaml # the exact manifest tying it together
```

Each file below is prefaced by what it says in words; the linked reference
pages explain every parameter. To keep this quickstart focused on the SDK
publish/ingest/derive loop, the snippets show a **trimmed** version of the
catalog — the checked-in files add an embedding processor, a `deal_signals`
JSON processor, and named `record` and `view` sources for the playbook and
history, plus the
rebuild/review pair described below. For
the complete catalog run end-to-end with real captured outputs at every step,
see the [CRM profile walkthrough](crm-walkthrough.md).

### `collections/crm.yaml` — what gets stored

> "Keep every CRM event exactly as it happened — from Salesforce, HubSpot,
> support, or product telemetry, classified as a role change, commitment,
> preference, or routine interaction — and let me filter by any of those
> later. Separately, keep a current profile per contact."

```yaml
collections:
  - name: crm_events
    version: 1
    active: true
    mode: event                    # append-only history, never edited
    schema:
      type: object
      required: [text, source, event_kind]
      properties:
        text: {type: string}
        source: {type: string, enum: [salesforce, hubspot, support, product]}
        event_kind: {type: string, enum: [role, commitment, preference, interaction]}
        account_id: {type: string}
      additionalProperties: false
    fields:                        # typed filters for search and views
      source: {path: content.source, type: string, filter: true, project: true}
      event_kind: {path: content.event_kind, type: string, filter: true, project: true}
      account_id: {path: content.account_id, type: string, filter: true, project: true}
    required_processors: [importance]   # score before the event counts
    search_profile: pg_default

  - name: user_profiles
    version: 1
    active: true
    mode: keyed                    # one current value per (contact, key)
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
        tombstone: {type: boolean}
      additionalProperties: false
    search_profile: pg_default
```

`crm_events` is `mode: event` — history you never rewrite. `user_profiles`
is `mode: keyed` — the profile facts that supersede each other. The
`required_processors: [importance]` line is what guarantees no event feeds a
trigger before it has been scored. Reference: [Collections](collections.md).

### `conf/processors.yaml` — "how important is this event?"

> "Rate every event 1–10 by whether it should change a durable profile. Role
> changes, commitments, and stable preferences matter; routine interactions
> don't. If scoring fails, assume a low 3."

```yaml
processors:
  - name: importance
    kind: score
    source: llm
    input: {collections: [crm_events]}
    scale: [1, 10]
    default: 3
    model: importance_scorer
    prompt: |
      SCORER: importance
      Rate whether each CRM event should change a durable user profile.
      Role changes, explicit commitments, and stable preferences are important.
      Routine interactions are less important. Return one number per record.
```

The event text contains no hidden score markers — the model judges the text,
and the result lands as `scores.importance` on each record. Reference:
[Processors](processors.md).

### `conf/models.yaml` — models behind stable aliases

> "Everything else refers to models as `cheap`, `strong`, or
> `importance_scorer`, and embedding processors just use the one embedding model.
> Which endpoints and provider models those are is decided here, in one place."

```yaml
providers:
  openai:
    adapter: openai_compat
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY

aliases:
  cheap:
    targets: ["openai:gpt-5.4-nano-2026-03-17"]
    params: {max_output_tokens: 1200}
    context_tokens: 1050000
  strong:
    targets: ["openai:gpt-5.4-nano-2026-03-17"]
    params: {max_output_tokens: 4000}
    context_tokens: 1050000
  importance_scorer:
    targets: ["openai:gpt-5.4-nano-2026-03-17"]
    params: {max_output_tokens: 800}
    context_tokens: 1050000

embedding:
  provider: openai
  model: text-embedding-3-small
  dimensions: 1536
  space: default-v1

defaults:
  derivation: strong
  fold: strong
```

Swap the `targets` for models available to your account without touching any
other file — the aliases are the contract. The `embed` alias is required by
every catalog; the full checked-in catalog binds a `crm_embedding` processor
to the collections so events are findable by meaning (see the
[walkthrough](crm-walkthrough.md)).

### `derivations/crm_profile.yaml` — the self-maintaining profile

> "Once about 9 points of importance have piled up for a contact, re-read
> their new events alongside the current profile, and update `role`,
> `commitments`, `preferences`, `open_threads`, or `goals` — accumulating into the
> list-like slots rather than overwriting them — plus a one-sentence `summary`,
> citing the events that justify each change. Never infer sensitive traits."

```yaml
name: crm_profile
trigger:
  accumulator:
    metric: importance      # sum the committed importance scores...
    threshold: 9            # ...and queue a run when they reach 9
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
  current_profile:          # a guarded read of the current profile
    kind: current
    collections: [user_profiles]
    types: [profile]
    statuses: [active]
    keys: [role, commitments, preferences, open_threads, goals, summary]
    max_records: 20
    max_tokens: 6000
model: strong
limits:                     # the hard budget for one run
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
        Maintain a precise, durable CRM profile for {{entity}}. Use only facts
        explicitly supported by visible CRM events. Preserve useful current facts
        when new evidence does not supersede them.

        CURRENT PROFILE:
        {{current_profile.rendered}}

        NEW CRM EVENTS:
        {{new_crm_events.rendered}}

        Emit role, commitments, preferences, open_threads, or goals, plus a single
        summary — one sentence synthesizing the whole profile. Add newly-evidenced
        items to the list-like slots instead of replacing them. Do not infer
        sensitive traits. Every record must cite supporting visible CRM event
        UUIDs. Return only:
        {"records":[{"key":"role","text":"...","citations":["uuid"]}]}
emit:
  from: "{{result.records}}"
  collection: user_profiles
  type: profile
  keys: [role, commitments, preferences, open_threads, goals, summary]
```

Read the guardrails: `emit.keys` means the model *cannot* invent an undeclared
profile section; `emit.from` is the only Task value allowed across the
canonical-write boundary; citations are validated before anything is written;
and `limits` cap what one run may spend. When new, ready
evidence reaches the threshold, Memseek durably coalesces a
`derive/crm_profile` job — the client never decides when to derive.
Reference: [Derivations & triggers](derivations.md).

`kind: changes` consumes only evidence after this named pipeline's internal
checkpoint. Declaring keys without `complete: true` makes emission a partial
patch: omitted keys stay current. Cursor and transition details are runtime
receipts, not authoring controls. Adding `goals` lets future changes populate
it; reconsidering older evidence is the separate snapshot pipeline below.

### `derivations/crm_profile_rebuild.yaml` — reconsider all bounded evidence

The incremental path cannot populate `goals` from evidence already below its
checkpoint. The second pipeline is manual and uses a different composition:

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
        Reconstruct the complete profile for {{entity}} from
        {{crm_corpus.rendered}} through checkpoint {{run.checkpoint}}.
        Return all declared keys as records; use retract when no value exists:
        {"records":[
          {"key":"role","text":"...","citations":["uuid"]},
          {"key":"goals","retract":true,"citations":[]}
        ]}
emit:
  from: "{{result.records}}"
  collection: user_profiles
  type: profile
  keys: [role, commitments, preferences, open_threads, goals]
  complete: true
  review: required
```

`kind: snapshot` means every matching ready record through one sequence
checkpoint must fit the declared bounds; otherwise the run fails.
`complete: true` requires every declared key to be represented by a value or explicit
`retract: true`. `review: required` keeps the complete proposal in draft until
explicit promotion.

### `views/crm_history.yaml` — "show me relevant past events"

> "Given a contact and a question, return up to 20 matching CRM events for
> that contact, with their scores and timestamps, rendered ready for a
> prompt."

```yaml
views:
  - name: crm_history
    version: 1
    active: true
    parameters:
      entity: {type: string, required: true}
      query: {type: string, required: true}
    query:
      q: "{{query}}"
      mode: text
      scope:
        entities: ["{{entity}}"]
        collections: [crm_events]
        types: [crm_event]
      k: 20
      include: [text, collection, entity, scores, occurred_at]
      render: true
```

Reference: [Views & search](views-search.md).

### `artifacts/profile_brief.yaml` — the briefing handed to the copilot

> "A briefing is the current profile — the synthesized `summary` slot plus the
> durable factual slots, up to ~2,000 tokens — followed by the most relevant
> supporting events (up to ~3,000 tokens)."

```yaml
artifacts:
  - name: crm_profile_brief
    version: 1
    active: true
    kind: prompt
    lifecycle: live
    parameters:
      entity: {type: string, required: true}
      query: {type: string, required: true}
    blocks:
      profile:
        document:
          entity: "{{entity}}"
          collections: [user_profiles]
          status: active
        max_tokens: 2000
      evidence:
        view: crm_history@1
        args: {entity: "{{entity}}", query: "{{query}}"}
        max_tokens: 3000
    template: |
      CRM PROFILE
      The profile below leads with a synthesized `summary` slot — one sentence over
      the whole profile — followed by the durable role / commitments / preferences /
      open_threads / goals slots, each independently cited.
      {{profile}}

      SUPPORTING CRM EVENTS
      {{evidence}}
```

Every render records exactly which records went in. Reference:
[Artifacts](artifacts.md).

The companion `profile_candidate.yaml` is `lifecycle: reviewed`, names
`crm_profile_rebuild` as its candidate processor, and requires all five keys.
It is promotion policy, not mutable profile data.

### `packages/crm_user_profile.yaml` — the manifest

> "The copilot's memory, version 2.0.0, is exactly these pieces."

```yaml
name: crm_user_profile
version: 2.0.0
collections: [crm_events@1, user_profiles@1, playbooks@1]
processors: [crm_embedding, importance, deal_signals, crm_profile, crm_profile_rebuild]
triggers: [crm_profile.default]
views: [crm_history@1]
artifacts: [crm_profile_brief@1, crm_profile_candidate@1]
search_profiles: [pg_default]
```

(This is the full checked-in manifest; the trimmed snippets above omit the
`playbooks` collection, `crm_embedding`/`deal_signals` processors, and prompt
details they use.)

The service compiles and validates this complete graph before atomically
associating it with the workspace. Reference: [Packages](packages.md).

## 5. Publish and run the example

Run the checked-in client from the repository root:

```console
uv run python examples/sdk_crm_profile.py
```

Publishing is deliberately explicit and mechanical:

```python
async with MemseekClient(base_url, api_key) as client:
    await client.catalog.publish(
        package="crm_user_profile@2.0.0",
        directory="examples/crm_profile_catalog",
    )
    await client.records.ingest_many(crm_events)
```

`publish()` recursively reads `.yaml` and `.yml` files under the directory and
sends them to `POST /catalog`. It does not infer which package to activate; the
caller names it. Collections, models, processors, derivations, views, artifacts,
and package manifests are all loaded in the same atomic request. Both `publish`
and `ingest_many` return the server's response as a dict:

```python
catalog = await client.catalog.publish(
    package="crm_user_profile@2.0.0",
    directory="examples/crm_profile_catalog",
)
print(catalog["package"], catalog["catalog_hash"])         # 1. selected package + hash

result = await client.records.ingest_many(crm_events)
print(len(result["inserted"]), len(result["duplicates"]))  # 2. inserted vs. duplicate rows
```

After the worker has scored the events and the accumulator trigger has fired,
the same client reads the results back. Every read below is an SDK call:

```python
ENTITY = "contact:avery-chen"

# 3. The current profile document and derivation freshness.
document = await client.document(entity=ENTITY, collections="user_profiles")
for belief in document["beliefs"]:
    print(belief["key"], "→", belief["text"])
print("freshness:", document["freshness"])

# 4. The audited crm_profile run that produced those beliefs.
runs = await client.runs(entity=ENTITY, processor="crm_profile", source="changes")
if runs["runs"]:
    run = await client.run(runs["runs"][0]["id"])
    print("trigger_reasons:", run["run"]["content"]["trigger_reasons"])

# 5. Text search over the supporting CRM events.
hits = await client.search(
    query="commitments and launch dates",
    collections=["crm_events"],
    entity=ENTITY,
    mode="text",
    k=5,
    include=["text", "scores", "occurred_at"],
)
for hit in hits["hits"]:
    print(hit["scores"].get("importance"), hit["text"])

# 6. A live crm_profile_brief artifact built from profile + evidence.
brief = await client.render_artifact(
    "crm_profile_brief",
    entity=ENTITY,
    query="role commitments preferences",
)
print(brief["rendered"])
```

`document()` returns current keyed beliefs and `freshness`; `runs()`/`run()`
expose the audited derivation; `search()` returns ranked hits (re-checked
against canonical rows); and `render_artifact()` composes the profile and
evidence blocks into one bounded, prompt-ready string. The checked-in
`examples/sdk_crm_profile.py` runs exactly these calls.

The exact prose and importance values are model outputs. A typical profile has
these supported facts:

```text
role
  VP of Product for Acme Cloud, responsible for enterprise collaboration.

commitments
  Committed to deliver the Northstar beta by September 30.

preferences
  Prefers concise written updates before meetings.
```

Every value should cite the CRM event that supports it. The routine dashboard
interaction remains searchable history and should not become a profile fact.

## 6. Trigger a recomputation

This is the moment the design pays off: a week later Avery takes over a new
migration, and *nothing in your application changes* — you ingest the event
and the profile catches up on its own.

The example uses stable dedupe keys, so running it again does not create new
canonical events. Package publication is also safe to repeat.

To demonstrate recomputation, ingest genuinely new evidence with new dedupe
keys. The importance processor scores it. Once the score accumulated above the
last successful watermark reaches `9`, a successor profile job runs. Old keyed
rows remain history and the new rows become current.

```python
await client.records.ingest_many(
    [
        {
            "collection": "crm_events",
            "entity": "contact:avery-chen",
            "type": "crm_event",
            "text": "Avery now owns the Atlas migration and committed to an August launch.",
            "content": {
                "source": "salesforce",
                "event_kind": "commitment",
                "account_id": "acme-cloud",
            },
            "dedupe_key": "crm-demo:avery:atlas:2026-07-16",
        }
    ]
)
```

One event may not reach the threshold by itself. That is intentional: scoring
belongs to the processor, scheduling belongs to the trigger, and cross-record
reasoning belongs to the derivation.

## 7. Rebuild and review an expanded profile

Run the manual snapshot pipeline when a contract addition such as `goals`
needs old evidence to be reconsidered:

The checked-in client contains this flow behind
`MEMSEEK_RUN_REBUILD=1`; add `MEMSEEK_PROMOTE_REBUILD=1` only when you also
want the reviewed result activated.

```python
queued = await client.run_processor("crm_profile_rebuild", entity=ENTITY)

while True:
    job = await client.job(queued["job_id"])
    if job.get("successful_run_id"):
        break
    if job["state"] == "dead":
        raise RuntimeError(job)
    await asyncio.sleep(0.5)

review = await client.run(job["successful_run_id"])
candidate = review["run"]["content"]["candidate_set"]
print(candidate["covered_keys"])
print(candidate["divergence"])
```

The emitted rows are draft and go through normal enrichment. When all are
ready, accept that exact proposal explicitly:

```python
await client.promote(
    entity=ENTITY,
    source_run_id=job["successful_run_id"],
    artifact="crm_profile_candidate",
)
```

Promotion copies the draft values into new active successor records; neither
artifact YAML nor old records are edited. It is all-or-none. If an incremental
profile run changes any captured active head while review is underway, the
request returns `409 promotion_stale` and activates nothing. See
[Pipeline execution and promotion internals](evaluation-bases.md) for the full contract
and the [CRM walkthrough](crm-walkthrough.md)
for an annotated manifest.

## 8. Build catalogs programmatically

YAML is an interchange format, not a requirement that application definitions
live as hard-coded files. An SDK consumer can generate definitions and publish
the resulting in-memory file map directly:

```python
await client.catalog.publish_files(
    package="crm_user_profile@2.0.0",
    files={
        "collections/crm.yaml": generated_collection_yaml,
        "conf/models.yaml": generated_models_yaml,
        "conf/processors.yaml": generated_processors_yaml,
        "derivations/profile.yaml": generated_derivation_yaml,
        "derivations/profile_rebuild.yaml": generated_rebuild_yaml,
        "views/history.yaml": generated_view_yaml,
        "artifacts/brief.yaml": generated_artifact_yaml,
        "artifacts/profile_candidate.yaml": generated_candidate_artifact_yaml,
        "packages/profile.yaml": generated_package_yaml,
    },
)
```

The same server-side validation, workspace lock, compatibility checks, semantic
hashing, and atomic replacement apply to file-backed and generated catalogs.

## 9. Troubleshooting

- `401 unauthorized`: recreate and export the workspace bearer key.
- `422 definition`: inspect the returned dotted path and machine-readable code;
  no partial catalog was installed.
- Profile timeout: confirm the worker uses the same `DATABASE_URL`, has
  `LLM_FAKE=0`, and receives the provider API key.
- Provider rejection: verify the model IDs and token-limit field for the
  configured OpenAI-compatible endpoint.
- No trigger yet: inspect event scores; only ready records above the last
  successful profile watermark contribute to the threshold.
- `409 catalog_incompatible`: existing records use an exact collection contract
  missing from the replacement package; version and migrate that contract.
- `409 promotion_stale`: active profile state changed after candidate
  generation; create and review a fresh rebuild rather than forcing it.

The automated acceptance test uses the deterministic fake provider so CI is
offline and repeatable. This quickstart intentionally uses a real provider.

When finished:

```console
make database-down
```
