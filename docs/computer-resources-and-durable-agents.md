---
title: How a Computer stays trustworthy
eyebrow: What is checked, and what is refused
---

A [Computer](computers.md) is the one place in Memseek where something *runs* —
your own code, or a model with tools and a filesystem. That is exactly the kind
of thing that usually forces a trade: give the agent enough access to be useful,
and you have given it enough access to be dangerous.

Memseek does not make that trade. This page is the plain-language list of what
is actually stopped, and how. The companion page,
[Computers, Programs & Agents](computers.md), covers the YAML.

## The one rule everything rests on

> Something running in a Computer may compute whatever it likes. Only Memseek
> itself may write a record.

No provider, Program, or Agent ever receives a database connection, a workspace
API key, or anything that can write to memory. A derivation's result comes back
as an ordinary value and still has to pass `emit`. A durable agent's output
comes back through paths you declared in YAML and is turned into records by
Memseek, which fills in the entity, collection, record type, status, and dedupe
key itself.

Take that rule away and none of the rest would matter. With it, the worst a
misbehaving run can do is fail.

## What could go wrong, and what stops it

| The worry | What actually happens |
| --- | --- |
| "Text we retrieved from memory tells the agent to do something." | Records rendered into context are escaped and labeled untrusted. Only the versioned instructions artifact you wrote is treated as instructions. |
| "The agent corrupts our data." | It has no writer. Derivations commit only through `emit`; durable runs only through the writeback paths the Computer declares. |
| "The agent invents a source." | It may cite only the record IDs that were in the evidence it was given. A citation outside that set fails the run. |
| "It writes somewhere it shouldn't." | The whole filesystem is hashed before and after. A change outside your declared writable roots fails the run. |
| "It edits its own instructions to loosen them." | `/.memseek` and `/inputs` are restored and reported after every run; tampering fails it. |
| "It reaches out to the internet." | Network access is refused at the boundary today. Not disabled by convention — refused. |
| "A retry runs the expensive work twice." | Work is identified deterministically and its result cached in the workspace, so a retry returns the original result. |
| "Someone replays or forges a provider response." | Requests are signed and timestamped; results carry hashes; database keys are idempotent. |
| "Definitions changed halfway through a long run." | Resuming requires every pinned version to be identical. Otherwise the API refuses and asks you to fork. |
| "It quietly drops evidence when context fills up." | At the pause threshold the run stops instead. Protected context is never silently evicted. |
| "It returns something enormous." | Request, response, event, outbox, file-count, byte, and step bounds all fail closed. |

## What a run is allowed to see

Before anything executes, Memseek assembles the run's material and writes it
into the sandbox as files:

```text
/.memseek/    instructions, skills, mounted context, and a manifest of exactly
              which records produced them
/inputs/      the typed input for this unit of work
```

That is the whole world. There is no query interface, no credential, and no way
to widen it from inside. The record IDs behind those files become the run's
citation list — the only IDs it may name in a result.

If your evidence plus instructions already exceed the context budget you
declared, the run does not start. It fails as `context_exhausted`, which is
recoverable, rather than silently working from a partial picture.

## What a run is allowed to return

Only two roots may change: `/workspace` for working files, and `/outbox` for
results. Everything else is compared to its pre-run hash.

In a **derivation**, `/outbox` may contain exactly one file: the result path the
task declared. It becomes a value, and that value goes through schema
validation, the citation check, `emit`, the destination collection's contract,
the staleness re-check, and review — the same path as any other task's output.
Nothing is auto-ingested.

In a **durable agent run**, `/outbox` may additionally contain the paths the
Computer declares in its `writeback:` block. Those are turned into records by
Memseek: observations become active evidence, maintained-state files become
drafts a person approves. Each candidate may carry only `text`, `content`,
`citations`, and an optional `key`. Everything that establishes trust is set by
the system.

Either way, the records, the result, the preserved files, and the completion
event commit together or not at all.

## How a long run keeps its head

An agent that works for twenty minutes will overflow any model's context. Four
structures keep that from becoming data loss:

- a **journal** of everything that happened, in order, that outlives the model's
  memory of it;
- a **working brief** holding the current objective, constraints, decisions,
  risks, and open questions, each pointing back at its source;
- **episode receipts** summarizing stretches of completed work; and
- **recall**, a search over the material the run is already allowed to see, which
  can pull an old detail back and write it to a file the agent — and you — can
  re-open exactly as it was shown.

