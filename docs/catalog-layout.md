---
title: Catalog layout
eyebrow: YAML and Python sources
---

Your whole memory design — what gets stored, how it is enriched, when reasoning
runs, and how it is read back — lives in a small tree of YAML files called the
**catalog**. This page is the map: which file to create for which purpose, and
the naming rules they all share.

Read [Core concepts](concepts.md) first if records, entities, or current facts
are new to you. The [Glossary](glossary.md) separates the catalog from the
package that releases it, and a processor from a derivation.

## Which file do I create?

Start from what you want to say, in words:

| "I want to…" | Create or edit | Guide |
| --- | --- | --- |
| …store a new kind of record | `collections/*.yaml` | [Collections](collections.md) |
| …name the models I use | `conf/models.yaml` | [Model aliases](models.md) |
| …score or classify each record as it arrives | `conf/processors.yaml` | [Processors](processors.md) |
| …combine records into profiles, reflections, or skills | `derivations/*.yaml` | [Derivations](derivations.md) |
| …control when that reasoning runs | an inline `trigger:`, or `triggers/*.yaml` | [Triggers](triggers.md) |
| …save a search my whole app can reuse | `views/*.yaml` | [Views & search](views-search.md) |
| …assemble prompts or briefings from memory | `artifacts/*.yaml` | [Artifacts](artifacts.md) |
| …choose exactly which tools an agent may call | `mcp/*.yaml` | [MCP](mcp.md) |
| …ship all of the above as one installable unit | `packages/*.yaml` | [Packages](packages.md) |

## Recommended tree

```text
my-memory/
├── conf/
│   ├── models.yaml
│   ├── processors.yaml
│   ├── processors/                     # optional, split into fragments
│   ├── rank_default.yaml
│   ├── search_profiles.yaml
│   └── deployment_overrides.yaml       # optional, operator-owned
├── collections/
│   └── customer.yaml
├── derivations/
│   └── customer_profile.yaml
├── triggers/
│   └── nightly_profile.yaml            # optional, for reusable triggers
├── views/
│   └── customer_context.yaml
├── artifacts/
│   └── customer_brief.yaml
├── mcp/
│   └── customer_memory.yaml            # optional, agent tool allowlist
└── packages/
    └── customer_memory.yaml
```

Directories are read in filename order, always the same way, so the result never
depends on how your filesystem happens to be sorted.

How much can go in one file depends on the kind:

- **Several per file** — collections, views, artifacts, and processors. Group
  them however reads best.
- **One per file** — each derivation, each standalone trigger, and each MCP
  interface.
- **Either** — a package file holds one package, or several under a
  `packages:` list.

## What each file family holds

| Path | Top-level shape | Contains |
| --- | --- | --- |
| `conf/models.yaml` | `aliases`, `defaults` | Which provider models you use, under stable names |
| `conf/processors.yaml` | `processors: [...]` | Per-record enrichment: embeddings, scores, structured data |
| `conf/rank_default.yaml` | `candidates`, `variants` | The default relevance formula for each search mode |
| `conf/search_profiles.yaml` | `profiles: {...}` | Where and how collections are searched |
| `conf/processors/*.yaml` | `processors: [...]` | Optional fragments, for splitting a long processor list |
| `conf/search_profiles/*.yaml` | `profiles: {...}` | Optional search-profile fragments |
| `conf/deployment_overrides.yaml` | `collection_profiles` | Operator's choice of which search setup a collection uses |
| `collections/*.yaml` | `collections: [...]` | Versioned contracts for what a record may be |
| `derivations/*.yaml` | one mapping | One bounded piece of automated reasoning |
| `triggers/*.yaml` | one mapping | A reusable trigger pointing at a derivation |
| `views/*.yaml` | `views: [...]` | Saved, named, typed searches |
| `artifacts/*.yaml` | `artifacts: [...]` | Recipes that render memory into text |
| `mcp/*.yaml` | one mapping | The allowlist of tools an agent may call |
| `packages/*.yaml` | one mapping | The exact versions that ship together |

## Two ways to publish a design

Both paths run the identical validation, so a design that loads one way loads
the other way too.

