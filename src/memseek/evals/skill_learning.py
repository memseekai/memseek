"""Does a learned skill beat the bare one? Run arms of the same scrape and compare.

An arm is a pair of run options: what a training run may read and write back,
and what a test run may read. The runner treats every arm identically, gives
each (arm, trial) its own entity so nothing learned leaks between arms, and
interleaves arms in time so drift in the site affects all of them equally.
Scoring and statistics are pure functions over the rows the runner records.
"""

from __future__ import annotations

import asyncio
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self

import yaml
from jsonschema import Draft202012Validator
from pydantic import Field, TypeAdapter, model_validator

from memseek.definitions.base import NonBlank, PublicName, StrictModel
from memseek.harnesses.contract import LearningMode

if TYPE_CHECKING:
    from memseek.sdk import MemseekClient

ArmName = Literal["cold", "native", "playbook", "playbook+native"]
Phase = Literal["train", "test"]


@dataclass(frozen=True, slots=True)
class Arm:
    """Run options for each phase. A test run never writes back, so held-out
    pages cannot teach the arm their own answers."""

    learning: tuple[LearningMode, LearningMode]
    native: tuple[LearningMode, LearningMode]

    def options(self, phase: Phase) -> dict[str, str]:
        index = 0 if phase == "train" else 1
        return {"learning": self.learning[index], "native": self.native[index]}


ARMS: Mapping[ArmName, Arm] = {
    "cold": Arm(learning=("off", "off"), native=("off", "off")),
    "native": Arm(learning=("off", "off"), native=("read_write", "read")),
    "playbook": Arm(learning=("read_write", "read"), native=("off", "off")),
    "playbook+native": Arm(learning=("read_write", "read"), native=("read_write", "read")),
}
BASELINES: tuple[ArmName, ...] = ("cold", "native")
DELTA_METRICS = ("score", "steps", "tokens")
_ARM_NAMES = TypeAdapter(tuple[ArmName, ...])


def parse_arms(text: str) -> tuple[ArmName, ...]:
    """``cold,playbook`` as arm names, refusing unknown and duplicate ones."""

    names = [name.strip() for name in text.split(",") if name.strip()]
    if not names or len(set(names)) != len(names):
        raise ValueError(f"arms must be distinct and non-empty; choose from {sorted(ARMS)}")
    return _ARM_NAMES.validate_python(names)


class Target(StrictModel):
    url: NonBlank
    goal: NonBlank


class Check(StrictModel):
    output_schema: dict[str, Any] = Field(alias="schema")
    min_items: int = Field(default=0, ge=0)
    field_coverage: dict[str, float] = Field(default_factory=dict)


class SuiteTask(StrictModel):
    id: PublicName
    domain: NonBlank
    train: tuple[Target, ...] = Field(min_length=1)
    test: tuple[Target, ...] = Field(min_length=1)
    check: Check

    @model_validator(mode="after")
    def held_out(self) -> Self:
        shared = {item.url for item in self.train} & {item.url for item in self.test}
        if shared:
            raise ValueError(f"task {self.id}: test URLs must be held out of train: {shared}")
        return self


_SUITE = TypeAdapter(tuple[SuiteTask, ...])


def load_suite(path: Path) -> tuple[SuiteTask, ...]:
    return _SUITE.validate_python(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True, slots=True)
class Score:
    passed: bool
    score: float


