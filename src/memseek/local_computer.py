"""The ``local`` Computer provider: run a harnessed Agent in a directory on this machine.

It composes three modules and branches on none of them. The harness module is
the agent loop, each skill pack is a tool skill, and this provider only
prepares a root, runs the harness entry against it, and collects the outbox
under the same rules as the Cloudflare runtime's ``collectOutbox``. It is a
development and evaluation provider: the harness runs with the worker's user
and network, not in a sandbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from memseek.computers import ComputerExecutionError, ComputerRequest, ComputerResult
from memseek.config import Settings
from memseek.definitions.base import StrictModel
from memseek.definitions.models import ComputerWriteback
from memseek.harnesses.contract import (
    HARNESS_INPUT_PATH,
    HarnessInput,
    HarnessLimits,
    HarnessManifest,
    HarnessModel,
    HarnessOutput,
    HarnessSkill,
    LearningMode,
    ManifestError,
    MissingRequirementError,
)
from memseek.harnesses.prompt import build_system_prompt
from memseek.harnesses.registry import check_harness, load_harness
from memseek.skillpacks import (
    LEARNINGS_PATH,
    PLAYBOOK_PATH,
    SkillMount,
    SkillPackError,
    SkillPackManifest,
    check_skillpack,
    load_skillpack,
    materialize_skillpack,
    pack_environment,
)

# Without these nothing runs at all; every other variable must be declared by
# the harness manifest or a skill pack to reach the harness.
_BASE_ENV = ("PATH", "HOME")
_OUTBOX_MAX_FILES = 100
_OUTBOX_MAX_BYTES = 1024 * 1024
_OUTBOX_MAX_ENTRIES = 2_000
# The harness enforces max_wall_s itself; this only reclaims one that ignores it.
_WALL_GRACE_S = 30


class RunOptions(StrictModel):
    """What one run may read and write back, taken from the invocation's task input.

    Both default to the normal mode, so an option can only narrow a run.
    ``learning`` governs memseek's playbook; ``native`` governs each pack's own
    state directory: ``off`` is fresh, ``read`` is a copy of the entity's kept
    directory, and ``read_write`` is the kept directory itself.
    """

    learning: LearningMode = "read_write"
    native: LearningMode = "read_write"


class _OutboxFile(StrictModel):
    path: str
    type: Literal["observations", "maintained_state"]
    content: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)


class LocalComputerProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def execute(self, request: ComputerRequest) -> ComputerResult:
        return await asyncio.to_thread(self._execute, request)

    def _execute(self, request: ComputerRequest) -> ComputerResult:
        agent = request.agent
        if agent is None or agent.harness is None:
            raise ComputerExecutionError(
                "capability", "the local provider runs only Agents that name a harness"
            )
        options = _run_options(request.input)
        tools = list((request.toolset or {}).get("tools", ()))
        try:
            manifest = load_harness(agent.harness, self._settings.harness_paths)
            packs = [
                load_skillpack(str(tool["pack"]), self._settings.skillpack_paths)
                for tool in tools
                if tool.get("kind") == "skillpack"
            ]
        except ManifestError as exc:
            raise ComputerExecutionError("reference", str(exc)) from exc

        env = self._environment(manifest, request)
        try:
            check_harness(manifest, path=env.get("PATH", ""))
            for pack in packs:
                check_skillpack(pack, path=env.get("PATH", ""))
        except MissingRequirementError as exc:
            raise ComputerExecutionError("capability", str(exc)) from exc

        root = self._settings.local_computer_root.expanduser() / request.session_key
        context_files = dict(request.context_files or {})
        if options.learning == "off":
            # The cold baseline must not be able to find the playbook at all.
            context_files.pop(PLAYBOOK_PATH, None)
        _prepare_root(root, context_files, request.input)

        skills_root = root / manifest.skills_dir
        # Regenerated every run, so a resumed session never keeps a stale playbook.
        shutil.rmtree(skills_root, ignore_errors=True)
        mounts = _mount_materialized_skills(root, context_files, skills_root)
        for pack in packs:
            env.update(
                pack_environment(
                    pack,
                    state_dir=self._state_dir(root, request, pack, options),
                    parent=os.environ,
                )
            )
            try:
                mounts.append(
                    materialize_skillpack(
                        pack,
                        skills_root,
                        playbook_md=context_files.get(PLAYBOOK_PATH),
                        learning=options.learning,
                        env=env,
                    )
                )
            except SkillPackError as exc:
                raise ComputerExecutionError("provider", str(exc)) from exc

        harness_input = HarnessInput(
            task=_task_text(request.input),
            system_prompt=build_system_prompt(
                context_files=context_files,
                materialization=request.materialization,
                toolset=request.toolset,
                output_schema=request.output_schema or {"type": "object"},
                citation_ids=[str(value) for value in request.citation_ids],
                writeback_paths=[
                    path
                    for path in _writeback_paths(request)
                    if path != LEARNINGS_PATH or options.learning == "read_write"
                ],
                learning_packs=[
                    pack.name for pack in packs if pack.learns and options.learning == "read_write"
                ],
            ),
            output_schema=dict(request.output_schema or {"type": "object"}),
            model=_harness_model(request.model),
            limits=HarnessLimits(
                max_steps=agent.limits.max_steps, max_wall_s=agent.limits.max_wall_s
            ),
            tools=tuple(tools),
            skills=tuple(HarnessSkill(name=item.name, dir=str(item.dir)) for item in mounts),
            learning=options.learning,
            citation_ids=tuple(sorted(str(value) for value in request.citation_ids)),
        )
        (root / HARNESS_INPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
        (root / HARNESS_INPUT_PATH).write_text(harness_input.model_dump_json(), encoding="utf-8")

        output = _run_harness(manifest, root, env, wall_s=agent.limits.max_wall_s)
        if options.learning != "read_write":
            (root / LEARNINGS_PATH.lstrip("/")).unlink(missing_ok=True)
        outbox = _collect_outbox(root, request)
        encoded = json.dumps(
            output.value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        (root / request.output_path.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (root / request.output_path.lstrip("/")).write_bytes(encoded)
        return ComputerResult(
            value=output.value,
            citation_ids=frozenset(output.citation_ids),
            receipt={
                "provider": "local",
                "backend": request.computer.runtime.default,
                "session_key": request.session_key,
                "task_id": request.task_id,
                "executor_ref": request.agent_ref,
                "output_path": request.output_path,
                "output_sha256": hashlib.sha256(encoded).hexdigest(),
                "bytes": len(encoded),
                "root": str(root),
                "harness": {"name": manifest.name, "version": manifest.version},
                "skillpacks": [{"name": pack.name, "version": pack.version} for pack in packs],
                "learning": options.learning,
                "native": options.native,
                "metrics": output.metrics.model_dump(mode="json"),
                "events": [event.model_dump(mode="json") for event in output.events],
                "outbox": [item.model_dump(mode="json") for item in outbox],
            },
            steps=output.steps,
            awaiting_input=output.awaiting_input,
        )

    def _environment(self, manifest: HarnessManifest, request: ComputerRequest) -> dict[str, str]:
        env = {name: os.environ[name] for name in _BASE_ENV if name in os.environ}
        provider = _harness_model(request.model).provider
        key_name = manifest.model_env.get(provider)
        if key_name is not None and (key := self._settings.secret(key_name)):
            env[key_name] = key
        return env

    def _state_dir(
        self, root: Path, request: ComputerRequest, pack: SkillPackManifest, options: RunOptions
    ) -> Path:
        fresh = root / ".skillpacks" / pack.name
        shutil.rmtree(fresh, ignore_errors=True)
        if options.native == "off":
            fresh.mkdir(parents=True)
            return fresh
        entity = hashlib.sha256(f"{request.workspace}\0{request.entity}".encode()).hexdigest()
        kept = self._settings.local_computer_root.expanduser() / "_native" / entity / pack.name
        kept.mkdir(parents=True, exist_ok=True)
        if options.native == "read_write":
            return kept
        shutil.copytree(kept, fresh, symlinks=True)
        return fresh


def _run_options(value: Any) -> RunOptions:
    raw = value.get("input") if isinstance(value, Mapping) else None
    try:
        return RunOptions.model_validate(raw or {})
    except ValidationError as exc:
        raise ComputerExecutionError("validation", f"invalid run options: {exc}") from exc


def _task_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return json.dumps(value, sort_keys=True)
    parts = [str(value.get("prompt") or "")]
    replies = [str(turn.get("prompt")) for turn in value.get("turns") or () if turn.get("prompt")]
    if replies:
        parts.append("Operator replies, oldest first:\n" + "\n".join(f"- {r}" for r in replies))
    return "\n\n".join(parts)


def _harness_model(model: Mapping[str, Any] | None) -> HarnessModel:
    targets = list((model or {}).get("targets") or ())
    if not targets:
        raise ComputerExecutionError("reference", "Agent model alias has no target")
    provider, _, name = str(targets[0]).partition(":")
    return HarnessModel(provider=provider, model=name, params=dict((model or {}).get("params", {})))


def _inside(root: Path, path: str) -> Path:
    target = (root / path.lstrip("/")).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ComputerExecutionError("validation", f"path escapes the Computer root: {path!r}")
    return target


def _prepare_root(root: Path, context_files: Mapping[str, str], task_input: Any) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # Context is the request's, every run; a resumed session keeps /workspace
    # only. The outbox is cleared so one invocation never collects another's.
    for name in (".memseek", "inputs", "outbox"):
        shutil.rmtree(root / name, ignore_errors=True)
    for path, content in context_files.items():
        target = _inside(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (root / "inputs").mkdir()
    (root / "inputs" / "input.json").write_text(json.dumps(task_input), encoding="utf-8")
    (root / "workspace").mkdir(exist_ok=True)
    (root / "outbox").mkdir()


def _mount_materialized_skills(
    root: Path, context_files: Mapping[str, str], skills_root: Path
) -> list[SkillMount]:
    """Copy catalog skills to where the harness discovers skills."""

    mounts: list[SkillMount] = []
    for path in sorted(context_files):
        parts = Path(path).parts
        if len(parts) != 5 or parts[1:3] != (".memseek", "skills") or parts[4] != "SKILL.md":
            continue
        directory = skills_root / parts[3]
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_inside(root, path), directory / "SKILL.md")
        mounts.append(SkillMount(name=parts[3], dir=directory))
    return mounts


def _run_harness(
    manifest: HarnessManifest, root: Path, env: Mapping[str, str], *, wall_s: int
) -> HarnessOutput:
    try:
        process = subprocess.Popen(
            manifest.command(),
            cwd=root,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Its own process group, so a timeout reclaims the agent it started.
            start_new_session=True,
        )
    except OSError as exc:
        raise ComputerExecutionError("provider", f"cannot start harness: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=wall_s + _WALL_GRACE_S)
    except subprocess.TimeoutExpired as exc:
        if hasattr(os, "killpg") and hasattr(signal, "SIGKILL"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.communicate()
        raise ComputerExecutionError(
            "budget", f"harness {manifest.name!r} exceeded max_wall_s {wall_s}"
        ) from exc
    if process.returncode != 0:
        detail = " ".join(stderr.split())[-500:]
        raise ComputerExecutionError(
            "provider", f"harness {manifest.name!r} exited {process.returncode}: {detail}"
        )
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        return HarnessOutput.model_validate_json(lines[-1] if lines else "")
    except ValidationError as exc:
        raise ComputerExecutionError(
            "validation", f"harness {manifest.name!r} output breaks the contract: {exc}"
        ) from exc


def _outbox_paths(root: Path) -> list[str]:
    files: list[str] = []
    pending = [root / "outbox"] if (root / "outbox").is_dir() else []
    while pending:
        directory = pending.pop()
        for entry in directory.iterdir():
            path = "/" + entry.relative_to(root).as_posix()
            if entry.is_symlink():
                raise ComputerExecutionError("validation", f"outbox refuses symbolic link: {path}")
            if entry.is_dir():
                pending.append(entry)
            else:
                files.append(path)
            if len(files) + len(pending) > _OUTBOX_MAX_ENTRIES:
                raise ComputerExecutionError("validation", "outbox entry limit exceeded")
    return sorted(files)


def _configured_writeback(request: ComputerRequest) -> list[ComputerWriteback]:
    if request.mode != "invocation":
        return []
    return [item for item in request.computer.writeback if item.type != "final_result"]


def _writeback_paths(request: ComputerRequest) -> list[str]:
    return [item.path for item in _configured_writeback(request)]


def _collect_outbox(root: Path, request: ComputerRequest) -> list[_OutboxFile]:
    """The rules of the Cloudflare runtime's ``collectOutbox``, over a local directory."""

    configured = _configured_writeback(request)
    allowed = [request.output_path, *(item.path for item in configured)]
    files = _outbox_paths(root)
    unknown = [
        path
        for path in files
        if not any(path == prefix or path.startswith(f"{prefix}/") for prefix in allowed)
    ]
    if unknown:
        raise ComputerExecutionError("validation", f"unknown outbox files: {', '.join(unknown)}")
    result: list[_OutboxFile] = []
    total = 0
    for declaration in configured:
        assert declaration.type != "final_result"
        declared = [
            path
            for path in files
            if path == declaration.path or path.startswith(f"{declaration.path}/")
        ]
        if declaration.type == "observations" and any(p != declaration.path for p in declared):
            raise ComputerExecutionError(
                "validation", "observations writeback must be one JSONL file"
            )
        for path in declared:
            if declaration.type == "maintained_state" and not path.endswith(".json"):
                raise ComputerExecutionError("validation", f"proposal is not JSON: {path}")
            target = root / path.lstrip("/")
            if not target.is_file() or target.is_symlink():
                raise ComputerExecutionError("validation", f"invalid outbox file: {path}")
            data = target.read_bytes()
            total += len(data)
            if len(result) >= _OUTBOX_MAX_FILES or total > _OUTBOX_MAX_BYTES:
                raise ComputerExecutionError("validation", "interactive outbox limit exceeded")
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ComputerExecutionError(
                    "validation", f"outbox file is not UTF-8: {path}"
                ) from exc
            result.append(
                _OutboxFile(
                    path=path,
                    type=declaration.type,
                    content=content,
                    sha256=hashlib.sha256(data).hexdigest(),
                    bytes=len(data),
                )
            )
    return result


__all__ = ["LocalComputerProvider", "RunOptions"]
