---
title: Python SDK
eyebrow: Async application client
---

`MemseekClient` is a small async client for talking to a Memseek workspace
from Python. It wraps the HTTP API so your application can publish a catalog,
write records, read current state, search, run derivations, render artifacts,
and feed real outcomes back into maintained knowledge — without hand-building
requests.

Here is the whole loop most applications need, start to finish:

```python
from memseek.sdk import MemseekClient

async with MemseekClient("http://127.0.0.1:8000", api_key) as client:
    # 1. Put your memory design in effect.
    await client.catalog.publish(
        package="crm_user_profile@2.0.0",
        directory="examples/crm_profile_catalog",
    )

    # 2. Write down something that happened.
    await client.records.ingest(
        collection="crm_events",
        entity="contact:avery-chen",
        type="crm_event",
        text="Avery committed to an August launch.",
        content={"source": "salesforce", "event_kind": "commitment"},
        dedupe_key="crm:avery:launch:1",
    )

    # 3. Read the self-maintaining profile back.
    document = await client.document(
        entity="contact:avery-chen",
        collections="user_profiles",
    )
    for belief in document["beliefs"]:
        print(belief["key"], "→", belief["text"])
```

That is the entire shape: publish once, ingest as things happen, read current
state whenever you need it. The rest of this page walks through each call and
what it gives back.

