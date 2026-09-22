"""A deterministic harness for tests: it does what a real one would, without a model.

It takes fewer steps the more it was given: two fewer with a playbook mounted,
and one fewer when a skill pack's saved helpers survived from an earlier run.
That is what lets the learning eval assert its deltas as literal values.
"""

import json
import os
from pathlib import Path

root = Path.cwd()
request = json.loads((root / ".harness" / "input.json").read_text())
playbook = any((Path(skill["dir"]) / "PLAYBOOK.md").is_file() for skill in request["skills"])
state = Path(os.environ["ECHO_STATE"]) if os.environ.get("ECHO_STATE") else None
helpers = state is not None and (state / "helpers.txt").is_file()
if state is not None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "helpers.txt").write_text("saved by an earlier run\n")

steps = 5 - (2 if playbook else 0) - (1 if helpers else 0)
citations = list(request["citation_ids"][:1])
if citations:
    learning = {
        "text": "[echo-pack/extraction] Stories are tr.athing rows.",
        "content": {
            "pack": "echo-pack",
            "kind": "extraction",
            "detail": "Stories are tr.athing rows; points are in the next row.",
            "helper_code": None,
        },
        "citations": citations,
    }
    with (root / "outbox" / "learnings.jsonl").open("a") as handle:
        handle.write(json.dumps(learning) + "\n")

print(
    json.dumps(
        {
            "value": {
                "items": [{"title": f"story {index}", "points": index} for index in range(30)],
                "task": request["task"],
                "model": request["model"],
                "model_key": os.environ.get("ECHO_MODEL_KEY"),
                "leaked": os.environ.get("ECHO_LEAK"),
            },
            "citation_ids": citations,
            "steps": steps,
            "awaiting_input": False,
            "events": [
                {"kind": "model_step", "payload": {"index": index}} for index in range(steps)
            ],
            "metrics": {
                "wall_s": 0.5,
                "steps": steps,
                "tool_calls": steps - 1,
                "tool_errors": 0,
                "input_tokens": 1000 * steps,
                "output_tokens": 100 * steps,
                "cost_usd": None,
            },
        }
    )
)
