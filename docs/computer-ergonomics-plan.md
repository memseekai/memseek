---
title: Computer ergonomics — lessons from Archil
eyebrow: Design plan
---

# What Archil gets right, and what we should take

This is a planning document, not shipped behaviour. It reads
[Archil](https://docs.archil.com/) — infrastructure for stateful agents built
around *shared disks plus disposable compute* — against Memseek's
[Computers, Programs & Agents](computers.md) and
[the trust story behind them](computer-resources-and-durable-agents.md), and
proposes a set of changes to our Computer surface.

The one-line summary: **Archil's data plane is more ergonomic than ours; our
trust boundary is stronger than theirs. The work is to import their ergonomics
without importing their laxity.**

The build-vs-buy question this raises — should we adopt Archil as
infrastructure rather than implement these ourselves? — is answered separately
in [Archil: integrate or replicate?](archil-integration-decision.md). The short
answer is replicate: our containment proof is a whole-filesystem hash diff, and
a shared petabyte-scale disk cannot support one. Everything below is therefore
written as work on our own substrate.

Archil says outright that "branches aren't a security boundary" and that
"credentials are scoped to the entire disk, including all of its branches and
checkpoints." We cannot say that. Every proposal below is therefore stated in
terms of what it does to citation containment, the receipt, and the writeback
contract — not just what it makes convenient.

---

## 0. Where we are already ahead

Worth naming first, so the rest reads as a list of gaps rather than a verdict.

| We have | Archil has | Why it matters |
| --- | --- | --- |
| Schema-bound input **and** output on every Program, validated by us, not the sandbox | Free-form stdout, trailing 128 KiB, caller parses | A run cannot return a shape nothing agreed to |
| Citation containment — a run may only cite IDs it was shown, checked after the fact | No provenance model | This is the product |
| A declared writeback channel owned by the *Computer*, not the agent | Anything the process can write to the disk | An agent cannot choose where it writes |
| Definitions pinned by hash, and `resume` refused with `409` when a pin moved | Mutable disks, mutable code | A run whose instructions changed is not the run you started |
| `idempotency_key` and work identity reuse across retries | Not emphasised | Retries do not duplicate work |
| Receipts: every command, file change, model call, and hash | `timing` and exit code | Auditability |

Nothing below should weaken any row of that table.

---

## 1. The ceremony-to-first-result ratio is our worst problem

**Archil.** `disk.exec("grep -c ERROR /mnt/archil/logs/*")` — one call, no
resource to create, no YAML, billed at 100 ms granularity. Sandboxes exist for
when you outgrow that, and forking a sandbox is a separate, heavier door.

**Us.** *Any* code at all requires: a `computers/*.yaml`, a `programs/*.yaml`
with two JSON Schemas and inline source, a package listing both, a publish, and
then either a derivation task or a durable invocation to trigger it. There is
no way to run five lines against evidence and look at the output.

This is not a safety property — it is friction we have not yet paid down. The
publish-time checks are what make a Computer trustworthy; a *one-shot*
execution can carry the same checks and skip everything about persistence.

### Proposal 1 — `POST /computers/{ref}/exec`

An ephemeral run against an already-published Computer:

```json
{
  "entity": "account:acme",
  "program": "contract_extract@3",
  "input": {"document_id": "…"},
  "ephemeral": true
}
```

- No session, no workspace retention, **no writeback** — the result comes back
  in the response and is never ingested as records.
- Same schema validation, same capability check, same citation containment,
  same receipt (written to the journal, so ad-hoc runs are still auditable).
- Only permitted when the Computer declares `exec_adhoc: true`, so it is an
  opt-in property of a sandbox policy rather than a global back door.
- Hard caps well below the durable path: one step, small output, short timeout.

### Proposal 2 — an inline Program for development only

`use: computer` currently refuses inline source by design. Keep that for
published packages, and allow a `draft` catalog — an unpublished, workspace-
scoped overlay where a Program's source may be edited and re-run without a
version bump. Publishing freezes it into the normal immutable form. This is the
difference between "I can iterate" and "I bump `contract_extract` to @7 to fix
a typo."

---

## 2. Split the *disk* from the *run*

**Archil.** A disk is the durable, shareable, mountable thing. Compute is
disposable and attaches to it: `exec` mounts disks at `/mnt/archil/<key>`, each
with its own read-only flag or subpath restriction; a sandbox mounts the same
disk; so does a laptop, via `archil mount`. One disk, many attachers, many
lifetimes.

**Us.** The workspace *is* the session. It is created per run, lives
`retention.workspace_days`, and the only thing that crosses runs is the
`retention.preserve` allowlist copied at fork time. There is no object that
means "the durable working memory of this entity, across every run that ever
touched it."

That absence is why `preserve` has to do two unrelated jobs — naming what
outlives the workspace, *and* defining what a fork copies — and does neither
cleanly.

### Proposal 3 — `volumes/*.yaml`

A fifth optional catalog family: a named, versioned, **entity-scoped** durable
volume.

```yaml
volumes:
  - name: renewal_working_set
    version: 1
    scope: entity          # entity | workspace
    retention_days: 365
    quota_bytes: 1073741824
    writeback: []          # a volume is scratch; records still leave via /outbox
```

And on a Computer, a mount table rather than a single implicit workspace:

```yaml
mounts:
  - {path: /vol/working, volume: renewal_working_set@1, mode: read_write}
  - {path: /vol/corpus,  volume: shared_contract_corpus@2, mode: read_only}
```

Rules that keep this from becoming Archil's disk:

- A volume is **not** evidence. Nothing read from a volume is citable on its
  own; if a run wants to cite something it cached there, the record ID has to
  have been in that run's own citation set. The check is unchanged — it just
  now has to survive a file that outlived the run that wrote it.
- `scope: entity` means the volume is partitioned by entity and a run may only
  ever see its own entity's partition. Cross-entity sharing requires
  `scope: workspace` **and** `mode: read_only`, which is the only shape where
  one entity's run can read another's bytes — so it must be reviewed at publish
  time and named in the receipt.
- Quotas are enforced, not advisory. A volume that fills stops the run with a
  `budget` error rather than silently truncating.

This is the same object that the
[agent filesystem plan](selective-disclosure-and-agent-fs-plan.md) needs on the
other side — that document projects *memory* as files; this one gives the agent
a place to *write* files that is not a record. They should land together, and
share one path grammar.

---

## 3. Checkpoints during a run, not just a fork at the end

**Archil.** `archil checkpoints create /mnt/archil pre-migration` at any moment;
`archil branches create <disk> experiment-1 --from-checkpoint pre-migration`.
Checkpoints are immutable, branches are writable forks of one, and copy-on-write
means fifty branches off a 100 GiB parent meter at ~350 GiB, not 5 TiB.

**Us.** `POST /invocations/{id}/fork` copies `retention.preserve` from wherever
the parent happens to be. There is no way to say "fork from the state it was in
before it started the risky part," and no way to fan out several attempts from
one point.

### Proposal 4 — named checkpoints, and fork-from-checkpoint

- `POST /invocations/{id}/checkpoints {"name": "pre-negotiation"}` — and the
  same thing available to the run itself as a tool, so an Agent can mark its own
  ground state before an experiment.
- `POST /invocations/{id}/fork {"from_checkpoint": "pre-negotiation"}`.
- `POST /invocations/{id}/fork {"from_checkpoint": …, "count": 3}` — the
  fan-out case. Three children from one point, each pinnable to a different
  Agent or model alias, all sharing the parent's citation set (never widening
  it). This is the honest way to run "try three renewal strategies" and then
  compare receipts.
