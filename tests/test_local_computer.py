"""The local provider end to end: invocation, harness, skill pack, learnings, playbook."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from site_scrape_fixture import REPOSITORY_ROOT, ScrapeWorkspace, site_scrape_settings

from memseek.config import Settings
from memseek.db import DatabasePool

ENTITY = "site:news.ycombinator.com"
URL = "https://news.ycombinator.com/"
PROMPT = "Scrape the front page."


@pytest.fixture
async def site(settings: Settings, db_pool: DatabasePool, tmp_path: Path) -> ScrapeWorkspace:
    workspace = ScrapeWorkspace(db_pool, site_scrape_settings(settings, tmp_path), "scrape-test")
    await workspace.create()
    return workspace


def _gone(path: str) -> bool:
    return not Path(path).exists()


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
    # The scraper alias is whatever the example catalog names; the harness gets its first target.
    alias = site.catalog.models.aliases["scraper"]
    provider, _, model = alias.targets[0].partition(":")
    assert result["value"]["model"] == {
        "provider": provider,
        "model": model,
        "params": alias.params,
    }
    assert result["value"]["model_key"] == "k-123"
    assert result["value"]["leaked"] is None
    runtime = result["value"]["runtime"]
    assert result["value"]["runtime_exists"] is True
    # A browser daemon's socket goes here, and macOS caps socket paths at 104 bytes.
    assert len(runtime) < 40
    assert _gone(runtime)
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
    root = receipt["root"]
    assert (
        f"The only files you may write in {root}/outbox are: {root}/outbox/lessons.jsonl "
        "(only through the record_lessons tool). Anything else there is discarded. "
        "Return your answer only in the final JSON object, never as a file."
    ) in system_prompt.split("\n\n")
    assert (
        f"The Computer root is {root}, and every path in these instructions is already "
        f"absolute on this machine. Your working directory is {root}/workspace, so use "
        "relative paths for your working files."
    ) in system_prompt.split("\n\n")
    # Every Computer path is named where it really is; the agent opens them literally.
    assert re.findall(r"(?<![\w.:/-])/(?:\.memseek|outbox|workspace|inputs)\b", system_prompt) == []
    # A live run mangled a 64-hex root it had to copy; no path it sees has one.
    assert re.findall(r"[0-9a-f]{16,}", system_prompt) == []
    assert (
        "Before your final answer, record what you learned using the echo-pack skill: call the "
        "record_lessons tool, following the recording instructions at the end of each "
        "skill's SKILL.md. If the tool rejects a call, fix what it names and call it again."
    ) in system_prompt.split("\n\n")
    # echo-pack declares no lessons, so it records the built-in kinds.
    first_skill = (_skill_dir(first) / "SKILL.md").read_text()
    recording = first_skill[first_skill.index("## Recording lessons for echo-pack") :]
    assert [line.split(":")[0] for line in recording.splitlines() if line.startswith("- `")][
        :3
    ] == ["- `helper`", "- `tip`", "- `pitfall`"]
    assert "- A `helper` lesson must carry its code in `content.code`." in recording
    assert not (_skill_dir(first) / "PLAYBOOK.md").exists()
    # read_write keeps the pack's state per entity, beside the session roots.
    [stopped] = Path(receipt["root"]).parent.glob("_native/*/echo-pack/stopped")
    assert stopped.read_text() == f"stopped with {runtime}\n"

    learnings = await site.learnings(ENTITY)
    assert learnings == [
        {
            "content": {
                "text": "Stories are tr.athing rows.",
                "skill": "echo-pack",
                "kind": "tip",
                "detail": "Stories are tr.athing rows; points are in the next row.",
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
                "select id from record where collection = 'lessons' and entity = %s "
                "order by seq limit 1",
                (ENTITY,),
            )
        ).fetchone()
    assert row is not None
    # Only the kinds with lessons appear, each under its description.
    assert playbook == (
        "# Playbook: echo-pack\n\n"
        "What earlier runs learned using this skill here, newest first within each kind. "
        "Start from these instead of rediscovering them, and explore only what they do not "
        "cover.\n\n"
        "## tip\n\nSomething that worked and is worth doing again.\n\n"
        f"- Stories are tr.athing rows. (id {row['id']})\n"
    )
    skill = (_skill_dir(second) / "SKILL.md").read_text()
    assert skill.index("## Start from the playbook") < skill.index("## Recording lessons for")
    second_input = json.loads(
        (Path(second["result"]["receipt"]["root"]) / ".harness/input.json").read_text()
    )
    assert (f"## The echo-pack skill's PLAYBOOK.md\n\n{playbook.strip()}") in second_input[
        "system_prompt"
    ]
    # Installed beside the skill only; no playbook is left anywhere in /.memseek.
    second_root = Path(second["result"]["receipt"]["root"])
    assert list((second_root / ".memseek").rglob("PLAYBOOK.md")) == []


async def test_learning_off_hides_the_playbook_and_drops_learnings(site: ScrapeWorkspace) -> None:
    await site.write_task(ENTITY, URL, "top 30 stories")
    await site.invoke(ENTITY, PROMPT)

    cold = await site.invoke(ENTITY, PROMPT, {"learning": "off", "native": "off"})

    assert cold["status"] == "succeeded", cold["error"]
    cold_input = json.loads(
        (Path(cold["result"]["receipt"]["root"]) / ".harness/input.json").read_text()
    )
    cold_root = cold["result"]["receipt"]["root"]
    assert (
        f"Write nothing to {cold_root}/outbox. Return your answer only in the final JSON "
        "object, never as a file."
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


async def test_a_bad_outbox_costs_only_itself(site: ScrapeWorkspace, tmp_path: Path) -> None:
    rogue = tmp_path / "harnesses" / "echo"
    rogue.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "harnesses" / "echo"
    (rogue / "harness.yaml").write_text((fixture / "harness.yaml").read_text())
    # The shape a live run wrote by hand, which ingestion refuses.
    bad_line = '{"learning": "HN rows are tr.athing", "site": "news.ycombinator.com"}'
    # Valid for the collection, but filed where no playbook reads it: under the
    # site's name, under a kind echo-pack does not declare, and a helper without code.
    misfiled = "\n".join(
        json.dumps({"text": "Rows are tr.athing.", "content": content, "citations": ["{task_id}"]})
        for content in (
            {"skill": "news.ycombinator.com", "kind": "tip"},
            {"skill": "echo-pack", "kind": "extraction"},
            {"skill": "echo-pack", "kind": "helper"},
        )
    )
    (rogue / "run.py").write_text(
        "from pathlib import Path\n"
        "Path('outbox/notes.txt').write_text('x')\n"
        "import json as _json\n"
        "_task = _json.loads(Path('.harness/input.json').read_text())['citation_ids'][0]\n"
        f"Path('outbox/lessons.jsonl').write_text({bad_line!r} + '\\n' + "
        f"{misfiled!r}.replace('{{task_id}}', _task) + '\\n')\n" + (fixture / "run.py").read_text()
    )
    await site.write_task(ENTITY, URL, "top 30 stories")

    run = await site.invoke(ENTITY, PROMPT)

    assert run["status"] == "succeeded", run["error"]
    assert run["result"]["receipt"]["outbox_rejected"] == [
        {
            "path": "/outbox/lessons.jsonl",
            "line": 1,
            "reason": (
                "unknown keys ['learning', 'site']; an entry has only text, content, and "
                "citations; citations must be a non-empty list of authorized UUIDs; text must be a "
                "string"
            ),
        },
        {
            "path": "/outbox/lessons.jsonl",
            "line": 2,
            "reason": "skill: 'news.ycombinator.com' is not one of ['echo-pack']",
        },
        {
            "path": "/outbox/lessons.jsonl",
            "line": 3,
            "reason": "kind: 'extraction' is not one of ['helper', 'tip', 'pitfall']",
        },
        {
            "path": "/outbox/lessons.jsonl",
            "line": 4,
            "reason": "record: 'code' is a required property",
        },
        {"path": "/outbox/notes.txt", "reason": "not a declared writeback file"},
    ]
    assert [row["content"]["text"] for row in await site.learnings(ENTITY)] == [
        "Stories are tr.athing rows."
    ]


async def test_outbox_citations_count_even_when_the_answer_forgets_them(
    site: ScrapeWorkspace, tmp_path: Path
) -> None:
    forgetful = tmp_path / "harnesses" / "echo"
    forgetful.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "harnesses" / "echo"
    (forgetful / "harness.yaml").write_text((fixture / "harness.yaml").read_text())
    # The fixture, with its envelope's citations dropped: what a live run did
    # while its learnings still cited the task.
    (forgetful / "run.py").write_text(
        "import contextlib, io, json, runpy\n"
        "captured = io.StringIO()\n"
        "with contextlib.redirect_stdout(captured):\n"
        f"    runpy.run_path({str(fixture / 'run.py')!r})\n"
        "output = json.loads(captured.getvalue())\n"
        "output['citation_ids'] = []\n"
        "print(json.dumps(output))\n"
    )
    task_id = await site.write_task(ENTITY, URL, "top 30 stories")

    run = await site.invoke(ENTITY, PROMPT)

    assert run["status"] == "succeeded", run["error"]
    assert run["result"]["citation_ids"] == [str(task_id)]
    assert [row["derived_from"] for row in await site.learnings(ENTITY)] == [[task_id]]


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


def _harness(tmp_path: Path, run_py: str) -> None:
    harness = tmp_path / "harnesses" / "echo"
    harness.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "harnesses" / "echo"
    (harness / "harness.yaml").write_text((fixture / "harness.yaml").read_text())
    (harness / "run.py").write_text(run_py)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def test_a_spent_budget_fails_without_a_retry(site: ScrapeWorkspace, tmp_path: Path) -> None:
    _harness(
        tmp_path,
        "import sys\nsys.stderr.write('pi harness: exceeded max_wall_s 900\\n')\nsys.exit(2)\n",
    )
    await site.write_task(ENTITY, URL, "top 30 stories")
    invocation_id = await site.start(ENTITY, PROMPT)

    run = await site.attempt(invocation_id, final_attempt=False)

    assert (run["status"], run["error"]) == (
        "failed",
        {
            "kind": "budget",
            "detail": "harness 'echo' exited 2: pi harness: exceeded max_wall_s 900",
        },
    )
    assert [event["kind"] for event in await site.events(invocation_id)] == [
        "queued",
        "started",
        "failed",
    ]


async def test_an_interrupted_attempt_keeps_its_error(
    site: ScrapeWorkspace, tmp_path: Path
) -> None:
    _harness(
        tmp_path, "import sys\nsys.stderr.write('model 400: invalid request\\n')\nsys.exit(1)\n"
    )
    await site.write_task(ENTITY, URL, "top 30 stories")
    invocation_id = await site.start(ENTITY, PROMPT)

    run = await site.attempt(invocation_id, final_attempt=False)

    assert run["status"] == "queued"
    assert (await site.events(invocation_id))[-1] == {
        "kind": "execution_interrupted",
        "payload": {
            "error_kind": "provider",
            "error": "harness 'echo' exited 1: model 400: invalid request",
            "retryable": True,
        },
    }


async def test_the_agent_a_harness_started_ends_with_it(
    site: ScrapeWorkspace, tmp_path: Path
) -> None:
    pid_file = tmp_path / "agent.pid"
    _harness(
        tmp_path,
        "import subprocess, sys\n"
        "agent = subprocess.Popen(['sleep', '300'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(agent.pid))\n"
        "sys.exit(1)\n",
    )
    await site.write_task(ENTITY, URL, "top 30 stories")

    run = await site.invoke(ENTITY, PROMPT)

    assert run["status"] == "failed"
    agent = int(pid_file.read_text())
    assert await _until(lambda: not _alive(agent)), "the agent outlived its harness"


async def test_a_cancelled_run_stops_its_harness(site: ScrapeWorkspace, tmp_path: Path) -> None:
    pid_file = tmp_path / "harness.pid"
    _harness(
        tmp_path,
        f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(300)\n",
    )
    await site.write_task(ENTITY, URL, "top 30 stories")
    invocation_id = await site.start(ENTITY, PROMPT)
    attempt = asyncio.ensure_future(site.attempt(invocation_id, final_attempt=True))
    assert await _until(pid_file.exists), "the harness never started"

    attempt.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await attempt

    harness = int(pid_file.read_text())
    assert await _until(lambda: not _alive(harness)), "the harness outlived its cancelled run"


async def test_the_task_output_schema_binds_the_answer(site: ScrapeWorkspace) -> None:
    await site.write_task(ENTITY, URL, "top 30 stories")

    run = await site.invoke(ENTITY, PROMPT, output_schema={"type": "object", "required": ["rows"]})

    assert (run["status"], run["error"]) == (
        "failed",
        {"kind": "validation", "detail": "Agent output: 'rows' is a required property"},
    )


FAKE_PI = """#!/bin/sh
printf '%s\\n' "$@" > ../.harness/pi-argv
cat <<'JSONL'
{"type":"session","version":3}
{"type":"message_end","message":{"role":"assistant","content":[{"type":"toolCall","id":"1","name":"bash","arguments":{}}],"usage":{"input":100,"output":20,"cacheRead":10,"cacheWrite":0,"cost":{"total":0.001}},"stopReason":"toolUse"}}
{"type":"tool_execution_end","toolCallId":"1","toolName":"bash","result":{},"isError":true}
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"```json\\n{\\"value\\": {\\"items\\": []}, \\"citation_ids\\": [], \\"awaiting_input\\": false}\\n```"}],"usage":{"input":200,"output":40,"cacheRead":0,"cacheWrite":5,"cost":{"total":0.002}},"stopReason":"stop"}}
JSONL
"""


needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="the pi harness needs node on PATH"
)


def _pi_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_pi: str) -> Path:
    """The shipped run.mjs under the echo name, running `fake_pi` as pi."""

    harness = tmp_path / "harnesses" / "echo"
    harness.mkdir(parents=True)
    (harness / "harness.yaml").write_text(
        (REPOSITORY_ROOT / "harnesses/pi/harness.yaml")
        .read_text()
        .replace("name: pi", "name: echo")
    )
    shutil.copy(REPOSITORY_ROOT / "harnesses/pi/run.mjs", harness / "run.mjs")
    shutil.copy(REPOSITORY_ROOT / "harnesses/pi/memseek-tools.mjs", harness / "memseek-tools.mjs")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "pi").write_text(fake_pi)
    (bin_dir / "pi").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return harness


@needs_node
async def test_the_pi_harness_runs_under_the_local_provider(
    site: ScrapeWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped run.mjs, with a stand-in `pi` that prints canned JSONL."""

    harness = _pi_harness(tmp_path, monkeypatch, FAKE_PI)
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
        "--session-dir",
        str(root / ".harness/pi-sessions"),
        "--provider",
        "openai",
        "--model",
        "gpt-6-luna",
        "--append-system-prompt",
        str(root / ".harness/system-prompt.md"),
        "--no-skills",
        "--skill",
        str(root / ".agents/skills/echo-pack"),
        "--extension",
        str(harness / "memseek-tools.mjs"),
        "--",
        PROMPT,
    ]


