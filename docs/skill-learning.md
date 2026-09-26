---
title: Make a skill learn from its runs
eyebrow: How-to
---

This guide makes a skill that an Agent loads record lessons as it works, and
start the next run from those lessons. It works for a skill pack, such as
browser-harness, and for a catalog `skill` artifact. The complete example is
`examples/site_scrape_catalog/`.

You need an Agent that runs on a Computer with a `toolset:`, and a catalog
that declares the `pg_default` search profile. To learn how the loop works,
read [How skill lessons work](skill-lessons.md). For every field and check, see
[Skills that learn](toolsets.md#skills-that-learn).

## Turn learning on for the skill

Add `learning: true` to the skill's source in the toolset:

```yaml
# toolsets/scraper.yaml
toolsets:
  - name: scraper
    version: 1
    sources:
      - name: browser
        kind: skillpack
        pack: browser-harness
        learning: true
```

That is the whole change. Memseek adds the rest:

- the built-in `lessons@1` collection, which the package ships with it;
- the `/outbox/lessons.jsonl` writeback on each Computer the Agent runs on;
- one `record_lessons` tool for every skill that learns;
- recording instructions at the end of the skill's `SKILL.md`;
- from the second run on, a `PLAYBOOK.md` beside the skill.

Lessons are scoped to the run's entity. In the scrape example the entity is
`site:<domain>`, so each site has its own playbook.

## Say what is worth learning

A skill that declares nothing records three kinds of lesson: `helper`, `tip`,
and `pitfall`, and a `helper` lesson must carry runnable `code`. To choose the kinds, declare `lessons:` beside the skill. The
description of each kind is what the agent reads when it records a lesson, and
it heads that kind's section in the playbook.

For a skill pack, put the block in `skillpack.yaml`:

```yaml
# skillpacks/browser-harness/skillpack.yaml
lessons:
  kinds:
    helper: >-
      The Python you piped into browser-harness that produced your final rows,
      in `code` exactly as you ran it, so the next run can pipe it in again.
    navigation: A URL or JSON endpoint worth calling directly instead of clicking through the page.
    extraction: Where each field lives on the page, and the selector that finds it.
    pitfall: What went wrong or wasted steps, or a playbook lesson that proved false, and what is true now.
  require: [helper]
  code: [helper]
  guidance: In a helper, use only browser-harness functions you actually called.
```

For a catalog skill, put the same block on the artifact:

```yaml
# artifacts/sql.yaml
artifacts:
  - name: sql_skill
    version: 1
    active: true
    kind: skill
    description: Answer questions with SQL against the warehouse.
    lifecycle: live
    template: Write SQL that answers the question.
    lessons:
      kinds:
        query: A query that answered the question.
        schema: A table or column that means something other than its name says.
```

The fields do these things:

- `kinds` maps each kind name to its description. List the most useful kind
  first. The playbook shows kinds in this order.
- `require` lists kinds that every call must include for this skill. With
  `require: [helper]`, each run leaves code that the next run can run.
- `code` lists kinds whose lessons must carry runnable code in `code`.
- `guidance` is extra text for the recording instructions.
- `max_per_run` caps how many lessons the agent is asked to record. The
  default is 8.

## Override a skill's lessons in one toolset

To change what a skill learns in one toolset only, set the same fields on the
source. `kinds` merges with the skill's kinds, and the other fields replace
the skill's values. The three default kinds, and their `code: [helper]`, apply
only while no layer declares `kinds`: a source that adds one kind to a skill
that declares none gets just that kind.

```yaml
      - name: sql
        kind: skill
        artifact: sql_skill@1
        learning:
          max_per_run: 4
          kinds:
            query: A query that answered the question in under a second.
```

To record lessons without showing a playbook, write
`learning: {playbook: false}`. To show the playbook without asking for new
lessons, write `learning: {collect: false}`.

## Make a second skill learn

Add `learning: true` to its source. It gets its own playbook and its own
kinds, and it shares the `record_lessons` tool. The tool refuses a lesson whose
kind the skill does not declare.

```yaml
      - {name: browser, kind: skillpack, pack: browser-harness, learning: true}
      - {name: sql, kind: skill, artifact: sql_skill@1, learning: true}
```

## Check that the next run starts from the lessons

1. Check that the catalog compiles and that the lessons collection is wired to
   the skill:

   ```sh
   uv run memseek catalog-graph --dir examples/site_scrape_catalog --out graph.html
   ```

   In `graph.html`, the browser tool `writes` to `lessons@1` and `reads` its
   playbook from it.

2. Run the Agent twice on the same entity:

   ```sh
   uv run python examples/site_scrape.py https://news.ycombinator.com/
   ```

   The script prints the lessons from run 1 and the `PLAYBOOK.md` that run 2
   starts from.

3. Read the playbook where the harness found it:

   ```sh
   cat ~/.memseek/computers/*/.agents/skills/browser-harness/PLAYBOOK.md
   ```

   The first run has no `PLAYBOOK.md`. Memseek leaves the file out until a
   lesson exists.

## Read the lessons yourself

Lessons are ordinary records in the `lessons` collection, with the fields
`skill`, `kind`, and `code`. A view in your catalog can read them:

```yaml
views:
  - name: site_learnings
    version: 1
    active: true
    required_capabilities: [recent]
    parameters:
      entity: {type: string, required: true}
    query:
      mode: recent
      scope: {entities: ["{{entity}}"], collections: [lessons], status: active}
      k: 40
      include: [text, occurred_at]
      render: true
```

A view over `lessons` compiles only while some toolset source sets `learning`.

## Turn learning off for one run

To compare runs with and without lessons, pass run options in the invocation's
task `input`:

```json
{"learning": "off"}
```

`off` shows no playbook and records nothing. `read` shows the playbooks and
records nothing. `read_write` is the default. `memseek eval skill-learning` uses
these modes to compare arms.

## If the catalog refuses it

| Message | Fix |
| --- | --- |
| `… the built-in lessons@1 collection uses the 'pg_default' search profile, which this catalog does not declare` | Declare `pg_default` in the file `config.search_profiles` names in `catalog.yaml`. |
| `collection 'lessons' is built in when a skill learns; rename this one` | Rename your own `lessons` collection. |
| `toolset source '…' learns with require or code naming […], which its lessons do not declare as kinds` | Add those kinds to `kinds`, or remove them from `require` or `code`. |
| `a skill in this toolset learns, so computer '…' must list /outbox in writable` | Add `/outbox` to the Computer's `writable`. |
| `only a skill artifact declares lessons` | Move `lessons:` to an artifact with `kind: skill`. |
| `learning needs collect or playbook; omit it to turn both off` | Remove `learning` from the source. |
| `exec tool source forbids learning` | Only `skill` and `skillpack` sources can learn. |
