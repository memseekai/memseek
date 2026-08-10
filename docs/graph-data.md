---
title: Graph data
eyebrow: Catalog guide
---

Some of what you want to remember is not *about* one thing — it is a
**connection between two things**. "This service depends on that one." "Maya
advises Acme." "This conclusion cites that observation." Once you store enough
of those, useful questions become reachable: *what breaks if this goes down?*,
*who is connected to this customer?*, *what is this claim built on?*

Memseek answers those questions without adding a graph database. Connections
are stored as ordinary records, in an ordinary collection, with the same
schema checking, permissions, and provenance as everything else. You then
declare which collection holds your connections and which of its fields mean
"from", "to", and "what kind" — and Memseek can walk them.

This works for dependencies, org charts, citations, lineage, entity links, and
knowledge graphs. Nothing here is specific to any one of those.

!!! note "Terms used on this page"

    - **Node** — one endpoint of a connection, identified by a plain string
      such as `api` or `people/maya`. Nodes do not need their own records.
    - **Link** — one stored connection between two nodes. Elsewhere in the API
      you will see these called *edges*.
    - **Walk** — following links outward from a starting node, a bounded number
      of steps. Elsewhere called a *traversal*.
    - **Collection**, **entity**, **record**, **view** — the everyday Memseek
      vocabulary. See [Core concepts](concepts.md).

Setting this up takes three steps, plus an optional fourth.

## 1. Store your connections in a collection

A connection collection is an ordinary event collection with three declared
fields: where the connection starts, where it ends, and what kind of
connection it is. **You choose the field names** — use whatever your domain
already calls them.

```yaml
collections:
  - name: dependencies
    version: 1
    active: true
    mode: event
    schema:
      type: object
      required: [text, from_node, to_node, relationship]
      properties:
        text: {type: string}
        from_node: {type: string}
        to_node: {type: string}
        relationship: {type: string}
        metadata: {type: object}
      additionalProperties: false
    fields:
      from_node: {path: content.from_node, type: string, filter: true, project: true}
      to_node: {path: content.to_node, type: string, filter: true, project: true}
      relationship: {path: content.relationship, type: string, filter: true, project: true}
    search_profile: pg_default
```

What matters here:

- The collection must use **`mode: event`** — connections are things that were
  asserted, not slots that get overwritten. Correcting a connection means
  writing a new record, which is what keeps the history auditable.
- All three fields must be declared **strings** and marked **filterable**.
  Marking them returnable as well means the values come back with results.
