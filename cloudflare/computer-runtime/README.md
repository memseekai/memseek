# MemSeek Cloudflare Computer runtime

This Worker is the `cloudflare` implementation of MemSeek's provider-neutral
Computer protocol. MemSeek decides *what* may run, *on which evidence*, and
*what may be written back*; this Worker is the untrusted side that actually
runs it. It holds no database connection, no workspace API key, and no
canonical-record writer — it receives a signed, self-contained job and returns
a value plus a hash-bearing receipt.

The normative contract lives in
[docs/computer-resources-and-durable-agents.md](../../docs/computer-resources-and-durable-agents.md).
This README documents the implementation: the wire protocol, the execution
sequence, every limit and rejection, and what is deliberately not built yet.

---

## 1. What it is made of

| Piece | Where | Role |
| --- | --- | --- |
| HTTP boundary | [`src/index.ts`](src/index.ts) `default.fetch` | HMAC auth, size cap, request validation, error mapping |
| `MemSeekComputer` | [`src/index.ts`](src/index.ts) | One SQLite-backed Durable Object per session; owns the `@cloudflare/computer` workspace and its two execution backends |
| `MemSeekAgent` | [`src/index.ts`](src/index.ts) | Cloudflare Agent running the model/tool loop against Workers AI |
| Protocol types | [`src/protocol.ts`](src/protocol.ts) | `ExecuteRequest` / `ExecuteResponse`, mirroring MemSeek's `ComputerRequest` / `ComputerResult` |
| Path & crypto policy | [`src/security.ts`](src/security.ts) | `safeAbsolutePath`, `safeRelativePath`, `shellQuote`, `verifySignedBody`, `sha256` |
| Container image | [`Dockerfile`](Dockerfile) | Debian slim + `computerd` 0.2.1, FUSE-mounted at `/workspace`, port 8080 |

Bindings declared in [`wrangler.jsonc`](wrangler.jsonc):

- `COMPUTER` — `MemSeekComputer` Durable Object namespace (also the container class)
- `AGENT` — `MemSeekAgent` Durable Object namespace
- `LOADER` — Worker Loader, for the Dynamic Worker fast path
- `AI` — Workers AI, for Agent model calls
- `MEMSEEK_RUNTIME_SECRET` — the shared HMAC secret (a Worker secret, not a var)

Both DO classes are registered as `new_sqlite_classes` under migration tag
`v1`. The container runs `instance_type: standard-2`, `max_instances: 10`.
Compatibility date is `2026-08-27` with `nodejs_compat` only. The
`experimental` flag cannot be deployed to Cloudflare — the API rejects it with
error 10021 — and it turns out not to be needed: the `worker_loaders` binding
and the worker-javascript backend both work without it.

### Two execution backends, both network-denied

The workspace is constructed with an ordered backend list:

```ts
new WorkerJavaScriptBackend({ loader: env.LOADER, egress: { mode: "none" } })
self.containerBackend   // CloudflareContainerBackend, egress: { mode: "none" }
```

- **`worker-javascript`** — the fast path. A Dynamic Worker executes the
  Program's entrypoint source, or the Agent's shell tool, against the same
  durable filesystem. No container cold start.
- **`container-shell`** — the fallback. A network-denied Linux container
  FUSE-mounts the *same* workspace files. Selected only when the definition's
  runtime is `container`; MemSeek requires `fallback_requires: explicit_policy`
  for a Computer to allow it at all, and the choice is journaled.

Both set `egress: { mode: "none" }`. Egress is off at the backend, and
separately refused at the boundary for any Computer declaring
`capabilities.network: true`.

---

## 2. HTTP surface

### `GET /health`

```json
{ "ok": true, "provider": "cloudflare" }
```

Unauthenticated and side-effect free. Use it to confirm a deployment before
pointing MemSeek at it.

### `POST /v1/execute`

The only functional route. Everything else returns `404`; a non-`POST` to this
path returns `405`.

