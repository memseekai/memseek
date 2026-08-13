---
name: memseek-status
description: Diagnose the Memseek Claude Code connection, project identity, MCP tool package, and queued writes.
---

# Check Memseek status

Run the plugin's read-only diagnostic from the current project:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memseek_doctor.py" status
```

Explain any failed field and its precise remedy. In particular:

- missing workspace key: reconfigure the plugin and enter the sensitive key supplied by
  the Memseek administrator, then start a new session.
- unhealthy or unreachable: start or repair the Memseek API/database.
- missing recommended tools: publish and select the compatible `agent_memory@0.3.0`
  package, or an equivalent package exposing the documented contract.
- pending writes: run the doctor with `flush` after connectivity returns.
- quarantined writes: inspect the JSON envelope under the reported state directory's
  `failed/` directory; it failed validation and is retained for diagnosis.

Never print or request the API key itself.