- Nodes are just strings. You do not need a record for a node unless you want
  [orphan reporting](#4-optional-find-things-with-no-connections).
- The kind of connection is a string you choose — `depends_on`, `advises`,
  `cites`. Memseek has no built-in vocabulary.

Two things about scope:

- Every connection record still carries the normal **entity**, like any other
  record. Orphan reporting uses it to compare nodes and connections belonging
  to the same subject.
- A walk stays inside one workspace and one collection. So node names should be
  unique across your workspace — either naturally, or by namespacing them
  (`people/maya` rather than `maya`).

Connections can be written directly by your application, or produced
automatically by a derivation. Either way they keep their normal provenance, so
you can always ask where a connection came from.

## 2. Declare a graph view

A [view](views-search.md) is a saved, named, typed read. A graph view is the
one that says "these fields are the connection roles, and here is how far
callers may walk."

```yaml
views:
  - name: dependency_graph
    version: 1
    active: true
    kind: graph
    graph:
      edges: dependencies
      subject: from_node
      object: to_node
      predicate: relationship
    parameters:
      seed: {type: string, required: true, min_length: 1, max_length: 128}
      predicates: {type: string_array, default: [], max_items: 20}
      direction: {type: string, default: out, enum: [out, in, both]}
      depth: {type: integer, default: 1, minimum: 1, maximum: 4}
      limit: {type: integer, default: 20, minimum: 1, maximum: 100}
```

The `graph` block maps your field names onto the three standard roles:

| Role | Means | In the example |
| --- | --- | --- |
| `subject` | where the connection starts | `from_node` |
| `object` | where it ends | `to_node` |
| `predicate` | what kind of connection it is | `relationship` |

These roles are also the names results come back under, so every consumer sees
the same shape no matter what you called your fields.

Each role defaults to a field of the same name, so a collection that already
uses `subject`, `object`, and `predicate` needs only `graph: {edges: edges}`.

Your definitions will not load if the collection is missing, is not an event
collection, or if a role points at a field that is undeclared, not a string, or
not filterable. You find out at deploy time, not when a user runs a query.

### The parameters callers get

Every graph view takes exactly these five parameters — you cannot add or remove
any, which is what guarantees that no caller can start an unbounded walk:

| Parameter | Meaning |
| --- | --- |
| `seed` | Where to start walking. Required, 1–128 characters. |
| `predicates` | Which kinds of connection to follow. Empty means all of them. |
| `direction` | `out` (follow connections away from the seed), `in` (follow them toward it), or `both`. |
| `depth` | How many steps to walk. |
| `limit` | How many paths to return. |

What you *can* do is tighten them. The `minimum`/`maximum` and `enum` values in
the example above are your choices, and they show up in the schema that
tool-calling agents read — so they are the bounds an agent will respect.

Connection kinds are your own strings. Leave `item_enum` off to accept any
non-blank value, or declare it when your graph has a closed vocabulary:

```yaml
predicates:
  type: string_array
  default: []
  item_enum: [depends_on, replicates_to, blocks]
  max_items: 3
```

Your bounds are combined with the deployment's own ceilings
(`MAX_GRAPH_DEPTH` and `MAX_GRAPH_PATHS`), and the stricter of the two wins —
both when a query runs and in the schema agents are shown. Whichever is
smaller is what callers actually get.

## 3. Query it

A graph view is called exactly like any other view — there is no graph-specific
endpoint:

```text
POST /views/dependency_graph/query
```

with the parameters as the request body. From the [Python SDK](sdk.md):

```python
views = await client.views()
result = await client.query_view(
    "dependency_graph",
    seed="api",
    predicates=["depends_on"],
    direction="out",
    depth=2,
    limit=20,
)
```

You get back:

- **`paths`** — the routes found, in a deterministic order, so the same
  question always returns the same answer.
- **`nodes`** — everything the walk reached.
- **`citations`** — the actual connection records each path used, once each.
  A citation carries its record `id` and `text`, the normalized `subject`,
  `object`, and `predicate`, and the complete original record content, so
  nothing you stored is lost.
- **`hits`** — the same list as `citations`. It is repeated under this name so
  that artifacts and anything else that consumes an ordinary view can use a
  graph result without special handling.
- **`truncated`** — `true` when more paths existed than `limit` allowed.

[Views & search](views-search.md#graph-views) documents the full response,
field by field.

`GET /views` reports each graph view's role mapping and the collection it
reads, so tooling and audits can discover the graph contract without reading
your YAML.

### Which connections count

A walk only follows connections that are **fully processed, active, and not
retracted**. A path never revisits a node it has already been through, so a
loop in your data cannot produce an endless result.

## 4. Optional: find things with no connections

Sometimes the useful question is the inverse: *what is sitting there
unconnected?* An orphan view answers it. It needs one extra thing — a
collection that holds a record per node, so there is something to report as
isolated. That collection uses named slots (`mode: keyed`, or a mixed
collection containing them), because a node is a thing that exists, not
something that happened.

```yaml
views:
  - name: component_orphans
    version: 1
    active: true
    kind: graph_orphans
    graph:
      edges: dependencies
      subject: from_node
      object: to_node
      predicate: relationship
      nodes: components
    parameters:
      limit: {type: integer, default: 50, minimum: 1, maximum: 100}
```

It returns current, active, fully processed, non-retracted node records whose
key has no live connection in either direction within the same entity. Each
result carries `id`, `entity`, `key`, `text`, and its original content.

One subtlety worth understanding, because it affects whether you can trust the
report:

- A connection your application wrote directly is live immediately.
- A connection produced automatically *from* a node record stays live only
  while that exact node record is still the current one.

That second rule is what keeps the report honest. Without it, a connection
derived from an outdated version of a node could keep vouching for a node that
has since become isolated — and the thing you needed to see would stay hidden.

## Several graphs in one catalog

A catalog can expose as many graph views as it needs, over different
collections or different field mappings — for example one for infrastructure
dependencies and another for people.

Calling a view by name is already unambiguous. Other reads that can *use* a
graph as a signal take the view name as a selector:

```yaml
graph_boost:
  graph: dependency_graph
  anchor: api
  depth: 2
  weight: 0.05
  limit: 100
```

The grounded-answer read selects the same way:

```python
await client.answer(..., anchor="api", graph="dependency_graph")
```

When exactly one graph view is active you may omit `graph`. When several are
active, omitting it is an error (`graph_ambiguous`) rather than a silent guess
at which graph you meant.

See [graph proximity boost](views-search.md#graph-proximity-boost) for what
`graph_boost` actually does to search results.

## A note for operators

The built-in PostgreSQL indexes are tuned for the default role mapping —
connection collections whose fields are literally named `subject` and `object`.
Custom field names behave identically and are fully supported; they simply are
not covered by those prebuilt indexes. A high-volume deployment using custom
names should add matching indexes in its own migration.

## Where to go next

- [Views & search](views-search.md) — the view contract these build on, the
  full graph response, and graph proximity boost.
- [Collections](collections.md) — declaring fields, modes, and schemas.
- [Derivations](derivations.md) — producing connections automatically instead
  of writing them by hand.
