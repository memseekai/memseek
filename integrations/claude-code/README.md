# Memseek Memory for Claude Code

This plugin lets Claude Code remember a project between sessions. You can tell Claude a
decision today—“all stored timestamps are UTC”—close the terminal, return next week, and
Claude can apply that decision without asking you to repeat it. When a remembered claim is
important, Claude can also show the original conversation that produced it.

The plugin connects Claude Code to an existing Memseek service. It does not install or
start the service itself.

## Install in three steps

You need a current Claude Code release and Python 3.10 or newer. Ask your Memseek
administrator for:

1. the **Memseek service URL**, such as `https://memory.example.com`, without `/mcp` or a
   trailing slash;
2. your **workspace key**.

### 1. Install

The plugin is not published to a marketplace yet. Install it from a checkout of this
repository, which carries `.claude-plugin/marketplace.json` and is therefore a marketplace
itself:

```bash
claude plugin marketplace add ./          # `./`, not `.`; an absolute path also works
claude plugin install memseek-memory@memseek --scope local \
  --config MEMSEEK_URL=http://127.0.0.1:8000 \
  --config MEMSEEK_API_KEY="$(cat .memseek/api_key)" \
  --config MEMSEEK_CAPTURE_MODE=conversation
```

`--scope local` keeps the plugin to this project; use `--scope user` for every project.
`claude plugin details memseek-memory` should list five skills, five hooks, and one MCP
server.

While editing the plugin's own hooks or skills, run
`claude --plugin-dir ./integrations/claude-code` instead: install copies the plugin into
`~/.claude/plugins/cache/` at its declared version, so working-tree edits are not picked up
until the version changes.

### 2. Answer Claude Code's prompts

Omit the `--config` flags above and Claude Code asks for the same three values:

| Question | What to enter |
|---|---|
| **Memseek service URL** | The URL from your administrator, or `http://127.0.0.1:8000` for local development |
| **Memseek workspace key** | The workspace key from your administrator, or the output of `cat .memseek/api_key` |
| **Conversation capture (`MEMSEEK_CAPTURE_MODE`)** | Choose how this Claude session may add new information to Memseek; see the comparison below |

### Choose a capture mode

Capture mode controls **new writes from Claude Code**. It does not turn memory retrieval
on or off: Claude can still receive relevant existing memories in every mode.

| Mode | Saved automatically | Can you ask Claude to remember something? | Existing memory is recalled | Best for |
|---|---|---|---|---|
| `conversation` (recommended) | The exact text of user and assistant chat messages | Yes | Yes | Seamless memory across sessions with no extra steps |
| `explicit` | Nothing | Yes, with `/memseek-memory:memseek-remember ...` | Yes | Choosing each durable fact or decision yourself |
| `off` | Nothing | No; Claude is instructed not to use Memseek write tools | Yes | Using existing memory without intentionally adding to it |

In `conversation` mode, Memseek first stores chat messages as source evidence (L0). Its
worker can then derive reusable memories and summaries (L1–L3) asynchronously. The plugin
does **not** automatically capture terminal commands, file contents, or tool inputs and
outputs, although anything pasted or repeated in the chat can be part of a saved message.
Do not put credentials or other secrets in chat.

Changing the mode affects new activity; it does not delete memories already stored.
`off` is a behavioral policy, not a security boundary: the workspace key can still permit
MCP writes. If writes must be technically impossible, the Memseek administrator must deny
write access for that key or the host must block the write tools.

Claude Code masks the workspace key and stores it as sensitive configuration. You do not
need an `.env` file or shell exports. If you installed from inside an active session, start
a new session after answering the prompts.

### 3. Check the connection

Start a session in that repository — the plugin loads on startup and its `SessionStart` hook
reports what it connected to:

```bash
claude
# Memseek connected for project:payments-api.
```

Then run this inside Claude Code:

```text
/memseek-memory:memseek-status
```

A working installation says:

```text
Memseek is ready.
  Service: https://memory.example.com
  Project memory: project:payments-api
  Conversation capture: conversation
  Workspace key: configured
  Memory tools: 7/7 available
  Retry queue: 0 pending, 0 need inspection
```

If it says “needs attention,” follow the displayed next step. `/mcp` should also show the
plugin-provided `memseek` connection. To change a setting later, open `/plugin`, select
`memseek-memory@memseek`, update its configuration, and start a new session.

## The memory model

The plugin ships with Memseek's **L0–L3 agent-memory model**, packaged as
`agent_memory@0.3.0`. It does not treat one enormous chat transcript as “memory.” Instead,
it builds four layers, each with a different job:

