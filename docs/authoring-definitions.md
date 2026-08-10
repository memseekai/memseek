---
title: Authoring a workspace catalog
eyebrow: Existing guide
---

## The scenario

Memory is becoming a *feature of your product*, not a script on your laptop.
Say you ship a B2B SaaS with an AI assistant: every customer gets their own
workspace, and your memory design — what gets stored, how it's scored, when
profiles update, what the assistant retrieves — is part of the product you
deploy. That raises the questions any platform team will ask:

- Can the memory design go through **code review**, like a schema migration?
- Can we **deploy it per tenant** through an API, atomically, with a version
  we can point to when something misbehaves?
- Can we **roll it back** without corrupting the records customers already
  stored?

This guide is the deployment story that answers yes to all three. Your
catalog is a directory of YAML files that lives in your repository, gets
reviewed in a pull request, and is published to a workspace with one API
call. The service validates the whole graph before selecting it, hashes it,
and stamps that hash on everything the catalog later produces.

## How ownership works

Memseek is a multi-tenant service. The shipped YAML files are only the
bootstrap catalog used when a workspace has not installed a package. A user
owns the catalog for their workspace and uploads it through the API; the
service validates, hashes, stores, and resolves that catalog for every later
request and worker job in that workspace. No user catalog needs to be baked
into application settings.

The user-facing authoring loop is:

```text
durable record contract → processors → Pipelines/triggers
→ views/artifacts → package → POST /catalog
```

The process-wide settings catalog remains useful as a safe default and as an
operator-provided base. It is not the tenant's source of truth after a package
is loaded.

## Service workflow

Create a workspace with the CLI (or the equivalent provisioning API), keeping
the bearer key private. A package upload is a self-contained workspace catalog:
include every collection, processor, derivation, view, artifact, trigger,
model alias, rank profile, search profile, and exact package dependency that
the workspace needs. Uploaded collections and processors are authoritative
resource families rather than being silently merged with the shipped catalog;
operator defaults may still supply model, rank, and search capability files
when the package does not override them. Then upload the package as YAML text:

```console
workspace_json="$(uv run memseek create-workspace acme)"
export MEMSEEK_API_KEY="$(printf '%s' "$workspace_json" | \
  uv run python -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"
export MEMSEEK_AUTH="Authorization: Bearer $MEMSEEK_API_KEY"

curl -sS -X POST http://127.0.0.1:8000/catalog \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  --data-binary @catalog-upload.json
```

`catalog-upload.json` has one exact package reference and a path-to-YAML map:

```json
{
  "package": "acme_memory@1.0.0",
  "files": {
    "collections/chat.yaml": "collections:\n  - name: chat\n    ...\n",
    "conf/processors.yaml": "processors:\n  - name: ...\n",
    "derivations/profile.yaml": "name: profile\n...\n",
    "views/context.yaml": "views:\n  - name: context\n    ...\n",
    "packages/acme_memory.yaml": "name: acme_memory\nversion: 1.0.0\n...\n"
  }
}
```

The response contains the workspace, package, canonical catalog hash, and
sorted uploaded paths. `GET /catalog` returns the selected package metadata
without returning the YAML source. Uploading a replacement is atomic under the
workspace lock. A replacement that would reinterpret existing records (for
example, by removing their exact collection version/hash) is rejected with
`409 catalog_incompatible`; install a compatible version or migrate the data
first.

The same request shape is intentionally easy to wrap in an SDK:

```python
await client.catalog.publish_files(
    package="acme_memory@1.0.0",
    files={path: Path(path).read_text() for path in definition_paths},
)
await client.records.ingest(
    collection="chat", entity="user-42", type="line", text="hello"
)
await client.search(query="hello", collections=["chat"])
```

The service validates all submitted YAML before it changes the workspace
selection. Limits are bounded (256 files, 512 KiB per file, 4 MiB total),
paths must be relative YAML paths in the documented layout, duplicate YAML
keys are errors, and unknown fields are rejected. Error responses include a
machine-readable `error` code and a definition path when validation reaches a
specific field.