**Headers**

| Header | Value |
| --- | --- |
| `Content-Type` | `application/json` |
| `X-Memseek-Timestamp` | Unix seconds, integer |
| `X-Memseek-Signature` | 64 lowercase hex chars |

**Signature.** `HMAC-SHA256(secret, "<timestamp>." + body)`, hex-encoded, over
the exact bytes sent. The body is canonical JSON — `sort_keys=True`,
`separators=(",", ":")`, `ensure_ascii=False`, `allow_nan=False` — produced by
`_canonical_bytes` in [`src/memseek/computers.py`](../../src/memseek/computers.py).
Re-serializing the body invalidates the signature.

**Clock skew** is ±300 s (`verifySignedBody`). A signature outside that window
is rejected without a constant-time comparison being reached, so keep both
sides on NTP.

**Status codes**

| Status | Body | Cause |
| --- | --- | --- |
| `200` | `ExecuteResponse` | Executed, or replayed from the result cache |
| `401` | `{"error":"unauthorized"}` | Missing/malformed/stale/invalid signature |
| `404` | `not found` | Any path other than `/health` and `/v1/execute` |
| `405` | `method not allowed` | Non-`POST` to `/v1/execute` |
| `413` | `{"error":"request_too_large"}` | Body over 12 MiB (checked on both `content-length` and actual bytes) |
| `422` | `{"error":"<message>"}` | Every validation, policy, or execution failure |
| `503` | `{"error":"runtime_secret_missing"}` | `MEMSEEK_RUNTIME_SECRET` is unset |

`422` is the catch-all: a traversal path, a tampered immutable file, a Program
exit code, an agent envelope that does not parse, an outbox violation. The
message is the thrown `Error.message`; only the error *name* is logged, so
`wrangler tail` shows less than the response body does.

### Request and response

`ExecuteRequest` (see [`src/protocol.ts`](src/protocol.ts)) carries the whole
job: `mode` (`derivation` | `invocation`), `workspace`, `entity`,
`session_key`, optional `parent_session_key`, `task_id`, the resolved
`computer` definition, an `executor` (`program` or `agent`, with its exact
definition — and for agents the context policy and resolved model alias),
`input`, `source_ids`, `citation_ids`, `output_path`, and `context_files`
(absolute path → already-rendered content).

`session_key` is `sha256(workspace \0 run_key \0 computer_ref)` and must match
`/^[0-9a-f]{64}$/`. It is the Durable Object name, which is what makes "same
run, same Computer" mean "same files".

`ExecuteResponse` returns `value`, `citation_ids`, `steps`, `awaiting_input`,
and a `receipt` containing `provider`, `backend`, `session_key`, `task_id`,
`executor_ref`, `output_path`, `output_sha256`, `bytes`, `commands`, `files`
(the full before/after hash diff), `events`, `outbox`, `immutable_hashes`, and
`resumed`.

---

## 3. Filesystem topology

Every execution sees the same layout inside the session's durable workspace:

```text
/.memseek/                        immutable — rejected as a write target
  instructions.md                 the Agent's versioned instructions artifact
  skills/NN.md                    the Agent's skill artifacts, in order
  manifest.json                   MemSeek's provenance manifest of those renders
  runtime-manifest.json           this Worker's manifest: refs, permissions, sources, citations
  program/…                       the Program bundle, when the executor is a Program
  results/<sha256(task_id)>.json  the idempotency cache
  forked-from                     the parent session key, when this session is a fork
/inputs/<sha256(task_id)>/input.json    immutable typed Task input
/workspace/                       writable working files
  recalled/<sha256(query)>.json   materialized recall receipts
/outbox/                          writable structured return channel
```

`/.memseek` and `/inputs` are materialized fresh on every execution and their
exact contents are remembered in an `immutable` map. After the executor
finishes, `restoreAndFindTampering` re-reads each one; any difference is
restored *and* reported, and the execution fails with
`immutable files changed: <paths>`. The tamper attempt does not survive, and it
does not go unnoticed.