| Layer | Customer-friendly meaning | Example |
|---|---|---|
| **L0 — Conversation** | The exact user and Claude messages, kept as the source evidence | “All database timestamps must be UTC.” |
| **L1 — Memories** | Small, reusable facts, decisions, preferences, rules, and events extracted from the conversation | “Database timestamps must use UTC.” |
| **L2 — Scenes** | Living summaries for separate areas of work, so unrelated topics do not get mixed together | A “Payments migration” summary with its decisions, risks, and open questions |
| **L3 — Working profile** | Stable patterns that apply across several areas of work | “This team prefers explicit migrations and conservative rollout plans.” |

L0 is never replaced by the higher layers. L1–L3 are interpretations, so they keep links
back to the messages that support them. This is what lets Claude answer both “what do we
know?” and “why do we believe it?”

Memseek also maintains a separate **coding playbook**: the current procedure for a repeated
class of task, such as reviewing a migration or diagnosing an incident. Feedback can help
draft an improved playbook, but a positive result never promotes a new procedure
automatically.

### A concrete example

In the first session, you say:

```text
The payments migration must finish before September 10.
The webhook contract is still unapproved, so do not deploy until it is signed off.
```

Memseek keeps those exact words at L0. In the background it can create two L1 memories—a
deadline and a deployment rule—and update the L2 “Payments migration” scene. In a later
session, when you ask Claude to prepare the deployment, it receives a short memory brief
containing that relevant scene and rule. Claude can warn about the missing approval and
cite the original message instead of inventing a reason.

## What the plugin does for you

For each request, the customer-visible flow is:

1. **Find the right project memory.** The repository is mapped to one stable project
   notebook. Opening another terminal does not create a blank memory.
2. **Prepare a short memory brief.** Memseek selects the relevant scenes, rules, memories,
   working profile, and coding playbook. It does not send the entire history to Claude.
3. **Give that brief to Claude automatically.** “Context injection” simply means Claude
   Code places the memory brief beside your question before Claude answers. You do not
   need to run a search command on every turn.
4. **Save the conversation as evidence.** The exact user prompt and final Claude response
   are recorded in the background when automatic capture is enabled.
5. **Improve future memory.** The Memseek worker turns useful conversation evidence into
   L1 memories, L2 scenes, and L3 working patterns for later sessions.
6. **Keep working during an outage.** Claude continues without memory if Memseek cannot be
   reached. Configured writes wait in a private local queue and retry later.

Separate Claude sessions share the project notebook, but each session keeps its own exact
conversation history. That prevents messages from two terminals being replayed as if they
were one conversation.

## Plain-language glossary

| Term used in technical output | What it means here |
|---|---|
| **Project entity** | The stable name of the project's memory notebook |
| **Memory brief** | The small, relevant selection Memseek gives Claude for one request |
| **Context injection** | Automatically placing that brief beside the user's question before Claude answers |
| **Artifact** | The reviewed recipe Memseek uses to assemble a memory brief; it is not a stored fact |
| **Artifact use** | A receipt for one generated brief, recording exactly which recipe/version was used |
| **MCP tools** | The memory actions Claude can call explicitly: search, replay, open evidence, answer, and remember |
| **Provenance** | The links from a remembered claim back to the messages that support it |
| **Compaction** | Claude Code shortening a long local transcript to make room for more work |
| **Worker** | The background Memseek process that turns new messages into maintained memory |

## Good use cases

| Customer need | What changes with the plugin |
|---|---|
| Return to a project after days or weeks | Claude starts with the relevant decisions, active work areas, preferences, and rules |
| Stop repeating architecture decisions | The decision becomes reusable memory while the original wording remains auditable |
| Keep critical project rules visible | High-priority rules are selected exactly and included in the memory brief |
| Ask “why does Claude think that?” | Claude can follow a claim back to the literal conversation evidence |
| Work through a very long session | The important project memory is re-supplied when Claude shortens its local transcript |
| Improve a repeated workflow | Success, failure, or correction feedback is tied to the exact playbook used |
| Share context across terminals or coding agents | They use the same project notebook without merging their conversation histories |
| Control what is retained | `explicit` and `off` modes disable automatic conversation capture |

## What it deliberately does not do

- It does not give Claude the entire memory database on every request—only a bounded,
  relevant brief.
- It does not assume every sentence is a permanent truth. The background memory process
  selects durable facts, decisions, rules, preferences, and events.
