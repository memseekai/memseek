# Site scrape with a learned skill

This catalog runs one Agent, `site_scraper@1`, under the
[pi](https://github.com/earendil-works/pi) harness with the
[browser-harness](https://github.com/browser-use/browser-harness) skill pack.
Each run scrapes one site and records what it learned about that site. The
next run on the same site starts from those learnings.

This guide covers three things: how the parts fit, how to test them, and how
to measure whether the learned skill helps.

## How it fits together

The Agent's run has three parts. Each is a separate module, and none of them
names another.

| Part | Where it is declared | Module |
|---|---|---|
| Harness (the agent loop) | `harness: pi` on `agents/site_scraper.yaml` | `harnesses/pi/` |
| Skill pack (the tool skill) | `{kind: skillpack, pack: browser-harness}` in `toolsets/scraper.yaml` | `skillpacks/browser-harness/` |
| Provider (where it runs) | `provider: local` on `computers/scrape_workspace.yaml` | `src/memseek/local_computer.py` |

To add another harness, add a directory under `harnesses/`. To add another
tool skill, add a directory under `skillpacks/`. See `harnesses/README.md` and
`skillpacks/README.md` for the contracts.

### The learning loop

1. The demo writes a `scrape_tasks` record for the site's entity,
   `site:<domain>`. The run can cite this record.
2. browser-harness sets `learns: true`. Its `SKILL.md` therefore asks the
   agent to append lessons to `/outbox/learnings.jsonl`. Each lesson cites
   the task record.
3. `scrape_workspace@1` declares that file as an `observations` writeback
   into `skill_learnings@1`. It is ingested like any other outbox file.
4. On the next run, the Computer mounts `skill_playbook@1` at
   `/.memseek/playbook.md`. The provider copies the browser-harness rows
   into `.agents/skills/browser-harness/PLAYBOOK.md`, and `SKILL.md` points
   the agent at it.

`/outbox/learnings.jsonl` and `/outbox/final-result.json` are the only outbox
paths. The Computer declares both. The skill pack and the Agent declare none.

The rendered playbook shows the `text` of each lesson only. For this reason,
`skill_learnings` requires `text` to start with `[<pack>/<kind>] `. The
`detail` and `helper_code` fields stay on the record for a later
consolidation step.

## Test without a browser or a model key

The automated tests use a fixture harness and a fixture skill pack. They also
run the real `harnesses/pi/run.mjs` against a fake `pi`. They need the test
database and, for the `run.mjs` test, `node` on `PATH`.

```sh
make database
LLM_FAKE=1 DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test \
  uv run pytest -q tests/test_local_computer.py tests/test_skillpacks.py \
  tests/test_harness_registry.py tests/test_agent_harness_definition.py \
  tests/test_skill_learning_eval.py
```

If `node` is not on `PATH`, the test
`test_the_pi_harness_runs_under_the_local_provider` is skipped.

To see the catalog's wiring as a graph:

```sh
make catalog-graph CATALOG=examples/site_scrape_catalog
```

## Test a live scrape

The `local` provider runs the harness on the machine that runs the worker.
Run the API and the worker on the host, not with `make up`.

### 1. Install the tools

```sh
npm i -g --ignore-scripts @earendil-works/pi-coding-agent   # needs Node 22.19 or later
uv tool install --python 3.12 browser-harness
pi --version
browser-harness skill | head      # prints the SKILL.md that the pack mounts
```

### 2. Start Chrome with remote debugging

Use a separate profile, so the agent never runs in your everyday browser
session.

```sh
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/.memseek/chrome-agent" &
export BU_CDP_URL=http://localhost:9222
browser-harness --doctor
```

As an alternative, open `chrome://inspect/#remote-debugging` in Chrome and
turn on remote debugging. Then leave `BU_CDP_URL` unset.

### 3. Set up the database and a workspace

Use the persistent `postgres` service on port 5433, not `postgres-test`. The
test database lives on tmpfs, so it is emptied whenever its container is
recreated, and the test suite truncates it too. Learnings kept there disappear.

```sh
docker compose up -d --wait postgres
docker compose exec postgres psql -U postgres -d memseek -c "create extension if not exists vector"
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/memseek
uv run memseek migrate
uv run memseek create-workspace scrape-demo    # prints an api_key once
export MEMSEEK_API_KEY=<api_key from the output>
export ANTHROPIC_API_KEY=sk-ant-...
```

Export the same `MEMSEEK_API_KEY` for the demo and for the eval, so that both
use one workspace. The eval does not publish the catalog, and the demo does.

### 4. Start the API and the worker

Run each command in its own terminal. Each terminal needs the exports from
steps 2 and 3.

```sh
uv run uvicorn memseek.api:app --host 127.0.0.1 --port 8000   # terminal A
uv run memseek worker                                          # terminal B
```

The worker starts the harness. It passes on `PATH`, `HOME`, the model key and
the pack's variables (`BU_CDP_URL`, `BU_NAME`), and no other environment
variables. The worker's `PATH` must therefore include `node`, `pi` and
`browser-harness`.

### 5. Run the demo

```sh
make site-scrape-demo URL=https://news.ycombinator.com
```

The demo does the following:

1. Publishes this catalog.
2. Runs the Agent twice on the same site.
3. Prints the scraped items, the harness metrics, the new learnings and the
   second run's `PLAYBOOK.md`.

A successful test has these results:

- The first run returns items and writes `skill_learnings` records for
  `site:news.ycombinator.com`.
- The second run's `PLAYBOOK.md` contains those learnings.

To inspect a run, look in its session directory below `~/.memseek/computers`,
or below `LOCAL_COMPUTER_ROOT` if you set it:

```sh
ls ~/.memseek/computers/*/
cat ~/.memseek/computers/*/.agents/skills/browser-harness/PLAYBOOK.md
```

## Debug a pi run

Every run keeps a record of what pi did in its session directory below
`~/.memseek/computers/<session>/.harness/`:

| File | What it holds |
|---|---|
| `pi-events.jsonl` | Every pi event as it happens: turns, messages, tool calls and results, retries. Per-token deltas are left out. |
| `transcript.html` | The whole session rendered by `pi --export`, written when the run ends. Open it in a browser. |
| `pi-sessions/*.jsonl` | pi's own session file. |
| `pi-stderr.log` | Everything pi printed to stderr. |
| `system-prompt.md`, `input.json` | Exactly what the run was told. |

To watch the newest run live, turn by turn, run this in another terminal:

```sh
make pi-trace                         # follows the newest run until it ends
make pi-trace ROOT=<session dir>      # a specific run; the demo prints the command
make pi-trace FULL=1                  # tool arguments and results untruncated
```

The trace shows these lines:

- The user task.
- Each assistant turn, with its text and tool calls (`→`).
- Each tool result (`←`). A failure shows as `ERROR`.
- Tokens and cost for each turn, and for the run so far.

A rejected learning looks like this:

```
── turn 1
  → record_skill_learnings {"records":[{"learning":"HN rows are tr.athing", ...}]}
  ← record_skill_learnings ERROR Validation failed for tool "record_skill_learnings": ...
── turn 2
  → record_skill_learnings {"records":[{"text":"[browser-harness/extraction] ...", ...}]}
  ← record_skill_learnings ok Recorded 1 entry.
```

To keep working inside a finished run's context, open its session in pi:

```sh
cd ~/.memseek/computers/<session>/workspace
pi --session ../.harness/pi-sessions/<file>.jsonl
```

This continues the conversation and makes new model calls, so it costs money.

Outbox problems appear in the receipt as `outbox_rejected`, and the demo prints
each one as a `rejected` line.

## Measure the gain

The eval compares browser-harness with and without the learned playbook. It
runs on pages that were held out from training.

```sh
uv run memseek eval skill-learning --suite evals/scrape_suite.yaml --trials 3
uv run memseek eval skill-learning --suite evals/scrape_suite.yaml \
  --arms cold,playbook --trials 1 --k-train 1      # a quick first pass
```

| Arm | Playbook | browser-harness helpers | What it measures |
|---|---|---|---|
| `cold` | off | fresh on every run | The baseline: vanilla browser-harness, with no memory |
| `native` | off | kept for the domain | The self-healing helpers of browser-harness alone |
| `playbook` | on | fresh on every run | The memseek skill alone |
| `playbook+native` | on | kept for the domain | Both together |

- **Isolation.** Each arm and trial uses its own entity, so learnings cannot
  leak between arms.
- **Scoring.** The checks are deterministic. The value must match the
  schema, have enough items and cover the required fields. No model judge is
  used.
- **Report.** For each arm, the report shows:
  - pass rate and score;
  - median steps, tokens, cost and wall time;
  - deltas against `cold` and against `native`, with bootstrap
    95% confidence intervals;
  - a learning curve over training runs.
- **JSON output.** Add `--json` for machine-readable output. Each run is
  also stored as a `skill_eval_runs` record, which links to its invocation.

The memseek skill has a real effect if `playbook` beats `cold` and `native` on
steps and tokens, with the same or a higher pass rate. The learning curve
should also improve as `k_train` grows.

A full run makes many live model calls: arms × trials × (k_train + tests) ×
tasks. Start with the quick pass above.

## Settings and troubleshooting

| Setting | Default | Purpose |
|---|---|---|
| `LOCAL_COMPUTER_ROOT` | `~/.memseek/computers` | Session directories |
| `HARNESS_PATHS` | `["./harnesses"]` | Where harness modules are found |
| `SKILLPACK_PATHS` | `["./skillpacks"]` | Where skill packs are found |

- **The model.** The model is the `scraper` alias in `conf/models.yaml`. It
  is currently `anthropic:claude-sonnet-4-5`. Change the target to any
  `provider:model` that pi accepts, and export that provider's key. The key
  names are in the `model_env` field of `harnesses/pi/harness.yaml`. To set
  a thinking level, add a suffix, as in `anthropic:<model>:low`.
- **A missing binary.** The run fails before it starts, and the error gives
  the install command from the manifest.
- **Scraping fails at once.** Check `browser-harness --doctor` in the
  worker's terminal. A missing `BU_CDP_URL` or a closed debugging port is the
  usual cause.
- **The Cloudflare provider.** The Cloudflare provider does not run
  harnesses or skill packs yet, and it refuses them with a `capability`
  error. The container version is a follow-up.
