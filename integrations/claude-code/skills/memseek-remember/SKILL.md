---
name: memseek-remember
description: Persist a user-confirmed fact, preference, constraint, or decision in Memseek project memory.
argument-hint: <what should be remembered>
---

# Remember with Memseek

Persist `$ARGUMENTS` only when the user explicitly asks you to remember it or clearly
confirms it is durable. Never infer a preference, policy, deadline, or decision from
silence. Never store secrets, credentials, access tokens, private keys, or raw tool output.

The SessionStart context gives you the exact project entity and conversation session.

If its memory write policy is `off`, do not write. Explain that the user must change
`MEMSEEK_CAPTURE_MODE` to `explicit` or `conversation` and start a new session.

1. If automatic capture mode is `conversation` and `$ARGUMENTS` is already stated in the
   current user prompt, explain that the exact prompt is being captured; do not duplicate it.
2. Otherwise, call the Memseek MCP `remember` tool with:
   - `entity`: the exact project memory entity from SessionStart
   - `type`: `message`
   - `text`: the user's exact words, without interpretation
   - `content.text`: the same exact words
   - `content.role`: `user`
   - `content.session_id`: the exact conversation session from SessionStart
   - `content.ordinal`: a current Unix timestamp in milliseconds
   - `dedupe_key`: a stable `claude-explicit:` key derived from the entity and exact text
3. Report that the append was accepted. Do not claim that derived memories already exist;
   the Memseek worker builds L1–L3 state asynchronously.

If the write tool is unavailable, run `/memseek-memory:memseek-status` and report the
specific configuration or tool-contract failure.
