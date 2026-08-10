---
title: A migration, start to finish
eyebrow: Worked example
---

This is one catalog followed through four changes, in the order a real team hits
them. Nothing is skipped: every publish, every call, and every response below is
what the service actually returns.

Every step is an ordinary authenticated request made with the workspace key, so
the whole walkthrough runs from application code against a hosted service — no
shell on the server, no filesystem access, nothing that assumes you can log in
somewhere. The CLI can do all of it too, and
[Alternative entry points](#alternative-entry-points) maps each step to its
`memseek` command and raw HTTP route; nothing below requires them.

The app is a support desk. It stores tickets, triages them with a model, and
summarizes each account. Over a few weeks it needs to:

1. **filter tickets by channel** — a field it did not declare on day one;
2. **improve the triage prompt** — and apply it to the tickets already stored;
3. **make `channel` required** and constrain it to known values;
4. **move the existing tickets** onto that stricter contract.

Only the third of those needs a new collection version. That is the point of the
walkthrough.

If you want the reference rather than the story, see
[Changing definitions](changing-definitions.md).

## Day one

A catalog needs at least one collection, one processor, one derivation, one view,
and one artifact — the loader refuses an empty family, so a minimal real catalog
has all five. Here is the part that matters for the rest of this page.

`collections/tickets.yaml`:

```yaml
collections:
  - name: tickets
    version: 1
    active: true
    mode: event
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
      # Content keys the schema does not name are still accepted and stored.
      additionalProperties: true
    fields:
      severity:
        path: annotations.triage_v1.severity
        type: integer
        filter: true
        sort: true
    required_processors: [embedding_v1, triage_v1]
    optional_processors: [importance]
    search_profile: pg_default

  - name: summaries
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
    required_processors: [embedding_v1]
    search_profile: pg_default
```

`conf/processors.yaml`:

```yaml
processors:
  - name: embedding_v1
    kind: embedding
    input: {collections: [tickets, summaries]}

  - name: importance
    kind: score
    source: constant
    input: {collections: [tickets]}
    scale: [1, 10]
    value: 5

  - name: triage_v1
    kind: json
    source: llm
    input: {collections: [tickets]}
    model: cheap
    prompt: Rate the severity of this support ticket from 1 to 5 and say why.
    output_schema:
      type: object
      required: [severity]
      properties:
        severity: {type: integer, minimum: 1, maximum: 5}
        reason: {type: string}
    default_output: {severity: 3, reason: unclassified}
```

`derivations/account_summary.yaml` — note the pinned source, which
[matters later](#why-the-source-is-pinned):

```yaml
name: account_summary
trigger:
  accumulator: {metric: count, threshold: 3}
  cooldown_s: 60
sources:
  new_tickets:
    kind: changes
    collections: [tickets]
    collection_versions: {tickets: [1]}   # pin it
    types: [ticket]
    statuses: [active]
    keyed: false
    max_records: 50
    max_tokens: 16000
    allow_empty: false
model: cheap
limits: {max_tasks: 1, max_llm_calls: 2, max_retrieved_records: 0,
         max_visible_records: 60, max_total_tokens: 30000, max_wall_s: 60}
tasks:
  - id: result
    use: llm
    with:
      output_schema: { ... }              # one keyed record: open_themes
      prompt: |
        Summarize the recurring themes across these tickets for {{entity}}.
        {{new_tickets.rendered}}
emit:
  from: "{{result.records}}"
  collection: summaries
  type: summary
  keys: [open_themes]
```

Publish it and start writing tickets. One client, held open for the rest of the
walkthrough:

```python
from memseek.sdk import MemseekClient

async with MemseekClient("https://memseek.example.com", api_key) as client:
    await client.catalog.publish(package="support@1.0.0", directory="./catalog")

    await client.records.ingest(
        collection="tickets", entity="acct:northwind", type="ticket",
        text="Cannot log in to the dashboard", content={"channel": "email"},
    )
```

`publish` reads the YAML directory in your own process and sends the files, so
the service never needs to see your working tree. If the catalog is assembled in
code rather than on disk, `publish_files(package=..., files={...})` takes the same
map directly.

Six tickets go in, across three channels. Note that `channel` is already being
stored even though the schema never mentions it — `additionalProperties: true`
accepts it, and it is durable, searchable text from the moment it lands.

## Change 1 — filter by channel

Now the team wants "show me email tickets". That means *declaring* `channel`: a
named schema property, and a `fields` entry so it can be filtered and sorted.

Ask what that would do first. `check` compiles and plans exactly as a publish
does, then returns the plan instead of applying it — nothing is installed:

```python
report = await client.catalog.check(package="support@1.1.0", directory="./catalog")
```

```json
{
  "verdict": "additive",
  "publishable": true,
  "stored_rows": 6,
  "changes": [
    {
      "family": "collection", "name": "tickets", "version": 1,
      "status": "modified", "class": "additive",
      "differing_fields": ["fields", "schema"],
      "detail": "record contract grew; new schema properties ['channel']; new declared fields ['channel']",
      "required_action": "existing values for ['channel'] are verified against the new schema on publish"
    }
  ],
  "rewrites": [
    {"collection": "tickets", "version": 1, "rows": 6,
     "reason": "additive_contract", "verify_keys": ["channel"]}
  ],
  "blockers": [],
  "notes": ["6 record(s) have their stored contract hash rewritten forward on publish"]
}
```

Read that back: **additive, publishable, no new version.** The `verify_keys` entry
is the publish saying "your schema already allowed arbitrary keys, so I will check
the `channel` values those six records already hold against your new declaration
before accepting it". If one of them held `channel: 7`, the publish would refuse
and name the record.

The change itself:

```yaml
     schema:
       type: object
       required: [text]
       properties:
         text: {type: string}
+        channel: {type: string}          # optional: no new version
       additionalProperties: true
     fields:
+      channel: {path: content.channel, type: string, filter: true, sort: true}
       severity:
         path: annotations.triage_v1.severity
```

Publish it:

```python
result = await client.catalog.publish(package="support@1.1.0", directory="./catalog")
```

```json
{"package": {"name": "support", "version": "1.1.0"}, "loaded": true, "rewritten_records": 6}
```

`rewritten_records: 6` is the six stored tickets moving onto the new contract,
inside the publish transaction. Nothing to run, nothing to backfill — and the
filter works immediately on records written days ago:

```python
hits = await client.search(
    query="",
    collections=["tickets"],
    mode="structured",
    k=10,
    where={"channel": {"eq": "email"}},
    order_by=[{"field": "channel", "direction": "asc"}],
)
len(hits["hits"])
# 3 — every email ticket, including the ones ingested before the field existed
```

That works because PostgreSQL resolves declared field paths at query time. If you
use an external search backend, ask for a projection rebuild so the index picks up
the new attribute:

```python
rebuilt = await client.reindex(since_seq=0)
# {"workspace": "support", "mode": "incremental", "target_count": …, "enqueued_jobs": 1}
```

That queues ordinary projection jobs for the worker to drain; canonical
PostgreSQL is never rewritten by it. `since_seq` is the sequence to resume from —
`0` covers everything, and a later rebuild can start where the last one ended.
Note that `target_count` counts every ready record in the workspace, not just the
six tickets: a rebuild is workspace-scoped, because an index is.

## Change 2 — a better triage prompt

Triage is under-rating billing problems. The fix is a better prompt — but a
processor's annotations are write-once, so editing `triage_v1` in place would
publish successfully and then quietly do nothing to the tickets already triaged.

Publish a **new name** instead, and declare that it replaces the old one:

```yaml
   - name: triage_v2
     kind: json
     source: llm
     input: {collections: [tickets]}
     model: cheap
     prompt: |
       Rate this support ticket's severity from 1 to 5. Treat data loss and
       billing errors as at least 4.
     output_schema: { ... }               # same shape as triage_v1
     default_output: {severity: 3, reason: unclassified}
     supersedes: triage_v1
```

Then bind it and point the existing `severity` field at it:

```yaml
     fields:
       severity:
-        path: annotations.triage_v1.severity
+        path: annotations.triage_v2.severity
         type: integer
         filter: true
         sort: true
     required_processors: [embedding_v1, triage_v1]
-    optional_processors: [importance]
+    optional_processors: [importance, triage_v2]
```

Two things to notice. `triage_v1` **stays required** — that is what keeps the
`severity` field legal to filter on, because every ticket is guaranteed to hold at
least the oldest annotation in the chain. And `triage_v2` arrives as an *optional*
binding, which is outside the record contract entirely.

The preflight:

```json
{
  "verdict": "additive",
  "publishable": true,
  "changes": [
    {
      "family": "collection", "name": "tickets", "version": 1, "class": "additive",
      "differing_fields": ["fields", "optional_processors"],
      "detail": "record contract grew; fields ['severity'] now prefer a superseding annotation",
      "required_action": "none — stored contract hashes are rewritten forward on publish"
    },
    {"family": "processor", "name": "triage_v2", "status": "added", "class": "additive"}
  ],
  "rewrites": [
    {"collection": "tickets", "version": 1, "rows": 6, "reason": "additive_contract",
     "verify_absent_annotations": ["triage_v2"]}
  ],
  "blockers": []
}
```

Repointing a field is normally a reinterpreting change. It is additive *here*
because `triage_v2 supersedes triage_v1`: the field now reads
`annotations.triage_v2.severity` and falls back to `annotations.triage_v1.severity`,
so every stored ticket keeps answering with exactly the value it answered with
before. `verify_absent_annotations` is the publish confirming the one case that
would break that — a ticket already holding a `triage_v2` annotation.

Publish it. The `severity` field keeps working continuously, before any backfill
runs, because the fallback answers for every existing ticket.

### Reach the tickets you already have

New tickets get `triage_v2` on the way in. The ones already stored need a
backfill, and it needs no budget:

```python
handle = await client.backfill.start(
    collection="tickets", version=1, processor="triage_v2"
)
```

```json
{
  "id": "2e920f7a-…", "collection": "tickets", "version": 1, "processor": "triage_v2",
  "state": "queued", "cursor_seq": 0, "scanned": 0, "annotated": 0, "max_rows": null
}
```

`max_rows: null` means "reach every eligible record" — pass it only to impose a
ceiling. The call returns the handle immediately; the work runs in the worker, and
the handle is how you watch it:

```python
progress = await client.backfill.retrieve(handle["id"])
```

```json
{
  "state": "done", "scanned": 6, "annotated": 6, "cursor_seq": 0,
  "completed_at": "2026-08-04T15:02:45.835917+00:00"
}
```

`scanned == annotated` means nothing failed. `cursor_seq: 0` on a finished
backfill is deliberate: the worker sweeps once more from the first record and only
reports `done` when that sweep finds nothing left, so the zero is the evidence.

Every ticket now holds *both* annotations — `triage_v1` untouched and auditable,
`triage_v2` alongside it — and `severity` reads the newer one.

## Change 3 — make channel required

Six weeks in, `channel` is load-bearing: routing depends on it, and a ticket
without one is a bug. It should be required, and limited to the three channels
that exist.

That **is** a reinterpreting change — an existing ticket without `channel` would
be invalid under it — so it arrives as a new version. Keep version 1 in the
catalog, mark it inactive, and add version 2:

```yaml
collections:
  - name: tickets
    version: 1
    active: false          # keep it: six tickets are bound to this contract
    # ...everything else byte-for-byte unchanged...

  - name: tickets
    version: 2
    active: true
    mode: event
    schema:
      type: object
      required: [text, channel]                       # now required
      properties:
        text: {type: string}
        channel: {type: string, enum: [email, portal, phone]}
      additionalProperties: false                     # and closed
    fields:
      channel: {path: content.channel, type: string, filter: true, sort: true}
      severity:
        path: annotations.triage_v2.severity
        type: integer
        filter: true
        sort: true
    required_processors: [embedding_v1, triage_v1]
    optional_processors: [importance, triage_v2]
    search_profile: pg_default
```

The preflight confirms adding a version is itself additive — nothing is stranded,
because version 1 is still there:

```python
report = await client.catalog.check(package="support@2.0.0", directory="./catalog")
```

```
verdict: additive | collection changes: [('tickets', 2, 'additive')]
```

Once new tickets land in version 2 they must carry a valid `channel`. The six older
tickets are still valid members of version 1, still searchable, still enriched. The
publish itself waits for [Change 4](#change-4-move-the-old-tickets-forward),
because the migration that moves those six forward is part of the same `2.0.0`
package.

Note that version 2 still lists `triage_v1` in `required_processors`. It does not
have to: you are minting a new contract, so this is the one moment when promoting
`triage_v2` to required is free. The version above keeps `triage_v1` required
purely so version 2 differs from version 1 in the schema alone — one change at a
time is easier to reason about, and easier to roll back.

## Change 4 — move the old tickets forward

Records are immutable, so a migration does not rewrite them: it **copies them
forward with lineage**. That is what a derivation does, so a migration is an
ordinary derivation using the deterministic `map_records` Task.

Use a **`changes`** source. Emission is capped at 100 records per run, so covering
a corpus needs a cursor — and a `changes` source has one, consumes forward, and
queues its own successors until it drains.

`derivations/tickets_v1_to_v2.yaml`:

```yaml
name: tickets_v1_to_v2
sources:
  legacy:
    kind: changes
    collections: [tickets]
    collection_versions: {tickets: [1]}     # read the old contract explicitly
    statuses: [active]
    keyed: false
    max_records: 100
    max_tokens: 40000
    allow_empty: false
model: null
limits: {max_tasks: 1, max_llm_calls: 0, max_retrieved_records: 0,
         max_visible_records: 100, max_total_tokens: 40000, max_wall_s: 30}
tasks:
  - id: migrated
    use: map_records
    input: {records: "{{legacy.records}}"}
    with:
      keep: [text]
      set:
        # Carry the value over, and supply the default the new contract needs
        # for any ticket that never had one.
        channel: {from: content.channel, default: email}
      carry_key: false
emit:
  from: "{{migrated}}"
  collection: tickets
  collection_version: 2
  type: ticket
  max_records: 100
```

`keep` and `set` may not both produce the same property — `channel` comes from
`set` because that is where the default lives, so `keep` lists only `text`.

Publish it, then run it once per entity:

```python
await client.catalog.publish(package="support@2.0.0", directory="./catalog")

job = await client.run_processor("tickets_v1_to_v2", entity="acct:northwind")
# {"job_id": "…", "enqueued": true, "coalesced": false, "run_after": "…"}
```

`run_processor` enqueues the lane and returns; `client.job(job["job_id"])` reports
its state, and `client.runs(entity="acct:northwind", processor="tickets_v1_to_v2")`
lists the audited runs it produced.

The lane drains itself. Afterwards:

```
tickets by version:        {1: 6, 2: 6}
version 2 rows with lineage: 6
by version and channel:    v1: 3 email, 2 portal, 1 phone
                           v2: 3 email, 2 portal, 1 phone
```

Every ticket was copied exactly once, with its channel preserved, and every new
record cites the original it came from. Running the migration again does nothing —
the cursor is drained.

## What is left over

The originals still exist. That is the immutability guarantee working as intended,
not an oversight — a copy-forward migration adds the new contract's records, it
does not delete history.

So asking what is safe to retire will tell you version 1 is **not**:

```python
prune = await client.catalog.prune()
```

```json
{
  "candidates": [
    {"family": "collection", "name": "tickets", "version": 1,
     "references": 6, "reference_kind": "records",
     "detail": "6 record(s) are bound to this contract", "safe_to_delete": false}
  ],
  "safe_to_delete": []
}
```

That leaves you a deliberate choice, which is the right place for it:

- **Keep both.** Version 1 is the original record of what came in; version 2 is
  the shape your application reads. Nothing more to do, and `tickets@1` stays in
  the package.
- **Retire the originals.** Erase the version 1 records once you are satisfied
  with the copies — `await client.erase(record_ids=[...])`, or
  `client.erase(entity=...)` for a whole account. Erasure expands the provenance
  closure, so erasing an original also removes the copy derived from it — check
  that is what you want before calling it, because it cannot be undone. After the
  records are gone, `catalog.prune()` reports `tickets@1` as safe to delete and you
  can drop it from the package.

Until then, leave version 1 in the catalog. It costs nothing, and removing it
while records reference it is exactly what `409 catalog_incompatible` prevents.

## Why the source is pinned

Look back at `account_summary` on day one:

```yaml
    collections: [tickets]
    collection_versions: {tickets: [1]}
```

A source that names a collection *without* pinning versions follows whichever
version is active. So the moment `tickets@2` became active in Change 3, an
unpinned `account_summary` would silently start reading a different contract — and
because its `changes` cursor was established over version 1, the preflight reports
it as reinterpreting:

```json
{
  "family": "derivation", "name": "account_summary", "class": "reinterpreting",
  "differing_fields": ["sources"],
  "detail": "completed runs keep their recorded contract; a changes source keeps its cursor only if its source scope is unchanged",
  "required_action": "publish under a new derivation name"
}
```

With the pin, the same publish reports no derivation change at all. The rule:

> **Pin `collection_versions` in any derivation source whose collection you might
> version.** Then a version migration is a decision you make about that
> derivation, not something that happens to it.

If a cursor has already drifted this way, repoint it deliberately:

```python
await client.rebind_cursor("account_summary", entity="acct:northwind", policy="carry")
```

`policy="reset"` re-reads the widened scope from the beginning; `policy="carry"`
keeps the watermark and adopts the new scope. Both write an audit row naming the
old and new source hashes, so the decision is on the record.

## Alternative entry points

Every step above is one authenticated HTTP call. The Python client is the shortest
way to make it, the CLI is convenient on a laptop against a database you can
reach, and the route is there when you are neither — a Go service, a Lambda, an
agent with an HTTP tool and nothing else.

| Step | Client | CLI | Route |
| --- | --- | --- | --- |
| Publish a catalog | `catalog.publish()` / `catalog.publish_files()` | — | `POST /catalog` |
| Preflight a publish | `catalog.check()` | `memseek catalog-check` | `POST /catalog?dry_run=true` |
| Standing of what is installed | `catalog.compatibility()` | — | `GET /catalog/compatibility` |
| Write a ticket | `records.ingest()` | — | `POST /records` |
| Filter and sort | `search()` | — | `POST /search` |
| Apply a processor to history | `backfill.start()`, `backfill.retrieve()` | `memseek backfill` | `POST /backfill`, `GET /backfill/{id}` |
| Run the migration | `run_processor()` | — | `POST /processors/{name}/run` |
| Rebuild external projections | `reindex()` | `memseek reindex` | `POST /reindex` |
| What is safe to retire | `catalog.prune()` | `memseek catalog-prune` | `GET /catalog/prune` |
| Repoint a drifted cursor | `rebind_cursor()` | `memseek rebind-cursor` | `POST /derivations/{name}/rebind` |
| Erase the originals | `erase()` | — | `POST /erase` |

Two of these are worth being explicit about, because they are the ones that used
to be reachable only from a shell. `GET /catalog/prune` is read-only and answers
with reference counts, so it is safe to expose to whoever owns the catalog.
`POST /reindex` plans a rebuild and enqueues projection jobs; it never rewrites
canonical records, and a full `reset` rebuild additionally requires
`confirm: true` outside a test database.

The CLI commands are workspace-scoped by `--workspace` and connect to the database
directly, which is what makes them operator tools rather than tenant ones. The
routes are scoped by the bearer key instead, so a tenant can only ever act on its
own workspace. Same operation, same report, different trust boundary — pick by
where the caller is standing, not by which one you learned first.

## What the four changes cost

| Change | New version? | What you called |
| --- | --- | --- |
| Declare `channel` and filter on it | No | one publish |
| Better triage prompt, applied to history | No | one publish + one backfill |
| Require `channel`, constrain its values | **Yes** | one publish |
| Move the old tickets onto version 2 | — | one derivation run |

Three of the four were ordinary publishes. The habits that made it uneventful:

1. **Preflight before you publish.** One call, and every surprise becomes a plan
   with row counts attached.
2. **Treat a processor's name as its version.** `triage_v2` rather than an edited
   `triage_v1`, with `supersedes` so readers do not have to know both.
3. **Pin derivation sources** for any collection you might version.
4. **Leave old versions in the catalog** until `catalog.prune()` proves nothing
   references them.