# Each first call saves a new session and dies mid-turn. A resume answers only
# once the test allows it, so the first attempt fails and leaves its session.
DYING_PI = """#!/bin/sh
printf '%s\\n' "$@" >> ../.harness/pi-argv
case " $* " in
  *" --export "*) exit 0 ;;
  *" --session "*)
    [ -f "{may_answer}" ] || exit 1 ;;
  *)
    while [ "$1" != "--session-dir" ]; do shift; done
    n=$(ls "$2" | wc -l | tr -d ' ')
    echo '{"type":"session","version":3}' > "$2/2026-09-23T2${n}_s${n}.jsonl"
    exit 1 ;;
esac
cat <<'JSONL'
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"{\\"value\\": {\\"items\\": []}, \\"citation_ids\\": []}"}],"stopReason":"stop"}}
JSONL
"""


@needs_node
async def test_a_retried_attempt_resumes_its_own_session(
    site: ScrapeWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    may_answer = tmp_path / "pi-may-answer"
    _pi_harness(tmp_path, monkeypatch, DYING_PI.replace("{may_answer}", str(may_answer)))
    await site.write_task(ENTITY, URL, "top 30 stories")
    invocation_id = await site.start(ENTITY, PROMPT)
    assert (await site.attempt(invocation_id, final_attempt=False))["status"] == "queued"
    may_answer.touch()

    run = await site.attempt(invocation_id, final_attempt=True)

    assert run["status"] == "succeeded", run["error"]
    sessions = Path(run["result"]["receipt"]["root"]) / ".harness/pi-sessions"
    assert sorted(path.name for path in sessions.iterdir()) == [
        "2026-09-23T20_s0.jsonl",
        "2026-09-23T21_s1.jsonl",
    ]
    argv = (sessions.parent / "pi-argv").read_text().splitlines()
    resumed = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--session"]
    assert resumed[-1] == str(sessions / "2026-09-23T21_s1.jsonl")


# 61 model steps against the Agent's max_steps of 60.
ENDLESS_PI = "#!/bin/sh\n" + "\n".join(
    [
        "cat <<'JSONL'",
        *[
            '{"type":"message_end","message":{"role":"assistant","content":[],'
            '"stopReason":"toolUse"}}'
        ]
        * 61,
        "JSONL",
        "exec sleep 30",
    ]
)


@needs_node
async def test_the_pi_harness_reports_a_step_limit_as_spent_budget(
    site: ScrapeWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pi_harness(tmp_path, monkeypatch, ENDLESS_PI)
    await site.write_task(ENTITY, URL, "top 30 stories")
    invocation_id = await site.start(ENTITY, PROMPT)

    run = await site.attempt(invocation_id, final_attempt=False)

    assert (run["status"], run["error"]) == (
        "failed",
        {"kind": "budget", "detail": "harness 'echo' exited 2: pi harness: exceeded max_steps 60"},
    )


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
    assert scrape_settings.catalog_file is not None
    scraping = scrape_settings.catalog_file.parent / "scraping"
    computer = scraping / "workspace.yaml"
    computer.write_text(computer.read_text().replace("provider: local", "provider: cloudflare"))
    workspace = ScrapeWorkspace(db_pool, scrape_settings, "scrape-cloudflare")
    await workspace.create()
    await workspace.write_task(ENTITY, URL, "top 30 stories")

    failed = await workspace.invoke(ENTITY, PROMPT)

    assert failed["error"] == {
        "kind": "capability",
        "detail": "skillpack tool 'browser' is not yet executable on cloudflare",
    }

    (scraping / "tools.yaml").write_text(
        (scraping / "tools.yaml")
        .read_text()
        .replace(
            "      - name: browser\n        kind: skillpack\n        pack: echo-pack\n"
            "        learning: true\n",
            "",
        )
    )
    # With no skill that learns there is no lessons collection, so the view over it goes too.
    text = (scraping / "views.yaml").read_text()
    (scraping / "views.yaml").write_text(text[: text.index("\n  # Everything learned")] + "\n")
    package = scrape_settings.catalog_file
    package.write_text(package.read_text().replace("  site_learnings@1: scraping/views.yaml\n", ""))
    without_pack = ScrapeWorkspace(db_pool, scrape_settings, "scrape-cloudflare-2")
    await without_pack.create()
    await without_pack.write_task(ENTITY, URL, "top 30 stories")

    failed = await without_pack.invoke(ENTITY, PROMPT)

    assert failed["error"] == {
        "kind": "capability",
        "detail": "harness 'echo' is not yet executable on cloudflare",
    }


async def test_every_skill_that_opts_in_gets_its_own_playbook_and_one_shared_tool(
    settings: Settings, db_pool: DatabasePool, tmp_path: Path
) -> None:
    scrape_settings = site_scrape_settings(settings, tmp_path)
    assert scrape_settings.catalog_file is not None
    scraping = scrape_settings.catalog_file.parent / "scraping"
    # A catalog skill that declares its own kinds of lesson, beside itself.
    with (scraping / "instructions.yaml").open("a") as handle:
        handle.write(
            "\n  - name: field_notes\n    version: 1\n    active: true\n    kind: skill\n"
            "    description: Keep notes about a site while scraping it.\n"
            "    lifecycle: live\n    template: Write down what you notice about the site.\n"
            "    lessons:\n      kinds:\n"
            "        page: How the site splits its content across pages.\n"
            "        quirk: Something about the site that surprised you.\n"
            "      require: [quirk]\n"
        )
    with (scraping / "tools.yaml").open("a") as handle:
        handle.write(
            "      - {name: notes, kind: skill, artifact: field_notes@1, learning: true}\n"
        )
    package = scrape_settings.catalog_file
    package.write_text(
        package.read_text().replace(
            "  scraper_instructions@1: scraping/instructions.yaml",
            "  scraper_instructions@1: scraping/instructions.yaml\n  field_notes@1: scraping/instructions.yaml",
        )
    )
    site = ScrapeWorkspace(db_pool, scrape_settings, "scrape-two-skills")
    await site.create()
    await site.write_task(ENTITY, URL, "top 30 stories")
    await site.write(
        ENTITY, "lessons", "lesson", text="Page two is ?p=2.", skill="field-notes", kind="page"
    )

    first = await site.invoke(ENTITY, PROMPT)

    assert first["status"] == "succeeded", first["error"]
    root = Path(first["result"]["receipt"]["root"])
    harness_input = json.loads((root / ".harness/input.json").read_text())
    [tool] = harness_input["writeback_tools"]
    assert tool["name"] == "record_lessons"
    entry = tool["input_schema"]["properties"]["records"]["items"]
    assert entry["properties"]["content"]["properties"]["skill"]["enum"] == [
        "echo-pack",
        "field-notes",
    ]
    assert (
        "record what you learned using the echo-pack, field-notes skills"
        in (harness_input["system_prompt"])
    )
    notes = (root / ".agents/skills/field-notes/SKILL.md").read_text()
    assert notes.index("## Start from the playbook") < notes.index("Write down what you notice")
    assert "- `page`: How the site splits its content across pages." in notes
    assert "Every call that records for field-notes must include a `quirk` lesson." in notes
    # Each skill's kinds hold only for that skill, and require only for its own calls.
    command = harness_input["writeback_command"]
    task = str(harness_input["citation_ids"][0])

    def call(*contents: dict[str, str]) -> subprocess.CompletedProcess[str]:
        records = [{"text": "x", "content": c, "citations": [task]} for c in contents]
        return subprocess.run(
            [*command, "record_lessons"],
            input=json.dumps({"records": records}),
            capture_output=True,
            text=True,
            check=False,
        )

    assert call({"skill": "field-notes", "kind": "tip"}).returncode == 1
    assert call({"skill": "field-notes", "kind": "page"}).returncode == 1
    assert (
        call(
            {"skill": "field-notes", "kind": "page"}, {"skill": "field-notes", "kind": "quirk"}
        ).returncode
        == 0
    )
    assert call({"skill": "echo-pack", "kind": "tip"}).returncode == 0

    second = await site.invoke(ENTITY, PROMPT)

    assert second["status"] == "succeeded", second["error"]
    skills = Path(second["result"]["receipt"]["root"]) / ".agents/skills"
    pack_playbook = (skills / "echo-pack/PLAYBOOK.md").read_text()
    notes_playbook = (skills / "field-notes/PLAYBOOK.md").read_text()
    assert "- Stories are tr.athing rows." in pack_playbook
    assert "?p=2" not in pack_playbook
    assert "## page\n\nHow the site splits its content across pages.\n\n- Page two is ?p=2." in (
        notes_playbook
    )
    assert "## quirk" not in notes_playbook
    assert "tr.athing" not in notes_playbook
