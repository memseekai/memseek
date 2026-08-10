---
title: Collections
eyebrow: Durable record contracts
---

A collection is where your records live. Think of it as a labeled drawer in a
filing cabinet: everything in the drawer follows the same rules. The
collection definition is where you write those rules down — what a valid
record looks like, whether records are diary-style entries or named slots that
get updated, which enrichment must run on each record, and how the records are
searched.

Collection files live in `collections/*.yaml`. A file starts with a
`collections:` list and may contain several collections.

If **entity**, **key**, **current**, or **ready** are unfamiliar, pause at the
[Glossary](glossary.md). Those terms determine the collection shape before any
YAML field does.

## Start by describing what you want to remember

Before writing any YAML, say it in words. For example:

> "I run a support desk. I want to keep **every email, call, and note about
> each customer**, exactly as it happened, and never edit old entries. Each
> entry must say which channel it came from, and I want to filter searches by
> channel later. Separately, I want a **short profile of each customer** —
> their needs, commitments, risks — where each part can be updated over time."

That description maps directly to two collections: an *event* collection for
the raw entries, and a *keyed* collection for the profile.

```yaml
collections:
  # "Keep every email, call, and note, exactly as it happened."
  - name: customer_events
    version: 1
    active: true
    mode: event                     # append-only: entries are never edited
    schema:                         # what a valid entry looks like
      type: object
      required: [text, channel]     # every entry needs text and a channel
      properties:
        text: {type: string}
        channel: {type: string, enum: [email, call, note]}
      additionalProperties: true
    fields:                         # "I want to filter by channel later"
      channel:
        path: content.channel
        type: string
        filter: true
    required_processors: [embedding_v1, importance]
    search_profile: pg_default

  # "A short profile per customer, where each part can be updated."
  - name: customer_profiles
    version: 1
    active: true
    mode: keyed                     # named slots: one current value per key
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
        tombstone: {type: boolean}    # system-owned retraction marker; see below
      additionalProperties: true
    required_processors: [embedding_v1]
    search_profile: pg_default
```

The rest of this page explains every field you can put in a collection, what
it does, and when you would use it.

## Every field, explained

### `name` (required)

The permanent, public name of the collection. Everything else — derivations,
views, packages, API calls — refers to the collection by this name, so choose
it the way you would choose a database table name: short, lowercase, and
descriptive of the *content*, not the current use case.

Names must start with a lowercase letter and may contain lowercase letters,
digits, `.`, `_`, and `-`, up to 64 characters (`customer_events` is fine;
`CustomerEvents` and `2024_events` are not).

### `version` (required)