- A checkpoint captures the whole workspace, not a `preserve` allowlist. That
  lets `preserve` shrink to its one real job: what survives
  `retention.workspace_days`.

Copy-on-write is an implementation detail we should nonetheless commit to in
the metering model, or fan-out becomes unaffordable and nobody uses it.

---

## 4. Pause should mean pause

**Archil.** *Stop* sends `SIGTERM` and shuts the VM down, disk intact. *Pause*
snapshots CPU and memory, and resume continues at the exact instruction. Forking
a running source checkpoints it first; forking a stopped one cold-boots from
disk.

**Us.** `awaiting_input` re-queues the invocation and reuses the same workspace
and work identity, which is genuinely good — but the executor restarts. Whatever
the model was holding that never reached a file is gone, and we rebuild context
from the journal.

### Proposal 5 — state the resume contract, then strengthen it

Two separable pieces:

1. **Document what resume guarantees today.** Right now `docs/computers.md`
   says the workspace and work identity are reused; it does not say the model
   loop restarts. Users are inferring a stronger guarantee than we give. Fix the
   documentation first — it costs nothing.
2. **Make `awaiting_input` checkpoint automatically.** Reuse Proposal 4: pausing
   for a human takes an implicit checkpoint named `awaiting_input:<n>`. That
   gives the human replying a fork point if the answer changes direction, and
   gives us a real snapshot to resume from when the runtime can carry one.

