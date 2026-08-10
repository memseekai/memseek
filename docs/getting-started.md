---
title: Getting started
eyebrow: First run
---

In about ten minutes, and without signing up for anything, you will have a
running memory service doing the thing most AI products eventually need: events
flow in, get scored and embedded, and a **profile keeps itself up to date, with
every claim cited** — no cron jobs, no prompt stuffing, no bookkeeping in your
application code.

You will start a database, an API, and a worker; publish a memory design;
write a record; and read it back through both search and a rendered briefing.

Nothing here needs a model provider account. A built-in deterministic stand-in
handles the LLM and embedding calls, which keeps this walkthrough offline and
repeatable. Real providers plug in later by
[changing a model alias](skill-maintenance.md), not by changing your design.

When you finish, continue with [Core concepts](concepts.md) and the
[Glossary](glossary.md). They explain why a freshly written record is not
immediately searchable, and how a current profile value keeps its history.

## Prerequisites

- Python 3.14.6
- [`uv`](https://docs.astral.sh/uv/)
- Docker with Compose
- PostgreSQL 16 with pgvector — the supplied Compose file provides this

From the repository root:

```console
uv sync --frozen --all-groups
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
export LLM_FAKE=1
make database
uv run memseek migrate
```

`make database` starts a throwaway PostgreSQL 16 with pgvector on
`127.0.0.1:55432`, database `memseek_test`, which disappears when you stop it.
`DATABASE_URL` must point at it. As a safety measure, test and reindex commands
refuse to run unless the database name contains `test`, so you cannot aim them
at something real by accident.

`LLM_FAKE=1` selects the deterministic stand-in provider. This is what lets the
walkthrough run with no API key.

!!! tip "Keep those two exports somewhere"
    Every terminal below needs `DATABASE_URL` and `LLM_FAKE` set. There is
    deliberately no committed environment file to source — `.env` and `.env.sh`
    are git-ignored precisely because yours will eventually hold provider
    credentials. `.env.example` documents every available setting: copy it to
    `.env`, or put the two exports in your own `.env.sh` and source that. The
    blocks below repeat them so each one stands alone.

## Start the two processes

Memseek runs as two pieces. Use separate terminals, with the same environment
in each:

```console
# Terminal A
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
export LLM_FAKE=1
uv run uvicorn memseek.api:app --host 127.0.0.1 --port 8000
```

```console
# Terminal B
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
export LLM_FAKE=1
uv run memseek worker
```

The **API** handles authenticated reads and writes — this is what your
application talks to. The **worker** does the slow background work: enriching
new records, running derivations, calling model providers, and retrying or
parking anything that fails. You need both running, because a record is not
searchable until the worker has finished with it.

## Create a workspace key

A workspace is your isolated slice of memory. Creating one prints a bearer key
**once**. Only a one-way fingerprint of that key is stored, so it cannot be
recovered later — keep what the command returns:

```console
workspace_json="$(uv run memseek create-workspace local)"
export MEMSEEK_API_KEY="$(printf '%s' "$workspace_json" | \
  uv run python -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"
export MEMSEEK_AUTH="Authorization: Bearer $MEMSEEK_API_KEY"
export MEMSEEK_BASE_URL=http://127.0.0.1:8000
```

## Publish a memory design

Your memory design — collections, processors, derivations, views, artifacts —
is a directory of YAML published as one versioned **package**. A ready-made CRM
design makes a good first one:

```python
import os
from pathlib import Path
from memseek.sdk import MemseekClient

api_key = os.environ["MEMSEEK_API_KEY"]

async with MemseekClient(
    "http://127.0.0.1:8000", api_key,
) as client:
    await client.catalog.publish(
        package="crm_user_profile@2.0.0",
        directory=Path("examples/crm_profile_catalog"),
    )
```

The remaining Python snippets on this page continue inside that same
`async with` block.

The same thing works over plain HTTP — the body is a package reference plus the
YAML files, keyed by their relative paths:

```json
{
  "package": "crm_user_profile@2.0.0",
  "files": {
    "collections/crm.yaml": "collections:\n  - name: crm_events\n    ...\n",
    "conf/models.yaml": "aliases:\n  ...\n",
    "packages/crm_user_profile.yaml": "name: crm_user_profile\nversion: 2.0.0\n...\n"
  }
}
```

Every file is validated *before* anything about your workspace changes, and the
switch is all-or-nothing: either the whole new design goes live or the old one
stays untouched. A successful publish tells you which package is now selected,
a fingerprint identifying the exact design, and the files it contains. Later,
`GET /catalog` reports what is published without handing back the YAML source.

## Write a record and read the current state

```python
await client.records.ingest(
    collection="crm_events",
    entity="contact:avery-chen",
    type="crm_event",
    text="Avery prefers concise written updates.",
    content={
        "source": "support",
        "event_kind": "preference",
        "account_id": "acme-cloud",
    },
    dedupe_key="crm:avery:preference:1",
)

document = await client.document(
    entity="contact:avery-chen",
    collections="user_profiles",
)
```

A record that needs enrichment comes back as `ready: false` at first. That is
not an error — it means the worker has not finished scoring and embedding it
yet. Until it does, the record is deliberately invisible to search and cannot
set off any reasoning, so nothing ever acts on a half-processed memory.

`document` is the "what do we currently believe?" read: it returns the current
value of each fact about that contact, anything that has been retracted, and
how fresh the reasoning behind it is.

## Search, and render a briefing

```python
results = await client.search(
    query="written updates",
    collections=["crm_events"],
    entity="contact:avery-chen",
    mode="text",
    k=5,
)

brief = await client.render_artifact(
    "crm_profile_brief",
    entity="contact:avery-chen",
    query="role commitments preferences",
)
```

Search never trusts an index blindly: whatever the search engine proposes is
re-fetched from the database and re-checked against your query's rules before
you see it. Rendering is deterministic — the same records, the same definitions,
and the same parameters always produce the same briefing, which is what makes
an output reproducible weeks later.

## Stop the throwaway service

```console
make database-down
```

## Where to go next

- [Core concepts](concepts.md) — the data model behind what you just ran.
- [Catalog layout](catalog-layout.md) — build your own design instead of
  publishing a ready-made one.
- [Real-LLM skill maintenance](skill-maintenance.md) — swap in a real model
  provider and run a reviewed improvement loop.

For a complete runnable version of everything above, run
`uv run python examples/sdk_crm_profile.py` with the API and worker started.

## Render this documentation locally

From the repository root:

```console
make docs
```

Open <http://127.0.0.1:8001>. The site rebuilds as you edit; press `Ctrl-C` to
stop it. If that port is taken, override it with
`make docs DOCS_ADDR=127.0.0.1:8002`. The documentation tooling comes with the
project's normal `uv sync --all-groups` setup — no container needed.
