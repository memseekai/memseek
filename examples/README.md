# Run the Computer renewal example

Run these commands from the **repository root**, where `Makefile` lives.
Use the full checkout: the example needs its catalog, support modules, Memseek
source, and Docker configuration. Copying the Python script alone is insufficient.

## Prerequisites

| Required locally | Check |
| --- | --- |
| macOS, Linux, or Windows with WSL | Use a POSIX shell; run all commands inside WSL on Windows |
| Docker Engine/Desktop, running, with Compose v2 | `docker compose version` and `docker compose ls` |
| GNU Make | `make --version` |
| `uv` on your shell's PATH | `uv --version` |
| Internet access on first setup | Downloads Python, locked dependencies, and Docker images |

Install [Docker](https://docs.docker.com/get-started/get-docker/) and
[uv](https://docs.astral.sh/uv/getting-started/installation/) if missing. The
repository pins Python **3.14.6**; `uv` can install it, so your system Python
version does not need to change. Node.js and a Cloudflare account are only needed
for the optional real runtime.

## First run: deterministic, no model credentials

```sh
uv python install 3.14.6
uv sync --frozen
make computer-demo SCRIPTED=1
```

No `.env` file, API key, manual migration, catalog publication, or second terminal
is required. The launcher starts a local stand-in runtime, PostgreSQL, migrations,
the API, and the worker; creates a demo workspace; and publishes the included
catalog. It supplies matching runtime URLs and credentials to each process.

**Local mode overrides inherited `COMPUTER_RUNTIME_URL` and
`COMPUTER_RUNTIME_TOKEN`.** You can leave deployment settings in your shell;
only `MODE=cloudflare` selects real execution. `COMPUTER_RUNTIME_SECRET` optionally
sets the local shared secret (default: `local-computer-demo`).

The stand-in simulates the provider's signed HTTP protocol. It does not execute
JavaScript in a sandbox or call a model. The API, database, worker, validation,
invocation journal, and writeback are real.

A successful run finishes with:

```text
PASS: terms, cited risk, answered invocation, active observation, draft proposal
```

This verifies contract extraction, a cited renewal risk, two answered pauses,
an active observation, and a proposal held as a draft for review. A failed stage
exits nonzero. Every launcher run creates a new demo workspace.

## Run modes

```sh
make computer-demo                     # short walkthrough; type replies at pauses
make computer-demo SCRIPTED=1          # fixed replies, checks outcomes, then exits
make computer-demo ADVANCED=1          # interactive desk with journal/recall/fork
make computer-demo ADVANCED=1 SCRIPTED=1 # scripted tour of the advanced desk
```

Interactive replies require a terminal. Piped input uses scripted replies.
The advanced desk supports `help`, `ask`, `reply`, `events`, `memory`, `recall`,
`fork`, `drafts`, and `why`; the [full tutorial](../docs/computer-renewal-example.md)
explains them. Invocation IDs belong to their workspace: attaching from another
client also requires that workspace's API key.

## See the catalog before you run it

The demo publishes a catalog of fifteen files whose parts reference each other
by exact version. To read it as the graph it is:

```sh
make catalog-graph                     # writes catalog-graph.html; open it
```

No database, workspace, or server is involved — the command compiles the
directory and writes one self-contained page. Both execution modes appear in
one view: `renewal_evidence` triggers `contract_extract`, which runs the Program
in `fast_workspace@1`, while the nightly `renewal_assessment` runs the analyst
Agent in `research_workspace@1` and routes its writeback into
`task_observations@1` and the review-gated `renewal_proposals@1`. The Agent's
toolset is drawn tool by tool, so its whole reachable surface is one glance.
Click any part for its compiled definition and the budgets it commits to. Point
`CATALOG` at another directory for any other catalog;
`uv run memseek catalog-graph --help` lists the options.

## Optional: real Cloudflare runtime

Both the deterministic local walkthrough and the Cloudflare walkthrough have
passed end to end. The September 9, 2026 Cloudflare run verified Program
extraction, a cited risk, an answered invocation pause, an active observation,
and a review-required draft proposal. Real model wording and the number of
questions can vary; the deterministic stand-in always exercises two pauses.

For a local Worker, you additionally need **Node.js 22 or newer**, npm, and a
Cloudflare account authorized to use the runtime's Workers AI bindings.
Model calls are real and billable. Keep Node.js on PATH in the shell running Make.

```sh
node --version
npm --version
cd cloudflare/computer-runtime
npm ci
cp -n .dev.vars.example .dev.vars
npx wrangler login
cd ../..
```

The included `.dev.vars.example` supplies `MEMSEEK_RUNTIME_SECRET` for local
use. `cp -n` preserves an existing `.dev.vars`. The launcher reads that secret
and shares it with the API and worker. `.dev.vars` is Git-ignored.

To start the local Worker automatically, leave no remote URL exported:

```sh
unset COMPUTER_RUNTIME_URL COMPUTER_RUNTIME_TOKEN
make computer-demo MODE=cloudflare SCRIPTED=1
```

The `unset` is load-bearing. The launcher chooses between Wrangler and a
deployed Worker by looking for `COMPUTER_RUNTIME_URL` **in the environment**. A
value in the repository's `.env` does not select the remote path — but
`source .env.sh`, or an export left from an earlier session, does, without
saying so. `echo $COMPUTER_RUNTIME_URL` settles it.

Node has to be on the PATH of the shell running Make, not merely installed. If
a version manager sets it up only in your interactive profile, `make` reports
`Install Node.js and run npm ci in cloudflare/computer-runtime` even though
Node is present.

`make computer-demo-cloudflare` is an alias for `MODE=cloudflare`.
The launcher starts Wrangler, waits for its health endpoint, then runs the example.
Do not start another Wrangler on the same port.

**This is local Cloudflare development, not a deployment.** Wrangler runs the
Worker and its `MemSeekComputer` and `MemSeekAgent` Durable Objects on your
machine, with local state under `cloudflare/computer-runtime/.wrangler/state`.
Workers AI calls use your Cloudflare account. This command does not create a
deployed Worker, Durable Object namespaces, or a container application in the
Cloudflare dashboard.

To run against resources in your account, first follow the runtime README's
[deployment instructions](../cloudflare/computer-runtime/README.md#8-configure-deploy-verify),
then use the deployed URL below. Deployment registers both Durable Object
classes and the configured container application. Individual objects are used
on demand, and a running container is needed only for `container-shell`
execution. The renewal fixture uses `worker-javascript`, so it does not exercise
the container backend.

For an **already deployed** runtime, export its URL and matching secret instead:

```sh
export COMPUTER_RUNTIME_URL=https://YOUR-WORKER.workers.dev
# Set COMPUTER_RUNTIME_TOKEN securely in your shell to the Worker's
# MEMSEEK_RUNTIME_SECRET; do not use the sample local-development secret.
make computer-demo MODE=cloudflare SCRIPTED=1
```

The remote path requires the exported token and skips local Worker startup.
These names are **unprefixed**: `Settings` declares no env prefix, so a
`MEMSEEK_`-prefixed name binds nothing and you get `Cloudflare Computer runtime
URL/token is not configured`. `scripts/setup_cloudflare_smoke.sh` prompts for
both and writes them to `.env` and a mode-600 `.env.sh` without echoing the
secret.

Deployment itself is covered by the
[runtime README](../cloudflare/computer-runtime/README.md#8-configure-deploy-verify),
and the three modes are compared side by side in the
[tutorial](../docs/computer-renewal-example.md#three-ways-to-run-the-same-demo).

### A smaller canary

When you only need to know whether the runtime works, skip the full demo. The
database-free canary performs two model turns on one session — write a file,
then reopen it from a fresh task — with no PostgreSQL and no published
workspace:

```sh
make cloudflare-agent-smoke-local   # wrangler dev, nothing deployed
make cloudflare-agent-smoke         # against the deployed Worker
```

## Ports, logs, and cleanup

Default host ports: API **8000**, PostgreSQL **5433**, runtime **8799**.
The default command uses the repository's normal Compose project. To keep the
entire stack separate from an existing Memseek installation:

```sh
export COMPOSE_PROJECT_NAME=memseek-renewal-demo
export MEMSEEK_PORT=18000
export MEMSEEK_DB_PORT=15433
export COMPUTER_RUNTIME_PORT=18799
make computer-demo SCRIPTED=1
```

Keep those variables in the same shell for logs and cleanup:

```sh
docker compose logs --tail 100 api worker
docker compose down
```

The launcher stops only runtime processes it started. Docker services and data
remain after the example exits. `docker compose down` stops services and keeps
data. For a full reset of the **isolated demo project above**, including its
workspaces and database, use `docker compose down -v`.

## Debug a real-runtime HTTP 422

Start with the smaller canary, which performs two real Agent turns (write a
proof file, then read it back) without starting the Memseek database/API/worker:

```sh
make cloudflare-agent-smoke-local
```

It uses the same local Cloudflare prerequisites as the renewal example. It
ignores inherited deployment URLs and starts its own Worker. To reproduce the
full renewal workflow afterward:

```sh
unset COMPUTER_RUNTIME_URL COMPUTER_RUNTIME_TOKEN
make computer-demo MODE=cloudflare SCRIPTED=1
```

Each locally started runtime prints `Runtime log: <path>`. Its complete output
survives shutdown in a unique `.memseek/logs/computer-runtime-*.log` file, including
startup failures and early exceptions. These files are private (0600), Git-ignored,
and have the shared runtime credential redacted at shutdown. Set
`COMPUTER_RUNTIME_LOG_DIR` to choose another directory. Remote runtime logs must
be read from the deployment's Workers logs instead.

```sh
# Inspect all retained runs, or replace the wildcard with the printed file path.
rg 'computer.execution_failed|computer.model_step' .memseek/logs/computer-runtime-*.log
```

`computer.execution_failed` includes `scope`, `stage`, task/session IDs, a
request ID, elapsed time, and bounded exception details (message, stack, nested
cause). The Agent logs its exception before the Durable Object RPC boundary can
lose those details. Correlate `scope: runtime` and `scope: agent` entries using
`task_id` and `session_key`; each scope has its own request ID.

`computer.model_step` records tool names, tool errors, finish reason, and text
length. It does not dump tool inputs, result bodies, prompts, or model text.
The HTTP 422 response retains `error` and adds `stage` and `request_id`; Python
error messages include those identifiers too.

| Failing stage | What to investigate |
| --- | --- |
| `validate_request` | Request fields or declared capabilities |
| `model_generation` | Nested provider error/status or model/tool execution failure |
| `agent_output_validation` | Missing final JSON, invalid envelope, or widened citations; inspect preceding model steps |
| `immutable_validation` / `writable_validation` | Changes to protected files or undeclared writable paths |
| `outbox_collection` | Outbox path declarations, file types, and output limits |

The September 7, 2026 diagnostic runs reproduced two separate failures:

- The small canary wrote its proof successfully, then its read turn exhausted
  24 tool-call steps without final text (`agent_output_validation`).
- Renewal assessment returned a final answer, but also wrote
  `/outbox/observations.jsonl` and `/outbox/proposals/renewal-proposal.json`.
  `outbox_collection` rejected these: derivations allow only the final-result
  path and publish records through pipeline `emit`. The shared renewal Agent
  instructions describe invocation writeback, while the generated system prompt
  does not explicitly distinguish this execution mode.

The runtime now appends the effective run-mode contract after reusable Agent
instructions: derivations return records for `emit`, while invocations use their
declared writeback paths. Outbox validation remains enforced. The last allowed
model step disables tools and requests the final envelope; it counts within the
existing step budget. An identical repeated cycle of tool calls and results also
ends tool use, preventing repeated operations from consuming the whole budget.
Invocation decisions use one structured response without tools, so a question
can return without writeback. Execution uses the remaining step budget. Result
caches distinguish the full request, including human replies; reusing an
invocation ID no longer replays an earlier pause. Recall searches supplied
context and workspace notes, excluding runtime metadata and previous recall
receipts.
The invocation context also includes destination collection schemas. Candidate
`content`, merged with top-level `text`, must satisfy the destination schema;
arbitrary extra fields are rejected. Record-validation errors now retain the
specific constraint in the invocation error. The final result's `citation_ids`
must cover every citation used by its writeback files.
Schema-specific writeback tools expose the destination record shape directly
and reject invented fields before writing. The server still validates the
resulting records, citation authority, and review status.

For example, `finish reason: tool-calls, steps: 24/24` means the Agent exhausted
its step budget without returning the required final answer. It is not evidence
of an HTTP authentication problem. A missing `/.memseek/results/...json` file can
be an expected first-run cache miss; follow the structured failure rather than
assuming that filesystem warning caused the 422.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `uv: command not found` | Install uv and reopen your shell; check `uv --version`. You can pass `UV=/absolute/path/to/uv` to Make. |
| Cannot connect to Docker | Start Docker Desktop/Engine; verify `docker compose ls` succeeds. |
| `Port … is occupied` | Stop the process holding that port, or set `COMPUTER_RUNTIME_PORT`. Use the isolated stack settings above for API/database conflicts. |
| Older launcher says “unset COMPUTER_RUNTIME_URL” | Update to this version, which overrides it in local mode. Immediate workaround: `env -u COMPUTER_RUNTIME_URL make computer-demo SCRIPTED=1`. |
| Node/npm missing in real mode | Activate your Node installation in the same shell, then run `npm ci` in the runtime directory. |
| Missing Cloudflare secret | Copy `.dev.vars.example` to `.dev.vars` as above; ensure `MEMSEEK_RUNTIME_SECRET` is nonempty. |
| Real runtime returns HTTP 401 | Verify the configured runtime token matches its `MEMSEEK_RUNTIME_SECRET`. |
| HTTP 422, no renewal risk, or stalled work | Read worker logs and the runtime diagnostics printed on failure. The local deterministic mode separates setup problems from real runtime/model failures. |

## Files included

- [Short walkthrough](computer_renewal.py) and [advanced desk](computer_renewal_advanced.py).
- [Catalog](computer_renewal_catalog/README.md): models, collections, artifacts,
  Computers, Programs, Agents, toolset, context policy, derivations, and package.
- `_computer_common.py`, `_computer_runtime.py`, `_computer_renewal_support.py`,
  and `_workspace_explorer.py`: supporting fixtures, runtime, setup, and presentation.
- [Launcher](../scripts/run_computer_demo.py), root `Makefile`, `Dockerfile`,
  `docker-compose.yml`, `pyproject.toml`, and `uv.lock`: startup and dependencies.
- [Cloudflare runtime](../cloudflare/computer-runtime/README.md): source, locked
  Node dependencies, Wrangler configuration, and `.dev.vars.example`.

For application code, see the [SDK usage and concept guide](../docs/computers.md#configure-computer-work-once).
For the catalog's own shape, run `make catalog-graph` or read
[Seeing the package](../docs/packages.md#seeing-the-package).