### Proposal 6 — a wall clock that is actually enforced, and a TTL

`limits.max_wall_s` is documented as "declared intent today" because the
Cloudflare runtime bounds the loop by steps. Archil's sandboxes take a TTL —
default 8 h, range 60–28,800 s — and terminate on it. We need both:

| Limit | Applies to | Terminal state |
| --- | --- | --- |
| `max_wall_s` | one execution | `failed` with a `budget` code |
| `ttl_s` | the whole invocation, including `awaiting_input` | new `expired` state |

`expired` matters: an invocation sitting in `awaiting_input` forever is an
entity-scoped resource leak with no story. Archil answers this with a TTL by
default. We should too, and make it a final state alongside `cancelled`.

---

## 5. Egress: from a flat refusal to a reviewed gateway

This is the largest single win available, and the place Archil's design is most
directly transplantable.

**Archil's sandbox firewall.** Default `deny`. Allowlist by CIDR *and* by
domain, with HTTP on 80/443 validated across SNI, `Host`, and URI authority
(HTTP/3 blocked when domain filters are on, since it defeats inspection).
Policies update at runtime without stopping the sandbox, so a run can move
through phases — fetch inputs, then total isolation while the agent reasons,
then a narrow allow to upload results. And the piece that matters most: the
gateway can **inject credentials into outgoing requests so the sandbox never
sees the secret**.

**Us.** `capabilities.network: true` is refused at execution time with *"network-
enabled Computers require a separately reviewed egress gateway."* That gateway
does not exist. So a Computer can never look anything up, and the whole
"research workspace" framing is currently aspirational.

### Proposal 7 — `egress:` in the Computer policy

```yaml
capabilities: {filesystem: true, exec: true, network: true}
egress:
  default: deny
  allow:
    - host: api.example.com
      methods: [GET]
      paths: ["/v1/contracts/*"]
      inject: {header: Authorization, secret: example_api_key}
  phases:
    research: {allow: [api.example.com]}
    reasoning: {default: deny}
  record_as_source: true
```

Three properties make this ours rather than a copy:

- **The sandbox never holds a secret.** `inject.secret` names a workspace secret
  resolved *at the gateway*. The run's own environment stays empty, so a prompt
  injection that gets the agent to `cat` its environment or exfiltrate to an
  allowed host still gets nothing.
- **`record_as_source: true` turns a fetch into evidence.** Every allowed
  request is hashed, stored, and given a record ID that enters the run's
  citation set. A run that used an external page can then *cite* it, and the
  claim it supports is as traceable as one grounded in a collection. This is the
  inversion that makes network access a provenance feature instead of a hole:
  today an agent cannot look anything up; under this proposal it can, and every
  lookup becomes auditable evidence rather than untracked context.
- **Phases are declared, not requested.** The run cannot widen its own policy
  mid-flight; the derivation or invocation says which phase it is in, and the
  transition is journalled.

Everything fetched is untrusted input and must be escaped and labelled by the
same renderer that handles records today. An allowed host is not a trusted one.

---

## 6. Search that scales, and structured state

**Archil.** `grep` fans listing and matching across many ephemeral containers —
claimed 10× ripgrep over millions of files — bounded by `maxResults`,
`maxDurationSeconds`, and an optional explicit `concurrency`. Their framing is
worth quoting: high-performance agents increasingly prefer "lowest-common
denominator infrastructure like file storage" over vector databases.

