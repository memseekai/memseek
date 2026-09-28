---
title: Packages
eyebrow: One root, one release
---

A package is the compiled release of a catalog. Its human entry point is
**`catalog.yaml`**, which names each definition and its source file. Read
[Catalog layout](catalog-layout.md) for the complete format and example.

There is no separate package inventory to keep in sync. Every declared
definition ships, along with generated derivation processors, inline triggers,
and required built-ins such as the lessons collection.

## Package identity and contents

```yaml
name: customer_memory
version: 1.0.0
description: Cited account memory for the support assistant.
config:
  models: conf/models.yaml
  ranking: conf/rank_default.yaml
  search_profiles: conf/search_profiles.yaml
collections:
  customer_events@1: customer/events.yaml
  customer_profiles@1: customer/profiles.yaml
derivations:
  customer_profile: customer/maintain.yaml
views:
  customer_context@1: customer/context.yaml
artifacts:
  customer_brief@1: customer/brief.yaml
```

Package versions use semantic versions, such as `customer_memory@1.0.0`.
Versioned definitions use exact integer versions, such as `customer_brief@1`.
Processors, derivations, and standalone triggers use their existing names.

The source map is authoring metadata. Compiled packages still contain the exact
reference lists consumed by runtime APIs. Reordering the map or moving files
does not change semantic identity. A change to the released definitions does.
All configured search profiles ship; `optional_search_profiles: [memory_tpuf]`
marks a configured profile as optional when its credentials are unavailable.

## Declared MCP interfaces

Declaring an MCP file and choosing to expose it are separate decisions:

```yaml
mcp:
  customer_memory@1: customer/mcp.yaml
expose_mcp: customer_memory@1
```

Without `expose_mcp`, the package exposes no MCP interface. The selected interface
must exist and all its targets must resolve. See [MCP](mcp.md).

## Tombstone retention

Retention policies remain root package policies:

```yaml
retentions:
  - name: purge_deleted_pages
    collection: customer_pages@1
    after_days: 30
    cron: "23 3 * * *"
    max_pages: 25
```

The referenced collection must be declared by the root. Only a slot whose
current active head is a tombstone is eligible. Age is measured using the
server-written `created_at`, not a client's `occurred_at`. Retention runs in the
worker; it is not a public deletion endpoint. `after_days` is bounded to 1–3650,
and `max_pages` to 1–100 slots per job.

## Validate, then publish

```console
uv run memseek catalog-validate --dir ./my-catalog
uv run memseek catalog-check --workspace acme --dir ./my-catalog
```

Local validation requires no database. `catalog-check` additionally checks
compatibility against a workspace's existing data. Package identity is inferred
from the root unless `--package` is explicitly provided.

```python
await client.catalog.publish(
    package="customer_memory@1.0.0",
    directory="./my-catalog",
)
```

The SDK collects `catalog.yaml` and its declared files only. `publish_files`
accepts the same bundle as a mapping of relative paths to YAML strings. The
`POST /catalog` request retains its `package` and `files` fields. The requested
package must match the root identity. Upload limits remain 256 files, 512 KiB
per file, and 4 MiB total.

Validation and compatibility checks finish before a workspace switches
catalogs. Another workspace's definitions are never blended into the upload.
Old stored bundles without `catalog.yaml` require republishing.

## Seeing the package

```console
uv run memseek catalog-graph --dir ./my-catalog --out my-catalog.html
```

The interactive graph shows compiled definitions, dependencies, budgets, and
source locations. Omit `--out` to write HTML to stdout, or use `--json` for its
structured representation. One catalog root defines one package, so no package
selection is needed.

To jump directly to a source:

```console
uv run memseek catalog-locate customer_profile --dir ./my-catalog
uv run memseek catalog-locate customer_profile.default --dir ./my-catalog
```

Both references lead to the derivation that owns the processor and inline
trigger.