**Upload it to a workspace.** Send the YAML files to `POST /catalog`, or use the
SDK's publish call. This is how a multi-tenant product ships a design per
customer, and how you deploy without restarting anything. Once installed, that
package is the source of truth for that workspace; it is never quietly blended
with another tenant's definitions.

**Point the service at a directory.** A deployment can load a catalog straight
from disk. This suits a single-tenant deployment or local development, where the
design lives in your repository alongside the code.

The repository ships a starter tree you can copy as a starting point rather than
authoring every file from scratch.

## The rules every file follows

Memseek is deliberately strict about YAML, because the alternative is a typo
that silently does nothing until a user notices.

- **Unknown fields are errors.** There is no typo tolerance. A misspelled key
  fails the publish rather than being ignored.
- **Duplicate keys are errors**, including inside uploaded text.
- **Names are lowercase and bounded.** Public names match
  `[a-z][a-z0-9._-]{0,63}`; processor names are stricter, matching
  `[a-z][a-z0-9_]{0,31}`.
- **References to collections, views, and artifacts use exact versions** —
  `name@1`.
- **Package versions use three-part versions** — `name@1.0.0`.
- **Uploaded paths are relative**, end in `.yaml` or `.yml`, and must sit in the
  layout above.
- **Never write a `definition_hash` yourself.** Those are computed for you.

Uploads are capped at 256 files, 512 KiB per file, and 4 MiB in total.
`deployment_overrides.yaml` is an operator setting on the filesystem, not
something a workspace uploads.

The whole design is validated as one connected whole *before* anything about
your workspace changes, so a broken definition can never leave you half-switched
between two designs.

## When validation fails

Every error carries a machine-readable code, the file it came from, and the
path inside that file where possible. The codes you will meet:

| Code | Means |
| --- | --- |
| `yaml` | The file isn't valid YAML, or has a duplicate key. |
| `schema` | A field is missing, mistyped, or unknown. |
| `reference` | Something points at a name or version that doesn't exist. |
| `duplicate` | The same name is defined twice. |
| `budget` | A derivation's declared limits don't add up or exceed what's allowed. |
| `capability` | A search or model setup can't do what a definition needs. |
| `package_reference` | A package lists something that isn't in the catalog. |
| `catalog_incompatible` | The design conflicts with what's already installed. |
| `automatic_cycle` | Automated reasoning would set itself off in a loop. |

Treat this validation as a deployment gate — the same way you would treat a
failing migration.

## Generating definitions from Python

Most people write YAML. If your application *generates* memory designs — one per
customer, say — you can build the definitions in Python instead:

```python
from memseek.definitions import DefinitionSources, compile_definition_catalog

source = DefinitionSources(
    models=models,
    processors=(embedding, importance),
    rank_defaults=rank_defaults,
    search_profiles={"pg_default": pg_default},
    collections=(events,),
    derivations=(profile,),
    views=(context,),
    artifacts=(brief,),
    packages=(package,),
)
catalog = compile_definition_catalog(settings, source)
```

This is not a second, looser schema. It is serialized and put through exactly
the same duplicate-key, reference, dependency, budget, and fingerprinting checks
as YAML. To publish generated definitions to a workspace, serialize them to YAML
and use the SDK's file-publishing call.

To load from a directory instead, point the settings at your tree:

```python
from memseek.config import Settings
from memseek.definitions import load_definition_catalog

settings = Settings(
    collections_dir="my-memory/collections",
    derivations_dir="my-memory/derivations",
    triggers_dir="my-memory/triggers",
    views_dir="my-memory/views",
    artifacts_dir="my-memory/artifacts",
    mcp_dir="my-memory/mcp",
    packages_dir="my-memory/packages",
    models_file="my-memory/conf/models.yaml",
    processors_file="my-memory/conf/processors.yaml",
    rank_default_file="my-memory/conf/rank_default.yaml",
    search_profiles_file="my-memory/conf/search_profiles.yaml",
)
catalog = load_definition_catalog(settings)
```

## Where to go next

Build the files in the order they depend on each other:
[Collections](collections.md) → [Model aliases](models.md) →
[Processors](processors.md) → [Derivations](derivations.md) →
[Triggers](triggers.md) → [Views & search](views-search.md) →
[Artifacts](artifacts.md) → [Packages](packages.md).