**Us.** `recall` already has the right *shape* — it searches only `/.memseek`
and `/workspace`, so it can never widen what a run knows, and it writes every
hit to `/workspace/recalled/<hash>.json` so the exact text is re-openable
afterwards. That last property is better than Archil's, and we should keep
saying so.

### Proposal 8 — bound `recall` the way Archil bounds grep

Add `max_results`, `max_duration_s`, and a byte budget to the `recall` tool, and
report all three in the receipt. Extend its scope to mounted volumes
(Proposal 3) under the same containment rule. A long run that greps a large
volume should degrade into a truncated, *labelled* result rather than eating its
step budget.

### Proposal 9 — SQLite in the workspace, and a query-shaped writeback

**Archil.** Native SQLite on a disk: `createDatabase()` / `getDatabase()`,
separate `read` (parallel) and `write` (exclusive) methods, and a `transaction`
for atomic batches.

Our writeback is JSONL and per-file JSON. For a run that accumulates a few
thousand structured observations, that is the wrong container. Propose a fourth
writeback type:

```yaml
writeback:
  - path: /outbox/findings.db
    type: table
    query: "SELECT text, citations, key FROM findings WHERE ready = 1"
    review: true
    collection: renewal_proposals@1
    record_type: pricing_commitment
```

The declared `query` is part of the Computer, hashed with it, and run by *us*
against the returned file — the sandbox does not get to choose the projection.
Every row still goes through the same four-key candidate check and the same
citation containment. This is a container change, not a trust change.

---

## 7. Sizing, images, and inspection

Three smaller gaps, grouped.

**Sizing.** Archil sandboxes: 1–32 vCPU, 0.25–64 GiB RAM, declared per sandbox.
Our `runtime:` has `default`, `fallback`, and `fallback_requires`, and no way to
say a container Program needs 4 GiB. Propose
`runtime.resources: {vcpu, memory_mib}` with defaults, validated at publish and
recorded in the receipt so cost is attributable.

**Images.** Archil pulls base images from public OCI registries. Our container
runtime "builds an image" with no declared base. Propose `runtime.image:` pinned
**by digest**, never by tag — a floating tag would break the property that "the
same package" means the same code.

**Inspection.** Archil's most quietly useful debugging affordance is that you
can `archil mount` the agent's disk on your laptop and watch it work. We expose
preserved artifacts only *after* the run, via
`GET /invocations/{id}/artifacts`. Propose a read-only
`GET /invocations/{id}/workspace/{path}` available **while running**, subject to
the caller's own authorization rather than the run's. Watching an agent work is
how you learn to write its instructions.

---

## 8. Cost has to be visible

**Archil.** Time-weighted average of active data at a published per-GiB-month
rate; 100 ms minimum billing per invocation; a free tier scaled to data volume;
a Usage tab per disk. Copy-on-write metering is explained explicitly so users
can reason about branch fan-out before they try it.

**Us.** Budgets are expressed in steps and bytes — enforcement units, not cost
units. Nothing tells you what a run *cost*.

### Proposal 10 — usage in every receipt

```json
"usage": {
  "wall_ms": 4210, "cpu_ms": 1180,
  "model": {"alias": "…", "tokens_in": 18422, "tokens_out": 1204},
  "bytes_read": 2118400, "bytes_written": 44210,
  "egress_requests": 3, "volume_gib_hours": 0.004
}
```

Aggregate it on `GET /invocations/{id}`, and let `limits:` be expressible in
those units — `max_model_tokens` is a far more useful budget than `max_steps`,
which is only a proxy for it. This should be built against, not beside,
[the cost tracking plan](cost-tracking-plan.md).

---

## 9. SDK ergonomics

Small, cheap, and disproportionately visible.

| Archil | Us | Proposal |
| --- | --- | --- |
| Every method is sync, with `.aio()` for the awaitable form | Async only | Offer the same duality — the first thing anyone does is try it from a REPL or a notebook |
| Transient failures (429, 5xx, network) retry automatically; other 4xx surface immediately | Caller's problem | Same policy, and say so in the docs |
| One error base class, `ArchilError` | — | Confirm we have the equivalent for invocation and Computer errors, with the `code` already carried by `ComputerExecutionError` exposed on it |
| `put_object()` transparently multiparts large uploads | — | Applies to volume writes if Proposal 3 lands |