A positive whole number identifying this revision of the contract: `1`, `2`,
`3`, … Records permanently store the collection name *and* version they were
written under, so old data never silently changes meaning. When you change
what records mean — new required properties, different field types — add a new
version instead of editing the old one. See the
[versioning checklist](#when-to-create-a-new-version) below, and
[which "latest" is which](concepts.md#versioning-which-latest-is-which) for
how definition versions relate to record supersession.

### `active` (default `false`)

Marks which version is the default when someone refers to the collection
without a version (just `customer_events` instead of `customer_events@2`).
Exactly one version of a name can be active at a time.

**When do you need to think about this?** For a brand-new collection, never —
you have one version, and you mark it `active: true`. The flag earns its keep
during a migration: you add `customer_events` version `2` with the new schema
and `active: true`, and flip version `1` to `active: false` *but keep it in
the catalog*. New records land in version 2; old records still validly belong
to version 1, and anything that pinned `customer_events@1` keeps working. If
you deleted version 1 instead, replacing the catalog would fail because
existing records would lose their contract.

### `mode` (required)

The shape of the records. This is the most important choice you make:

- **`event`** — append-only entries, like lines in a journal. You never edit
  an old entry; you only add new ones. Use this for things that *happened*:
  messages, meeting notes, log lines, observations.
- **`keyed`** — named slots, like labeled sticky notes on a whiteboard. Each
  entity has one *current* value per key (for example key `needs` or
  `commitments`). Writing a new value for the same key supersedes the old one
  — the old value is kept as history, but searches see only the current one by
  default. Use this for things that *are true right now*: profiles, skills,
  preferences.
- **`mixed`** — the collection accepts both records with a key and records
  without one.

```yaml
mode: event   # a diary: "what happened"
mode: keyed   # a whiteboard: "what is currently true"
mode: mixed   # both forms accepted
```

**How to choose.** Ask: "if the same information arrives twice, do I want two
entries, or an update?"

- Two support emails → two entries. That is `event`. You need `event` whenever
  losing or overwriting an entry would lose history you care about: audit
  trails, conversations, observations an agent might re-analyze later.
- "The customer's current plan is Enterprise" → an update. That is `keyed`.
  You need `keyed` whenever downstream consumers should see *one* answer per
  question, not every answer ever given — profiles, settings, skills, the
  output of most derivations. The key (`plan`, `needs`, `risks`) is the name
  of the question; the record is the current answer.
- You need `mixed` less often: typically when a derivation writes keyed state
  into the same collection where free-form events also land, or during a
  gradual migration. When in doubt, keep events and state in two separate
  collections — it keeps the contracts simpler.

### `schema` (required)

A [JSON Schema](https://json-schema.org/draft/2020-12) (Draft 2020-12)
describing the structured `content` of each record — the machine-checkable
part of "what a valid entry looks like." Memseek adds three rules on top of
the standard:

1. The root must be an object (`type: object`).
2. There must be a string property named `text`.
3. `text` must be listed in `required`.

`text` is the human-readable sentence of the record and the default text that
gets indexed for search; everything else in `content` is structured payload.
A realistic calendar example:

> "Every calendar entry has a description, a start time, and a list of
> attendee names. Nothing else is allowed."

```yaml
schema:
  type: object
  required: [text, starts_at, attendees]
  properties:
    text: {type: string}                          # "Weekly sync with ACME"
    starts_at: {type: string, format: date-time}  # timestamps are strings with this format
    attendees:
      type: array
      items: {type: string}
  additionalProperties: false     # reject entries with unexpected extras
```

Set `additionalProperties: false` when you want strict entries, or `true` when
callers may attach extra data you do not need to validate.

### `text_projection` (optional)

A template that builds the searchable text out of the structured content,
when the raw `text` alone is not what you want indexed. Every `{{...}}`
reference must name a property declared in the schema.

> "When searching, an event should read as its text plus the channel it came
> through."

```yaml
text_projection: "{{text}} via {{channel}}"
```

If you omit this, the record's `text` is used as-is. That is the right choice
for most collections.

### `fields` (optional)

Declares which parts of the structured content can be used in typed queries —
filtering (`where`), sorting (`order_by`), and projection (returning the value
with search results). Without a declared field, the data is still stored; it
just cannot be filtered or sorted on.

Each entry gives a public field name and says where the value lives and what
you may do with it:

```yaml
fields:
  starts_at:
    path: content.starts_at   # where the value lives in the record
    type: datetime             # what kind of value it is
    filter: true               # allow "where starts_at >= ..." queries
    sort: true                 # allow ordering results by this field
    project: true              # allow returning this value with results
  attendees:
    path: content.attendees
    type: [string]             # a list of strings
    filter: true
    project: true
```

| Option | Allowed values | What it does |
| --- | --- | --- |
| `path` | `content.foo` or `annotations.foo` (dots for nesting) | Where in the record the value lives. Must resolve to a matching property in the content schema (or an annotation's output schema). |
| `type` | `string`, `number`, `integer`, `boolean`, `datetime` | The value's type. Wrap it in brackets — `[string]` — for a list of that type. `datetime` corresponds to a schema string with `format: date-time`. |
| `filter` | `true` / `false` (default `false`) | Lets queries filter on this field with `where`. |
| `sort` | `true` / `false` (default `false`) | Lets queries order results by this field. Lists cannot be sorted. |
| `project` | `true` / `false` (default `false`) | Lets queries return this field's value alongside each result. |

Clients always use the field *name* (`starts_at`), never the raw path — the
path is an internal detail you can keep stable even if the name is public.

A field may also point into `annotations.` — values written by a processor
rather than by the client. If a trigger or derivation depends on such a field,
bind that processor as a *required* processor so the value is guaranteed to
exist before the record is used.

### `required_processors` (default: none)

Names of processors (embedding, score, or json — see
[Processors](processors.md)) that must finish successfully before a
record counts as **ready**. Ready means: visible to search, and able to feed
triggers and derivations. Use this for enrichment the rest of the system
depends on, such as the embedding used for semantic search:

```yaml
required_processors: [embedding_v1, importance]
```

The trade-off: every required processor delays readiness. Only require what
your consumers truly need.

Requiring **none** is a legitimate and sometimes correct choice. A collection of
[feedback signals](artifact-uses.md#the-record-that-gets-written) is the classic
case: you want a signal to be able to set off reasoning the instant it is
written, not after an embedding queue drains.

### `optional_processors` (default: none)

Processors that run on each record too, but *without* holding up readiness —
the record becomes searchable immediately and the enrichment fills in when it
completes. Good for nice-to-have annotations like sentiment:

```yaml
optional_processors: [customer_sentiment]
```

A processor cannot appear in both lists, and every processor named here must
declare this collection in its own input scope.

### `search_profile` (required)

The name of the search backend profile this collection uses by default, from
`conf/search_profiles.yaml`. If you are unsure, `pg_default` (the built-in
PostgreSQL profile) is the standard choice.

```yaml
search_profile: pg_default
```

### `allowed_search_profiles` (default: none)

Additional profiles a deployment operator is allowed to switch this
collection to — for example, an external vector index that is only enabled
when credentials exist:

```yaml
search_profile: pg_default
allowed_search_profiles: [pg_default, memory_tpuf]
```

### `answerable` (default: `false`)

Whether `POST /answer` — and the `answer` MCP tool — may synthesize prose over this
collection.

```yaml
answerable: true
```

Answering is the only read that writes *new sentences* out of several records
rather than handing back what is stored. Because of that, which drawers it may
open should be a decision you make deliberately — not something a collection
gets by accident just because it has embeddings.

A collection can be perfectly searchable and still be a bad source for
synthesis. Raw transcripts, saved prompt snapshots, feedback signals, and
previously generated answers are all things you want to *retrieve and cite*,
not things you want a model paraphrasing back to a caller as fact. Turn
`answerable: true` on for your interpreted layers — profiles, reflections,
summaries — and leave it off everywhere else. If no collection in a workspace
declares it, answering returns `422 answer_unavailable`.

Like the search-routing options, this is a **binding** rather than part of the
record contract. Turning it on or off changes who may read a record, never what
the record means, so it never strands stored data and never needs a new
collection version.

## How records enter a collection

A record request supplies `collection`, `entity` (whose memory this is —
the customer, user, or agent the record is about), `type`, `text`, optional
`key`, optional structured `content`, optional `occurred_at`, and an
idempotent `dedupe_key`. For keyed collections, `key` is the durable slot
name. Removing a keyed value is done by writing a successor (a retraction),
never by editing in place — history is always preserved. That successor is a
**tombstone**: a system-written record that marks the slot as no longer holding
a value. You declare the `tombstone: {type: boolean}` field in the schema so it
is known, but you never set it yourself — a derivation emits `retract: true` or
the API insert passes `tombstone=true`, and the runtime writes it. See
[reacting to tombstones](triggers.md#retraction-react-to-tombstones) for the
full lifecycle.

Entities, types, and keys are chosen by you at write time, not declared in
the collection — see [Core concepts](concepts.md#the-vocabulary) for how to
choose them well.

## When to create a new version

Records store the **record contract** of the version that admitted them: `mode`,
`schema`, `text_projection`, `fields`, and `required_processors`. Create a new
`version` when you change one of those in a way that would make an existing
record mean something different:

- adding a **required** property,
- retyping, re-pathing, or removing a declared field,
- redefining or removing a schema property,
- adding or removing a `required_processors` entry (readiness gates visibility),
- changing `mode` or `text_projection`.

Keep the old version present (and inactive) while records and packages still
reference it, and list both in the package. Then move the old records forward with
a [migration derivation](changing-definitions.md#move-a-corpus-to-a-new-version)
if you want the old version to retire.

Most edits are **not** in that list and publish over a live collection:

- `active`, `optional_processors`, `search_profile`, and
  `allowed_search_profiles` are *bindings*, outside the contract entirely;
- adding an **optional** property, declaring a `fields` entry over it, relaxing
  `additionalProperties` from `false` to `true`, and reordering
  `required_processors` are *provably additive* — the publish moves stored records
  onto the new contract for you.

Writing a content key the schema does not name is free on any collection with
`additionalProperties: true`; declaring it is the additive publish above.

Run a preflight if you are unsure — it answers exactly this question against your
real records:

```bash
uv run memseek catalog-check --workspace acme --dir ./catalog --package acme@1.4.0
```

See [Changing definitions](changing-definitions.md) for the full matrix and how
the additive cases are proved rather than assumed, or
[A migration, start to finish](migration-walkthrough.md) for one catalog followed
through four changes.

## Validation rules the loader enforces

These checks run when the catalog is loaded or uploaded, so mistakes fail
fast instead of corrupting data:

- Collection names and versions must be unique; one active version per name.
- `required_processors` and `optional_processors` must not overlap, and every
  named processor must exist and include this collection in its input scope.
- The named `search_profile` (and every allowed profile) must exist.
- The schema root must be an object with a required string `text`.
- Every declared field path must resolve to a schema property of a compatible
  type.
- Every `{{...}}` in `text_projection` must name a declared schema property.

One thing you cannot currently do: there is no free-text `description` field on
a collection. The name, the schema, and the field names *are* the contract, and
they are meant to read well enough to stand on their own. Adding a `description`
key of your own will fail the publish, because unknown keys are rejected —
document the intent in a YAML comment instead.
