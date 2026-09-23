"""The local provider end to end: invocation, harness, skill pack, learnings, playbook."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from site_scrape_fixture import REPOSITORY_ROOT, ScrapeWorkspace, site_scrape_settings

from memseek.config import Settings
from memseek.db import DatabasePool

ENTITY = "site:news.ycombinator.com"
URL = "https://news.ycombinator.com/"
PROMPT = "Scrape the front page."
PLAYBOOK_ROW = "- Stories are tr.athing rows. (id {id})"


@pytest.fixture
async def site(settings: Settings, db_pool: DatabasePool, tmp_path: Path) -> ScrapeWorkspace:
    workspace = ScrapeWorkspace(db_pool, site_scrape_settings(settings, tmp_path), "scrape-test")
    await workspace.create()
    return workspace


def _skill_dir(invocation: dict[str, Any]) -> Path:
    return Path(invocation["result"]["receipt"]["root"]) / ".agents/skills/echo-pack"


async def test_a_run_writes_learnings_that_the_next_run_reads(
    site: ScrapeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECHO_MODEL_KEY", "k-123")
    monkeypatch.setenv("ECHO_LEAK", "must not reach the harness")
    task_id = await site.write_task(ENTITY, URL, "top 30 stories")

    first = await site.invoke(ENTITY, PROMPT)

    assert first["status"] == "succeeded", first["error"]
    result = first["result"]
    assert result["steps"] == 5
    assert result["citation_ids"] == [str(task_id)]
    assert len(result["value"]["items"]) == 30
    assert result["value"]["task"] == PROMPT
    assert result["value"]["model"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "params": {"max_output_tokens": 8000},
    }
    assert result["value"]["model_key"] == "k-123"
    assert result["value"]["leaked"] is None
    receipt = result["receipt"]
    assert receipt["provider"] == "local"
    assert receipt["harness"] == {"name": "echo", "version": 3}
    assert receipt["skillpacks"] == [{"name": "echo-pack", "version": 2}]
    assert (receipt["learning"], receipt["native"]) == ("read_write", "read_write")
    assert receipt["metrics"] == {
        "wall_s": 0.5,
        "steps": 5,
        "tool_calls": 4,
        "tool_errors": 0,
        "input_tokens": 5000,
        "cache_read_tokens": 20000,
        "cache_write_tokens": 0,
        "output_tokens": 500,
        "cost_usd": None,
    }
    assert "outbox" not in receipt
    harness_input = json.loads((Path(receipt["root"]) / ".harness/input.json").read_text())
    system_prompt = harness_input["system_prompt"]
    assert (
        "The only files you may write in /outbox are: /outbox/learnings.jsonl. Any other file "
        "there fails the run. Return your answer only in the final JSON object, never as a file."
    ) in system_prompt.split("\n\n")
    assert (
        "Before your final answer, record what you learned about this site: follow the "
        '"Recording what you learned" section of the echo-pack skill.'
    ) in system_prompt.split("\n\n")
    assert not (_skill_dir(first) / "PLAYBOOK.md").exists()

    learnings = await site.learnings(ENTITY)
    assert learnings == [
        {
            "content": {
                "text": "[echo-pack/extraction] Stories are tr.athing rows.",
                "pack": "echo-pack",
                "kind": "extraction",
                "detail": "Stories are tr.athing rows; points are in the next row.",
                "helper_code": None,
            },
            "derived_from": [task_id],
            "status": "active",
        }
    ]

    second = await site.invoke(ENTITY, PROMPT)

    assert second["status"] == "succeeded", second["error"]
    assert second["result"]["steps"] == 2
    playbook = (_skill_dir(second) / "PLAYBOOK.md").read_text()
    async with site.pool.connection() as conn:
        row = await (
            await conn.execute(
                "select id from record where collection = 'skill_learnings' and entity = %s "
                "order by seq limit 1",
                (ENTITY,),
            )
        ).fetchone()
    assert row is not None
    assert PLAYBOOK_ROW.format(id=row["id"]) in playbook.splitlines()
    skill = (_skill_dir(second) / "SKILL.md").read_text()
    assert skill.index("## Start from the playbook") < skill.index("## Recording what you learned")
    second_input = json.loads(
        (Path(second["result"]["receipt"]["root"]) / ".harness/input.json").read_text()
    )
    assert (
        "## What earlier runs learned about this site\n\n"
        "This is the echo-pack skill's PLAYBOOK.md. Start from it. Try its URLs, selectors, "
        "and pitfalls before you explore, and explore only what it does not cover. When a "
        "learning turns out to be wrong, say so in a new learning.\n\n"
        f"{playbook.strip()}"
    ) in second_input["system_prompt"]


async def test_learning_off_hides_the_playbook_and_drops_learnings(site: ScrapeWorkspace) -> None:
    await site.write_task(ENTITY, URL, "top 30 stories")
    await site.invoke(ENTITY, PROMPT)

    cold = await site.invoke(ENTITY, PROMPT, {"learning": "off", "native": "off"})

    assert cold["status"] == "succeeded", cold["error"]
    cold_input = json.loads(
        (Path(cold["result"]["receipt"]["root"]) / ".harness/input.json").read_text()
    )
    assert (
        "Write nothing to /outbox. Return your answer only in the final JSON object, never as "
        "a file."
    ) in cold_input["system_prompt"].split("\n\n")
    assert "Recording what you learned" not in cold_input["system_prompt"]
    assert cold["result"]["steps"] == 5
    root = Path(cold["result"]["receipt"]["root"])
    assert not (root / ".memseek/playbook.md").exists()
    assert not (_skill_dir(cold) / "PLAYBOOK.md").exists()
    assert len(await site.learnings(ENTITY)) == 1


async def test_learning_read_mounts_the_playbook_and_writes_nothing(site: ScrapeWorkspace) -> None:
    await site.write_task(ENTITY, URL, "top 30 stories")
    await site.invoke(ENTITY, PROMPT)

    reader = await site.invoke(ENTITY, PROMPT, {"learning": "read", "native": "off"})

    assert reader["status"] == "succeeded", reader["error"]
    assert reader["result"]["steps"] == 3
    assert "Recording what you learned" not in (_skill_dir(reader) / "SKILL.md").read_text()
    assert len(await site.learnings(ENTITY)) == 1


async def test_kept_native_state_survives_between_runs_and_read_does_not_change_it(
    site: ScrapeWorkspace,
) -> None:
    await site.write_task(ENTITY, URL, "top 30 stories")
    off = {"learning": "off"}

    fresh = await site.invoke(ENTITY, PROMPT, {**off, "native": "off"})
    read_before_any_write = await site.invoke(ENTITY, PROMPT, {**off, "native": "read"})
    writer = await site.invoke(ENTITY, PROMPT, {**off, "native": "read_write"})
    kept = await site.invoke(ENTITY, PROMPT, {**off, "native": "read_write"})
    reader = await site.invoke(ENTITY, PROMPT, {**off, "native": "read"})

    assert [
        run["result"]["steps"] for run in (fresh, read_before_any_write, writer, kept, reader)
    ] == [5, 5, 5, 4, 4]


async def test_an_undeclared_outbox_file_fails_the_run(
    site: ScrapeWorkspace, tmp_path: Path
) -> None:
    rogue = tmp_path / "harnesses" / "echo"
    rogue.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "harnesses" / "echo"
    (rogue / "harness.yaml").write_text((fixture / "harness.yaml").read_text())
    (rogue / "run.py").write_text(
        "from pathlib import Path\n"
        "Path('outbox/notes.txt').write_text('x')\n" + (fixture / "run.py").read_text()
    )
    await site.write_task(ENTITY, URL, "top 30 stories")

    failed = await site.invoke(ENTITY, PROMPT)

    assert failed["status"] == "failed"
    assert failed["error"] == {
        "kind": "validation",
        "detail": "unknown outbox files: /outbox/notes.txt",
    }
    assert await site.learnings(ENTITY) == []


async def test_a_missing_requirement_fails_with_its_install_hint(
    site: ScrapeWorkspace, tmp_path: Path
) -> None:
    needy = tmp_path / "harnesses" / "echo"
    needy.mkdir(parents=True)
    (needy / "harness.yaml").write_text(
        "name: echo\nversion: 9\nentry: [python3, run.py]\nskills_dir: skills\n"
        "requires: [{bin: memseek-no-such-bin, install: 'brew install memseek-no-such-bin'}]\n"
    )
    await site.write_task(ENTITY, URL, "top 30 stories")

    failed = await site.invoke(ENTITY, PROMPT)

    assert failed["status"] == "failed"
    assert failed["error"] == {
        "kind": "capability",
        "detail": "harness 'echo' requires 'memseek-no-such-bin' "
        "(install: brew install memseek-no-such-bin)",
    }


FAKE_PI = """#!/bin/sh
printf '%s\\n' "$@" > ../.harness/pi-argv
cat <<'JSONL'
{"type":"session","version":3}
{"type":"message_end","message":{"role":"assistant","content":[{"type":"toolCall","id":"1","name":"bash","arguments":{}}],"usage":{"input":100,"output":20,"cacheRead":10,"cacheWrite":0,"cost":{"total":0.001}},"stopReason":"toolUse"}}
{"type":"tool_execution_end","toolCallId":"1","toolName":"bash","result":{},"isError":true}
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"```json\\n{\\"value\\": {\\"items\\": []}, \\"citation_ids\\": [], \\"awaiting_input\\": false}\\n```"}],"usage":{"input":200,"output":40,"cacheRead":0,"cacheWrite":5,"cost":{"total":0.002}},"stopReason":"stop"}}
JSONL
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="the pi harness needs node on PATH")
async def test_the_pi_harness_runs_under_the_local_provider(
    site: ScrapeWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped run.mjs, with a stand-in `pi` that prints canned JSONL."""

    harness = tmp_path / "harnesses" / "echo"
    harness.mkdir(parents=True)
    (harness / "harness.yaml").write_text(
        (REPOSITORY_ROOT / "harnesses/pi/harness.yaml")
        .read_text()
        .replace("name: pi", "name: echo")
    )
    shutil.copy(REPOSITORY_ROOT / "harnesses/pi/run.mjs", harness / "run.mjs")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pi").write_text(FAKE_PI)
    (bin_dir / "pi").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    await site.write_task(ENTITY, URL, "top 30 stories")

    run = await site.invoke(ENTITY, PROMPT)

    assert run["status"] == "succeeded", run["error"]
    result = run["result"]
    assert (result["value"], result["steps"]) == ({"items": []}, 2)
    metrics = result["receipt"]["metrics"]
    assert {key: value for key, value in metrics.items() if key != "wall_s"} == {
        "steps": 2,
        "tool_calls": 1,
        "tool_errors": 1,
        "input_tokens": 300,
        "cache_read_tokens": 10,
        "cache_write_tokens": 5,
        "output_tokens": 60,
        "cost_usd": 0.003,
    }
    root = Path(result["receipt"]["root"])
    argv = (root / ".harness/pi-argv").read_text().splitlines()
    assert argv == [
        "--mode",
        "json",
        "--no-session",
        "--provider",
        "anthropic",
        "--model",
        "claude-sonnet-4-5",
        "--append-system-prompt",
        str(root / ".harness/system-prompt.md"),
        "--no-skills",
        "--skill",
        str(root / ".agents/skills/echo-pack"),
        "--",
        PROMPT,
    ]


async def test_invalid_run_options_are_refused(site: ScrapeWorkspace) -> None:
    await site.write_task(ENTITY, URL, "top 30 stories")

    failed = await site.invoke(ENTITY, PROMPT, {"learning": "sometimes"})

    assert failed["status"] == "failed"
    assert failed["error"]["kind"] == "validation"
    assert failed["error"]["detail"].startswith("invalid run options:")


async def test_cloudflare_refuses_skill_packs_and_harnesses_before_calling_out(
    settings: Settings, db_pool: DatabasePool, tmp_path: Path
) -> None:
    scrape_settings = site_scrape_settings(settings, tmp_path)
    assert scrape_settings.computers_dir is not None
    computer = scrape_settings.computers_dir / "scrape_workspace.yaml"
    computer.write_text(computer.read_text().replace("provider: local", "provider: cloudflare"))
    toolset = scrape_settings.toolsets_dir
    assert toolset is not None
    workspace = ScrapeWorkspace(db_pool, scrape_settings, "scrape-cloudflare")
    await workspace.create()
    await workspace.write_task(ENTITY, URL, "top 30 stories")

    failed = await workspace.invoke(ENTITY, PROMPT)

    assert failed["error"] == {
        "kind": "capability",
        "detail": "skillpack tool 'browser' is not yet executable on cloudflare",
    }

    (toolset / "scraper.yaml").write_text(
        (toolset / "scraper.yaml")
        .read_text()
        .replace("      - name: browser\n        kind: skillpack\n        pack: echo-pack\n", "")
    )
    without_pack = ScrapeWorkspace(db_pool, scrape_settings, "scrape-cloudflare-2")
    await without_pack.create()
    await without_pack.write_task(ENTITY, URL, "top 30 stories")

    failed = await without_pack.invoke(ENTITY, PROMPT)

    assert failed["error"] == {
        "kind": "capability",
        "detail": "harness 'echo' is not yet executable on cloudflare",
    }