---

## 10. What we should deliberately *not* copy

- **Shared mutable disks with disk-wide credentials.** Archil's own docs say
  branches are not a security boundary and that credentials span every branch
  and checkpoint. Our volumes must be entity-partitioned and the cross-entity
  case must be read-only and reviewed. This is the whole difference.
- **POSIX permissions as isolation.** Archil says plainly they should not be
  used that way for co-tenant untrusted workloads. Our boundary stays the
  workspace and the declared writeback, not file modes.
- **Publicly reachable service endpoints.** Archil sandboxes get stable HTTPS
  URLs and the docs warn that these are public and you must add your own auth.
  A Memseek Computer should never be addressable from outside; if something
  needs to be served, it is served by us, from ingested records.
- **Free-form stdout as the result channel.** Their 128 KiB trailing-output
  convention is a pragmatic answer to an unschematised API. We have schemas.
  Keep them.

---

## 11. Suggested order

Roughly by ratio of value to risk.

| Phase | Items | Why here |
| --- | --- | --- |
| **A** | Proposal 5.1 (document the resume contract), 6 (`ttl_s` + enforced wall clock), 8 (bound `recall`), 9 (SDK duality and retries) | Documentation and limits. No new concepts, and 5.1 is a correctness fix to the docs we should not sit on. |
| **B** | Proposal 1 (`exec` one-shot), 2 (draft catalog) | Fixes the ceremony problem. Reuses every existing check. |
| **C** | Proposal 10 (usage in receipts), 7 (sizing, pinned images, live workspace read) | Makes A and B measurable and debuggable. |
| **D** | Proposal 3 (volumes) + 4 (checkpoints and fan-out forks) | One design, landed together, shared with the agent-filesystem plan. The largest data-model change here. |
| **E** | Proposal 7 (`egress:` gateway) | Highest value, highest review burden. Needs the receipt work from C to be honest about what a run reached. |
| **F** | Proposal 9 (SQLite writeback) | Only worth it once D exists and someone has actually hit the JSONL ceiling. |

## Open questions

1. **Do volumes belong to an entity or a session family?** Entity is the more
   useful scope and the more dangerous one. If two sessions on the same entity
   write the same path concurrently, what happens — last write wins, a
   compare-and-swap head like the record path, or an exclusive lease?
2. **Is a fetched page a record?** Proposal 7's `record_as_source` implies
   egress responses land in a collection. Which one, whose retention, and does
   it count against the entity's evidence budget?
3. **Does the fan-out fork share a citation set or narrow it?** Sharing is
   simpler and is what "same checkpoint, different strategy" means. Narrowing
   per child would let us run genuinely blinded comparisons.
4. **Can an ad-hoc `exec` run an Agent, or only a Program?** Programs are the
   obvious yes. An ad-hoc Agent run with no writeback is tempting for
   development and is also the easiest way to accidentally build an
   unaccountable chat endpoint.

## Sources

Archil documentation, read 2026-09-03 —
[sandboxes](https://docs.archil.com/compute/sandboxes/introduction.md),
[firewall](https://docs.archil.com/compute/sandboxes/firewall.md),
[serverless execution](https://docs.archil.com/compute/serverless-execution.md),
[branches & checkpoints](https://docs.archil.com/concepts/branches-and-checkpoints.md),
[authorization](https://docs.archil.com/concepts/authorization.md),
[metering](https://docs.archil.com/concepts/metering.md),
[search files](https://docs.archil.com/compute/search-files.md),
[SQLite](https://docs.archil.com/compute/sqlite.md),
[Python SDK](https://docs.archil.com/sdks/python.md),
[fork sandbox](https://docs.archil.com/api-reference/sandboxes/fork-sandbox.md),
[bash tool for agents](https://docs.archil.com/guides/ai/bash-tool.md),
[multi-agent systems](https://docs.archil.com/guides/ai/multi-agent-systems.md).
