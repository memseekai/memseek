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
workspace:                            # optional: files the skill expects to find
  dir: "{state}/agent-workspace"
  from: {repo: "https://github.com/browser-use/browser-harness", ref: <commit>, path: agent-workspace}
stop: [browser-harness, --reload]     # run after the harness exits
```

`{runtime}` is a private directory below `/tmp` that exists for one run. Put
sockets and pid files there. macOS caps a Unix socket path at 104 bytes, and a
path below `{state}` exceeds that. `stop` runs after the harness exits, even when
the run fails, with the pack's environment. It is best effort, and its job is to
shut down anything the pack started, such as browser-harness's daemon.

`workspace` copies a directory from a pinned commit of a git repository into
`dir` before the harness starts. Use it for files the skill refers to that the
installed tool does not ship, such as browser-harness's `agent_helpers.py` and
`domain-skills/`. The directory is fetched once into `~/.memseek/skillpacks/`
(`SKILLPACK_CACHE`), and a file already in `dir` is never replaced, so
a kept `{state}` keeps what earlier runs changed.

The provider installs a pack into `<skills_dir>/<name>/` in three steps:

1. Run `skill.command`, or read `skill.file`, into `SKILL.md`.
2. If the skill learns and has lessons, write its playbook as `PLAYBOOK.md`
   and point to it from the top of `SKILL.md`.
3. If the run records lessons for this skill, append the recording
   instructions made from its `lessons`.

## Declaring what the pack learns

A pack declares no outbox paths, collections, or artifacts. Whether it learns
is the toolset's choice: `learning: true` on its source. What is worth
learning is the pack's own knowledge, so it declares that in `skillpack.yaml`:

```yaml
lessons:
  kinds:                     # each kind's description is what the agent reads
    helper: The Python you piped into browser-harness that produced your final rows, in `code`.
    navigation: A URL or JSON endpoint worth calling directly instead of clicking through the page.
    extraction: Where each field lives on the page, and the selector that finds it.
    pitfall: What went wrong, or a playbook lesson that proved false, and what is true now.
  require: [helper]          # every call that records for this pack includes one
  code: [helper]             # these lessons must carry runnable code
  guidance: In a helper, use only browser-harness functions you actually called.
```

A pack without `lessons` records the built-in kinds: `helper`, `tip`, and
`pitfall`. A toolset can override any field on the source. The storage, the
tool, the instructions, and the playbook are built in. The full guide is
`docs/skill-learning.md`, and the field reference is the "Skills that learn"
section of `docs/toolsets.md`.

## Learning modes

A run asks for one of three modes. The eval uses them to compare arms.

| Mode | PLAYBOOK.md | Recording instructions | Lessons writeback |
| --- | --- | --- | --- |
| `off` | not installed | not appended | dropped before collection |
| `read` | installed | not appended | dropped before collection |
| `read_write` | installed | appended | ingested |

The pack's `{state}` directory has the same three modes, separately. `off` is a
fresh directory per run. `read` is a copy of the entity's kept directory, so
the run sees the pack's own saved helpers and cannot change them.
`read_write` is the kept directory itself.