For a complete executable package using this workflow, see the
[SDK CRM user-profile quickstart](sdk-user-profile-quickstart.md).

The normal authoring loop is:

```text
collection → processor binding → Pipeline/emission → view/artifact → package
```

Every edge is resolved and validated at startup. A typo or incompatible
reference fails before the API or worker starts.

## Definition tree

Point `Settings` at a deployment-owned tree (or copy the shipped tree and add
your files):

```text
my-memory/
├── conf/
│   ├── models.yaml
│   ├── processors.yaml
│   ├── processors/                 # optional fragments
│   ├── rank_default.yaml
│   └── search_profiles.yaml
├── collections/
│   └── customer.yaml
├── derivations/
│   └── customer_profile.yaml
├── views/
│   └── customer_context.yaml
└── packages/
    └── customer_memory.yaml
```

The loader accepts multiple collection/view/artifact documents in a file, but
each derivation is one mapping and each standalone trigger is one mapping. The
directories are deterministic and YAML duplicate keys are rejected.

## 1. Define the collection

`collections/customer.yaml` declares the durable record contract and which
per-record processors are required before a row becomes ready:

```yaml
collections:
  - name: customer_events
    version: 1
    active: true
    mode: mixed
    schema:
      type: object
      required: [text, channel]
      properties:
        text: {type: string}
        channel: {type: string, enum: [email, call, note]}
      additionalProperties: true
    fields:
      channel:
        path: content.channel
        type: string
        filter: true
        project: true
    required_processors: [customer_importance]
    optional_processors: [customer_sentiment]
    search_profile: pg_default

  - name: customer_profiles
    version: 1
    active: true
    mode: keyed
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
        tombstone: {type: boolean}
      additionalProperties: true
    search_profile: pg_default
```

`required_processors` contains per-record processor names, not derivations. A
required processor is part of the readiness barrier. Optional processors enrich
an already-ready row later. The collection remains the canonical source of
truth; search indexes are projections.

## 2. Define processors

Add a numeric processor to `conf/processors.yaml` when one record should receive one
numeric signal. The result is persisted as `scores.customer_importance` and can
be used by rank expressions or accumulator triggers:

```yaml
processors:
  - name: customer_importance
    kind: score
    source: llm
    input: {collections: [customer_events]}
    scale: [1, 10]
    default: 4
    model: importance_scorer
    prompt: |
      Rate the long-term business importance of each customer event from 1 to 10.
      Return only a JSON array with one number per record.
```

For a structured annotation, add another entry to `conf/processors.yaml`:

```yaml
processors:
  - name: customer_sentiment
    kind: json
    source: llm
    input:
      collections: [customer_events]
      types: [event]
    model: cheap
    prompt: Classify the event sentiment as positive, neutral, or negative.
    output_schema:
      type: object
      required: [label]
      properties:
        label: {type: string, enum: [positive, neutral, negative]}
```

Bind the annotation in the collection when it is required or optional. This
example keeps it optional so readiness does not wait for the sentiment call:

```yaml
required_processors: [customer_importance]
optional_processors: [customer_sentiment]
```

The processor's input scope must include every collection that binds it. The
loader also checks output schemas, model aliases, provider parameters, and
score-field collisions.

## 3. Define a Pipeline and its trigger

`derivations/customer_profile.yaml` consumes ordered customer events and emits
keyed profile facts. The file describes data and computation, not storage
transition machinery:

```yaml
name: customer_profile
trigger:
  accumulator:
    metric: customer_importance
    threshold: 20
  cooldown_s: 60
sources:
  new_events:
    kind: changes
    collections: [customer_events]
    types: [event]
    statuses: [active]
    keyed: false
    max_records: 100
    max_tokens: 12000
  current_profile:
    kind: current
    collections: [customer_profiles]
    types: [fact]
    statuses: [active]
    keys: [needs, commitments, risks]
    max_records: 10
    max_tokens: 4000
limits:
  max_tasks: 1
  max_llm_calls: 2
  max_retrieved_records: 0
  max_visible_records: 100
  max_total_tokens: 20000
  max_wall_s: 90
model: strong
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
        Update the current customer profile for {{entity}}.
        CURRENT PROFILE:
        {{current_profile.rendered}}
        NEW EVENTS:
        {{new_events.rendered}}
        Return only JSON with cited records:
        {"records":[{"key":"needs","text":"...","citations":["uuid"]}]}
emit:
  from: "{{result.records}}"
  collection: customer_profiles
  type: fact
  keys: [needs, commitments, risks]
```

