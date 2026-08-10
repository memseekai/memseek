# Domain context

## Pipeline

A bounded, entity-scoped computation declared in one file under
`derivations/`. A Pipeline names the canonical data it may read, calls a
sequence of registered Tasks, and emits one typed list of record drafts. A
Pipeline never writes canonical storage directly; its emission passes through
the runtime's provenance, schema, concurrency, and commit guarantees.

## Named Source

A value made available to Pipeline Tasks under an author-selected name. Every
Pipeline has exactly one driving Source of kind `changes` or `snapshot`, plus
any number of `current`, `record`, or `view` Sources. Each Source exposes
structured `records` and an escaped, unfenced `rendered` row form. Current and record
Sources are guarded reads and wait rather than falling back when the newest
matching row is not ready; view Sources are bounded queries whose selected
record IDs become provenance.

## Task

A process-installed, version-hashed Adapter selected by `use` and invoked in
declaration order. A Task receives optional typed per-run `input`, validated
strict `with` configuration, and a constrained context with bounded model,
search, and template capabilities. Built-in LLM results pass their complete,
explicitly authored `output_schema` to schema-capable provider Adapters and are
always validated locally; installed object configs inherit
`TaskConfigModel`. It returns an output-adapter-validated
JSON-compatible value with tracked provenance. Workspace YAML can select
registered Tasks but cannot upload executable code, access the database, or
write canonical records. Deployments list trusted registration modules in
`TASK_MODULES` so API and worker processes load the same registry.

## Emission

The Pipeline's statically declared canonical destination and one exact
reference to a Task result. Emitted drafts use one record vocabulary:
`key`, `text`, `content`, `citations`, and `retract`. The destination contract
infers the write behavior: no declared keys appends events; declared keys
update a subset; `complete: true` requires every declared key; and
`review: required` stages the records for explicit Promotion.

## Evaluation Basis

The private immutable read receipt captured before Tasks execute. It records
the driving Source checkpoint and record IDs, guarded current reads, and the
active keyed heads at the emission destination. A changes Source consumes a
suffix after the Pipeline cursor. A snapshot Source evaluates its complete
bounded scope through an exact checkpoint. The runtime persists this receipt
for audit and rechecks its guards before commit, but authors do not configure
an Evaluation Basis directly. Incremental receipts also persist a
Source-membership hash: computation and budget changes may continue at the same
cursor, while a change to which records belong to it is rejected.

## Candidate Set

The private write proposal compiled from the emitted record drafts after
citation, key, bound, and collection-schema validation. Its internal behavior
is inferred as append, partial keyed update, or complete keyed replacement;
its activation is inferred as immediate or reviewed. Authors declare emission
intent instead of constructing a Candidate Set.

## Divergence

A deterministic keyed comparison between a Candidate Set and the active heads
captured in its Evaluation Basis. Each proposed key is classified as `added`,
`changed`, `removed`, or `unchanged`. Divergence describes difference; it does
not evaluate quality.

## Promotion

The explicit, atomic activation of a reviewed emission. Promotion copies draft
proposals into new active successor records; it never mutates the drafts. It
rechecks the active-head preconditions captured before Task execution and
rejects the entire operation when the candidate is stale.

## Artifact Use

The registered correlation handle proving that one exact rendering was prepared
for external use. It holds artifact identity, definition and rendered-content
hashes, the resolved Learning Target, and an optional snapshot reference — and
has no column able to hold a render, request parameters, a model response, a
tool call, or a trace span. It asserts only that Memseek rendered an artifact,
never that a model ran. It is operational metadata and expires.

## Learning Target

The improvement destination an artifact declares once and the runtime resolves
per render: a named document block plus the exact reviewed artifact owning that
value's promotion lifecycle. Resolution captures the active keyed heads that were
in force and, when one promotion wrote them all, that run as the base version.
Heads promoted separately resolve to no single base, and a block that read no
head resolves to no target, so a signal is never attributed to a version that
never influenced the execution.

## Learning Signal

An ordinary event record naming an Artifact Use and one selected outcome —
kind, source, optional score and label, and bounded evidence. It is written
through the public record path, so dedupe, schema validation, provenance,
search, and erasure keep their normal semantics. Its entity routes it to the
reviewed artifact that should improve and its type is the signal kind, which is
what lets a candidate Pipeline select it with an ordinary Source. With an
artifact snapshot it cites that snapshot; without one it carries identity and
hashes as metadata and claims no provenance edge to the original sources.

## Structural Graph

A bounded projection of ordinary canonical event records into directed edges.
A graph View declares the edge collection and maps its filterable string fields
to subject, object, and predicate roles; an orphan View may also declare a keyed
node collection. Traversal preserves workspace isolation, readiness, tombstone,
and provenance semantics and returns the used edge records as citations. Graphs
remain named Views over canonical storage, not a separate database or endpoint.
