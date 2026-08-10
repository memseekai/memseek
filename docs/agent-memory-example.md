---
title: The L0–L3 agent memory example
eyebrow: Tutorial — a memory that keeps separate contexts separate
---

`examples/agent_memory.py` is a live terminal walkthrough of a four-layer agent
memory. It follows the L0–L3 design in
[TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory):
conversations become small memories, memories become independently maintained scene
blocks, and changed scene blocks inform a durable persona.

It is an opt-in example catalog, published as `agent_memory@0.3.0` when the script
starts. It does not change the default Memseek catalog.

## What a scene is

A scene is one editable context document for one coherent area of work. It might be
the billing API migration, the team’s UI design rules, an incident response method,
or a decision that keeps affecting later work.

It is not a chat summary, a task-list row, or a container holding every scene. Each
scene has its own stable name and its own history. Updating the billing scene does
not replace the UI scene. This is why `scenes` can list several `▣` entries.

Every scene uses the same readable sections:

| Section | What it tells an agent |
|---|---|
| **Work Context** | what this context is about |
| **Applies When** | when the guidance is relevant |
| **Core SOP** | the repeatable steps to take |
| **Decision Logic** | why those steps and trade-offs apply |
| **Prohibitions and Failure Modes** | what not to do and why |
| **Key Evidence** | facts that support the guidance |
| **Related Tasks and Assets** | work still in flight and useful references |
| **Evolution Record** | meaningful changes or contradictions over time |
| **Open Questions** | uncertainty that still matters |

These headings keep the block natural for a person to read while making its categories
reliable for a self-healing agent parser.

## The four layers

| Layer | Stores | Use it for |
|---|---|---|
| **L0 Conversation** | the original messages | verify exact wording, time, and source |
| **L1 Atom** | individual facts, preferences, rules, and events | recall one actionable claim precisely |
| **L2 Scene** | one maintained context document per topic | restore a working context quickly |
| **L3 Persona** | stable cross-context traits and working patterns | adapt an agent’s behaviour consistently |

```text
L0  messages    what was said
    │  l1_extract
L1  memories    atomic facts and rules
    │  scene_synthesis
L2  scenes      separate Markdown context blocks
    │  persona
L3  persona     durable cross-context patterns
```

L0 and L1 preserve history. L2 and L3 keep a current version for each named block
or trait while retaining the earlier version for audit. The persona runs after an
L2 block changes, so it is based on consolidated context rather than a single raw
message.

## Run it

The terminal walkthrough and the web reactor use the same example, but they have
different jobs:

- `examples/agent_memory.py` writes real records and waits for the real worker.
- `/showcase/agent-memory/` simulates the cascade immediately and can also load a
  read-only snapshot of the real run through Memseek's HTTP API.

A real JSON-capable model is required for the full ladder. `LLM_FAKE=1` can store
and display L0 messages, but cannot create cited L1, L2, or L3 records. The model
behind the `strong` alias must follow structured output and reproduce source UUIDs
exactly.

### 1. Install dependencies and start PostgreSQL

From the repository root:

```console
uv sync --frozen --all-groups
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
make database
uv run memseek migrate
```

Use the same `DATABASE_URL` in every terminal below. If you keep local settings in
an untracked `.env.sh`, `source .env.sh` in each terminal instead of repeating the
exports.

### 2. Configure the model used by the example

`examples/agent_memory_catalog/conf/models.yaml` declares the endpoint, model IDs,
and credential variable. Its shipped configuration reads `OPENAI_API_KEY`:

```console
export OPENAI_API_KEY='replace-with-your-provider-key'
unset LLM_FAKE
```

If you use another OpenAI-compatible provider, change the endpoint and model IDs
in that catalog file and export the credential variable named by `api_key_env`.
The API, worker, and example must all see the same provider configuration.

### 3. Start the API with the web origin allowed

In terminal A:

```console
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
export OPENAI_API_KEY='replace-with-your-provider-key'
export API_CORS_ORIGINS='["http://127.0.0.1:4321"]'
uv run uvicorn memseek.api:app --host 127.0.0.1 --port 8000
```

The CORS value is a JSON array of exact origins. `http://localhost:4321` and
`http://127.0.0.1:4321` are different origins; add the one shown by Astro. Do not
use a wildcard.

### 4. Start the worker

In terminal B:

```console
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
export OPENAI_API_KEY='replace-with-your-provider-key'
uv run memseek worker
```

Keep this process running. It owns `l1_extract`, `scene_synthesis`, and `persona`,
so the upper layers remain empty while the worker is stopped.

### 5. Create a reusable workspace and run the example

In terminal C, create a workspace before starting the example. The API key is
printed once; the commands below capture it without echoing it:

```console
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
workspace_json="$(uv run memseek create-workspace agent-memory-web)"
export MEMSEEK_API_KEY="$(printf '%s' "$workspace_json" | \
  uv run python -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"
export MEMSEEK_BASE_URL=http://127.0.0.1:8000
export AGENT_MEMORY_RUN_ID=demo
uv run python examples/agent_memory.py
```

`AGENT_MEMORY_RUN_ID` makes the entity name predictable: this run writes to
`agent.alice-demo`. It accepts 1–32 ASCII letters, digits, hyphens, or underscores.
Without it, the script generates a random six-character ID and prints the entity
name in the terminal.

