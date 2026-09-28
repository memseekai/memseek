---
title: Catalog layout
eyebrow: Start at catalog.yaml
---

Open **`catalog.yaml`** first. It is the complete table of contents of one
release: every authored definition has a name and a direct path to its file.
You do not need to guess a filename or search through directories.

## A complete example

The [site scrape catalog](https://github.com/memseekai/memseek/tree/main/examples/site_scrape_catalog)
uses feature folders:

```text
site_scrape_catalog/
├── catalog.yaml
├── config/
│   ├── models.yaml
│   ├── ranking.yaml
│   └── search_profiles.yaml
├── scraping/
│   ├── agent.yaml
│   ├── tools.yaml
│   ├── workspace.yaml
│   ├── budget.yaml
│   ├── instructions.yaml
│   ├── views.yaml
│   ├── tasks.yaml
│   └── processors.yaml
└── evaluation/
    └── runs.yaml
```

```yaml
name: site_scrape
version: 1.0.0
description: Scrape websites and reuse lessons from earlier runs.
config:
  models: config/models.yaml
  ranking: config/ranking.yaml
  search_profiles: config/search_profiles.yaml

# Run the scraper
agents:
  site_scraper@1: scraping/agent.yaml
toolsets:
  scraper@1: scraping/tools.yaml
computers:
  scrape_workspace@1: scraping/workspace.yaml
context_policies:
  scrape_budget@1: scraping/budget.yaml

# Assemble instructions and read memory
artifacts:
  scraper_instructions@1: scraping/instructions.yaml
views:
  current_task@1: scraping/views.yaml
  site_learnings@1: scraping/views.yaml

# Store tasks and evaluation results
collections:
  scrape_tasks@1: scraping/tasks.yaml
  skill_eval_runs@1: evaluation/runs.yaml
processors:
  importance: scraping/processors.yaml
```

Follow `scraper_instructions@1` to `scraping/instructions.yaml`. If that artifact
references `current_task@1`, its source is immediately visible in the root too.
Paths are relative to `catalog.yaml`, regardless of your working directory.

## Choose filenames for readers

Group by feature, or retain familiar `collections/`, `views/`, and `derivations/`
directories. Directory names have no compiler meaning. Prefer one substantial
definition per file; small related definitions may share a file. Every definition
in a declared file must have its own root entry.

Definitions retain their normal YAML shape:

| Root section | Source file shape | Reference |
| --- | --- | --- |
| `collections`, `views`, `artifacts` | Matching list, e.g. `views: [...]` | `name@1` |
| `computers`, `programs`, `agents`, `context_policies`, `toolsets` | Matching list | `name@1` |
| `processors` | `processors: [...]` | `name` |
| `derivations` | One derivation mapping | `name` |
| `triggers` | One standalone trigger mapping | `name` |
| `mcp` | One MCP interface mapping | `name@1` |

Models, ranking, and search profiles retain their existing configuration shapes.
The three `config` paths are required; nothing is inherited from deployment files.
Every configured search profile belongs to the release; name optional ones under
`optional_search_profiles`.

## Derivations and triggers

```yaml
derivations:
  crm_profile: profile/maintain.yaml
  crm_summary: profile/summarize.yaml
  crm_profile_rebuild: profile/rebuild.yaml
triggers:
  nightly_rebuild: profile/nightly.yaml
```

Each derivation declares its sources, tasks, and emission in its own file. Its
processor and any inline `trigger:` are included automatically. Do not repeat
the derivation under `processors`, or list its generated `<name>.default` trigger.
Use `triggers` only for separately authored trigger files.

## Validate and navigate locally

```console
uv run memseek catalog-validate --dir examples/site_scrape_catalog
uv run memseek catalog-locate scraper_instructions@1 --dir examples/site_scrape_catalog
```

Without `--dir`, both commands find the nearest ancestor containing
`catalog.yaml`. Validation does not connect to a database or call a model.
It checks schemas, references, source ownership, dependencies, and static
capabilities. Credentials can still affect whether a configured backend is
available. Use `--json` for structured results and diagnostics.

`catalog-locate` prints a file, line, and column. Use `--kind artifacts` to
disambiguate names shared across families, and an exact version when necessary.
It also locates generated processors and triggers at their owning derivation.

Examples contain `yaml-language-server: $schema=...` comments for editor
completion and structural checks. Generate the schemas after model changes:

```console
uv run python scripts/generate_catalog_schemas.py
```

Semantic cross-file checks belong to `catalog-validate`. For an interactive
view of the wiring, use [the catalog graph](packages.md#seeing-the-package).

## Strict, explicit loading

Only listed files load. Unrelated YAML files are ignored. Missing paths, wrong
names or versions, unlisted definitions in grouped files, duplicate definitions,
unknown fields, and duplicate YAML keys fail validation. Paths cannot escape the
catalog root, including through symlinks. Every path is catalog-relative, uses
forward slashes, and ends in `.yaml` or `.yml`; `catalog.yaml` cannot list
itself. One file serves one family: listing the same file under `collections:`
and `views:` fails, although several entries of one family may share a file.
Moving a source file and updating its root entries does not change definition
identity.

One root compiles to one complete package. No separate `packages/` manifests,
module indexes, imports, or automatic directory discovery are supported.

To load a catalog at service startup:

```python
from memseek.config import Settings
from memseek.definitions import load_definition_catalog

catalog = load_definition_catalog(Settings(catalog_file="my-memory/catalog.yaml"))
```

Or set `CATALOG_FILE`. The file must be named `catalog.yaml`. Leaving it unset
means the service supplies no default catalog; workspaces publish their own.
The old directory settings (`COLLECTIONS_DIR`, `PACKAGES_DIR`,
`PROCESSORS_FILE`, and the other `*_DIR` variables) are rejected with
`set catalog_file to catalog.yaml` when `CATALOG_FILE` is unset. Operator search-profile overrides remain
separate from uploaded definitions.

## Migrating an older catalog

Create `catalog.yaml`, move package identity and policies into it, and replace
membership lists with name-to-file mappings. Declare derivations separately and
omit their generated processors and inline triggers. Declare all definitions in
any grouped file, including inactive versions. Add explicit configuration paths,
and remove the old package manifest.

Existing stored catalogs must be republished in the new format. There is no
legacy loader or automatic data conversion. Compare release contents and bump
the package version when the migration includes previously omitted definitions.