Your [context policy](computers.md#context_policiesyaml-the-token-budget) sets
when each kicks in: large finished tool results become pointers at 70% of
budget, completed stretches become receipts at 82%, and at 92% the run pauses
rather than throwing anything away. The active objective, pending tool calls,
and unread results are never evicted at any level.

Briefs and receipts are the run's notes, not evidence. A final answer cites
original records or preserved source files.

## Resuming, retrying, forking

Three different things, often confused:

- **Retry** happens after a transport failure. Same workspace, same work
  identity, cached results for anything already finished.
- **Resume** continues a paused or finished run in its own workspace. It is
  refused unless every pinned definition is byte-for-byte identical — a run
  whose instructions changed mid-flight is not the run you started.
- **Fork** starts a child run from the parent's preserved files, and may pin new
  versions. Only the paths in `retention.preserve` cross over, so a fork
  inherits checkpoints rather than scratch state.

Throughout, the **entity** is the stable identity — the account, the customer,
the project. Several runs can work on one entity at the same time without
sharing a filesystem.

## What you can read afterwards

Every Computer-backed run leaves a receipt naming the exact definition versions
and their hashes, the provider and which backend actually ran, the Program's
hash when there was one, the range of operations, the input record IDs, the
output hashes, and the preserved files. Durable runs additionally expose their
ordered events, over both a cursor-paged endpoint and a replayable stream.

If a run behaved oddly last Tuesday, the answer is not reconstruction. It is
reading what it was given, what it did, and what it returned.

## What is proven, and what is not

The deterministic `fake` provider enforces the identical contract in-process —
standing in for the executing side, so every check runs without a sandbox — and
it is what CI runs: resource closure, both derivation task types, durable
run states, resume and fork identity, preserved-path copying, ordered and
streamed events, interactive turns, result files, receipts and recall, the
context pause, catalog-scoped writeback, the SDK and MCP bindings, and the
complete renewal example.

Not covered by automated tests, and exercised by hand against the same request
and response shapes: a live `provider: cloudflare` run. Reviewed network egress,
external Program bundles, offloading large payloads to blob storage, and
expiring an aged workspace on its declared `retention.workspace_days` are
deliberate future increments — they are absent, not hidden. In particular, do
not treat `workspace_days` as a deletion guarantee: it is recorded and carried
to the provider, and nothing sweeps on it yet.

## Running the Cloudflare provider

Switching a Computer from `fake` to `cloudflare` adds one process and one shared
secret. Nothing about the contract changes, and no YAML other than the
`provider:` line. That switch is also what turns declared execution into real
execution: `fake` stands in for the executing side, so a Program's code and an
Agent's model calls only actually happen against a runtime that speaks the
signed protocol.

### Settings

The adapter reads four environment variables. They carry **no** `MEMSEEK_`
prefix; a prefixed name binds nothing, and the adapter fails with *"Cloudflare
Computer runtime URL/token is not configured"*:

| Setting | Default | Meaning |
| --- | --- | --- |
| `COMPUTER_RUNTIME_URL` | *empty* | Base URL of the deployed Worker; the adapter posts to `<url>/v1/execute` |
| `COMPUTER_RUNTIME_TOKEN` | *empty* | Shared secret; must equal the Worker's `MEMSEEK_RUNTIME_SECRET` |
| `COMPUTER_REQUEST_TIMEOUT_S` | `300` | Per-request HTTP timeout |
| `COMPUTER_RESPONSE_MAX_BYTES` | `16777216` | Response cap; a larger body fails as `budget` |

Requests are signed over their exact bytes and carry a timestamp, with ±300 s of
allowed clock skew — so a proxy that re-serializes JSON bodies breaks
authentication.

### What the deployment requires

- **Workers AI models only.** An Agent's model alias must resolve to
  `workers_ai:@cf/…`; anything else is refused. Agent calls are real, billed
  Workers AI calls, and the model must return the
  `{"value": …, "citation_ids": [...]}` envelope or the run fails.
- **Inline Program source only.** `bundle: {uri, sha256}` is refused until a
  resolver can verify the hash before execution. For `worker-javascript`, only
  the entrypoint's source is loaded, so sibling files are readable from
  `/.memseek/program/` but are not importable.
- **No network capability.** `capabilities.network: true` is refused, and both
  backends run with egress disabled.
- **`/workspace` and `/outbox` only.** Every declared writable root must resolve
  under one of those two.
- **Docker, to deploy.** The Worker declares a container fallback, so
  `wrangler deploy` builds its image; running `worker-javascript` work needs no
  container instance.

The full implementation reference — what is deployed, the signed wire protocol,
the execution order, every limit, and the failure table — is
[The Cloudflare Computer runtime](computer-cloudflare.md).

## Next

- Write the files: [Computers, Programs & Agents](computers.md)
- Run it for real: [The Cloudflare Computer runtime](computer-cloudflare.md)
- See it end to end: [Run an agent inside a Computer](computer-renewal-example.md) — `make computer-demo`, step by step
- What happens to the result: [Runtime receipts and Candidate Sets](evaluation-bases.md)
