# Harnesses

A harness is the agent loop that runs an Agent on a Computer. An Agent names
one with `harness: <name>`, and memseek finds it at `harnesses/<name>/`. Adding
a harness means adding a directory. Nothing in memseek branches on a harness
name.

A harness knows nothing about skill packs, and the provider that runs it knows
nothing about either. The provider prepares a root, runs the harness entry, and
reads one line back.

## The manifest

`harness.yaml`:

```yaml
name: pi                      # must match the directory name
version: 1                    # recorded in every receipt and eval row
entry: [node, run.mjs]        # argv; an argument naming a file here is made absolute
requires:                     # checked before each run, reported with the install hint
  - {bin: pi, install: "npm i -g --ignore-scripts @earendil-works/pi-coding-agent"}
skills_dir: .agents/skills    # where this harness discovers skills, relative to root
model_env:                    # which variable carries each model provider's key
  anthropic: ANTHROPIC_API_KEY
```

The Python side of this contract is `src/memseek/harnesses/contract.py`.

## What the provider prepares

The provider runs `entry` with its working directory set to the Computer root.
Before that, the root already holds these directories:

| Path | Holds |
| --- | --- |
| `.memseek/` | The materialized, read-only context: instructions, manifest, mounted artifacts |
| `inputs/` | The invocation input, as `input.json` |
| `workspace/` | The agent's working directory, empty on a new session |
| `outbox/` | Where declared writeback files land, through the writeback tools |
| `<skills_dir>/<name>/` | One directory per skill, each with a `SKILL.md` |
| `.harness/input.json` | The `HarnessInput` below |

The process environment holds `PATH`, `HOME`, the model key named by
`model_env`, and each skill pack's declared variables. Nothing else from the
worker's environment is passed.

## Input

`.harness/input.json`:

```json
{
  "task": "the user's prompt",
  "system_prompt": "append this to the harness's own system prompt",
  "output_schema": {"type": "object"},
  "model": {"provider": "anthropic", "model": "claude-sonnet-4-5", "params": {}},
  "limits": {"max_steps": 32, "max_wall_s": 300},
  "tools": [{"name": "shell", "kind": "exec", "description": "..."}],
  "skills": [{"name": "browser-harness", "dir": "/abs/root/.agents/skills/browser-harness"}],
  "learning": "read_write",
  "citation_ids": ["<uuid>"]
}
```

`system_prompt` is built by `src/memseek/harnesses/prompt.py`, the same for
every harness, so runs under different harnesses are told the same things.
`model.params` is the model alias's parameters. A harness uses the ones it
understands, such as `thinking`, and ignores the rest.

## Output

Print exactly one JSON line to stdout and exit 0:

```json
{
  "value": {},
  "citation_ids": ["<uuid>"],
  "steps": 3,
  "awaiting_input": false,
  "events": [{"kind": "model_step", "payload": {"index": 0}}],
  "metrics": {
    "wall_s": 12.5, "steps": 3, "tool_calls": 7, "tool_errors": 1,
    "input_tokens": 1200, "cache_read_tokens": 16000, "cache_write_tokens": 800,
    "output_tokens": 900, "cost_usd": 0.07
  }
}
```

- `value`, `citation_ids`, and `awaiting_input` come from the envelope the
  agent ends with.
- `events` is at most 256 journal events. Each `kind` is lowercase snake case,
  and each `payload` is a small object.
- Every harness fills every `metrics` field, which keeps runs comparable across
  harnesses. Use `null` for tokens or cost that the model provider did not
  report. `null` is not the same as `0`.
- `input_tokens` counts uncached input only. Report cache reads and cache writes
  in their own fields, the way the Anthropic API does. They are billed at very
  different rates, and a harness resends its whole conversation every turn.

A nonzero exit fails the run. Anything on stderr becomes the failure detail.
Exit 2 when the run hit `max_steps` or `max_wall_s`: the run fails as
`budget` and is not retried. Any other nonzero exit fails as `provider`, which
the worker retries.

The provider ends the harness's whole process group when the run ends, so an
agent the harness started never outlives it.

## Writeback tools

`writeback_tools` lists one tool per declared `observations` writeback, each as
`{name, description, path, input_schema}`. `input_schema` is plain JSON Schema.
Offer each one as a native tool if the harness can. To run a call, execute
`writeback_command + [name]` with the call's arguments as JSON on stdin:

- Exit 0 means the records were appended. Stdout is the result for the model.
- Exit 1 means nothing was written. Stdout says what to fix. Return it to the
  model as a tool error, so the agent can correct the records and call again.

The command runs the same checks that ingestion does, so a record the tool
accepts is one that ingestion accepts. A harness with no custom tools can still
tell its agent to run the command from its shell. `pi/memseek-tools.mjs` is the
pi adapter.

## What a harness does not do

It does not collect the outbox, validate the envelope against the schema, or
touch memseek storage. The provider does all three, the same way for every
harness. When it collects the outbox, the provider keeps what would ingest
cleanly and lists the rest under `outbox_rejected` in the receipt. A
malformed side file never fails a run.

## Writing a new harness

1. Create `harnesses/<name>/harness.yaml` and the entry file.
2. Read `.harness/input.json`, run your agent in `workspace/` with the prompt
   appended and the listed skills loaded, and enforce both limits.
3. Print the output line.
4. Set `harness: <name>` on an Agent whose Computer uses `provider: local`.

`tests/fixtures/harnesses/echo/` is the smallest working example.
