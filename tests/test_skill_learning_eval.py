"""The skill-learning eval, run against the fixture harness through real invocations.

The fixture harness takes 5 steps bare, 2 fewer with a playbook mounted, and 1
fewer when the skill pack's saved helpers survived, so every delta below is a
literal the arms' definitions predict.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError
from site_scrape_fixture import REPOSITORY_ROOT, ScrapeWorkspace, site_scrape_settings

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.evals.skill_learning import (
    Check,
    RunOutcome,
    RunRow,
    SuiteTask,
    Target,
    bootstrap_ci,
    load_suite,
    parse_arms,
    render_table,
    run_suite,
    score_value,
    summarize,
)

TASK = SuiteTask.model_validate(
    {
        "id": "hn-front",
        "domain": "news.ycombinator.com",
        "train": [{"url": "https://news.ycombinator.com/", "goal": "top stories"}],
        "test": [{"url": "https://news.ycombinator.com/news?p=2", "goal": "page 2 stories"}],
        "check": {
            "schema": {
                "type": "object",
                "required": ["items"],
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "object", "required": ["title"]},
                    }
                },
            },
            "min_items": 25,
            "field_coverage": {"points": 0.9},
        },
    }
)
ARMS = ("cold", "native", "playbook", "playbook+native")


class DatabaseBackend:
    """The eval's Backend over the same functions the API and worker call."""

    def __init__(self, site: ScrapeWorkspace) -> None:
        self.site = site

    async def write_task(self, entity: str, target: Target) -> None:
        await self.site.write_task(entity, target.url, target.goal)

    async def invoke(self, entity: str, prompt: str, options: Mapping[str, str]) -> RunOutcome:
        state = await self.site.invoke(entity, prompt, dict(options))
        receipt = state["result"]["receipt"]
        return RunOutcome(
            invocation_id=state["invocation_id"],
            succeeded=state["status"] == "succeeded",
            value=state["result"]["value"],
            metrics=receipt["metrics"],
            harness=receipt["harness"],
            skillpacks=receipt["skillpacks"],
        )

    async def record_run(self, row: RunRow) -> None:
        await self.site.write("eval:test", "skill_eval_runs", "eval_run", **row.content())


@pytest.fixture
async def site(settings: Settings, db_pool: DatabasePool, tmp_path: Path) -> ScrapeWorkspace:
    workspace = ScrapeWorkspace(db_pool, site_scrape_settings(settings, tmp_path), "eval-test")
    await workspace.create()
    return workspace


async def test_arms_are_isolated_and_their_deltas_match_what_each_arm_can_read(
    site: ScrapeWorkspace,
) -> None:
    rows = await run_suite([TASK], DatabaseBackend(site), arms=ARMS, trials=2, k_train=2)

    assert len(rows) == 40
    assert {(row.harness["name"], row.skillpacks[0]["name"]) for row in rows} == {
        ("echo", "echo-pack")
    }
    for trial in (0, 1):
        for arm, expected in (("cold", 0), ("native", 0), ("playbook", 2), ("playbook+native", 2)):
            entity = f"site:news.ycombinator.com#{arm}-{trial}"
            assert len(await site.learnings(entity)) == expected, entity
    async with site.pool.connection() as conn:
        stored = await (
            await conn.execute(
                "select count(*) as count from record where collection = 'skill_eval_runs'"
            )
        ).fetchone()
    assert stored is not None
    assert stored["count"] == 40

    report = summarize(rows)

    assert report["final_k_train"] == 2
    arms = report["arms"]
    assert {arm: summary["median"]["steps"] for arm, summary in arms.items()} == {
        "cold": 5,
        "native": 4,
        "playbook": 3,
        "playbook+native": 2,
    }
    assert {arm: (s["pass_rate"], s["mean_score"]) for arm, s in arms.items()} == dict.fromkeys(
        ARMS, (1.0, 1.0)
    )
    assert arms["playbook"]["delta"] == {
        "cold": {
            "score": {"mean": 0.0, "ci95": [0.0, 0.0]},
            "steps": {"mean": -2.0, "ci95": [-2.0, -2.0]},
            "tokens": {"mean": -10200.0, "ci95": [-10200.0, -10200.0]},
        },
        "native": {
            "score": {"mean": 0.0, "ci95": [0.0, 0.0]},
            "steps": {"mean": -1.0, "ci95": [-1.0, -1.0]},
            "tokens": {"mean": -5100.0, "ci95": [-5100.0, -5100.0]},
        },
    }
    assert set(arms["cold"]["delta"]) == {"native"}
    assert arms["playbook+native"]["delta"]["native"]["steps"]["mean"] == -2.0
    assert {arm: s["curve"]["steps"] for arm, s in arms.items()} == {
        "cold": {0: 5.0, 1: 5.0, 2: 5.0},
        "native": {0: 5.0, 1: 4.0, 2: 4.0},
        "playbook": {0: 5.0, 1: 3.0, 2: 3.0},
        "playbook+native": {0: 5.0, 1: 2.0, 2: 2.0},
    }
    lines = render_table(report).splitlines()
    assert lines[0] == "trained state: test runs after 2 training runs"
    assert lines[4] == (
        "playbook            2   1.00   1.00      3      0    15300    12000        -     0.5  "
        "+0 [+0, +0], -2 [-2, -2] / +0 [+0, +0], -1 [-1, -1]"
    )
    assert lines[-2] == "  playbook         0: 1.00 (5 steps), 1: 1.00 (3 steps), 2: 1.00 (3 steps)"


def test_scoring_is_schema_count_and_coverage() -> None:
    check = TASK.check
    full = {"items": [{"title": f"t{i}", "points": i} for i in range(30)]}
    sparse = {"items": [{"title": f"t{i}", "points": None if i % 2 else i} for i in range(20)]}

    assert (score_value(full, check).passed, score_value(full, check).score) == (True, 1.0)
    # The mean of schema 1, count 20/25, and coverage 0.5/0.9.
    assert (score_value(sparse, check).passed, score_value(sparse, check).score) == (
        False,
        0.785185,
    )
    assert (score_value({"rows": []}, check).passed, score_value({"rows": []}, check).score) == (
        False,
        0.0,
    )


def test_bootstrap_interval_is_reproducible() -> None:
    assert bootstrap_ci([3.0]) == (3.0, 3.0)
    assert bootstrap_ci([2.0, 2.0, 2.0]) == (2.0, 2.0)
    assert bootstrap_ci([0.0, 1.0, 2.0, 3.0], seed=7) == (0.5, 2.5)


def test_the_shipped_suite_holds_its_test_pages_out_of_training() -> None:
    suite = load_suite(REPOSITORY_ROOT / "evals" / "scrape_suite.yaml")

    assert [task.id for task in suite] == ["hn-front", "books-catalogue"]
    assert isinstance(suite[0].check, Check)
    with pytest.raises(ValidationError, match="test URLs must be held out of train"):
        SuiteTask.model_validate(
            {**TASK.model_dump(by_alias=True), "test": TASK.model_dump()["train"]}
        )


def test_arms_parse_strictly() -> None:
    assert parse_arms("cold, playbook") == ("cold", "playbook")
    with pytest.raises(ValueError, match="distinct and non-empty"):
        parse_arms("cold,cold")
    with pytest.raises(ValidationError):
        parse_arms("warm")
