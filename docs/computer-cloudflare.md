---
title: The Cloudflare Computer runtime
eyebrow: The deployed provider, end to end
---

A [Computer](computers.md) declares *what* may run, on *which* evidence, and
*what* may come back. It does not say how a sandbox is built. That is the
provider's job, and `provider: cloudflare` is the deployed one: a Worker that
owns a durable filesystem per session, runs either your Program's source or an
Agent's model-and-tool loop, and answers with a value plus a hash-bearing
receipt.

The important property is what the Worker does **not** hold. No database
connection. No workspace API key. No record writer. No list of your
collections. It receives one signed, self-contained job — the resolved
definitions, the rendered evidence, the record IDs that job may cite — and it
returns a value. Every promise about provenance is re-checked by Memseek after
the answer comes back, because the runtime is not trusted to have kept any of
them.

!!! note "Three pages, one system"
    [Computers, Programs & Agents](computers.md) is the YAML.
    [How a Computer stays trustworthy](computer-resources-and-durable-agents.md)
    is the list of guarantees. This page is the implementation of the deployed
    provider: what is deployed, what crosses the wire, in what order things
    happen, and every limit that applies. The source is
    `cloudflare/computer-runtime/` in the repository.

## If you have never deployed a Worker

Four Cloudflare words appear throughout this page. In the terms that matter
here:

| Word | What it is | Why this runtime uses it |
| --- | --- | --- |
| **Worker** | A small program Cloudflare runs on its own network. You deploy it with the `wrangler` CLI and it gets an HTTPS URL. | It is the front door: it checks the signature on each job, validates it, and routes it. It holds no state of its own. |
| **Durable Object** | A single, *named* instance of a class, with private storage that stays put between requests. Ask for the same name and you reach the same instance and the same data. | This is what makes a session a real workspace. The name is the session key, so "the same session" literally means "the same files". |
| **Workers AI** | Cloudflare's hosted models, reached through a binding rather than an API key. | An Agent's model alias must resolve to one of these. The calls are real and **billed to your Cloudflare account**. |
| **Container** | A Linux container Cloudflare can start alongside the Worker. | Only used by the optional `container` backend, for Programs that need a shell. Declaring it is why deploying needs Docker. |

Two things follow that are worth being explicit about, because they surprise
people:

- **Nothing about your memory lives on Cloudflare.** The Worker has no database
  connection, no workspace key, and no record writer. It receives one
  self-contained job — the resolved definitions, the already-rendered evidence,
  the record IDs that job may cite — and answers with a value and a receipt.
  Records are written afterwards, by Memseek, from that value.
- **The files, however, do.** A session's workspace is a Durable Object's
  storage, and anything an Agent writes to `/workspace` stays there. A Computer
  declares an intended lifetime in `retention.workspace_days`, but nothing
  deletes a workspace on that schedule today — so mount only what the work
  needs, and treat what you write as persistent until you remove the Durable
  Object yourself.

Costs, in the same spirit: Programs on `worker-javascript` are ordinary Worker
requests and Durable Object storage. Agent runs additionally make **billable
Workers AI calls** — including from `wrangler dev`, because the `AI` binding is
always remote. The container backend bills only when you actually use it. There
is no Memseek-side meter; your Cloudflare dashboard is the source of truth.

