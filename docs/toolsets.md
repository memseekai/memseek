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

Every kind except `skill` requires a `description`. A skill's description
belongs to the skill artifact, because two toolsets describing the same skill
would be two different promises about one body of text.

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

## Versions are exact

Like an MCP interface, a toolset has no `active:` alias. An Agent binds one
exact version, because a tool surface that could change under a pinned Agent is
not a surface. Rolling a toolset forward means a new Agent version — which is
correct: the tool surface is part of what the Agent *is*.

A package must declare every view and artifact its bound toolset reaches, so a
toolset cannot widen the package's surface from the side.

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
