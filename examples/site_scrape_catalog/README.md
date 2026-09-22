# Site scrape with a learned skill

This catalog runs one Agent, `site_scraper@1`, under the
[pi](https://github.com/earendil-works/pi) harness with the
[browser-harness](https://github.com/browser-use/browser-harness) skill pack.
Each run scrapes one site and records what it learned about the site. The next
run on the same site starts from those learnings.

The three parts are separate modules, and none of them names another:

- `harness: pi` on the Agent selects `harnesses/pi/`, the agent loop.
- `{kind: skillpack, pack: browser-harness}` in `toolsets/scraper.yaml` selects
  `skillpacks/browser-harness/`, the tool skill.
- `provider: local` on `scrape_workspace@1` runs both on the worker's machine.

## The learning loop

1. The demo writes a `scrape_tasks` record for the site's entity,
   `site:<domain>`. The instructions render it, so the run can cite it.
2. browser-harness sets `learns: true`, so its `SKILL.md` asks the agent to
   append lessons to `/outbox/learnings.jsonl`. Each lesson cites the task.
3. `scrape_workspace@1` declares that file as an `observations` writeback into
   `skill_learnings@1`. Ingestion is the ordinary outbox path.
4. On the next run, the Computer mounts `skill_playbook@1` at
   `/.memseek/playbook.md`. The provider copies browser-harness's rows into
   `.agents/skills/browser-harness/PLAYBOOK.md`, and `SKILL.md` points at it.

The rendered playbook shows each lesson's `text` only, which is why
`skill_learnings` requires `text` to start with `[<pack>/<kind>] `. The
structured `detail` and `helper_code` stay on the record for a later
consolidation step.

## Running it

The local provider runs the harness on the machine that runs the worker, so
run the API and the worker on the host rather than in Docker:

```sh
npm i -g --ignore-scripts @earendil-works/pi-coding-agent
uv tool install --python 3.12 browser-harness
browser-harness --doctor           # needs Chrome with remote debugging, or BU_CDP_URL
export ANTHROPIC_API_KEY=...

make quickstart                    # database, schema, and a workspace key
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
uv run uvicorn memseek.api:app &   # terminal A
uv run memseek worker &            # terminal B
make site-scrape-demo URL=https://news.ycombinator.com
```

The demo publishes this catalog, runs the Agent twice on the same site, and
prints the items, the harness metrics, the new learnings, and the second run's
`PLAYBOOK.md`. Session directories live below `~/.memseek/computers`, or
`LOCAL_COMPUTER_ROOT`.

To compare runs with and without the playbook, see `evals/scrape_suite.yaml`
and `memseek eval skill-learning`.

## Seeing it

```sh
make catalog-graph CATALOG=examples/site_scrape_catalog
```
