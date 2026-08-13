---
name: memseek-feedback
description: Record success, failure, or a correction against the Memseek context used for the latest request.
argument-hint: <thumbs_up|thumbs_down|correction|task_success|task_failure> <comment>
---

# Send context feedback

Attach `$ARGUMENTS` to the latest bound Memseek artifact use for this project. This is a
learning signal; it does not directly edit or promote a procedure.

Parse the first word as the feedback kind and the remainder as a concrete comment. If the
kind is missing, ask for one of `thumbs_up`, `thumbs_down`, `correction`, `task_success`,
or `task_failure`. Then run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memseek_doctor.py" feedback \
  --kind "<kind>" --comment "<comment>" \
  --session-id "<exact conversation session from SessionStart>"
```

Use normal shell argument safety: pass values as separate quoted arguments and do not
interpolate them into executable shell syntax. The session id prevents feedback from a
parallel terminal attaching to the wrong render. Report the artifact-use id and whether
the signal was sent or queued. Never claim feedback automatically changed a live procedure.