---

## 4. Execution sequence

`execute()` in [`src/index.ts`](src/index.ts):

1. **Route to the session.** `env.COMPUTER.idFromName(request.session_key)`,
   then `getWorkspace(stub)`.
2. **Fork, once.** If `parent_session_key` is set and `/.memseek/forked-from`
   does not exist, copy *only* the paths named by `computer.retention.preserve`
   from the parent workspace, then write the marker. If the marker exists with
   a different value → `fork parent mismatch`. Copying refuses symlinks and
   caps at 2 000 files / 50 MiB.
3. **Idempotency probe.** Read `/.memseek/results/<sha256(task_id)>.json`. On a
   hit, return it with `receipt.resumed = true` — no re-execution, no second
   model call. This is what makes a MemSeek retry safe.
4. **Materialize.** Write `context_files` (each must be under `/.memseek`),
   the input, the Program bundle (each relative path must be normalized), and
   `runtime-manifest.json`; `mkdir /workspace` and `/outbox`.
5. **Snapshot.** Hash every file under `/` (cap 2 000 files).
6. **Execute** — `runProgram`, or `MemSeekAgent.execute` addressed by
   `${session_key}:${executor.ref}`.
7. **Restore immutables** and fail on tampering.
8. **Snapshot and diff.** Any changed path failing
   `safeAbsolutePath(path, computer.writable)` →
   `files changed outside writable roots`.
9. **Cap the output.** Agents use `executor.definition.limits.max_output_bytes`;
   Programs use a flat 10 MiB.
10. **Write** the encoded value to `output_path`.
11. **Collect the outbox** (§7).
12. **Snapshot again**, build the receipt, and cache the whole response at the
    path from step 3.

### Programs

`runProgram` branches on the declared runtime:

- **`worker-javascript`** — `source = program.files[program.entrypoint]`;
  `request.input` is handed to `workspace.runtime.exec` as the invocation
  input; the return `value` is the result.
- **`container`** — `program.command` argv is `shellQuote`d into one command
  line, `cwd: /.memseek/program`; the value is read back by parsing
  `output_path` as JSON.

Either way, a non-zero exit code or a `status !== "completed"` fails the
execution, and `commands[]` records the backend, `source_sha256`, exit code,
duration, and stdout/stderr hashes — never their contents.

> **Multi-file bundles do not link.** Only the entrypoint's *source string* is
> passed to the Dynamic Worker. Sibling files are materialized under
> `/.memseek/program/` and are readable from the filesystem, but an
> `import "./lib/parser.js"` will not resolve. Keep `worker-javascript`
> Programs single-file, or read siblings explicitly.

### Agents

`MemSeekAgent.execute` runs inside `keepAliveWhile`, attaches to the *same*
session workspace, and:

- **Resolves the model.** The first entry of `executor.model.targets` must
  start with `workers-ai:` or `workers_ai:`, and the remainder must start with
  `@cf/`. Anything else →
  `Cloudflare runtime accepts only workers-ai model targets`. The call goes
  through `createWorkersAI({ binding: env.AI })`.
- **Builds the system prompt** from the *versioned* instruction and skill
  artifacts in `context_files`, sorted by path, with `/.memseek/manifest.json`
  excluded as machine metadata. The prompt is fixed by this Worker: the
  catalog supplies content, never the frame.
- **Grants tools.** `createAITools` gives a bounded `read` (32 KiB, 800 lines)
  and — only when `capabilities.exec` is true — a `shell` on the backend
  implied by the runtime. `recall` is added only when the Agent definition
  lists it.
