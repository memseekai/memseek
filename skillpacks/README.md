# Skill packs

A skill pack is a tool skill that any harness can mount, such as
browser-harness. A toolset grants one with `{kind: skillpack, pack: <name>}`,
and memseek finds it at `skillpacks/<name>/`. A pack knows nothing about which
harness runs it.

## The manifest

`skillpack.yaml`:

```yaml
name: browser-harness          # must match the directory name
version: 1                     # recorded in every receipt and eval row
requires:
  - {bin: browser-harness, install: "uv tool install --python 3.12 browser-harness"}
skill:
  command: [browser-harness, skill]   # stdout becomes SKILL.md; or `file: SKILL.md`
env:
  pass: [BU_CDP_URL, BU_NAME]         # copied from the worker's environment when set
  set:
    BH_HOME: "{state}"                # {state} is the pack's state directory
    BH_RUNTIME_DIR: "{runtime}"       # {runtime} is short and lasts one run
capabilities: [exec, network]         # every Computer that runs it must allow these
learns: true                          # opt into the learning convention below
stop: [browser-harness, --reload]     # run after the harness exits
```

`{runtime}` is a private directory below `/tmp` that exists for one run. Put
sockets and pid files there. macOS caps a Unix socket path at 104 bytes, and a
path below `{state}` exceeds that. `stop` runs after the harness exits, even when
the run fails, with the pack's environment. It is best effort, and its job is to
shut down anything the pack started, such as browser-harness's daemon.

The provider materializes a pack into `<skills_dir>/<name>/` in three steps:

1. Run `skill.command`, or read `skill.file`, into `SKILL.md`.
2. If `learns` is set and the run records learnings, append `LEARNING.md`.
3. If the Computer mounted a playbook with a section for this pack, write it as
   `PLAYBOOK.md` and point to it from `SKILL.md`.

## The learning convention

A pack declares no outbox paths, collections, or artifacts. `learns: true` is
its only link to memory, and everything else belongs to the Computer:

- **Write.** The agent records each learning,
  `{text, content: {pack, kind, detail, helper_code?}, citations}`, through the
  writeback tool the provider derives from `/outbox/learnings.jsonl`. The tool
  checks every entry against the collection's schema when it is written and
  returns what to fix, so a malformed learning is corrected within the run.
  `LEARNING.md` tells the agent how. `text` starts with `[<pack>/<kind>] `,
  which is how one playbook serves several packs.
- **Ingest.** The Computer declares `/outbox/learnings.jsonl` once, as an
  `observations` writeback. The generic outbox walk ingests it like any other
  writeback file.
- **Read.** The Computer mounts a playbook artifact at `/.memseek/playbook.md`
  through its `context:` list. The provider copies this pack's rows from it
  into `PLAYBOOK.md`, grouped by kind.

The catalog refuses a learning pack on a Computer that does not declare the
learnings writeback. A new pack reuses the whole loop by setting
`learns: true`.

## Learning modes

A run asks for one of three modes. The eval uses them to compare arms.

| Mode | PLAYBOOK.md | LEARNING.md | `/outbox/learnings.jsonl` |
| --- | --- | --- | --- |
| `off` | not mounted | not appended | dropped before collection |
| `read` | mounted | not appended | dropped before collection |
| `read_write` | mounted | appended | ingested |

The pack's `{state}` directory has the same three modes, separately. `off` is a
fresh directory per run. `read` is a copy of the entity's kept directory, so
the run sees the pack's own saved helpers and cannot change them.
`read_write` is the kept directory itself.
