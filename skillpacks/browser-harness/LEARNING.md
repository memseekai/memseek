## Recording what you learned

This run is expected to leave the next run on this site better off. Before you
return your final answer, record each thing you learned about this site that a
later run would otherwise have to rediscover. Record at most 8 learnings, and
skip anything specific to today's data. If a playbook learning was wrong or out
of date, record a `pitfall` that says so and what is true now.

Record them with the learnings tool named in your instructions. One call can
carry several. The tool checks each entry and, when it rejects a call, says what
to fix: fix it and call again. Each entry looks like this:

```json
{"text": "[browser-harness/extraction] Stories are tr.athing rows; points live in the next row's span.score.", "content": {"pack": "browser-harness", "kind": "extraction", "detail": "Stories are tr.athing rows; points live in the next row's span.score.", "helper_code": "rows = page.query_selector_all('tr.athing')"}, "citations": ["<task record UUID>"]}
```

- `kind` is one of `navigation`, `extraction`, `pitfall`, or `helper`.
- `text` starts with `[browser-harness/<kind>] ` and stays on one line, under 400
  characters. It is the part the next run reads, so make it self-contained.
- `detail` is the same learning, and it may be longer.
- `helper_code` is optional. Use it for a short snippet that worked, and leave
  the key out when there is none.
- `citations` holds the UUID of the scrape task record from your context. Put
  the same UUID in your final `citation_ids`.

If no learnings tool is offered, append the same entries to
`../outbox/learnings.jsonl`, one JSON object per line. Record nothing on a turn
that sets `awaiting_input`.