- **Bounds the loop** with `stopWhen: stepCountIs(limits.max_steps)`.
- **Filters model parameters** through `safeGenerationOptions`: `temperature`
  0–2, `top_p` 0–1, `frequency_penalty` and `presence_penalty` −2–2, `seed` a
  safe integer, `max_output_tokens` a positive integer, `stop` at most 8
  strings of at most 512 chars. An out-of-range value throws
  `invalid model parameter: <name>`; an unrecognized key is dropped. Catalog
  data cannot reach through this to replace the model, tools, system prompt,
  or step terminator.
- **Requires an envelope.** The final message, after stripping an optional
  ```` ```json ```` fence, must parse as
  `{"value": …, "citation_ids": [...], "awaiting_input": bool?}`. A citation
  outside `request.citation_ids` → `agent widened citation authority`.
- **Journals everything.** `model_request`, `model_step` (finish reason,
  usage), `tool_call`, `tool_result`. Any audit payload over 32 KiB is replaced
  by `{pointer: true, bytes, sha256}` so receipts stay bounded.

### The recall tool

`createRecallTool` is a *disclosure-safe* search: it greps only
`/.memseek` and `/workspace` — content the run is already authorized to see —
so it can surface evidence that has fallen out of live model context without
widening authority.

Bounds come from the context policy: `max_recall_hits` clamped to 1–50
(default 20) and `max_exposed_bytes` clamped to 1 KiB–256 KiB (default 64 KiB),
trimming whole matches until the payload fits and setting `truncated`. Every
result is also written to `/workspace/recalled/<sha256(query)>.json` and
returned as `materialized_path`, so the agent can re-open the exact bytes it
was shown and a later reader can audit them.

---

## 5. Enforcement summary

Everything below fails closed, with a `422`:

| Boundary | Rule |
| --- | --- |
| Authentication | HMAC-SHA256 over `timestamp.body`, ±300 s, 64-hex signature |
| Request size | 12 MiB |
| Session identity | `session_key` and `parent_session_key` must be 64 lowercase hex |
| Mode | `derivation` or `invocation` only |
| Identity | `task_id` and `executor.ref` required |
| Output path | must resolve under `/outbox` |
| Capabilities | `filesystem` required; `network: true` **rejected outright** |
| Writable roots | each must resolve under `/workspace` or `/outbox` |
| Path shape | no `.`, `..`, `\0`, no absolute or empty segments in bundle paths |
| Context paths | `context_files` keys must resolve under `/.memseek` |
| Program bundles | `bundle:` (external URI) rejected — `requires an installed artifact resolver` |
| Immutability | `/.memseek` and `/inputs` restored and reported if changed |
| Write scope | post-execution hash diff must contain no path outside `writable` |
| Output size | agent `max_output_bytes`; Program 10 MiB |
| Steps | `stopWhen: stepCountIs(max_steps)` |
| Model targets | `workers-ai:@cf/…` only |
| Model params | narrow numeric/string allowlist |
| Citations | agent citations ⊆ request `citation_ids` |
| Snapshot size | 2 000 files |
| Fork copying | preserved paths only; no symlinks; 2 000 files / 50 MiB |
| Outbox | see §7 |

MemSeek then re-validates the response independently — output against the
declared JSON Schema, byte cap, step cap, and `citation_ids` subset — because
this Worker is not trusted to have enforced any of it.

---

## 6. Idempotency, resume, and fork

Three distinct identities do three different jobs:

- **`session_key`** names the workspace. Same value → same files. Tasks in one
  derivation run naming the same Computer share a filesystem; different
  Computer references are isolated.
- **`task_id`** names one unit of work. Its result is cached in the workspace,
  so a transport interruption and retry return the original value with
  `resumed: true` rather than paying for the work twice. Reusing a `task_id`
  for genuinely different work returns the stale result — MemSeek derives it
  from run identity for exactly this reason.
- **`parent_session_key`** requests a fork. Only `retention.preserve` paths
  cross over, so a fork inherits checkpoints, not scratch state. The
  `/.memseek/forked-from` marker makes the copy happen exactly once and makes a
  retry that names a different parent an error.

---

## 7. Outbox protocol

`collectOutbox` treats `/outbox` as a declared return channel, not a directory.

**Derivation mode** allows exactly one path: `output_path`. Any other file
under `/outbox` → `unknown outbox files: …`. Nothing is auto-ingested; the
declared result becomes a Task value and still has to pass MemSeek's `emit`.

**Invocation mode** additionally allows each non-`final_result` entry in
`computer.writeback`, and returns their contents with hashes:

- `observations` must be a single JSONL file, not a directory.
- `maintained_state` files must end in `.json`.
- Every entry must be a regular file — `lstat` rejects symlinks, and
  `listFiles` refuses a symlink anywhere in the tree.
- At most 100 files and 1 MiB total; at most 2 000 entries while walking.

MemSeek verifies the reported SHA-256 and byte count, matches each path to one
writeback declaration, requires non-empty visible citations, and fixes the
entity, collection, record type, status, and dedupe key itself. Observations
become active evidence; maintained state becomes a review-required draft.

---

## 8. Configure, deploy, verify

Node comes from `nvm` in this repo; `npm` is not on the default `PATH`.

```sh
export PATH="$HOME/.nvm/versions/node/v22.22.1/bin:$PATH"
cd cloudflare/computer-runtime
npm install
npm run check          # tsc --noEmit && vitest run
```

Set the shared secret and deploy:

```sh
SECRET=$(openssl rand -hex 32)
echo -n "$SECRET" | npx wrangler secret put MEMSEEK_RUNTIME_SECRET
npm run deploy
curl -s https://memseek-computer-runtime.<subdomain>.workers.dev/health
```

Point MemSeek at it. **The environment variable names are unprefixed** —
`Settings` sets no `env_prefix`, so a `MEMSEEK_`-prefixed name binds nothing
and you get `Cloudflare Computer runtime URL/token is not configured`:

```sh
COMPUTER_RUNTIME_URL=https://memseek-computer-runtime.<subdomain>.workers.dev
COMPUTER_RUNTIME_TOKEN=<the same secret>
# optional
COMPUTER_REQUEST_TIMEOUT_S=300
COMPUTER_RESPONSE_MAX_BYTES=16777216
```

Then flip the Computer definition from `provider: fake` to
`provider: cloudflare` and republish the catalog. For derivations, also raise
`max_computer_runs` — it defaults to `0`, so Computer-backed derivations are
opt-in per pipeline.

### Local development

```sh
npm run dev            # wrangler dev
```

Durable Objects and the Worker Loader run locally. Two caveats: the container
backend needs Docker to build [`Dockerfile`](Dockerfile), and Workers AI calls
leave your machine and are billed regardless of local mode. Exercise the
`worker-javascript` path first — it needs neither.

### Testing

The vitest suite ([`test/security.test.ts`](test/security.test.ts)) covers the
path and quoting policy in `security.ts`. It is a unit suite over the helpers,
**not** an execution test: no Durable Object, no backend, no Workers AI.

The deterministic contract gate is on the Python side, against the `fake`
provider:

```sh
docker compose up -d --wait postgres-test
LLM_FAKE=1 DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:55432/memseek_test" \
  uv run pytest tests/test_computer_resources.py tests/test_computer_renewal_catalog.py -q
