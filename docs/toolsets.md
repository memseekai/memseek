# Toolsets

A toolset is the declared surface of tools an Agent may reach. It answers one
question, from the catalog alone and without reading any code: *what can this
Agent actually do?*

It is the inbound counterpart to an [MCP interface](mcp.md). That one publishes
Memseek's own operations **outward**, to clients like Claude Code. A toolset
enumerates what an Agent running **inside** a Computer may reach.

```yaml
# toolsets/renewal.yaml
toolsets:
  - name: renewal
    version: 1
    title: Renewal analyst tools
    instructions: Keep working notes in /workspace.
    sources:
      - name: workspace
        kind: filesystem
        description: Read and write working files in the Computer workspace.
        root: /workspace
        modes: [read, ls, find, grep, write, edit]

      - name: recall
        kind: recall
        description: Search already-authorized immutable context.

      - name: renewal_research
        kind: skill
        artifact: renewal_research_skill@1
        skill_name: renewal-research

      - name: observations
        kind: writeback
        description: Record what is true now, with the citations it rests on.
        path: /outbox/observations.jsonl
```

An Agent binds one:

```yaml
agents:
  - name: renewal_analyst
    version: 2
    toolset: renewal@1
    # `tools:` and `skills:` are the pre-toolset spelling of the same fact.
    # Declaring either alongside `toolset:` is an error.
```

## Nothing is implicit

A filesystem source that does not list `write` grants no write tool. This is
not fussiness: the underlying SDK's convenience helper installs whatever tools
it currently ships, including one that mints public URLs when the workspace has
an asset store. A surface assembled by *subtracting* from that set would widen
on its own the day the dependency grows a tool. A surface assembled by naming
what it wants cannot.

The Computer stays the ceiling. A toolset may narrow what a Computer permits and
may never widen it: declaring `exec` against a Computer with
`capabilities.exec: false` fails at catalog compile time, not at run time.

## Source kinds

| `kind` | Requires | Grants |
| --- | --- | --- |
| `filesystem` | `modes` | One tool per declared mode: `read`, `ls`, `find`, `grep`, `write`, `edit`, `delete`. Optional `root` narrows the reachable tree. |
| `exec` | — | A shell in the Computer's declared runtime. Needs `capabilities.exec`. |
| `recall` | — | Bounded search over already-authorized context, materializing a receipt. |
| `writeback` | `path` | Schema-checked write tools for one of the Computer's own declared writeback paths. |
| `skill` | `artifact` | See below. Many skill sources produce **one** tool with many choices. |
| `view` | `view` | Bounded search over a named view's rows, materialized before the run. |
| `mcp_server` | `url` | Declared and validated; not executable yet. |
| `skillpack` | `pack` | A `skillpacks/<name>/` module mounted as a skill for the Agent's harness. Its declared capabilities must be allowed by every Computer. See [Harnesses and skill packs](computers.md#harnesses-and-skill-packs). |