- It does not treat remembered text as executable instructions. Retrieved text is marked
  as untrusted, and important claims can be checked against their evidence.
- It does not record Bash commands, file contents, edit payloads, or other tool results
  automatically; those often contain noise or secrets.
- It does not make L1–L3 memory appear instantly. Background processing takes time.
- It does not automatically promote a new coding playbook after positive feedback.
- It is not a secret store. Never remember credentials, tokens, or private keys.

## Test that it is working

Use a disposable Memseek workspace for the first end-to-end test. The cross-session test
creates real memory records. Keep the API and worker running, and use a real model: the
`LLM_FAKE=1` provider can validate transport and exact L0 capture, but it cannot produce
the L1–L3 ladder these steps check.

### Do not have a service yet?

[`docs/claude-code-plugin.md`](../../docs/claude-code-plugin.md) carries the whole
procedure from an empty machine: it starts PostgreSQL, the API, the worker, and a published
`agent_memory@0.3.0` catalog with one `docker compose up`, says where to get the model API
key and where to put it, and proves the service and that key work before Claude Code is
involved. In short:

```bash
git clone https://github.com/memseekai/memseek && cd memseek
printf 'OPENAI_API_KEY=sk-your-real-key\n' > .env
docker compose up -d --build --wait
cat .memseek/api_key          # the workspace key this plugin asks for
```

The service URL is then `http://127.0.0.1:8000`. Nothing but Docker is installed on the
host for the server side; the plugin's own hooks still need host `python3` 3.10 or newer.

### Five-minute customer test

You do not need to understand artifacts, hooks, or MCP to test the product behavior:

1. **Check the connection.** In Claude Code, run
   `/memseek-memory:memseek-status`. It should say the service is healthy and identify
   the current project memory.
2. **Teach one temporary rule.** Say:

   ```text
   For this test project, every distributed-cache key must start with orbit:.
   Treat this as a priority-90 coding rule until I revoke it.
   ```

3. **Wait for background learning.** Run
   `/memseek-memory:memseek-search distributed-cache key prefix` every few seconds until
   it returns the `orbit:` rule. This confirms the conversation became durable memory. On
   the Docker stack, `docker compose logs -f worker` shows the same thing happening as it
   happens: `"derivation":"l1_extract","status":"ok"`.
4. **Test a fresh session.** Close Claude Code, reopen it in the same repository, and ask:

   ```text
   Do not call a memory tool. What prefix must distributed-cache keys use in this project,
   and why?
   ```

5. **Check the explanation.** The answer should say `orbit:`. Run
   `/memseek-memory:memseek-explain` on that rule; Claude should show the original message
   as its evidence, not merely repeat the rule.

If all five steps pass, the important customer journey works: Claude saved a project rule,
the background memory model learned it, a new session received the relevant memory brief,
and the remembered claim remained traceable to what you actually said.

If step 3 passes but step 4 fails, the memory exists and the automatic memory brief is the
problem. If step 3 fails, check the worker/model before debugging Claude Code.

### Detailed operator checks

The remaining checks expose each technical boundary separately.

#### A. Validate the package before starting Claude

From the repository root, validate both the marketplace and plugin manifests:

```bash
claude plugin validate . --strict
claude plugin validate ./integrations/claude-code --strict
```

Run the diagnostic directly or invoke `/memseek-memory:memseek-status`:

```bash
python3 integrations/claude-code/scripts/memseek_doctor.py status --json
```

A healthy report has:

- `ok: true`;
- `health.ok: true` and `health.db: true`;
- `api_key: "configured"`;
- the intended project `entity`;
- all seven expected tools and an empty `missing_recommended_tools` list;
- zero pending and quarantined writes.

If this step fails, Claude hooks cannot repair the server configuration. Resolve the
reported key, health, package, or tool-contract problem first.

#### B. Verify that Claude loaded every integration component

Start a fresh debug session:

```bash
claude --debug --plugin-dir ./integrations/claude-code
```

The debug output should show the `memseek-memory` plugin, its hooks and skills, and the
plugin-provided `memseek` MCP server. At startup Claude should display a message like
`Memseek connected for <entity>`. Inside Claude:

1. Run `/mcp` and confirm `memseek` is connected with seven tools.
2. Type `/memseek-memory:` and confirm the five Memseek skills are offered.
3. Run `/memseek-memory:memseek-status`; it should report the same entity and healthy
   contract as the shell diagnostic.

#### C. Verify automatic context binding

Submit any ordinary prompt, for example:

