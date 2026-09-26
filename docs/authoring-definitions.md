---
title: Authoring a workspace catalog
eyebrow: Read, validate, publish
---

A catalog is a versioned memory design that lives beside your application.
Start at **`catalog.yaml`**: it is the table of contents, with an explicit path
beside every definition. One root produces one complete release.

## Start from a worked example

Copy `examples/site_scrape_catalog/` for an agent with learned browser skills,
`examples/crm_profile_catalog/` for incremental CRM profiles, or
`examples/computer_renewal_catalog/` for deterministic and agent execution.

Open the copied `catalog.yaml`. Follow its paths to change the definitions.
Keep related files in feature folders, or use type directories where that is
clearer. Directory names are conventions, not discovery rules.

See [Catalog layout](catalog-layout.md) for the full source-map format.

## Add a definition

1. Write its definition file using the existing kind-specific schema.
2. Add its name and relative file path to the appropriate root section.
3. Validate the catalog locally.

For example:

```yaml
# catalog.yaml — additions to an existing root
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

Declare each definition once. A derivation's processor and inline trigger are
generated automatically. For grouped files, list every contained definition.
Unlisted files are not loaded; unlisted definitions inside listed files are errors.

The root's `config` explicitly selects models, ranking, and search profiles.
No configuration is inherited from the deployment when publishing.

## Check your work

```console
uv run memseek catalog-validate --dir ./my-catalog
uv run memseek catalog-locate customer_brief@1 --dir ./my-catalog
uv run memseek catalog-graph --dir ./my-catalog --out catalog.html
```

The first two commands also discover `catalog.yaml` from the current directory
or its parents. Errors identify source files and field locations. Source lookup
prints a file, line, and column; the graph shows the relationships between
compiled definitions. Generated JSON schemas supply editor completion and
structural validation.

## Publish to a workspace

```python
await client.catalog.publish(
    package="customer_memory@1.0.0",
    directory="./my-catalog",
)
```

Use `dry_run=True` to check compatibility without installing. The SDK reads
only the root and its declared files. Applications that generate YAML can send
the equivalent bundle with `client.catalog.publish_files(package=..., files=...)`.
The bundle must include `catalog.yaml` and every declared source.

The service validates the complete graph before atomically selecting it for the
workspace. Definitions are workspace-owned; no other tenant's catalog is merged
in. See [Packages](packages.md) and [Changing definitions](changing-definitions.md)
for release and compatibility behavior.

## Load a service-owned catalog

```python
from memseek.config import Settings
from memseek.definitions import load_definition_catalog

settings = Settings(catalog_file="./my-catalog/catalog.yaml")
catalog = load_definition_catalog(settings)
```

The environment equivalent is `CATALOG_FILE=./my-catalog/catalog.yaml`.
Leaving it unset means the process supplies no default definitions.

## Generate definitions in Python

`DefinitionSources` and `compile_definition_catalog` remain available for
in-memory generation. They use the same compiler validation and do not require
source paths. `DefinitionSources.from_catalog(catalog)` provides an editable
in-memory representation. Source locations are navigation metadata and do not
change semantic definition hashes.

## Migrate older catalogs

Replace `packages/*.yaml` with a root source map and declare every authored
definition explicitly. Move derivations out of the package's processor list into
`derivations`, and remove explicit membership entries for their inline triggers.
Keep definition bodies unchanged. Include inactive versions needed by existing
records. Compare release membership and bump the version when contents change.

Republish existing workspace catalogs. Legacy directory discovery and old
uploaded package files are no longer supported; stored records are not deleted
or automatically converted.