The script publishes `agent_memory@0.3.0`, writes both sessions, waits for the
cascade, demonstrates recall and provenance, then opens the interactive prompt.
Keep it open if you want to try `say`, `scenes`, `trace`, and the other commands
below.

### 6. Start the interactive website

In terminal D:

```console
cd marketing
npm ci
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:4321/showcase/agent-memory/>. The staged reactor, recall
composer, provenance graph, and skill loop work without a backend. To attach the
live snapshot, scroll to **Connect API** and enter:

| Field | Value |
|---|---|
| **API URL** | `http://127.0.0.1:8000` |
| **Workspace API key** | the value in terminal C's `MEMSEEK_API_KEY` |
| **Run ID** | `demo` |

Click **Connect to Memseek**. The page checks `/health`, then requests
`/timeline?entity=agent.alice-demo&status=all&limit=100` with the workspace bearer
key. It groups the returned rows into L0 messages, L1 memories, L2 scenes, and L3
persona. The key stays in the input in the current tab; the page does not write it
to local storage, cookies, or the URL.

For a remote deployment, serve both pages over HTTPS and add the website's exact
origin to `API_CORS_ORIGINS`, for example `["https://memory.example.com"]`. Never
embed a workspace key in the JavaScript bundle. This connector is an
operator-facing development aid; give public users a server-side proxy or
short-lived, narrowly scoped credentials instead.

If you do not need the web connector, you may omit `MEMSEEK_API_KEY` and let the
script create a fresh disposable workspace from `DATABASE_URL`. The interactive
terminal still works, but the browser will not know that generated workspace key.

## Use the interactive prompt

| Command | What it does |
|---|---|
| `say <text>` | add a message and watch the memory update |
| `scenes` | list the actual L2 scene documents |
| `memories` | show the atomic L1 memories |
| `rules [floor]` | show high-priority instructions |
| `persona` | show stable L3 traits |
| `recall <task>` | render the context an agent would receive |
| `l0 [session]` | replay the original messages |
| `trace <id>` | follow a record back to its evidence |
| `status` | show the number of records at each layer |

### Open a new scene

Tell the agent about a distinct, durable context. Then run `scenes`.

```text
say The payments migration is under way. We must finish it before September 10, but the new webhook contract is still unapproved.
scenes
```

You will see a block similar to this:

```text
▣ Payments Migration
  Work Context
    The payments migration is under way and depends on approval of the new webhook contract.
  Core SOP
    - Keep the migration plan and contract approval together.
  Open Questions
    - When will the webhook contract be approved?
```

You do not create an empty scene with a separate command. The example extracts the
durable facts and decides whether they belong in an existing block or warrant a new
one. If a message really describes a separate context, name that separation plainly:

```text
say This is separate from payments: the office move is blocked until the new lease is signed, and it must happen by September 20.
scenes
```

The list should now contain both `Payments Migration` and `Office Move`. A second
scene is a second block, not a second item hidden inside the first one.

### Add a standard or rule

Standards can be scenes too. This makes them visible instead of leaving them buried
in an unrelated project:

```text
say UI and UX guidelines must use blue by default.
scenes
```

When no existing design block covers that rule, the example creates a separate
`UI Design Guidelines` scene. The original L1 instruction remains visible through
`rules`, and a genuinely cross-context pattern can later inform `persona`.

### How a scene changes

The default action is **update**: new evidence about billing rewrites the billing
block, preserving its useful context and adding the new information naturally.

A new block is created only for a distinct durable context. Related blocks may be
merged when they describe the same method or narrative. The catalog holds at most
15 live blocks; near that limit it merges related context before opening another.

Finishing work does not make its scene disappear. The outcome, prohibitions, and
lessons are still useful when an agent resumes work or investigates a later problem.
A block is removed only as part of an explicit merge or when its context has truly
become obsolete; its prior versions remain auditable.

### If `scenes` is empty or always shows one block

First make sure the worker is running and you are using a real model credential.
Then give the model enough context to distinguish the new work from the old work:

- Say what is separate: “This is separate from payments …”.
- Include a durable fact, constraint, decision, or method—not only a greeting or
  a temporary chat remark.
- Run `scenes` after the `scene_synthesis` line completes. The `status` command
  should show the same count of scenario blocks.

If the run says that no scene block changed, the model judged the message to be
covered by existing context or too short-lived to preserve. That is a no-op, not a
hidden scene. With `LLM_FAKE=1`, no scene can be created at all.

## What the walkthrough also demonstrates

The scripted tour shows every layer building from a real platform conversation,
then repeats part of that conversation with a moved deadline and a new constraint.
It also includes a separate reviewed-procedure example and a bounded recall prompt.

The catalog lives in `examples/agent_memory_catalog/`:

```text
collections/   messages · memories · scenes · persona
derivations/   l1_extract · scene_synthesis · persona
views/         recall · standing instructions · memory audit
artifacts/     the bounded agent context prompt
```

The important L2 detail is simple: `scenes` contains one record per scene block.
Any index used to navigate those blocks is system metadata and is never presented as
the only scene to an end user.