```text
Summarize the boundaries I should preserve before changing this repository.
```

The prompt should proceed even if memory is empty. Confirm that the hook registered an
artifact use without printing the stored prompt:

```bash
python3 - <<'PY'
import json
from pathlib import Path

directory = Path.home() / ".memseek" / "plugin" / "claude-code" / "sessions"
latest = max(directory.glob("*.json"), key=lambda path: path.stat().st_mtime)
state = json.loads(latest.read_text())
print(json.dumps({
    "session_id": state.get("session_id"),
    "entity": state.get("entity"),
    "last_use_id": state.get("last_use_id"),
    "render_truncated": state.get("last_render_truncated"),
}, indent=2))
PY
```

`last_use_id` must be a UUID. Its presence proves that `UserPromptSubmit` successfully
bound a context artifact; an absent value means the prompt hook failed open. If
`MEMSEEK_PLUGIN_STATE_DIR` is set, inspect the `state_dir` reported by the doctor instead
of the default path in this command.

#### D. Verify exact L0 capture immediately

In Claude, send a unique but non-sensitive test message:

```text
For the Memseek smoke test, remember this exact marker: ORBIT-CACHE-17.
```

Wait for Claude's response, then ask:

```text
Use Memseek replay_session with the project entity and conversation session from startup.
Confirm that ORBIT-CACHE-17 appears in a user message and that your response appears after it.
```

Wait a few seconds for both asynchronous hooks. This tests the automatic user and assistant
write hooks independently of semantic recall. The replay should return both roles in
ordinal order. If it does not, check that capture mode is `conversation`, the worker is
running, and `pending_writes` is zero.

#### E. Verify cross-session derived recall

Tell Claude one meaningful temporary rule, so the extraction model has something durable
to derive:

```text
For this test project, every distributed-cache key must start with orbit:.
Treat this as a priority-90 coding rule until I revoke it.
```

Because derivation is asynchronous, first use
`/memseek-memory:memseek-search distributed-cache key prefix` until the rule is returned;
if it never appears, inspect the worker before testing the hook. Then exit Claude and start
a new session in the same repository. Without invoking a memory skill or tool, ask:

```text
Do not call a memory tool. Based only on context supplied before this request,
what prefix must distributed-cache keys use here? Cite the memory evidence you received.
```

A successful answer says `orbit:` and cites Memseek evidence. This is the strongest smoke
test: it proves automatic capture, asynchronous derivation, stable project identity,
cross-session retrieval, and prompt-context injection. If the explicit search still finds
the rule but this prompt fails, storage and retrieval work and the fault is isolated to
automatic context binding or injection.

#### F. Verify provenance and feedback

Run `/memseek-memory:memseek-explain` on the recalled rule. The explanation should
distinguish the derived rule from the literal L0 message and include record ids.

Then attach an outcome to the context used for the latest request:

```text
/memseek-memory:memseek-feedback task_success The smoke-test rule was recalled and cited correctly.
```

Success reports an artifact-use id, at least one sent event, no errors, and zero remaining
writes. This proves feedback was attached to a real render; it does not mean Memseek
automatically promoted a procedure.

#### G. Optionally test outage recovery

Only do this in a disposable environment:

1. Stop the Memseek API while leaving Claude open.
2. Submit a non-sensitive prompt and let Claude respond.
3. Run the doctor; `pending_writes` should be greater than zero while Claude itself remains
   usable.
4. Restart the API and run:

   ```bash
   python3 integrations/claude-code/scripts/memseek_doctor.py flush
   ```

5. Expect `remaining: 0` and an empty `errors` list, then replay the session to confirm the
   queued messages arrived once.

This verifies fail-open behavior, atomic local spooling, and server-side deduplication.

## Local development and self-hosted setup

The marketplace installation above needs no shell exports. This section is only for plugin
developers and operators creating a new self-hosted workspace.

Load the plugin directly from this checkout:

```bash
claude --plugin-dir ./integrations/claude-code
```

Claude Code still asks for the service URL, workspace key, and capture policy.

Before customers connect to a newly created self-hosted workspace, an operator must enable
the included memory model once. Export the two values used by the standalone publishing
script:

```bash
export MEMSEEK_URL="http://127.0.0.1:8000"
export MEMSEEK_API_KEY="replace-with-workspace-key"
```

Preview the change, then apply it:

```bash
python3 integrations/claude-code/scripts/publish_agent_memory.py
python3 integrations/claude-code/scripts/publish_agent_memory.py --apply
```