If you only want to *build* a catalog with Computers in it, you do not need any
of this yet: `provider: fake` enforces the same contract in-process, with no
account, no deployment, and no model bill — though it stands in for the
executing side rather than running your code, so see
[Choosing a provider](computers.md#choosing-a-provider) for what that does and
does not prove. `make computer-demo` is the middle ground: the real protocol and
the real Program code, served by a process on your laptop.

## The shape of it

```mermaid
flowchart LR
  subgraph MS["your deployment (trusted)"]
    API["API / worker<br/><br/>resolves definitions,<br/>renders evidence,<br/>signs the job"]
    DB[("Postgres<br/><br/>records, receipts,<br/>invocation journal")]
    API <--> DB
  end
  subgraph CF["Cloudflare (untrusted)"]
    W["Worker<br/><br/>HMAC auth, request<br/>validation, error mapping"]
    C["MemSeekComputer<br/>Durable Object<br/><br/>one per session:<br/>the durable filesystem"]
    AG["MemSeekAgent<br/>Durable Object<br/><br/>model + tool loop"]
    AI["Workers AI"]
    W --> C
    W --> AG
    AG --> C
    AG --> AI
  end
  API -- "POST /v1/execute<br/>signed canonical JSON" --> W
  W -- "value + citations + receipt" --> API
```

Nothing flows from Cloudflare back into memory except a JSON value, a list of
citation IDs, and a receipt. Records are written by Memseek, from that value,
after its own checks.

## What gets deployed

| Piece | Where | Role |
| --- | --- | --- |
| HTTP boundary | `src/index.ts`, `default.fetch` | HMAC auth, size cap, request validation, error mapping |
| `MemSeekComputer` | `src/index.ts` | One SQLite-backed Durable Object per session. Owns the `@cloudflare/computer` workspace and both execution backends. |
| `MemSeekAgent` | `src/index.ts` | A Cloudflare Agent that runs the model-and-tool loop against Workers AI, attached to the same workspace. |
| Protocol types | `src/protocol.ts` | `ExecuteRequest` / `ExecuteResponse`, mirroring Memseek's `ComputerRequest` / `ComputerResult`. |
| Path and crypto policy | `src/security.ts` | `safeAbsolutePath`, `safeRelativePath`, `shellQuote`, `verifySignedBody`, `sha256`. |
| Container image | `Dockerfile` | Debian slim plus `computerd`, FUSE-mounted at `/workspace`, port 8080. |

### Bindings and Worker parameters

From `wrangler.jsonc`:

| Binding / setting | Value | Why it is there |
| --- | --- | --- |
| `COMPUTER` | Durable Object namespace for `MemSeekComputer`, and the container class | The session workspace. `idFromName(session_key)` is what makes "same session" mean "same files". |
| `AGENT` | Durable Object namespace for `MemSeekAgent` | The reasoning loop, addressed as `<session_key>:<agent ref>`. |
| `LOADER` | `worker_loaders` binding | Dynamic Workers, for the `worker-javascript` backend. |
| `AI` | Workers AI binding | Agent model calls. Programs never use it. |
| `MEMSEEK_RUNTIME_SECRET` | Worker **secret**, not a var | The shared HMAC secret. Unset means every request gets `503`. |
| `migrations` | tag `v1`, `new_sqlite_classes: [MemSeekComputer, MemSeekAgent]` | Both Durable Objects are SQLite-backed. |
| `containers` | `instance_type: standard-2`, `max_instances: 10` | The container fallback backend. Declaring it is why `wrangler deploy` needs Docker even if you only ever use the fast path. |
| `compatibility_date` / `compatibility_flags` | a pinned date, `nodejs_compat` | No `experimental` flag: the deploy API rejects it (error 10021), and neither the `worker_loaders` binding nor the `worker-javascript` backend needs it. |
| `observability` | enabled, with traces | `wrangler tail` and dashboard traces. Note that only the error *name* is logged, never a response body. |

### Two execution backends, both network-denied

The workspace is built with an ordered backend list, and both entries disable
egress at construction:

| Backend | Runtime name | What it is | When it is chosen |
| --- | --- | --- | --- |
| `WorkerJavaScriptBackend` | `worker-javascript` | A Dynamic Worker executing the Program entrypoint's source, or the Agent's `exec` tool, against the same durable filesystem. No container cold start. | The default, and the only one a `worker-javascript` Program or a Computer with `runtime.default: worker-javascript` uses. |
| `CloudflareContainerBackend` | `container-shell` | A network-denied Linux container that FUSE-mounts the *same* workspace files. | Only when the executing definition's runtime is `container` — which requires the Computer to declare `fallback: container` with `fallback_requires: explicit_policy`. The choice is journaled. |

Egress is off twice over: at the backend (`egress: {mode: "none"}`) and at the
request boundary, which refuses any Computer declaring
`capabilities.network: true` outright.

## Configuring Memseek's side

The API and worker processes read four settings. They carry **no** `MEMSEEK_`
prefix — a prefixed name binds nothing, and the adapter fails with *"Cloudflare
Computer runtime URL/token is not configured"*:

| Setting | Default | Meaning |
| --- | --- | --- |
| `COMPUTER_RUNTIME_URL` | *empty* | Base URL of the deployed Worker. The adapter posts to `<url>/v1/execute`. |
| `COMPUTER_RUNTIME_TOKEN` | *empty* | Shared secret. Must equal the Worker's `MEMSEEK_RUNTIME_SECRET`. |
| `COMPUTER_REQUEST_TIMEOUT_S` | `300` | Per-request HTTP timeout. A slow Agent loop that exceeds it fails as `transport`, and a durable invocation is requeued rather than failed. |
| `COMPUTER_RESPONSE_MAX_BYTES` | `16777216` | Cap on the response body. A larger body fails as `budget` before it is parsed. |

Only `provider:` changes in the catalog. Nothing else about a Computer,
Program, Agent, or context policy is provider-specific.

## The signed request

### Routes

| Route | Auth | Notes |
| --- | --- | --- |
| `GET /health` | none | `{"ok":true,"provider":"cloudflare"}`. Side-effect free; use it to confirm a deployment before pointing Memseek at it. |
| `POST /v1/execute` | HMAC | The only functional route. |
| anything else | — | `404`. A non-`POST` to `/v1/execute` is `405`. |

### Headers and signature

| Header | Value |
| --- | --- |
| `Content-Type` | `application/json` |
| `X-Memseek-Timestamp` | Unix seconds, integer |
| `X-Memseek-Signature` | `HMAC-SHA256(secret, "<timestamp>." + body)`, 64 lowercase hex characters |

The signature covers the **exact bytes sent**, and the body is canonical JSON:
sorted keys, no spaces, no `NaN`/`Infinity`, Unicode left unescaped. That is
what the adapter does, and it is reproducible:

```python
import hashlib, hmac, json, time

body = json.dumps(
    payload,
    ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
).encode("utf-8")
timestamp = str(int(time.time()))
signature = hmac.new(
    token.encode(), timestamp.encode() + b"." + body, hashlib.sha256
).hexdigest()
```

Two consequences worth remembering:

- **Any proxy that re-serializes the JSON body breaks authentication.** Adding
  a space, reordering a key, or escaping non-ASCII invalidates the signature.
- **Clock skew is ±300 s.** Outside that window the request is rejected before
  the comparison is even reached, so keep both sides on NTP.

### Status codes

| Status | Body | Cause |
| --- | --- | --- |
| `200` | `ExecuteResponse` | Executed, or replayed from the session's result cache. |
| `401` | `{"error":"unauthorized"}` | Missing, malformed, stale, or invalid signature. |
| `404` | `not found` | Any path other than `/health` and `/v1/execute`. |
| `405` | `method not allowed` | Non-`POST` to `/v1/execute`. |
| `413` | `{"error":"request_too_large"}` | Body over 12 MiB, checked on both `content-length` and the actual bytes. |
| `422` | `{"error":"<message>"}` | Every validation, policy, or execution failure. |
| `503` | `{"error":"runtime_secret_missing"}` | `MEMSEEK_RUNTIME_SECRET` is unset on the Worker. |

`422` is the catch-all: a traversal path, a tampered immutable file, a Program
exit code, an agent envelope that does not parse, an outbox violation. Memseek
surfaces the runtime's own `error` string in the failure it raises, so you
rarely need `wrangler tail` — and the log holds less than the response does.

### What is in the request

| Field | Meaning |
| --- | --- |
| `mode` | `derivation` or `invocation`. Decides whether writeback paths under `/outbox` are collected at all. |
| `workspace` | The Memseek workspace name. Used for identity, never for access — the Worker cannot reach it. |
| `entity` | The entity this unit of work is about. |
| `session_key` | `sha256(workspace \0 run_key \0 computer_ref)`, 64 lowercase hex. The Durable Object name, and therefore the filesystem identity. |
| `parent_session_key` | Present only for a fork. Same shape. |
| `task_id` | Names this one unit of work. The idempotency-cache key. |
| `computer_ref` / `computer` | The exact reference and the fully resolved Computer definition: `writable`, `runtime`, `capabilities`, `writeback`, `retention`. |
| `executor` | `{kind: "program", ref, definition}` or `{kind: "agent", ref, definition, context_policy_ref, context_policy, model}`. The definitions are the resolved catalog objects, so the Worker never looks anything up. |
| `executor.model` | For agents: `{alias, targets, params, context_pressure}`. The Worker reads the first target and the params; the pressure figures are carried for the receipt. |
| `input` | The typed input value: a derivation task's input, a `compute` invocation's `input`, or for a durable Agent `{kind, prompt, turns}`. |
| `source_ids` | Every record ID that contributed to this job. |
| `citation_ids` | The subset the executor may cite. Citing anything else fails the run — twice, once here and once in Memseek. |
| `output_path` | Where the value must be written. Must resolve under `/outbox`. |
| `context_files` | Absolute path → already-rendered text. Keys must resolve under `/.memseek`. The Worker never renders an artifact; it receives the render. |

### What comes back

`ExecuteResponse` is `value`, `citation_ids`, `steps`, `awaiting_input`, and a
`receipt`:

| Receipt field | Meaning |
| --- | --- |
| `provider` | `cloudflare`. |
| `backend` | The runtime this execution ran under: a Program's own `runtime`, or the Computer's `runtime.default` for an Agent. |
| `session_key`, `task_id`, `executor_ref` | The three identities this execution ran under. |
| `output_path`, `output_sha256`, `bytes` | The declared result file, its hash, and its size. |
| `commands` | One entry per command, naming the backend that actually executed it (`worker-javascript` or `container-shell`), plus `source_sha256`, exit code, duration, and stdout/stderr **hashes** — never their contents. |
| `files` | The full before/after hash diff of the workspace: `{path, before_sha256, after_sha256}`. |
| `events` | The model and tool journal: `model_request`, `model_step`, `tool_call`, `tool_result`. |
| `outbox` | For invocations, the declared writeback files with content, hash, and byte count. |
| `immutable_hashes` | The hash of every file that was materialized read-only. |
| `resumed` | `true` when the answer came from the cache instead of a fresh execution. |

Memseek appends its own `definition_refs` — each pinned reference with its
definition hash — plus the measured `context_pressure` for agents, then stores
the whole thing as the run's receipt.

### One job on the wire

Both tables above are easier to hold onto next to a real payload. This is an
actual `POST /v1/execute` body for an Agent — the committed renewal fixture,
with long strings elided at `…`. Nothing is looked up on the far side; every
definition arrives resolved:

```json
{
  "mode": "invocation",
  "workspace": "memseek-cloudflare-smoke",
  "entity": "smoke:demo",
  "session_key": "9f3c…64 hex…",
  "task_id": "cloudflare-smoke-write:demo",
  "computer_ref": "research_workspace@1",
  "computer": {
    "name": "research_workspace", "version": 1, "active": true,
    "provider": "cloudflare",
    "context": [
      {"path": "/.memseek/context.md", "artifact": "renewal_instructions@1", "mode": "read_only"}
    ],
    "writable": ["/workspace", "/outbox"],
    "runtime": {"default": "worker-javascript", "fallback": "container",
                "fallback_requires": "explicit_policy"},
    "capabilities": {"filesystem": true, "exec": true, "network": false},
    "writeback": [
      {"path": "/outbox/observations.jsonl", "type": "observations", "review": false,
       "collection": "task_observations@1", "record_type": "observation"},
      {"path": "/outbox/final-result.json", "type": "final_result", "review": false}
    ],
    "retention": {"workspace_days": 30, "preserve": ["/outbox", "…"]}
  },
  "executor": {
    "kind": "agent",
    "ref": "renewal_analyst@1",
    "definition": {
      "name": "renewal_analyst", "version": 1, "active": true,
      "model": "renewal_reasoner",
      "instructions": "renewal_instructions@1",
      "skills": ["renewal_research_skill@1"],
      "tools": ["computer", "recall"],
      "computers": ["research_workspace@1"],
      "context_policy": "evidence_spine@1",
      "limits": {"max_steps": 24, "max_wall_s": 300, "max_output_bytes": 1048576}
    },
    "context_policy_ref": "evidence_spine@1",
    "context_policy": {
      "name": "evidence_spine", "version": 1, "active": true,
      "max_input_tokens": 24000, "reserve_output_tokens": 2400,
      "thresholds": {"pointerize": 0.7, "compact": 0.82, "pause": 0.92},
      "max_recall_hits": 50, "max_exposed_bytes": 262144,
      "max_recall_pages": 5, "receipt_fanout": 8
    },
    "model": {
      "alias": "renewal_reasoner",
      "targets": ["workers_ai:@cf/meta/llama-3.3-70b-instruct-fp8-fast"],
      "params": {"temperature": 0, "max_output_tokens": 2400}
    }
  },
  "input": {"kind": "answer", "prompt": "…", "turns": []},
  "source_ids": ["3147a6a1-61ba-5056-9c68-e78fb0ca97f2"],
  "citation_ids": ["3147a6a1-61ba-5056-9c68-e78fb0ca97f2"],
  "output_path": "/outbox/final-result.json",
  "context_files": {
    "/.memseek/instructions.md": "…rendered instructions…",
    "/.memseek/context.md": "…rendered evidence…",
    "/.memseek/manifest.json": "{\"source_records\":[…]}"
  }
}
```

Three things to notice, because they are the whole security posture in one
payload:

- **There is no credential in it.** No connection string, no workspace key, no
  model API key — the model is reached through the Worker's own `AI` binding,
  and the only shared secret is the one that signs the request itself.
- **`citation_ids` is the entire universe of things this run may point at.** It
  is a *list*, computed before the job left, from the records that actually went
  into the rendered files. The Worker checks the returned envelope against it,
  and Memseek checks it again.
- **Definition hashes are not sent.** They are excluded from the serialized
  definitions on purpose: the runtime has no use for them, and identity is
  something Memseek asserts about a receipt afterwards, not something the
  sandbox reports about itself.

The answer to that job looks like this — abridged the same way:

```json
{
  "value": {"records": [{"text": "…", "citations": ["3147a6a1-…"], "content": {"…": "…"}}]},
  "citation_ids": ["3147a6a1-61ba-5056-9c68-e78fb0ca97f2"],
  "steps": 3,
  "awaiting_input": false,
  "receipt": {
    "provider": "cloudflare",
    "backend": "worker-javascript",
    "session_key": "9f3c…", "task_id": "cloudflare-smoke-write:demo",
    "executor_ref": "renewal_analyst@1",
    "output_path": "/outbox/final-result.json",
    "output_sha256": "1c9d…", "bytes": 412,
    "commands": [],
    "files": [
      {"path": "/workspace/proof.json", "before_sha256": null, "after_sha256": "8ab1…"}
    ],
    "events": [
      {"kind": "model_request", "payload": {"target": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                                            "params": {"temperature": 0}, "input_sha256": "d41d…"}},
      {"kind": "model_step",   "payload": {"index": 0, "finish_reason": "tool-calls", "usage": {"…": 0}}},
      {"kind": "tool_call",    "payload": {"index": 0, "tool_name": "write", "input": {"path": "…"}}},
      {"kind": "tool_result",  "payload": {"index": 0, "tool_name": "write", "output": {"ok": true}}}
    ],
    "outbox": [],
    "immutable_hashes": {"/.memseek/instructions.md": "5f2a…"},
    "resumed": false
  }
}
```

`steps` is what the loop actually took, `files` is the before/after hash diff of
the whole workspace, and `events` is the model-and-tool journal that becomes
the invocation's own timeline. Memseek then adds `definition_refs` — each pinned
reference with its hash — and, for an Agent, the `context_pressure` it measured
before the job left, and stores the result as the run's receipt.

## The execution sequence

This is `execute()`, in order. Every step fails closed, and because step 4
re-materializes `/.memseek` and `/inputs` on every execution, a run that failed
halfway cannot leave doctored context behind for the next one.

```mermaid
sequenceDiagram
  participant MS as Memseek
  participant W as Worker
  participant DO as Session workspace
  participant EX as Program / Agent
  MS->>W: POST /v1/execute (signed)
  W->>W: auth, size cap, validate request
  W->>DO: idFromName(session_key)
  W->>DO: fork from parent, once (preserve paths only)
  W->>DO: read /.memseek/results/sha256(task_id).json
  alt this task_id already finished
    DO-->>MS: cached value + receipt, resumed = true
  else fresh execution
    W->>DO: materialize context, input, program bundle, manifest
    W->>DO: snapshot hashes (before)
    W->>EX: run
    EX-->>W: value, steps, citations, events
    W->>DO: restore immutables, report tampering
    W->>DO: snapshot (after), diff against writable roots
    W->>DO: write value to output_path, collect /outbox
    W->>DO: cache the whole response
    W-->>MS: value + citation_ids + receipt
  end
```

1. **Route to the session.** `COMPUTER.idFromName(session_key)`, then attach to
   its workspace. Same key, same files.
2. **Fork, exactly once.** If `parent_session_key` is set and
   `/.memseek/forked-from` does not exist, copy *only* the paths named by
   `retention.preserve` from the parent workspace, then write the marker. A
   marker holding a different parent is `fork parent mismatch`. Copying refuses
   symlinks and stops at 2 000 files or 50 MiB.
3. **Probe the idempotency cache.** Read
   `/.memseek/results/<sha256(task_id)>.json`. On a hit, return it with
   `receipt.resumed = true` — no re-execution, no second model call. This is
   what makes a Memseek retry safe rather than expensive.
4. **Materialize.** Write every `context_files` entry (each must resolve under
   `/.memseek`), the input at `/inputs/<sha256(task_id)>/input.json`, the
   Program bundle under `/.memseek/program/` (each relative path normalized),
   and `/.memseek/runtime-manifest.json` — the Worker's own record of the
   refs, permissions, sources, and citations for this run. Create `/workspace`
   and `/outbox`. Everything written here is remembered as immutable.
5. **Snapshot.** Hash every file under `/`, up to 2 000 files.
6. **Execute.** `runProgram`, or the `MemSeekAgent` addressed as
   `<session_key>:<executor ref>`.
7. **Restore immutables.** Re-read each remembered file. Any difference is
   restored *and* reported, and the run fails with
   `immutable files changed: <paths>`. The tamper attempt neither survives nor
   goes unnoticed.
8. **Snapshot and diff.** Any changed path that does not resolve under a
   declared writable root fails with `files changed outside writable roots`.
9. **Cap the output.** An Agent's `limits.max_output_bytes`; a flat 10 MiB for
   Programs.
10. **Write the value** to `output_path`.
11. **Collect the outbox** — see below.
12. **Snapshot again**, build the receipt, and cache the whole response at the
    path from step 3.

### Filesystem topology

```text
/.memseek/                        immutable — rejected as a write target
  instructions.md                 the Agent's versioned instructions artifact
  skills/NN.md                    the Agent's skill artifacts, in order
  manifest.json                   Memseek's provenance manifest of those renders
  runtime-manifest.json           the Worker's manifest: refs, permissions,
                                  sources, citations
  program/…                       the Program bundle, when a Program is running
  results/<sha256(task_id)>.json  the idempotency cache
  forked-from                     the parent session key, in a forked session
/inputs/<sha256(task_id)>/input.json   immutable typed input
/workspace/                       writable working files
  recalled/<sha256(query)>.json   materialized recall receipts
/outbox/                          writable structured return channel
```

## Programs on this runtime

`runProgram` branches on the Program's declared runtime.

**`worker-javascript`** — the entrypoint's *source string* is handed to a
Dynamic Worker with `request.input` as the invocation input, and the returned
`value` is the result. The backend confines code paths to its own root, so no
working directory is set; `/.memseek` sits outside every writable root by
design.

```javascript
export default async function (input) {
  return {records: []};   // becomes the Task value
}
```

**`container`** — `program.command` is shell-quoted into one command line and
run with `cwd: /.memseek/program`. The value is read back by parsing
`output_path` as JSON, so a container Program must write its own result file.
Its input is the immutable file at `/inputs/<sha256(task_id)>/input.json`.

Either way, a non-zero exit code or a status other than `completed` fails the
execution, and the receipt's `commands` entry records the backend, source hash,
exit code, duration, and stdout/stderr hashes.

!!! warning "Multi-file bundles do not link"
    Only the entrypoint's source reaches the Dynamic Worker. Sibling files in
    `files:` are materialized under `/.memseek/program/` and can be *read*, but
    `import "./lib/parser.js"` will not resolve. Keep `worker-javascript`
    Programs single-file, or read siblings from the filesystem explicitly.

## Agents on this runtime

`MemSeekAgent.execute` runs inside `keepAliveWhile`, attaches to the same
session workspace, and does five things.

**Resolves the model.** The first entry of `executor.model.targets` must begin
`workers-ai:` or `workers_ai:`, and the remainder must begin `@cf/`. Anything
else fails with *"Cloudflare runtime accepts only workers-ai model targets"*.
The call goes through the `AI` binding. Agent runs are therefore real, billed
Workers AI calls.

**Builds the system prompt itself.** Your versioned instructions and skills
supply the *content*; the frame is fixed by the Worker and cannot be replaced
from YAML:

```text
You are the exact versioned MemSeek Agent named in the manifest.
Use the Computer filesystem as working memory. Read immutable context before acting.
Use recall to recover buried authorized evidence; open its materialized receipt before citing it.
You may write only below the writable roots named in the manifest.
Network access is denied unless the manifest explicitly enables it.
End with one JSON object: {"value": <object>, "citation_ids": [<visible UUIDs>], "awaiting_input": <boolean> }.
Set awaiting_input true only when work cannot continue without one concrete user answer.
Never cite an ID absent from the manifest.

## /.memseek/instructions.md
…
## /.memseek/skills/01.md
…
```

Every `context_files` entry except `/.memseek/manifest.json` is appended as a
`## <path>` section, sorted by path. The manifest is excluded as machine
metadata. The user message is the JSON encoding of `input`.

**Grants tools.** The filesystem set comes from `createAITools`, over the same
durable workspace; the loop's authority is exactly this list.

| Tool | Bounds | Granted when |
| --- | --- | --- |
| `read` | 32 KiB and 800 lines per call | Always. |
| `ls`, `find`, `grep` | Whole-workspace traversal, read-only | Always. |
| `write`, `edit`, `delete` | The write itself is not path-checked at call time — it is caught by the post-run hash diff, which fails the whole run for anything outside `writable:` | Always. |
| `exec` | Runs on the backend implied by the runtime: a Dynamic Worker, or the container shell | Only when the Computer sets `capabilities.exec: true`. |
| `recall` | Context-policy bounds, below | Only when the Agent lists `recall` in `tools`. |

There is no network tool, no database tool, and no record writer in that set,
on any Computer. A model that wants to write a record has exactly one route:
put it in the value, or in a declared writeback file, and let Memseek decide.

**Filters model parameters** through a narrow allowlist. An out-of-range value
throws `invalid model parameter: <name>`; an unrecognized key is silently
dropped. Catalog data cannot reach through this to change the model, the tools,
the system prompt, or the step terminator.

| Parameter | Accepted range |
| --- | --- |
| `temperature` | 0–2 |
| `top_p` | 0–1 |
| `frequency_penalty`, `presence_penalty` | −2–2 |
| `seed` | a safe integer |
| `max_output_tokens` | a positive integer |
| `stop` | at most 8 strings, each at most 512 characters |

**Requires the envelope.** The loop is bounded by
`stopWhen: stepCountIs(max_steps)`, and the final message — after an optional
` ```json ` fence is stripped — must parse as
`{"value": …, "citation_ids": [...], "awaiting_input": bool?}`. The failures
are named separately, because they mean different things:

| Message | What happened |
| --- | --- |
| `agent returned no final text; expected the MemSeek envelope (finish reason: …, steps: n/m)` | The model spent its last step on a tool call and never wrote a reply. Usually the step limit is too low for the work. |
| `agent final output was not valid JSON` | It replied in prose. |
| `agent final output does not match the MemSeek envelope` | Valid JSON, but no `value` key or no `citation_ids` array. |
| `agent widened citation authority` | It cited an ID absent from `citation_ids`. |
| `agent awaiting_input flag must be boolean` | `awaiting_input` was a string or a number. |

Say the envelope requirement explicitly in the instructions artifact. It is the
most common first-run failure.

**Journals everything.** `model_request` (target, filtered params, input hash),
then per step `model_step` (finish reason, usage), `tool_call`, and
`tool_result`. Any audit payload over 32 KiB is replaced by
`{pointer: true, bytes, sha256}` so receipts stay bounded. In a durable
invocation these become first-class events on the invocation journal, replayable
over `GET /invocations/{id}/events`.

### The `recall` tool

`recall` greps only `/.memseek` and `/workspace` — content this run is already
authorized to see — so a long run can recover a detail that fell out of live
context without widening what it is allowed to know. Bounds come from the
context policy, clamped by the runtime:

| Bound | Policy field | Clamp | Fallback when out of range |
| --- | --- | --- | --- |
| Hits per call | `max_recall_hits` | 1–50 | 20 |
| Bytes exposed per call | `max_exposed_bytes` | 1 KiB – 256 KiB | 64 KiB |

Whole matches are dropped from the tail until the payload fits, and the result
sets `truncated`. Every result is also written to
`/workspace/recalled/<sha256(query)>.json` and returned as `materialized_path`,
so the agent can re-open the exact bytes it was shown — and so can you, from
the run's preserved files.

## Three identities, three jobs

| Identity | Formula | What it decides |
| --- | --- | --- |
| `session_key` | `sha256(workspace \0 run_key \0 computer_ref)` | The workspace. Tasks in one derivation run that name the same Computer share a filesystem; different Computer references are fully isolated. For a durable invocation, `run_key` is the session row's UUID. |
| `task_id` | The derivation task id, or the invocation UUID | One unit of work, and its cache entry. A retry after a transport failure returns the original value with `resumed: true`. Reusing a `task_id` for genuinely different work would return the stale result — which is exactly why Memseek derives it from run identity rather than letting a caller choose it. |
| `parent_session_key` | The parent session's key | A fork. Only `retention.preserve` paths cross over, so a child inherits checkpoints, not scratch state. The `/.memseek/forked-from` marker makes the copy happen once. |

## The outbox, by mode

`/outbox` is a declared return channel, not a directory.

**`derivation`** allows exactly one path: `output_path`. Any other file under
`/outbox` fails with `unknown outbox files: …`. Nothing is auto-ingested — the
declared result becomes a Task value and still has to pass `emit` and the
normal [Candidate Set](evaluation-bases.md) checks.

**`invocation`** additionally allows every non-`final_result` entry in the
Computer's `writeback`, and returns their contents with hashes:

- `observations` must be a single JSONL file, not a directory;
- `maintained_state` files must end in `.json`;
- every entry must be a regular file — a symlink anywhere in the tree is
  refused;
- at most 100 files and 1 MiB in total, and at most 2 000 entries while
  walking.

Memseek then re-verifies each reported hash and byte count, matches every path
to exactly one declaration, requires non-empty citations within what the run was
shown, and sets the entity, collection, record type, status, and dedupe key
itself.

## Every limit in one place

| Limit | Value | Enforced by |
| --- | --- | --- |
| Request body | 12 MiB | Worker |
| Response body | `COMPUTER_RESPONSE_MAX_BYTES`, default 16 MiB | Memseek adapter |
| Request timeout | `COMPUTER_REQUEST_TIMEOUT_S`, default 300 s | Memseek adapter |
| Signature skew | ±300 s | Worker |
| Program output | 10 MiB | Worker |
| Agent output | `limits.max_output_bytes` | Worker, then Memseek |
| Agent steps | `stopWhen: stepCountIs(…)` over the **Agent definition's** `limits.max_steps` | Worker terminates the loop there; Memseek then rejects a step count above the lower limit a derivation or task declared |
| `read` tool | 32 KiB, 800 lines per call | Worker |
| `recall` | 1–50 hits, 1 KiB–256 KiB | Worker, from the context policy |
| Audit payload inlined | 32 KiB, then pointerized | Worker |
| Filesystem snapshot | 2 000 files | Worker |
| Fork copy | 2 000 files, 50 MiB, no symlinks | Worker |
| Outbox | 100 files, 1 MiB total, 2 000 entries walked | Worker, then Memseek |
| Journal entries per run | 512 — commands, file diffs, and model/tool events together — each ≤ 64 KiB canonical | Memseek |
| Receipt (without events and outbox) | 1 MiB | Memseek |
| Writeback file content | 1 MiB each | Memseek |

## Deploy, verify, operate

**Before you start**, have four things: a Cloudflare account with Workers AI
available, Node 20 or newer, Docker running (the deploy builds the container
image, even if you never use that backend), and `wrangler` authenticated —
`npx wrangler login`, which opens a browser once.

The whole first-time path is a handful of commands, and each one has a visible
result, so you always know where you got to:

```sh
cd cloudflare/computer-runtime
npm install
npm run check                                   # tsc --noEmit && vitest run

SECRET=$(openssl rand -hex 32)                  # keep this; you need it twice
echo -n "$SECRET" | npx wrangler secret put MEMSEEK_RUNTIME_SECRET
npm run deploy                                  # prints the worker's URL

curl -s https://<worker-host>/health            # {"ok":true,"provider":"cloudflare"}
```

| Step | What success looks like | What it means if it fails |
| --- | --- | --- |
| `npm run check` | No output. | A local toolchain problem, not a Cloudflare one. Nothing has been deployed yet. |
| `wrangler secret put` | `✨ Success! Uploaded secret`. | You are not logged in, or the Worker does not exist yet — deploy first, then set the secret, then deploy again. |
| `npm run deploy` | A `https://<name>.<subdomain>.workers.dev` URL. | Usually Docker is not running, since the config declares a container. Containers themselves need a paid plan. |
| `curl /health` | `{"ok":true,"provider":"cloudflare"}` | `error code: 1101` means the Worker threw on startup — run `wrangler tail` in another terminal and request `/health` again to see why. |

Then point Memseek at it. These two go in the environment of **both** the API
and the worker process — the worker is the one that actually runs invocations,
and a URL set on only one of them fails in whichever half you forgot:

```sh
COMPUTER_RUNTIME_URL=https://<worker-host>
COMPUTER_RUNTIME_TOKEN=<the same secret>
```

Finally, flip the Computer's `provider` from `fake` to `cloudflare` and
republish the catalog. For derivations, also raise `limits.max_computer_runs` —
it defaults to `0`, so Computer-backed work is opt-in per pipeline. Nothing
else in the YAML changes, and you can point the same catalog back at `fake` by
republishing with the provider flipped again.

Operationally there is very little to run: the Worker has no queue, no cron,
and no state to migrate. `wrangler tail` streams live logs, and the dashboard
shows requests and Workers AI usage. Note that the logs deliberately hold
*less* than the responses do — only an error's name, never a body — because a
body may quote evidence.

### Locally

```sh
npm run dev            # wrangler dev
```

Durable Objects and the Worker Loader run locally. Two caveats: the container
backend needs Docker to build the image, and Workers AI calls leave your
machine and are billed even in local mode. Exercise the `worker-javascript`
path first — it needs neither.

### How it is tested

The deterministic contract is proven in Python against `provider: fake`, which
enforces the identical contract in-process — same schemas, same limits, same
citation rules, same receipts — with a registered stand-in standing where the
Program or Agent would run:

```sh
docker compose up -d --wait postgres-test
LLM_FAKE=1 DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:55432/memseek_test" \
  uv run pytest tests/test_computer_resources.py tests/test_computer_renewal_catalog.py -q
```

The Worker's own vitest suite covers the path and quoting policy in
`security.ts`. It is a unit suite over those helpers — no Durable Object, no
backend, no Workers AI.

The live boundary is proven by an operator-run canary, deliberately outside CI
because it calls a billable model:

```sh
make cloudflare-agent-smoke-setup     # prompts for URL and secret, writes .env
make cloudflare-agent-smoke           # against the deployed Worker
make cloudflare-agent-smoke-local     # the same canary against `wrangler dev`
```

The canary resolves the committed renewal Agent, switches its Computer to
`cloudflare` in memory, and makes two signed requests against one session: the
first Agent must `write` a marker file, and the second — which is never told the
marker — must `read` it back. It fails unless both receipts report
`worker-javascript`, the tool calls and results are journaled, the first receipt
carries the file diff, the second Agent returns the hidden marker, and both
outputs preserve the exact visible citation UUID. Its JSON summary names the
session, model, step counts, and output hashes, and never the secret. It needs
neither PostgreSQL nor a published workspace.

The setup helper never echoes the secret: it upserts the settings into the
untracked `.env` and writes a mode-600 `.env.sh` of shell exports. The local
variant needs no deploy — `wrangler dev` serves the Worker with the secret from
`.dev.vars` — but the `AI` binding is always remote, so its model turns are
still real and still billed.

## Failures worth recognizing

| Symptom | Cause |
| --- | --- |
| `Cloudflare Computer runtime URL/token is not configured` | `COMPUTER_RUNTIME_URL`/`COMPUTER_RUNTIME_TOKEN` unset — check for a stray `MEMSEEK_` prefix. |
| `401 unauthorized` | Secret mismatch, clock skew over 300 s, or a proxy that re-serialized the JSON body. |
| `503 runtime_secret_missing` | Deployed without `wrangler secret put MEMSEEK_RUNTIME_SECRET`. |
| `health endpoint returned HTTP 500: error code: 1101` | The Worker threw during startup. Authenticate `wrangler` and watch `wrangler tail` while requesting `/health`. |
| `network-enabled Computers require a separately reviewed egress gateway` | The definition sets `capabilities.network: true`. |
| `invalid writable root` | A `writable:` root outside `/workspace` and `/outbox`. The catalog permits others; this runtime does not. |
| `Cloudflare runtime accepts only workers-ai model targets` | The Agent's model alias does not resolve to `workers_ai:@cf/…`. |
| `invalid model parameter: <name>` | A model-alias param outside the allowlist range. |
| `agent final output does not match the MemSeek envelope` | Prose or a bare object instead of `{value, citation_ids}`. |
| `agent widened citation authority` | A citation absent from the manifest. |
| `immutable files changed: …` | Something wrote under `/.memseek` or `/inputs`. |
| `files changed outside writable roots: …` | A write landed outside `writable:`. |
| `unknown outbox files: …` | An undeclared path under `/outbox`, or a derivation writing more than its one declared result. |
| `external Program bundles require an installed artifact resolver` | The Program uses `bundle:` instead of inline `files:`. |
| `receipt.resumed: true` unexpectedly | A reused `task_id` hit the session's result cache. |
| `budget: Computer runtime response exceeds byte limit` | Response over `COMPUTER_RESPONSE_MAX_BYTES`. |
| `fork parent mismatch` | A retry named a different parent than the one already recorded in `/.memseek/forked-from`. |

## Deliberately not built

These are refused explicitly rather than half-supported:

- **Reviewed egress.** Any `network: true` Computer is rejected at the
  boundary, and both backends run with egress disabled.
- **External Program bundles.** `bundle: {uri, sha256}` needs a
  content-addressed resolver that verifies the hash *before* execution. Inline
  `files:` only, capped at 1 MiB by the catalog.
- **Multi-file `worker-javascript` linking.** Only the entrypoint's source is
  loaded.
- **Workspace expiry.** `retention.workspace_days` is validated, sent, and
  recorded, and there is an `expires_at` column waiting for it, but no reaper
  deletes an aged session workspace yet. It is a declared policy, not a
  lifecycle.
- **Blob offload for large payloads.** Results come back inline and bounded.
- **Automated live-deployment tests.** The canary above covers
  `provider: cloudflare` end to end, by hand.

The underlying `@cloudflare/computer` package is a preview. Treat this Worker
as a working implementation of a settled protocol on an unsettled runtime: the
Memseek-side contract is fixed by tests, the Cloudflare-side surface is not.

## Next

- The YAML: [Computers, Programs & Agents](computers.md)
- The guarantees: [How a Computer stays trustworthy](computer-resources-and-durable-agents.md)
- A full run, step by step: [Run an agent inside a Computer](computer-renewal-example.md)