Every method returns a plain `dict` parsed from the JSON response, and any
non-2xx status raises [`MemseekHTTPError`](#handling-errors). The examples show
the keys worth knowing on each response.

The SDK is a thin async client, not a separate memory model: its calls map to
the [HTTP API guide](api-surface.md). Read [Core concepts](concepts.md) or look
up an unfamiliar term in the [Glossary](glossary.md) before choosing between a
timeline, document, search, view, or artifact.

## Connecting

```python
from memseek.sdk import MemseekClient

async with MemseekClient("http://127.0.0.1:8000", api_key) as client:
    catalog = await client.catalog.retrieve()
```

The client is an async context manager — use it with `async with` so it closes
cleanly. It creates and owns an `httpx.AsyncClient` by default. If your
application already manages an HTTP transport (or a test wants to inject one),
pass it as `client=`:

```python
async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as http:
    client = MemseekClient("http://127.0.0.1:8000", api_key, client=http)
    ...  # the SDK will not close a transport it does not own
```

The `api_key` is the workspace bearer token you got from
`memseek create-workspace`. It goes in every request's `Authorization` header;
it is never written to disk by the SDK.

## Publishing a catalog

Publishing is what puts a memory design into effect for the workspace. It is
atomic: the service validates the whole catalog first, and either switches to
the new one or changes nothing.

```python
# From a directory of YAML files (read recursively, in sorted order):
metadata = await client.catalog.publish(
    package="crm_user_profile@2.0.0",
    directory="examples/crm_profile_catalog",
)

# Or from YAML you generated in code:
await client.catalog.publish_files(
    package="crm_user_profile@2.0.0",
    files={
        "collections/crm.yaml": crm_yaml,
        "conf/models.yaml": models_yaml,
        "packages/crm_user_profile.yaml": package_yaml,
    },
)

# Read back what is currently selected:
metadata = await client.catalog.retrieve()   # {"package": ..., "catalog_hash": ...}
```

Both publish methods send the **whole** catalog and name the exact package to
activate — there are no partial updates. `retrieve()` returns the selected
package identity and catalog hash without returning any YAML.

### Checking a publish before you make it

Once a workspace holds records, ask what a publish would do to them:

```python
report = await client.catalog.check(
    package="crm_user_profile@2.1.0",
    directory="examples/crm_profile_catalog",
)
if not report["publishable"]:
    for blocker in report["blockers"]:
        print(blocker["collection"], blocker["rows"], blocker["required_action"])
else:
    await client.catalog.publish(
        package="crm_user_profile@2.1.0",
        directory="examples/crm_profile_catalog",
    )

# How does the catalog I already published stand against its own records?
current = await client.catalog.compatibility()

# And which old definitions does nothing reference any more?
prune = await client.catalog.prune()
```

`check()` installs nothing. `verdict` is `invisible`, `additive`, or
`reinterpreting`; `publishable` is false only when `blockers` is non-empty. A
refused publish raises `MemseekHTTPError` whose `payload["compatibility"]` carries
the same report. See [Changing definitions](changing-definitions.md).

`prune()` is read-only and reports each inactive definition with the number of
records, annotations, or runs still bound to it, so retiring a collection version
is a decision made against evidence. It never offers the active choice.

### Evolving without a shell

Three more operations used to be reachable only from the `memseek` CLI, which a
hosted caller does not have:

```python
# Make an external search index adopt a newly declared field. Canonical records
# are untouched; this enqueues projection jobs the worker drains.
await client.reindex(since_seq=0)          # or reset=True, confirm=True

# Repoint a changes cursor after a deliberate source-scope change.
await client.rebind_cursor("profile", entity="contact:avery-chen", policy="reset")

# Remove an entity, or explicit records, with the provenance closure.
await client.erase(entity="contact:avery-chen")
```

`reindex` takes exactly one scope: `since_seq` (resume from a sequence) or
`reset=True` (every ready record), which outside a test database also needs
`confirm=True`. `rebind_cursor` writes an audit row naming the old and new source
hashes under either policy. `erase` cannot be undone, and its closure means erasing
an original also removes what was derived from it.
[A migration, start to finish](migration-walkthrough.md) uses all three in
sequence.

### Applying a processor to records you already have

Binding a processor annotates records ingested afterwards. To reach the ones
already stored, start a backfill and watch it:

```python
# No budget: reach every record. This is the normal case.
handle = await client.backfill.start(
    collection="crm_events", version=1, processor="sentiment_v2"
)
progress = await client.backfill.retrieve(handle["id"])
print(progress["state"], progress["annotated"], "of", progress["scanned"], "scanned")

await client.backfill.list()             # recent backfills, newest first
await client.backfill.cancel(handle["id"])   # keeps what it already wrote
```

Pass `max_rows` only when you want a ceiling — a cost cap or a canary on part of
the corpus. Without it the backfill runs until every eligible record is annotated,
in bounded batches that leave every other worker lane its turn.

A backfill never overwrites an existing annotation, resumes after a restart, and
allows one live backfill per target. See
[Changing definitions](changing-definitions.md#how-a-backfill-works) for the
counters, the bounds, and what `done` guarantees.

## Writing records

A record is one thing that happened (or one current fact). Write one with
`ingest`, or a batch with `ingest_many`:

```python
one = await client.records.ingest(
    collection="crm_events",
    entity="contact:avery-chen",
    type="crm_event",
    text="Avery prefers concise written updates.",
    content={"source": "support", "event_kind": "preference"},
    occurred_at="2026-07-16T12:00:00Z",   # optional; when it actually happened
    dedupe_key="crm:avery:preference:1",  # optional; makes retries safe
)

many = await client.records.ingest_many([
    {"collection": "crm_events", "entity": "contact:avery-chen",
     "type": "crm_event", "text": "..."},
    {"collection": "crm_events", "entity": "contact:avery-chen",
     "type": "crm_event", "text": "..."},
])
```

`ingest` takes the record fields as keyword arguments;
[Collections](collections.md#how-records-enter-a-collection) lists them all.
`ingest_many` writes a bounded batch in one atomic request.

The response separates what happened to each row:

```python
{
  "inserted": [{"index": 0, "id": "…", "ready": False}],
  "duplicates": [],
}
```

`ready: False` is normal — it means required enrichment (embedding, scoring) is
still running; the row is durably stored. A `dedupe_key` makes a retry safe:
re-sending the identical record lands it under `duplicates` instead of writing
a second copy. Re-using a key with *different* data is a `409` conflict.

## Reading current state

`document` returns an entity's current keyed state — one value per slot, newest
wins — plus retractions and derivation freshness:

```python
document = await client.document(
    entity="contact:avery-chen",
    collections="user_profiles",
    status="active",
)

for belief in document["beliefs"]:
    print(belief["key"], "→", belief["text"])
```

This is the call your application makes to answer "what do we currently know
about this contact?" Any extra keyword arguments pass straight through to
`GET /document`, so you can narrow collections or change status. See the
[API surface](api-surface.md#get-document-what-is-current-now) for the full
response shape.

## Searching

`search` runs a hybrid (semantic + keyword) search by default and returns
prompt-ready results:

```python
results = await client.search(
    query="open commitments",
    collections=["crm_events"],
    entity="contact:avery-chen",
    mode="hybrid",           # hybrid | vector | text | recent | structured
    k=10,
    include=["text", "scores", "occurred_at"],
    render=True,             # also return a token-bounded text block for a prompt
)
```

`search` covers the common single-source query directly. For multi-source
fusion, custom rank expressions, or typed filters, pass any additional
[SearchSpec](views-search.md) fields as keyword arguments — they are merged
into the request. Better still, define the query once as a
[view](views-search.md) and keep your application code out of the search
details.

## Querying named and graph views

The SDK exposes the same generic view interface used by HTTP and MCP. Discover
the catalog contracts, then query a search or graph-derived view by name:

```python
catalog = await client.views()
graph = await client.query_view(
    "dependency_graph",
    seed="api",
    predicates=["depends_on"],
    direction="out",
    depth=2,
    limit=20,
)

for path in graph["paths"]:
    print(path["nodes"])
```

Graph data is stored as ordinary records. See [Graph data](graph-data.md)
for edge collections, role mappings, citations, orphan views, and selecting
between multiple graphs.

## Asking a question

`search` gives you records; `answer` gives you prose the model had to ground in
those records:

```python
result = await client.answer(
    question="What did Avery commit to for the August launch?",
    rewrite=True,        # spend one cheap call improving the retrieval query
)

print(result["answer"])
print(result["citations"])   # record ids the answer actually leaned on
print(result["gaps"])        # what the memory did not cover
```

This is the one read that calls a model synchronously, so unlike `search` and
`render_artifact` it needs real provider credentials: under `LLM_FAKE=1` it fails
with `502 answer_model`. `anchor`, `graph`, `since`, `until`, and `save` are the remaining
parameters; see [`POST /answer`](api-surface.md#cited-synthesis-post-answer) for
the scope it searches, what `save: true` writes, and the failure codes.

## Following a citation and auditing a belief

Every derived belief carries citations, and two calls turn those ids into an
explanation. `record` dereferences one citation into the concrete evidence
behind it; `document_history` returns every version of one keyed slot, newest
first:

```python
belief = document["beliefs"][0]

for citation in belief["citations"]:
    evidence = await client.record(citation)
    print(evidence["text"], evidence["occurred_at"])

versions = await client.document_history(
    entity="contact:avery-chen",
    collection="user_profiles",
    key="commitments",
)
for version in versions["versions"]:
    print(version["seq"], version["run_id"], version["content"]["text"])
```

Together these answer "why does memory believe this, and when did it change?"
without leaving Python. Each version names the run that wrote it and the
evidence that run cited, so the whole chain is reconstructable.

## Running a derivation and waiting for it

Most derivations run on their own — a [trigger](triggers.md) fires when enough
new evidence arrives, and you just read the updated document later. When you
want to run one *now* (a manual rebuild, a snapshot reconstruction), enqueue it
and wait for the job to finish:

```python
import asyncio

queued = await client.run_processor("crm_profile_rebuild", entity="contact:avery-chen")

# Poll the job until it produces a successful run (or dies):
while True:
    job = await client.job(queued["job_id"])
    if job.get("successful_run_id"):
        break
    if job["state"] == "dead":
        raise RuntimeError(job)
    await asyncio.sleep(0.5)

run_id = job["successful_run_id"]
```

`run_processor` enqueues the derivation and returns immediately with a
`job_id`; the worker does the actual model work in the background. `job` reports
that job's state and, once it succeeds, the `successful_run_id` you use to
inspect or promote the result.

!!! note "This polling loop is boilerplate you shouldn't have to write"
    Waiting on a job by hand — the loop above — is the sharpest edge in the
    current SDK. A `wait_for_run` helper is the first item in the
    [ergonomics proposal](#where-the-sdk-is-headed); until it lands, copy this
    loop.

## Reviewing and promoting

A reviewed derivation stages its output as a **draft** for you to inspect
before it goes live. Read the run to see what it proposes, then promote it:

```python
review = await client.run(run_id)
candidate = review["run"]["content"]["candidate_set"]

print(candidate["covered_keys"])   # which slots the proposal fills
for change in candidate["divergence"]:
    print(change["key"], change["change"])   # added | changed | removed | unchanged

promoted = await client.promote(
    entity="contact:avery-chen",
    source_run_id=run_id,
    artifact="crm_profile_candidate",
)
```

`run` returns the full audited receipt for a run: the normalized proposal, its
divergence from current state, and the emitted rows. `promote` activates that
exact proposal atomically — it copies the draft values into new active
successor records and edits nothing. If live state changed after the candidate
was generated, promotion raises `MemseekHTTPError` with status `409` and a
`promotion_stale` payload, and activates nothing. See
[Runtime receipts and Candidate Sets](evaluation-bases.md) for the guarantees
behind this.

## Rendering artifacts

An [artifact](artifacts.md) assembles current memory into one finished output —
a system prompt, a briefing — in a single call:

```python
brief = await client.render_artifact(
    "crm_profile_brief",
    entity="contact:avery-chen",
    task="prepare the account update",
)
print(brief["rendered"])
```

Pass the artifact's parameters as keyword arguments. The response carries the
rendered text plus a manifest recording exactly which records went in, the
definition and package hashes, freshness, and what (if anything) was truncated.
A live render involves no LLM call and is deterministic for the same inputs.

## Using an artifact and learning from the outcome

`render_artifact` gives you text. When you also want the outcome of that run to
be able to improve the memory that produced it, **bind an artifact use** instead:

```python
handle = client.artifact("daily_agent_prompt")

async with handle.use({"entity": "agent:ada", "task": user_message}) as use:
    answer = await openai.responses.create(
        model="gpt-5",
        instructions=use.content,
        input=user_message,
    )

# One extra column beside your own result — that is the whole integration cost.
await messages.create(
    role="assistant",
    text=answer.output_text,
    memseek_use_id=use.id,
)
```

`handle.use(...)` is an async context manager: it renders, registers the handle,
and keeps OpenTelemetry correlation attributes active for the duration of the
`with` block without ever inspecting your SDK's request or response. Use
`handle.bind(...)` for the same result without the telemetry scope, and
`handle.render(...)` when you only want the text and manifest.

The `BoundArtifact` you get back carries `content` (the render), `id` (the field
to persist), `telemetry_attributes` (safe scalars for a span), `snapshot_id`,
`learning_target`, and `truncated`.

Later — minutes or weeks later — the outcome arrives, and all you need is that ID:

```python
message = await messages.get(message_id)

await client.feedback.submit(
    use_id=message.memseek_use_id,
    kind="thumbs_down",
    source="end_user",
    comment="It said the refund was complete.",
    label="incorrect_status",
    dedupe_key=f"message:{message.id}:thumbs_down",
)
```

Or the fluent form, when you already know which use you are talking about:

```python
feedback = client.feedback.for_use(message.memseek_use_id)

await feedback.correction(expected="Tell the customer the refund is pending.")
await feedback.evaluation(score=0.2, label="incorrect_status")
```

Each submission writes one ordinary `learning_signals` record, routed to the
maintained artifact the render declared as its learning target — so a candidate
derivation can pick it up without your application knowing the nested artifact
structure. `client.artifact_use(use_id)` reads a handle's metadata back for
debugging.

Resubmitting the same `dedupe_key` with the same payload is idempotent and
returns `{"duplicate": true}`. A handle past `ARTIFACT_USE_RETENTION_DAYS`
raises `MemseekHTTPError` with status `410`. See
[Artifact uses & feedback](artifact-uses.md) for every field, the learning-target
contract, and what snapshot provenance can honestly claim.

## Listing audited runs

`runs` lists an entity's past runs, newest first, for audit and debugging:

```python
history = await client.runs(
    entity="contact:avery-chen",
    processor="crm_profile",
    source="changes",   # changes | snapshot
    limit=20,
)
```

Filter by `processor`, `operation`, and derivation `source`. Each summary is
compact; fetch one in full with `client.run(run_id)`.

## Handling errors

Any non-2xx response raises `MemseekHTTPError`, which carries the status code
and the structured error payload:

```python
from memseek.sdk import MemseekHTTPError

try:
    await client.search(query="…", collections=["missing_collection"])
except MemseekHTTPError as error:
    print(error.status_code)   # e.g. 422
    print(error.payload)       # structured API error when available
```

The statuses you will actually branch on:

| Status | Means | Typical fix |
| --- | --- | --- |
| `401` | Bad or missing workspace key | Recreate and re-export the bearer key. |
| `404` | Unknown artifact, record, or artifact use — including one owned by another workspace | Check the identity; a foreign ID is deliberately indistinguishable from a missing one. |
| `409` | A conflict — dedupe mismatch, incompatible catalog, a live backfill for the same target, or stale promotion | Inspect `payload["error"]`; each has a distinct recovery. An incompatible catalog also carries `payload["compatibility"]` naming every blocker. |
| `410` | An artifact use expired and can no longer receive feedback | Nothing to recover; raise `ARTIFACT_USE_RETENTION_DAYS` if your feedback window is genuinely longer. |
| `422` | Request or catalog validation failed | Read the dotted path in the payload; nothing was written. |

## Where the SDK is headed

The current client is a faithful, minimal wrapper: every method mirrors one
endpoint and returns the raw JSON `dict`. That keeps it transparent, but it
leaves real ergonomics on the table — hand-written polling loops, deep dictionary
indexing like `review["run"]["content"]["candidate_set"]["divergence"]`, and no
autocompletion for response fields. A set of proposed improvements — a
`wait_for_run` helper, typed result objects, async iteration over paginated
reads, and an ingest builder — is tracked separately. If any of those would
help your integration, they are worth pulling forward.