The first command is read-only. The second selects `agent_memory@0.3.0` for the workspace.
The Memseek API and worker must both be running. Operators can publish an equivalent
production package if it preserves the documented memory and tool contract.

## Identity and cross-agent sharing

The default entity is derived from a credential-stripped, normalized `origin` URL. If no
origin exists, it hashes the absolute Git root. Only a short hash and repository name are
sent as the entity; the raw remote and local path are not sent.

Pin an explicit, non-secret identity when multiple checkouts or coding agents should share
one memory. Add `.memseek-project.json` to the project:

```json
{
  "entity": "project:payments-api",
  "skill_entity": "skill:payments-api:coding"
}
```

An environment override wins over this file. Keep the same entity in a future Codex
integration to share durable project state while retaining distinct conversation session
ids for exact replay.

## Advanced configuration

The installer prompts for the first three settings. Standalone operator scripts also
accept shell variables with the same names, which is useful for automation. Inside Claude
Code, the prompted plugin settings remain authoritative so the hooks and MCP connection
cannot accidentally use different credentials.

| Setting or environment variable | Default | Purpose |
|---|---|---|
| `MEMSEEK_URL` | `http://127.0.0.1:8000` | Base URL, without `/mcp` or a trailing slash |
| `MEMSEEK_API_KEY` | none | Sensitive workspace key |
| `MEMSEEK_CAPTURE_MODE` | `conversation` | Controls new writes from Claude Code, not recall; see [Choose a capture mode](#choose-a-capture-mode) |
| `MEMSEEK_ENTITY` | derived from Git | Overrides `.memseek-project.json` project entity |
| `MEMSEEK_SKILL_ENTITY` | `skill:<entity>:coding` | Maintained procedure entity |
| `MEMSEEK_CONTEXT_ARTIFACT` | `agent_context` | Artifact bound before each prompt |
| `MEMSEEK_TIMEOUT_SECONDS` | `5` | Hook HTTP timeout, maximum 30 seconds |
| `MEMSEEK_PLUGIN_STATE_DIR` | `~/.memseek/plugin/claude-code` | Session state and retry queue |

The installer exposes `MEMSEEK_CAPTURE_MODE` as the **Conversation capture** question.
Change it through `/plugin`, then start a new Claude Code session so every hook receives
the same policy. See [Choose a capture mode](#choose-a-capture-mode) for the behavior and
tradeoffs of each value.

## Reliability and privacy

- Every hook exits successfully on configuration, network, or server failure so memory
  cannot block coding.
- Writes use stable dedupe keys. Failed writes are atomically queued under `pending/` and
  retried by later hooks; validation failures move to `failed/` for inspection.
- State directories use mode `0700` and state files use `0600` where the OS supports it.
- Session state contains the last task because `PreCompact` must rebuild its anchor. Queue
  files can contain exact user or assistant messages until delivery succeeds.
- Claude Code stores the prompted workspace key as a sensitive plugin option, normally in
  the system keychain. The plugin never writes it to session state, queues, or logs.
- Retrieved records are wrapped and labeled as untrusted data. The plugin tells Claude to
  open citations before relying on consequential claims.
- Feedback creates an ordinary learning signal. It never auto-promotes a new live rule or
  procedure.

To retry queued writes:

```bash
python3 integrations/claude-code/scripts/memseek_doctor.py flush
```

To remove local plugin state, uninstall the plugin first, inspect the state directory named
by `memseek-status`, and then delete that exact directory. Removing local state does not
erase records already stored by Memseek; use the server's provenance-aware erasure workflow
for stored data.

## Troubleshooting

| Symptom | Check |
|---|---|
| Claude Code did not ask for settings | Update Claude Code, reinstall or re-enable the plugin, and answer all three configuration prompts |
| MCP is unauthorized | Reconfigure the plugin with the workspace key supplied by your administrator, then start a new session |
| Hooks work but MCP does not | Check that the configured service URL has no trailing slash and does not include `/mcp`; then use `/mcp` for connection details |
| Context is empty | Confirm the entity, start the worker, and inspect `recall` directly |
| Recommended tools are missing | Publish/select `agent_memory@0.3.0` or its compatible replacement |
| Writes remain queued | Restore connectivity/auth, then run `memseek_doctor.py flush` |
| A write is quarantined | Inspect `failed/<hash>.json`; the server rejected its schema or target |
| The wrong project memory appears | Pin `MEMSEEK_ENTITY` or `.memseek-project.json`, then restart |