The threshold is evaluated over ready `customer_importance` scores above the
Pipeline cursor. When the sum reaches `20`, the readiness transaction
coalesces a durable `derive/customer_profile` job. The worker resolves the
named Sources, invokes the registered `llm` Task, validates the unified record
drafts, and writes new immutable keyed rows. Because `emit.keys` is present
without `complete: true`, omitted keys remain unchanged. A later event creates
a successor run; it never mutates an old profile row.

The runtime privately captures the cursor, current Source IDs, and all declared
emission heads before computation. It rechecks them before commit. Pipeline
authors do not configure those transition details.

For a write trigger instead of a numeric threshold:

```yaml
trigger:
  write:
    collections: [customer_events]
    types: [event]
    statuses: [active]
```

Read, accumulator, write, cron, and manual conditions can be combined in one
trigger block. Standalone trigger files use the same condition schema and name
the target processor explicitly.

## 4. Define a view

`views/customer_context.yaml` exposes a typed, reusable search contract:

```yaml
views:
  - name: customer_context
    version: 1
    active: true
    parameters:
      entity: {type: string, required: true}
      task: {type: string, required: true}
    query:
      q: "{{task}}"
      sources:
        - name: events
          mode: text
          scope:
            entities: ["{{entity}}"]
            collections: [customer_events]
            types: [event]
          k: 20
          weight: 1.0
      fuse: {kind: rrf, rank_constant: 60}
      k: 20
      render: true
```

Views cannot forward backend-specific JSON. Their fields, scopes, capabilities,
and collection versions are checked at startup, then canonical PostgreSQL rows
are reloaded and rechecked at query time.

## 5. Bind exact definitions into a package

`packages/customer_memory.yaml` makes the deployment reviewable and repeatable:

```yaml
name: customer_memory
version: 1.0.0
collections: [customer_events@1, customer_profiles@1]
processors: [customer_importance, customer_sentiment, customer_profile]
triggers: [customer_profile.default]
views: [customer_context@1]
search_profiles: [pg_default]
```

The package must include every exact collection, processor, trigger, view, and
search-profile dependency. Its hash is included in run and artifact manifests.

## YAML reference and authoring rules

All definition files are ordinary YAML, but each family has a deliberate
contract. Use one of these paths in an upload:

| Path | Top-level shape | Purpose |
| --- | --- | --- |
| `collections/*.yaml` | `collections: [...]` | Durable record schemas, versions, fields, readiness processors, and search profile. |
| `conf/models.yaml` | `aliases: {...}` | Provider/model aliases and bounded model parameters. |
| `conf/processors.yaml` | `processors: [...]` | Embedding, score, JSON, client, and constant processors. |
| `conf/search_profiles.yaml` | `profiles: {...}` | Backend, field mappings, searchable/filterable fields, and rank profile. |
| `conf/rank_default.yaml` | rank expression mapping | Default typed rank AST and allowed score/field references. |
| `derivations/*.yaml` | one Pipeline mapping | Named Sources, registered Tasks, one typed emission, limits, and inline triggers. |
| `triggers/*.yaml` | one trigger mapping | Standalone write, accumulator, read, or cron trigger target. |
| `views/*.yaml` | `views: [...]` | Named, parameterized SearchSpec templates. |
| `artifacts/*.yaml` | `artifacts: [...]` | Deterministic prompt/report renderers and lifecycle policy. |
| `packages/*.yaml` | one package mapping | Exact dependency manifest and the package identity uploaded to a workspace. |

