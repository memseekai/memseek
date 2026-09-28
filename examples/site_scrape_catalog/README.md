# Site scrape with a learned skill

This catalog runs one Agent, `site_scraper@1`, under the
[pi](https://github.com/earendil-works/pi) harness with the
[browser-harness](https://github.com/browser-use/browser-harness) skill pack.
Each run scrapes one site and records what it learned about that site. The
next run on the same site starts from those learnings.

This guide covers three things: how the parts fit, how to test them, and how
to measure whether the learned skill helps.

## Start here

Open [catalog.yaml](catalog.yaml): each definition is listed beside its exact
source path. Agent execution lives in `scraping/`; evaluation records live in
`evaluation/`. Validate and locate definitions without starting the service:

```sh
uv run memseek catalog-validate --dir examples/site_scrape_catalog
uv run memseek catalog-locate scraper_instructions@1 --dir examples/site_scrape_catalog
```

## How it fits together

The Agent's run has three parts. Each is a separate module, and none of them
names another.

| Part | Where it is declared | Module |
|---|---|---|
| Harness (the agent loop) | `harness: pi` on `scraping/agent.yaml` | `harnesses/pi/` |
| Skill pack (the tool skill) | `{kind: skillpack, pack: browser-harness}` in `scraping/tools.yaml` | `skillpacks/browser-harness/` |
| Provider (where it runs) | `provider: local` on `scraping/workspace.yaml` | `src/memseek/local_computer.py` |

To add another harness, add a directory under `harnesses/`. To add another
tool skill, add a directory under `skillpacks/`. See `harnesses/README.md` and
`skillpacks/README.md` for the contracts.

### The learning loop

The toolset turns learning on for the browser skill with one line,
`learning: true`. What browser-harness learns (helpers, navigation, extraction,
pitfalls) is declared in `skillpacks/browser-harness/skillpack.yaml`. The
catalog declares no collection, view, artifact, or writeback for lessons.

1. The demo writes a `scrape_tasks` record for the site's entity,
   `site:<domain>`. The run can cite this record.
2. Because the skill learns, its `SKILL.md` ends with recording instructions
   made from the pack's kinds. The agent records lessons through the
   `record_lessons` tool. Each lesson names its `skill` and `kind` and cites
   the task record. Every call must include a `helper` with its code.
3. The run's `/outbox/lessons.jsonl` is ingested into the built-in
   `lessons@1` collection.
4. On the next run, memseek reads the browser-harness lessons for this site
   and installs them as `.agents/skills/browser-harness/PLAYBOOK.md`, grouped
   by kind, and `SKILL.md` points the agent at it.

The `site_learnings` view reads the same `lessons` collection, so the demo can
print every lesson for a site.

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
use one workspace. Both publish this catalog before they run, so a new
workspace needs no other setup.

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

- The first run returns items and writes `lessons` records for
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
  → record_lessons {"records":[{"learning":"HN rows are tr.athing", ...}]}
  ← record_lessons ERROR Validation failed for tool "record_lessons": ...
── turn 2
  → record_lessons {"records":[{"text":"Stories are tr.athing rows...", ...}]}
  ← record_lessons ok Recorded 1 entry.
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

The eval runs the same scrape under several arms and compares them. Each arm
turns the playbook and the browser-harness helpers on or off. The eval tests
each arm on pages that were held out from training.

| Arm | Playbook | browser-harness helpers | What it measures |
|---|---|---|---|
| `cold` | off | fresh on every run | The baseline: vanilla browser-harness, with no memory |
| `native` | off | kept for the domain | The self-healing helpers of browser-harness alone |
| `playbook` | on | fresh on every run | The memseek skill alone |
| `playbook+native` | on | kept for the domain | Both together |

### A minimal run of every arm

This command compares all four arms after one training run:

```sh
uv run memseek eval skill-learning --suite evals/scrape_suite.yaml --trials 1 --k-train 1
```

For each task, the eval does the following steps:

1. **k=0.** It runs one test for each arm, before any training. All four arms
   start with nothing learned, so these runs show how much the arms vary
   when nothing is different.
2. **k=1.** It runs one training run for each arm except `cold`, which has
   nothing to learn into. Then it runs one test for each arm again.

The comparison is between the arms at k=1. The k=0 runs are the start of the
learning curve and cannot be skipped.

That is 11 runs for each task: 4 tests at k=0, 3 training runs, and 4 tests at
k=1. The shipped suite has 3 tasks, so the command makes 33 live runs. A run
can take about five minutes and cost about $0.45, so plan for the total.

To make fewer runs, narrow the command:

- **Fewer arms.** `--arms cold,playbook` compares only the memseek skill with
  the baseline. That is 5 runs for each task.
- **Fewer tasks.** Copy `evals/scrape_suite.yaml`, keep one task, and pass the
  copy to `--suite`.
- **No training.** `--k-train 0` makes one test for each arm and nothing
  else. It is useful as a smoke test, but every arm is then equal to `cold`,
  so it measures nothing.

### How many runs a command makes

For each trial, each task makes this many runs:

```
arms × (k_train + 1) × test pages      tests
+ arms that train × k_train            training runs
```

Every arm trains except `cold`. The `[n/total]` counter on each progress line
shows the total for the command.

| Option | Default | Effect |
|---|---|---|
| `--arms` | all four | The arms to run, separated by commas |
| `--trials` | 3 | Independent repeats of the whole schedule. Each trial has its own entities. |
| `--k-train` | 3 | Training runs for each arm. There is one test after each training run. |
| `--out` | `out/skill-learning/<suite>-<UTC time>.json` | The results file |
| `--json` | off | Print the report as JSON instead of tables |
| `--catalog`, `--package` | this catalog, `site_scrape@1.0.0` | The package that the eval publishes before it runs |

### What the eval prints

When each run ends, the eval prints a progress line and the run's harness
metrics:

```
[7/11] books-catalogue playbook trial 0 test k=1: pass, score 1.00
metrics {"steps": 12, "wall_s": 140.2, "cost_usd": 0.21, "tool_calls": 11, "tool_errors": 0, ...}
```

At the end, the eval prints two tables:

1. **One line for each run.** The line shows the task, arm, trial, phase, k,
   result, score, steps, tool calls, tool errors, tokens, cached tokens,
   cost and wall time.
2. **The trained state for each arm.** This is every test run at the largest
   k. The table shows:
   - pass rate and mean score;
   - median steps, tool errors, tokens, cached tokens, cost and wall time;
   - deltas against `cold` and against `native`, with bootstrap 95%
     confidence intervals;
   - a learning curve of mean score and steps at each k.

The eval saves all of this to the `--out` file. The file contains the
settings, every run with its full metrics, and the report. The eval rewrites
the file after each run, so the finished runs are kept if the eval stops
early. Each run is also stored as a `skill_eval_runs` record, which links to
its invocation.

### Read the result

The memseek skill has a real effect if `playbook` beats `cold` and `native`
on steps and tokens, with the same or a higher pass rate. The learning curve
should also improve as `k_train` grows.

With `--trials 1`, each delta comes from one pair of runs, so its interval
has no width. One trial shows the direction of an effect. To know how large
the effect is, use `--trials 3` or more:

```sh
uv run memseek eval skill-learning --suite evals/scrape_suite.yaml --trials 3
```

Other rules that the eval applies:

- **Isolation.** Each eval run, arm and trial uses its own entity, named
  `site:<domain>#<run>-<arm>-<trial>`. `<run>` is the eval's start time. So
  learnings cannot leak between arms, and k=0 never starts from what an
  earlier eval learned.
- **Interleaving.** Each step runs every arm before the next step starts. The
  arms therefore meet the live site at about the same time.
- **Scoring.** The checks are deterministic. The value must match the
  schema, hold exactly the target's `items` rows and cover the required
  fields. No model judge is used. The agent is given the schema, so a value
  that breaks it fails the run instead of scoring part marks.
- **Test runs do not write back.** A test page therefore cannot teach an arm
  its own answer.

## Settings and troubleshooting

| Setting | Default | Purpose |
|---|---|---|
| `LOCAL_COMPUTER_ROOT` | `~/.memseek/computers` | Session directories |
| `HARNESS_PATHS` | `["./harnesses"]` | Where harness modules are found |
| `SKILLPACK_PATHS` | `["./skillpacks"]` | Where skill packs are found |

- **The model.** The model is the `scraper` alias in `config/models.yaml`. It
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