```

Those tests pin `provider: fake` and register in-process Python executors, so
they never reach this Worker. Both sides implement the same
`ExecuteRequest`/`ExecuteResponse` shape; only the fake side is covered by CI.

### Live Agent smoke test

The interactive setup helper asks for the Worker URL and shared secret without
echoing the secret:

```sh
scripts/setup_cloudflare_smoke.sh
```

It upserts the settings into the untracked `.env` and writes a mode-600
`.env.sh` containing shell exports. It can launch the billable canary
immediately, or you can run `source .env.sh` and invoke the canary separately.

After deploying the Worker and setting the same secret on both sides, run the
operator canary from the repository root:

```sh
make cloudflare-agent-smoke
```

`Settings` reads `COMPUTER_RUNTIME_URL` and `COMPUTER_RUNTIME_TOKEN` from the
environment or the repository's untracked `.env`. The command does not use
PostgreSQL or require a published workspace. It resolves the committed renewal
Agent, changes its Computer provider from `fake` to `cloudflare` in memory, and
then makes two signed requests against one session:

1. A real Workers AI model must call `write` for
   `/workspace/proof.json`.
2. A fresh task deliberately receives no marker and must call `read` on that
   file to recover it.

The canary fails unless both receipts report `worker-javascript`, the tool
calls and results are journaled, the first receipt contains the file diff, the
second Agent returns the hidden marker, and both outputs preserve the exact
visible citation UUID. A passing JSON summary includes the session, model,
step counts, and output hashes but never the runtime secret.

This is deliberately operator-run rather than part of CI because it calls a
billable Workers AI model. It proves the signed provider boundary, live Agent
tool loop, and durable Computer filesystem; the deterministic Python suite
continues to prove derivation and invocation semantics. Although the canary
uses only the Dynamic Worker backend, deploying the current Worker still needs
Docker because `wrangler.jsonc` declares the container fallback.

---

## 9. Failure modes worth recognizing

| Symptom | Cause |
| --- | --- |
| `Cloudflare Computer runtime URL/token is not configured` | `COMPUTER_RUNTIME_URL`/`COMPUTER_RUNTIME_TOKEN` unset — check for a stray `MEMSEEK_` prefix |
| `401 unauthorized` | Secret mismatch, clock skew over 300 s, or a proxy that re-serialized the JSON body |
| `503 runtime_secret_missing` | Deployed without `wrangler secret put MEMSEEK_RUNTIME_SECRET` |
| `network-enabled Computers require a separately reviewed egress gateway` | The definition sets `capabilities.network: true` |
| `Cloudflare runtime accepts only workers-ai model targets` | The model alias resolves to a non-Workers-AI target |
| `health endpoint returned HTTP 500: error code: 1101` | The Worker threw during startup or module initialization; authenticate Wrangler and inspect `wrangler tail` while requesting `/health` |
| `agent final output does not match the MemSeek envelope` | The model returned prose or a bare object instead of `{value, citation_ids}` — the most common first-run failure |
| `agent widened citation authority` | The model cited an ID absent from the manifest |
| `immutable files changed: …` | The executor wrote under `/.memseek` or `/inputs` |
| `files changed outside writable roots: …` | A write landed outside `computer.writable` |
| `unknown outbox files: …` | An undeclared path under `/outbox`, or a derivation writing more than its declared result |
| `external Program bundles require an installed artifact resolver` | The Program uses `bundle:` instead of inline `files:` |
| `receipt.resumed: true` unexpectedly | A reused `task_id` hit the result cache |
| `budget: Computer runtime response exceeds byte limit` | Response over `COMPUTER_RESPONSE_MAX_BYTES` (16 MiB default) |

---

## 10. Deliberately not built

These are refused explicitly rather than half-supported:

- **Reviewed egress.** Any `network: true` Computer is rejected at the
  boundary; both backends set `egress: { mode: "none" }`.
- **External Program bundles.** `bundle: {uri, sha256}` needs a content-address
  resolver that verifies the hash before execution. Inline `files:` only,
  capped at 1 MiB by the catalog.
- **Multi-file `worker-javascript` linking.** See §4.
- **R2 payload offload.** Results are returned inline and bounded; the
  `storage_uri` field exists in the schema but no blob adapter is wired.
- **Automated live-deployment tests.** The operator canary above exercises
  `provider: cloudflare` end to end, but it is intentionally not run in CI.

`@cloudflare/computer@0.2.1` is a preview package. Treat this Worker as a
working implementation of a settled protocol on an unsettled runtime: the
MemSeek-side contract is fixed by tests, the Cloudflare-side surface is not.