### Collections are durable contracts

Collection `name` and integer `version` identify a contract. The service also
computes a semantic hash. A record stores all three, so changing a schema or a
field projection never silently changes the meaning of old data. Use a new
version for an intentional migration. `schema` uses
[Draft 2020-12 JSON Schema](https://json-schema.org/draft/2020-12), with the
collection-specific requirement that the root is an object containing required
string `text`; use the standard for the rest of the schema language.
`fields` declares typed paths that may be filtered, projected, or ordered.
`required_processors` block readiness, while `optional_processors` run after a
record is already searchable. A `keyed` collection exposes one current value
per `(entity, key)`; a `public` collection must provide its client values at
ingest time.

### Processors are typed capabilities, not arbitrary code

Processors declare their input collections/types and output contract. The
catalog supports `embedding`, `score`, and `json` kinds, with `llm`, `client`,
or `constant` sources where applicable. A score writes a numeric
`scores.<name>` value and can be used by rank expressions or accumulator
triggers. A JSON processor writes a named structured annotation and may
promote numeric leaves through `score_fields`. Processor names are write-once
semantic identities; change behavior by introducing a new name and package
version.

### Pipelines and triggers are bounded computation

Pipelines name every Source they may read, invoke only process-registered Tasks,
and expose one static `emit` destination. Exactly one driver Source — `changes`,
`snapshot`, or `stale_citations` — drives each run; `current`, `record`, and
`view` Sources provide guarded state or bounded supporting evidence. Sources expose typed `.records` and
escaped `.rendered` row values; the element marking those rows as untrusted data
belongs in the task prompt, which the author writes.

Task calls have `id`, `use`, optional typed `input`, and Adapter-specific
static `with` configuration. Built-ins provide JSON model completion, search,
and template rendering. Trusted deployments may register typed async Tasks
before catalog compilation, but workspace YAML cannot upload executable code
or write canonical records directly.

`emit.from` selects one exact Task result containing unified record drafts.
The runtime infers append, partial keyed update, or complete keyed replacement
from `keys` and `complete`, and infers staged review from `review: required`.
It validates citation authority and the destination collection schema before
guarded commit. `limits` bounds Tasks, model calls, retrieved/visible records,
tokens, and wall time.

Inline triggers are convenient for a Pipeline; standalone trigger files are
useful when several packages share a condition. Supported conditions include
write, accumulator, read freshness, and cron. Automatic cycles and excessive
derivation depth are rejected at package load time. See
[Pipelines & triggers](derivations.md) for the complete Interface.

### Views, artifacts, and packages are explicit dependencies

Views are named SearchSpec templates with typed parameters and declared scopes;
they cannot smuggle backend-specific query JSON into the service. Artifacts
render bounded deterministic outputs and record their exact input IDs and
definition hashes. A package lists exact `name@version` references for every
collection, processor, trigger, view, artifact, and search profile it uses.
This makes a package reviewable, reproducible, and safe to roll back.

## Local compilation and generated definitions

For local validation or an operator-controlled deployment, the same compiler
can load a filesystem tree selected by `Settings`:

```python
from memseek.api import create_app
from memseek.config import Settings
from memseek.definitions import load_definition_catalog

settings = Settings(
    collections_dir="my-memory/collections",
    derivations_dir="my-memory/derivations",
    views_dir="my-memory/views",
    packages_dir="my-memory/packages",
    processors_file="my-memory/conf/processors.yaml",
    models_file="my-memory/conf/models.yaml",
    rank_default_file="my-memory/conf/rank_default.yaml",
    search_profiles_file="my-memory/conf/search_profiles.yaml",
)
catalog = load_definition_catalog(settings)
app = create_app(settings, catalog=catalog)
```

Python-authored `DefinitionSources` is also available when an application wants
to generate or modify definitions dynamically. It produces the same immutable
catalog and passes through the same validation rules; an SDK can serialize its
result into the `files` map sent to `POST /catalog`. YAML remains the recommended
review/deployment representation for a user-owned catalog.
