---
title: How skill lessons work
eyebrow: Explanation
---

An Agent that scrapes the same site every day should not rediscover the same
selectors every day. Skill lessons are how a run leaves the next run better off:
the agent records what it learned while using a skill, and the next run on the
same entity starts from a playbook built from those lessons.

This page explains the loop and why it is shaped the way it is. To set it up,
follow [Make a skill learn from its runs](skill-learning.md). For every field,
see [Skills that learn](toolsets.md#skills-that-learn).

## Learning is a capability of a loaded skill

The design follows two ideas from agent frameworks. From
[Pydantic AI capabilities](https://pydantic.dev/docs/ai/capabilities/overview/):
a capability is one unit of behavior that brings its own tools, instructions,
and storage, and you switch it on with a little configuration. From
[Eve skills](https://eve.dev/docs/skills): what a skill needs is declared beside
the skill, not somewhere else in the project.

So there are three places, each with one job:

| Where | What it says | Example |
| --- | --- | --- |
| The toolset source | Turn learning on for this skill, here. | `learning: true` |
| The skill | What is worth learning while using it. | `lessons: {kinds: …, require: …}` |
| Memseek | Everything else: storage, the tool, the instructions, the playbook. | built in |

A skill author knows which lessons matter for their tool, so the kinds live
with the skill. A catalog author decides which skills learn in which Agent, so
the switch lives in the toolset. Neither writes a collection, a view, an
artifact, or a writeback.

## The loop

```text
                     run N                                        run N+1
  ┌────────────────────────────────────────┐        ┌──────────────────────────────┐
  │ SKILL.md ends with instructions made   │        │ materialization reads this   │
  │ from the skill's kinds                  │        │ skill's lessons for the run's │
  │                                         │        │ entity, newest first         │
  │ agent calls record_lessons              │        │            │                 │
  │   {text, content: {skill, kind, code?}, │        │            ▼                 │
  │    citations}                           │        │ <skill>/PLAYBOOK.md, grouped │
  │            │                            │        │ by the skill's kinds, beside │
  │            ▼                            │        │ SKILL.md and in the prompt   │
  │ /outbox/lessons.jsonl                   │        └──────────────▲───────────────┘
  └────────────┬────────────────────────────┘                       │
               │ built-in observations writeback                    │
               ▼                                                    │
        lessons@1 (built in) ────────── filtered by skill ─────────┘
```

1. The agent records lessons through one tool. Memseek checks each entry when
   the agent writes it: the skill must be one this run loaded, the kind must be
   one that skill declares, and a kind listed in `code` must carry code. A bad
   lesson is fixed in the same run instead of being dropped after it.
2. The run's outbox is ingested into `lessons@1`. Each lesson is a record that
   cites the record it rests on.
3. On the next run, materialization reads each skill's lessons for the run's
   entity and writes that skill's playbook.

## Why the storage is one built-in collection

Every catalog with a learning skill gets the same `lessons@1` collection. The
loader adds it only when some toolset source sets `learning`, so a catalog
without learning compiles and hashes exactly as before. A package that ships a
learning toolset ships the collection with it.

Its schema is generic: `text`, `skill`, `kind`, and optional `detail` and
`code`. It does not list any skill's kinds. Kinds are checked per run, in the
tool. That choice keeps the catalog stable: changing what a skill learns, or
the wording of a kind, never changes the catalog's hash, so a published
workspace never has to be republished because a skill's lessons changed.

## Why memseek writes the playbook and the instructions

The playbook and the recording instructions are generated at run time from the
skill's declared kinds. The layout is generic: one section per kind, in the
order the skill lists them, each headed by its description, newest lesson
first, and code shown under the lesson that carries it. A kind with no lessons
is left out, and so is the whole playbook until a lesson exists.

Nothing in that code knows about scraping or any other domain. The words that
make a playbook useful for one tool are the skill's own kind descriptions and
`guidance`. A catalog cannot replace the layout with its own artifact today;
that can be added later without changing anything above.

## What each run mode shows

A run's task `input` can narrow what the run reads and writes back. The eval
uses these modes to compare arms.

| Mode | `PLAYBOOK.md` | Recording instructions | Lessons writeback |
| --- | --- | --- | --- |
| `off` | not installed | not appended | dropped before collection |
| `read` | installed | not appended | dropped before collection |
| `read_write` | installed | appended | ingested |

The provider removes the playbook and instructions files from `/.memseek`
before every run, and installs them only beside the skill. A run in `off` mode
cannot find a playbook anywhere.

## Skill lessons are not an artifact learning target

Memseek has a second way to improve a skill, and both use the word `learning`.

- **Skill lessons** (this page) are recorded by the agent during the run, take
  effect on the next run, and need no review. They live in `learning` on a
  toolset source and in `lessons` on a skill.
- **An artifact learning target** improves a reviewed, maintained skill from
  feedback that someone submits after a run. A derivation proposes a candidate
  and a person promotes it. It lives in `learning` on an artifact. See
  [Declaring a learning target](artifacts.md#declaring-a-learning-target) and
  [Maintain a skill from real feedback](skill-maintenance.md).

## Current limits

- **Nothing consolidates lessons yet.** The playbook shows a skill's 40 newest
  lessons for the entity. Duplicates stay, and a wrong lesson stays until a
  newer one corrects it. A derivation that merges lessons is the planned next
  step.
- **Lessons are scoped to the run's entity.** Lessons do not yet carry over
  from one entity to another, even for the same skill.
- **The playbook layout is fixed.** Only the kinds, their descriptions, and the
  guidance are yours to change.
- **Only the `local` provider installs playbooks and asks for lessons.** The
  `cloudflare` provider refuses skill packs and harnesses.