`skill` and `skillpack` sources also take an optional `learning` field. See
[Skills that learn](#skills-that-learn).

Every kind except `skill` and `skillpack` requires a `description`. A skill's
description belongs to the skill artifact, because two toolsets describing the
same skill would be two different promises about one body of text. A skill
pack's description comes from the `SKILL.md` it generates, for the same reason.

## Skills are disclosed, not pasted

A skill artifact with a `description` is materialized to
`/.memseek/skills/<name>/SKILL.md` with YAML frontmatter. Only its name and
description reach the system prompt; the procedure stays on disk until the Agent
calls the `skill` tool for it.

```text
## Skills
These procedures are available but are NOT in your context. Call the `skill`
tool with the exact name to read one. Do not claim to have followed a skill you
did not load.
- renewal-research — Work a renewal file evidence-first… (/.memseek/skills/renewal-research/SKILL.md)
```

A skill artifact **without** a description cannot be disclosed this way — there
is nothing to offer it by — so it is inlined into the prompt exactly as it was
before this mechanism existed. Adding a description is what opts a skill in.

This is not unconditionally cheaper. A loaded skill's body re-enters context on
every subsequent step as tool-result history, whereas an inlined skill appears
once in the system prompt. Progressive disclosure wins when skills are many and
selectively relevant. Keep always-relevant procedure text in the Agent's
`instructions` artifact; reserve skills for the selective case.

## Skills that learn

A `skill` or `skillpack` source can record lessons while the Agent uses it and
read them back on the next run. For the steps, see
[Make a skill learn from its runs](skill-learning.md). For the design, see
[How skill lessons work](skill-lessons.md).

```yaml
toolsets:
  - name: scraper
    version: 1
    sources:
      - {name: browser, kind: skillpack, pack: browser-harness, learning: true}
      - name: sql
        kind: skill
        artifact: sql_skill@1
        learning: {playbook: false, max_per_run: 4}
```

### The source `learning` field

Allowed on `skill` and `skillpack` sources only.

| Field | Default | Meaning |
| --- | --- | --- |
| `collect` | `true` | Offer the lessons tool for this skill and append its recording instructions. |
| `playbook` | `true` | Install `PLAYBOOK.md` beside the skill once it has lessons for the run's entity. |
| `kinds` | the skill's | Kind names mapped to descriptions. Merges with the skill's `kinds`; if the skill declares none, replaces the defaults below. |
| `require` | the skill's | Kinds each call must include for this skill. Replaces the skill's value. |
| `code` | the skill's | Kinds whose lessons must carry `code`. Replaces the skill's value. |
| `guidance` | the skill's | Extra text for the recording instructions. Replaces the skill's value. |
| `max_per_run` | the skill's, or 8 | How many lessons the agent is asked to record, from 1 to 50. |

`learning: true` means all defaults. `false`, and `{collect: false, playbook:
false}`, are refused: omit the field instead.

The name a skill learns under is its installed directory name: `pack` for a
`skillpack` source, and the resolved skill name for a `skill` source. Two
sources that learn cannot share a name.

### The skill's `lessons` block

A skill declares what is worth learning beside itself: `lessons:` in a pack's
`skillpack.yaml`, or on an artifact with `kind: skill`. It takes `kinds`,
`require`, `code`, `guidance`, and `max_per_run`, with the meanings above.

A skill that declares no `kinds` gets these, and `code: [helper]`:

| Kind | Description |
| --- | --- |
| `helper` | Code that did the job, so the next run can run it instead of writing it again. |
| `tip` | Something that worked and is worth doing again. |
| `pitfall` | Something that went wrong or wasted time, or an earlier lesson that proved false, and what is true now. |

### What memseek adds

When any source in a catalog's toolsets sets `learning`:

- The catalog gets the `lessons@1` collection. Its content is `text`
  (required, one line, up to 480 characters), `skill` and `kind` (required),
  and optional `detail` and `code`. It declares the fields `skill`, `kind`, and
  `code`, and uses the `pg_default` search profile.
- A package that ships the toolset ships `lessons@1`.

For a run of an Agent whose toolset has a skill with `collect` on:

- The Computer's writeback gains `/outbox/lessons.jsonl`, an `observations`
  writeback into `lessons@1`.
- The run gets one `record_lessons` tool. Its `skill` must name a skill in this
  run with `collect` on, and its `kind` must be one that skill declares.
- Each such skill's `SKILL.md` ends with recording instructions made from its
  kinds, `require`, `code`, and `guidance`.

For each skill with `playbook` on and at least one lesson for the run's entity,
the run gets `<skill>/PLAYBOOK.md`: that skill's 40 newest lessons, grouped by
kind in the skill's order. `SKILL.md` starts with a pointer to it, and the
system prompt includes it.

The run's `learning` option narrows all of this: `off` removes everything, and
`read` removes the lessons tool and the instructions. See
[What each run mode shows](skill-lessons.md#what-each-run-mode-shows).

### Checks at compile time

The catalog refuses:

- `learning` on a source that is not `skill` or `skillpack`;
- `lessons` on an artifact whose `kind` is not `skill`;
- a `require` or `code` kind that the merged `kinds` do not declare;
- a catalog with a learning source and no `pg_default` search profile;
- a collection of its own named `lessons` while a source learns;
- a Computer of a learning Agent that does not list `/outbox` in `writable`.

## Versions are exact

Like an MCP interface, a toolset has no `active:` alias. An Agent binds one
exact version, because a tool surface that could change under a pinned Agent is
not a surface. Rolling a toolset forward means a new Agent version — which is
correct: the tool surface is part of what the Agent *is*.

A package must declare every view and artifact its bound toolset reaches, so a
toolset cannot widen the package's surface from the side. The built-in
`lessons@1` collection is the one exception: a package that ships a learning
toolset gets it without listing it.

To read a surface rather than reconstruct it from YAML, draw the package:
`uv run memseek catalog-graph --dir <catalog> --out surface.html`. Each source
appears as its own part, bound to the Agent through its toolset, with the root,
modes, path, or artifact it grants — so "what can this Agent actually reach?"
is one glance rather than seven blocks. See
[Seeing the package](packages.md#seeing-the-package).

## What is declared but not yet executable

Two values compile and are then refused, deliberately, so a catalog can be
written against them before the machinery lands:

- `view` sources take `mode: snapshot | live`. `snapshot` materializes the rows
  before the run, so their record IDs are already inside the run's citation
  authority. `live` needs a host tool channel that does not exist yet.
- `writeback` sources take `commit: outbox | staged`. `outbox` writes a file the
  run's completion ingests atomically. `staged` needs the same channel.

`mcp_server` sources are validated for shape and then refused at run time, for a
specific reason: tools execute in the Agent's Durable Object, which has
unreviewed network egress, while the Computer itself declares
`capabilities.network: false`. An MCP source would be the first network path in
a system that advertises none. Refusing loudly costs nothing; ignoring it would
run an Agent missing a capability its author declared.