def score_value(value: Any, check: Check) -> Score:
    """Deterministic: schema validity, item count, and per-field coverage, each 0..1."""

    schema_ok = not any(Draft202012Validator(check.output_schema).iter_errors(value))
    items = value.get("items") if isinstance(value, Mapping) else value
    rows = [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []
    parts = [1.0 if schema_ok else 0.0]
    passed = schema_ok
    if check.min_items:
        parts.append(min(1.0, len(rows) / check.min_items))
        passed = passed and len(rows) >= check.min_items
    for name, threshold in sorted(check.field_coverage.items()):
        covered = sum(1 for row in rows if row.get(name) is not None) / len(rows) if rows else 0.0
        parts.append(min(1.0, covered / threshold) if threshold else 1.0)
        passed = passed and covered >= threshold
    return Score(passed=passed, score=round(sum(parts) / len(parts), 6))


@dataclass(frozen=True, slots=True)
class RunOutcome:
    invocation_id: str
    succeeded: bool
    value: Any
    metrics: Mapping[str, Any]
    harness: Mapping[str, Any]
    skillpacks: Sequence[Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class RunRow:
    task: str
    arm: ArmName
    trial: int
    phase: Phase
    k_train: int
    passed: bool
    score: float
    metrics: Mapping[str, Any]
    invocation_id: str
    harness: Mapping[str, Any] = field(default_factory=dict)
    skillpacks: Sequence[Mapping[str, Any]] = ()

    def content(self) -> dict[str, Any]:
        """The row as a ``skill_eval_runs`` record."""

        payload = asdict(self)
        payload["pass"] = payload.pop("passed")
        payload["metrics"] = dict(self.metrics)
        payload["harness"] = dict(self.harness)
        payload["skillpacks"] = [dict(item) for item in self.skillpacks]
        verdict = "pass" if self.passed else "fail"
        payload["text"] = (
            f"{self.task} {self.arm} trial {self.trial} {self.phase} k={self.k_train}: "
            f"{verdict}, score {self.score:.2f}"
        )
        return payload


class Backend(Protocol):
    """Where runs happen and rows land. The CLI uses the HTTP API; tests use a database."""

    async def write_task(self, entity: str, target: Target) -> None: ...

    async def invoke(self, entity: str, prompt: str, options: Mapping[str, str]) -> RunOutcome: ...

    async def record_run(self, row: RunRow) -> None: ...


def entity_for(task: SuiteTask, arm: ArmName, trial: int) -> str:
    return f"site:{task.domain}#{arm}-{trial}"


def prompt_for(target: Target) -> str:
    return f"Scrape {target.url}. Extract {target.goal}."


async def run_suite(
    suite: Sequence[SuiteTask],
    backend: Backend,
    *,
    arms: Sequence[ArmName],
    trials: int,
    k_train: int,
) -> list[RunRow]:
    """Test before any training, then after each of ``k_train`` training runs.

    Each step runs every arm before the next step starts, so the arms share the
    same stretch of time against the live site.
    """

    rows: list[RunRow] = []

    async def run(
        task: SuiteTask, arm: ArmName, trial: int, phase: Phase, k: int, target: Target
    ) -> None:
        entity = entity_for(task, arm, trial)
        await backend.write_task(entity, target)
        outcome = await backend.invoke(entity, prompt_for(target), ARMS[arm].options(phase))
        scored = score_value(outcome.value, task.check) if outcome.succeeded else Score(False, 0.0)
        row = RunRow(
            task=task.id,
            arm=arm,
            trial=trial,
            phase=phase,
            k_train=k,
            passed=scored.passed,
            score=scored.score,
            metrics=dict(outcome.metrics),
            invocation_id=outcome.invocation_id,
            harness=dict(outcome.harness),
            skillpacks=[dict(item) for item in outcome.skillpacks],
        )
        await backend.record_run(row)
        rows.append(row)

    for trial in range(trials):
        for task in suite:
            for k in range(k_train + 1):
                for arm in arms:
                    if k:
                        await run(
                            task, arm, trial, "train", k, task.train[(k - 1) % len(task.train)]
                        )
                    for target in task.test:
                        await run(task, arm, trial, "test", k, target)
    return rows


def bootstrap_ci(
    values: Sequence[float], *, resamples: int = 2_000, seed: int = 0
) -> tuple[float, float]:
    """A percentile 95% interval for the mean, reproducible for a given seed."""

    if len(values) == 1:
        return (values[0], values[0])
    generator = random.Random(seed)
    means = sorted(
        statistics.fmean(generator.choices(values, k=len(values))) for _ in range(resamples)
    )
    return (means[int(0.025 * (resamples - 1))], means[int(0.975 * (resamples - 1))])


def _metric(row: RunRow, name: str) -> float | None:
    if name == "score":
        return row.score
    if name == "tokens":
        billed = [row.metrics.get("input_tokens"), row.metrics.get("output_tokens")]
        if not all(isinstance(t, int | float) for t in billed):
            return None
        cached = [row.metrics.get("cache_read_tokens"), row.metrics.get("cache_write_tokens")]
        return float(sum(billed) + sum(t for t in cached if isinstance(t, int | float)))
    value = row.metrics.get(name)
    return float(value) if isinstance(value, int | float) else None


def _median(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.median(present) if present else None


def _trial_means(rows: Sequence[RunRow], metric: str) -> dict[int, float]:
    by_trial: dict[int, list[float]] = {}
    for row in rows:
        value = _metric(row, metric)
        if value is not None:
            by_trial.setdefault(row.trial, []).append(value)
    return {trial: statistics.fmean(values) for trial, values in by_trial.items()}


def summarize(rows: Sequence[RunRow], *, resamples: int = 2_000, seed: int = 0) -> dict[str, Any]:
    """Per arm: the trained state's outcomes, deltas against each baseline, and the curve.

    The trained state is every test run at the largest ``k_train``. Deltas pair
    trial ``t`` of one arm with trial ``t`` of the baseline, which ran beside it.
    """

    tests = [row for row in rows if row.phase == "test"]
    if not tests:
        return {"final_k_train": 0, "arms": {}}
    final_k = max(row.k_train for row in tests)
    arms = list(dict.fromkeys(row.arm for row in tests))
    trained = {arm: [r for r in tests if r.arm == arm and r.k_train == final_k] for arm in arms}
    report: dict[str, Any] = {"final_k_train": final_k, "arms": {}}
    for arm in arms:
        final = trained[arm]
        deltas: dict[str, Any] = {}
        for baseline in BASELINES:
            if baseline == arm or baseline not in trained:
                continue
            deltas[baseline] = {}
            for metric in DELTA_METRICS:
                ours = _trial_means(final, metric)
                theirs = _trial_means(trained[baseline], metric)
                paired = [ours[t] - theirs[t] for t in sorted(ours.keys() & theirs.keys())]
                if not paired:
                    continue
                low, high = bootstrap_ci(paired, resamples=resamples, seed=seed)
                deltas[baseline][metric] = {
                    "mean": round(statistics.fmean(paired), 6),
                    "ci95": [round(low, 6), round(high, 6)],
                }
        curve: dict[str, dict[int, float]] = {"score": {}, "steps": {}}
        for k in sorted({row.k_train for row in tests if row.arm == arm}):
            at_k = [row for row in tests if row.arm == arm and row.k_train == k]
            for metric in curve:
                values = [v for v in (_metric(row, metric) for row in at_k) if v is not None]
                if values:
                    curve[metric][k] = round(statistics.fmean(values), 6)
        report["arms"][arm] = {
            "runs": len(final),
            "pass_rate": round(sum(row.passed for row in final) / len(final), 6),
            "mean_score": round(statistics.fmean(row.score for row in final), 6),
            "median": {
                name: _median([_metric(row, name) for row in final])
                for name in (
                    "steps",
                    "tool_errors",
                    "tokens",
                    "cache_read_tokens",
                    "cost_usd",
                    "wall_s",
                )
            },
            "delta": deltas,
            "curve": curve,
        }
    return report


def render_table(report: Mapping[str, Any]) -> str:
    header = (
        f"{'arm':<16} {'runs':>4} {'pass':>6} {'score':>6} {'steps':>6} {'errors':>6} "
        f"{'tokens':>8} {'cached':>8} {'cost':>8} {'wall_s':>7}  "
        "delta vs cold / native (score, steps)"
    )
    lines = [f"trained state: test runs after {report['final_k_train']} training runs", header]
    for arm, summary in report["arms"].items():
        median = summary["median"]
        deltas = " / ".join(_delta_cell(summary["delta"].get(baseline)) for baseline in BASELINES)
        lines.append(
            f"{arm:<16} {summary['runs']:>4} {summary['pass_rate']:>6.2f} "
            f"{summary['mean_score']:>6.2f} {_cell(median['steps']):>6} "
            f"{_cell(median['tool_errors']):>6} {_cell(median['tokens']):>8} "
            f"{_cell(median['cache_read_tokens']):>8} "
            f"{_cell(median['cost_usd']):>8} {_cell(median['wall_s']):>7}  {deltas}"
        )
    lines.append("")
    lines.append("learning curve (k_train: mean test score, mean steps):")
    for arm, summary in report["arms"].items():
        steps = summary["curve"]["steps"]
        points = ", ".join(
            f"{k}: {score:.2f} ({_cell(steps.get(k))} steps)"
            for k, score in summary["curve"]["score"].items()
        )
        lines.append(f"  {arm:<16} {points}")
    return "\n".join(lines)


def _cell(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def _delta_cell(delta: Mapping[str, Any] | None) -> str:
    if not delta:
        return "-"
    parts = []
    for metric in ("score", "steps"):
        if metric in delta:
            low, high = delta[metric]["ci95"]
            parts.append(f"{delta[metric]['mean']:+g} [{low:+g}, {high:+g}]")
    return ", ".join(parts) or "-"


class ApiBackend:
    """Runs through a Memseek API whose worker hosts the ``local`` provider."""

    def __init__(
        self,
        client: MemseekClient,
        *,
        computer: str,
        agent: str,
        context_policy: str,
        results_entity: str,
        timeout_s: float = 1_800,
    ) -> None:
        self._client = client
        self._executor = {"kind": "agent", "agent": agent, "context_policy": context_policy}
        self._computer = computer
        self._results_entity = results_entity
        self._timeout_s = timeout_s

    async def write_task(self, entity: str, target: Target) -> None:
        written = await self._client.records.ingest(
            entity=entity,
            collection="scrape_tasks",
            type="task",
            text=f"Scrape {target.url}: {target.goal}",
            content={"url": target.url, "goal": target.goal},
        )
        record_id = str(written["inserted"][0]["id"])
        async with asyncio.timeout(60):
            while True:
                if (await self._client.record(record_id)).get("ready"):
                    return
                await asyncio.sleep(0.4)

    async def invoke(self, entity: str, prompt: str, options: Mapping[str, str]) -> RunOutcome:
        started = await self._client.invocations.start(
            entity=entity,
            computer=self._computer,
            executor=self._executor,
            task={"kind": "answer", "prompt": prompt, "input": dict(options)},
        )
        handle = self._client.invocations.attach(started["invocation_id"])
        state = await handle.wait(timeout_s=self._timeout_s)
        result = state.get("result") or {}
        receipt = result.get("receipt") or {}
        return RunOutcome(
            invocation_id=handle.id,
            succeeded=state["status"] == "succeeded",
            value=result.get("value"),
            metrics=receipt.get("metrics") or {},
            harness=receipt.get("harness") or {},
            skillpacks=receipt.get("skillpacks") or [],
        )

    async def record_run(self, row: RunRow) -> None:
        content = row.content()
        await self._client.records.ingest(
            entity=self._results_entity,
            collection="skill_eval_runs",
            type="eval_run",
            text=content.pop("text"),
            content=content,
        )


__all__ = [
    "ARMS",
    "ApiBackend",
    "Arm",
    "ArmName",
    "Backend",
    "Check",
    "RunOutcome",
    "RunRow",
    "Score",
    "SuiteTask",
    "Target",
    "bootstrap_ci",
    "entity_for",
    "load_suite",
    "render_table",
    "run_suite",
    "score_value",
    "summarize",
]
