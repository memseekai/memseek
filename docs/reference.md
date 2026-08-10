---
title: Reference checklist
eyebrow: Before you publish
---

Use this page as a compact review checklist for a catalog change.

It is a final review aid, not an introduction. Follow
[Catalog layout](catalog-layout.md) in order for first-time authoring, and use
the [Glossary](glossary.md) when a checklist term is unfamiliar.

## Identity and names

- [ ] Public names are lowercase and match the documented length/pattern rules.
- [ ] Collection, view, and artifact versions are positive integers.
- [ ] Package version is semantic versioning.
- [ ] References are exact (`name@1`, never just `name`).
- [ ] Computed `definition_hash` values are absent from source.

## Collections

- [ ] `schema` is an object JSON Schema and validates the intended `content`.
- [ ] `mode` matches how callers use `key`.
- [ ] Required and optional processor lists do not overlap.
- [ ] Every declared field path is under `content` or `annotations`.
- [ ] Array field types use exactly one scalar type and are not sortable.
- [ ] Search filters/order use field names declared in the collection.
- [ ] Breaking changes use a new collection version.
- [ ] `answerable: true` is declared on exactly the collections a model may synthesize prose
      over — interpreted layers, not raw transcripts, snapshots, signals, or saved answers.
- [ ] Callers of `POST /answer` pass `entities` whenever the workspace holds more than one
      memory scope; omitting it answers over every entity in those collections.

## Processors and models

- [ ] Every model target is `provider:model` and the provider accepts its params.
- [ ] `embed` is a single target with no completion params.
- [ ] LLM-sourced score processors have `default`, `model`, and `prompt`; `client`- and `constant`-sourced processors obey their narrower contracts.
- [ ] JSON processor output schemas are objects.
- [ ] JSON processor default outputs validate against their output schema.
- [ ] Projected score names are unique and resolve to numeric schema leaves.
- [ ] Every processor declares an `input` scope, and it includes every collection that binds it.

## Derivations and triggers

- [ ] `sources` contains exactly one driver — `changes`, `snapshot`, or `stale_citations`; every source scope references existing collections, versions, types, and statuses.
- [ ] A `snapshot` `window` is declared deliberately, and the run's bounds cover the windowed corpus rather than all history.
- [ ] `emit.driver_key` is used only where the output key genuinely comes from the driving record, with `max_records: 1` and no static `keys`.
- [ ] `emit.dynamic_keys` is used only for a bounded independent-block collection and declares `max_active_keys`.
- [ ] `changes` is intentional for incremental work; a `snapshot` source's record and token bounds cover its complete selected scope.
- [ ] `current`, `record`, and `view` sources expose only the extra named reads each task needs.
- [ ] `limits` cover the declared tasks and stay within deployment budgets.
- [ ] Every task has a unique `id`, selects an installed task type with `use`, and supplies only that task type's typed `input` and static `with` configuration.
- [ ] Built-in `search` tasks use exactly one of `q` or `foreach`; custom tasks have validated input/config/output types and no ability to write records directly.
- [ ] Template references name declared sources, earlier task results, or explicit `entity.*`/`run.*` values.
- [ ] `emit.from` is one exact typed reference to a task result.
- [ ] The emission collection and type are the contract you intended; event emission omits `keys`.
- [ ] Keyed partial updates declare `keys`; complete replacements also set `complete: true` and return every key as a value or retraction.
- [ ] Model-generated replacements that require approval set `review: required`; promotion stays explicit.
- [ ] Emitted drafts use generic `text`/`content`, optional `key`, `citations`, and `retract` fields.
- [ ] Trigger predicates use declared, filterable fields and correctly typed operands.
- [ ] Consuming trigger scopes (`write`, `quiet`, `changed`, `retraction`) are subsets of the driving source; observational scopes (`at`, `census`) reference existing collections.
- [ ] `at` trigger fields are declared filterable datetime scalars, and the derivation reads its dated records through a `snapshot`, `current`, or `view` source so they are visible at fire time.
- [ ] Accumulator metrics name existing scorers or required-annotation leaves; the aggregate and `comparison` direction are intentional.
- [ ] `quiet.after_s`, `debounce_s`, and `cooldown_s` pacing matches real burst length and run cost.
- [ ] Automatic trigger graph has no cycles and fits the depth limit.

## Views and artifacts

- [ ] View parameters have the right type, and required parameters have no default.
- [ ] Graph views bind an event edge collection and three declared, filterable string role fields; orphan views also bind a keyed node collection.
- [ ] Search mode, scope, fields, predicates, and backend capabilities agree.
- [ ] Structured sources have `order_by`; multi-source queries have `fuse`.
- [ ] Artifact blocks choose exactly one of `document` or `view`.
- [ ] Block token budgets and global render limits are sufficient.
- [ ] Reviewed artifacts specify a candidate processor and complete keys.
- [ ] The reviewed derivation emits a complete draft proposal; promotion stays explicit.
- [ ] Operators inspect persisted divergence and handle `promotion_stale` by rebuilding rather than forcing old state.

## Artifact uses and feedback

- [ ] Every artifact whose outcomes should teach something declares `learning`.
- [ ] `learning.target_block` names a `required` `document` block reading `status: active`.
- [ ] `learning.artifact` is an exact `name@version` reference to a `lifecycle: reviewed` artifact that maintains the target block's collections.
- [ ] The package lists the learning-target artifact alongside the artifact that names it.
- [ ] The catalog defines a `learning_signals` collection whose schema admits the signal kinds and sources the application submits.
- [ ] `learning_signals` requires no processors, so a signal is trigger-eligible the moment it commits.
- [ ] The candidate derivation's source scope matches the signal entity (`artifact:<name>`) and the signal kinds it should act on.
- [ ] Clients pass a `dedupe_key` scoped to the rated object, so retried feedback is idempotent.
- [ ] `snapshot: true` is used only where exact historical provenance is genuinely required, and the artifact declares a `snapshot:` target.
- [ ] `ARTIFACT_USE_RETENTION_DAYS` is at least as long as the real user feedback window.
- [ ] No processor depends on fetching an `execution_refs` target.
- [ ] Telemetry attributes carry only the reserved `memseek.*` scalars; no prompt, record content, model output, or customer identifier.

## Package and release

- [ ] The package lists every exact collection, processor, trigger, view, artifact, and required search profile it uses.
- [ ] Required and optional search profiles do not overlap.
- [ ] The uploaded request package matches the manifest file.
- [ ] The new package preserves collection contracts needed by existing records.
- [ ] The compiled catalog hash and normalized `/collections`, `/processors`, `/triggers` payloads were reviewed.
- [ ] `make check` and the relevant end-to-end flow pass.

## Source-of-truth links

When this guide and the implementation differ, verify the current release against:

- `src/memseek/definitions/models.py` for definition fields and local validation.
- `src/memseek/artifact_uses.py` for the artifact-use, telemetry, and feedback contracts.
- `src/memseek/derive/schema.py` for derivation/trigger fields.
- `src/memseek/derive/tasks.py` for the trusted task task type Interface and built-ins.
- `src/memseek/search/spec.py` for SearchSpec fields and limits.
- `src/memseek/config.py` for runtime settings and environment names.
- `examples/crm_profile_catalog/` for a complete package.
