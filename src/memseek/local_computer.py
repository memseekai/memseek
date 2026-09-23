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
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

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
from memseek.harnesses.writeback import (
    check_candidate,
    destination_schema,
    normalize_candidate,
    writeback_tools,
)
from memseek.skillpacks import (
    LEARNINGS_PATH,
    PLAYBOOK_PATH,
    SkillMount,
    SkillPackError,
    SkillPackManifest,
    bind_learnings_schema,
    check_skillpack,
    load_skillpack,
    materialize_skillpack,
    pack_environment,
    require_helper,
    stop_skillpack,
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


class _Rejected(StrictModel):
    path: str
    reason: str
    line: int | None = None


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

        # Short on purpose: packs put sockets here, and macOS caps a socket
        # path at 104 bytes, which a state directory below the root exceeds.
        runtime = Path(
            tempfile.mkdtemp(prefix="msk-", dir="/tmp" if Path("/tmp").is_dir() else None)
        )
        try:
            return self._execute_in(request, options, tools, manifest, packs, env, runtime)
        finally:
            for pack in packs:
                stop_skillpack(pack, env)
            shutil.rmtree(runtime, ignore_errors=True)

    def _execute_in(
        self,
        request: ComputerRequest,
        options: RunOptions,
        tools: list[Any],
        manifest: HarnessManifest,
        packs: list[SkillPackManifest],
        env: dict[str, str],
        runtime: Path,
    ) -> ComputerResult:
        agent = request.agent
        assert agent is not None
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
                    runtime_dir=runtime,
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

        writeback_paths = [
            path
            for path in _writeback_paths(request)
            if path != LEARNINGS_PATH or options.learning == "read_write"
        ]
        citations = sorted(str(value) for value in request.citation_ids)
        learning_packs = [
            pack.name for pack in packs if pack.learns and options.learning == "read_write"
        ]
        schemas = _schema_overrides(context_files, learning_packs)
        tools_for_writeback = writeback_tools(
            (
                item.model_dump(mode="json")
                for item in _configured_writeback(request)
                if item.path in writeback_paths
            ),
            context_files,
            citations,
            schema_overrides=schemas,
        )
        tools_for_writeback = [
            require_helper(tool) if tool.path == LEARNINGS_PATH and learning_packs else tool
            for tool in tools_for_writeback
        ]
        harness_input = HarnessInput(
            task=_task_text(request.input),
            system_prompt=build_system_prompt(
                context_files=context_files,
                materialization=request.materialization,
                toolset=request.toolset,
                output_schema=request.output_schema or {"type": "object"},
                citation_ids=[str(value) for value in request.citation_ids],
                writeback_paths=writeback_paths,
                writeback_tools={tool.path: tool.name for tool in tools_for_writeback},
                learning_packs=learning_packs,
                root=str(root),
                playbooks={mount.name: mount.playbook for mount in mounts if mount.playbook},
            ),
            output_schema=dict(request.output_schema or {"type": "object"}),
            model=_harness_model(request.model),
            limits=HarnessLimits(
                max_steps=agent.limits.max_steps, max_wall_s=agent.limits.max_wall_s
            ),
            tools=tuple(tools),
            skills=tuple(HarnessSkill(name=item.name, dir=str(item.dir)) for item in mounts),
            learning=options.learning,
            citation_ids=tuple(citations),
            writeback_tools=tuple(tools_for_writeback),
            writeback_command=(sys.executable, "-m", "memseek.harnesses.writeback", str(root)),
        )
        (root / HARNESS_INPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
        (root / HARNESS_INPUT_PATH).write_text(harness_input.model_dump_json(), encoding="utf-8")

        output = _run_harness(manifest, root, env, wall_s=agent.limits.max_wall_s)
        if options.learning != "read_write":
            (root / LEARNINGS_PATH.lstrip("/")).unlink(missing_ok=True)
        outbox, rejected = _collect_outbox(root, request, context_files, schemas)
        encoded = json.dumps(
            output.value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        (root / request.output_path.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (root / request.output_path.lstrip("/")).write_bytes(encoded)
        return ComputerResult(
            value=output.value,
            # What the outbox cites is authorized (it was checked against the
            # request), and ingestion only trusts citations the result carries;
            # an agent that forgot one in its envelope must not lose the file.
            citation_ids=frozenset(output.citation_ids) | _outbox_citations(outbox),
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
                "outbox_rejected": [
                    item.model_dump(mode="json", exclude_none=True) for item in rejected
                ],
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


def _outbox_paths(root: Path, rejected: list[_Rejected]) -> list[str]:
    files: list[str] = []
    pending = [root / "outbox"] if (root / "outbox").is_dir() else []
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir()):
            path = "/" + entry.relative_to(root).as_posix()
            if len(files) + len(pending) >= _OUTBOX_MAX_ENTRIES:
                rejected.append(_Rejected(path=path, reason="outbox entry limit reached"))
            elif entry.is_symlink():
                rejected.append(_Rejected(path=path, reason="symbolic link"))
            elif entry.is_dir():
                pending.append(entry)
            else:
                files.append(path)
    return sorted(files)


def _configured_writeback(request: ComputerRequest) -> list[ComputerWriteback]:
    if request.mode != "invocation":
        return []
    return [item for item in request.computer.writeback if item.type != "final_result"]


def _writeback_paths(request: ComputerRequest) -> list[str]:
    return [item.path for item in _configured_writeback(request)]


def _schema_overrides(
    context_files: Mapping[str, str], learning_packs: list[str]
) -> dict[str, dict[str, Any]]:
    schema = destination_schema(context_files, LEARNINGS_PATH)
    if schema is None or not learning_packs:
        return {}
    return {LEARNINGS_PATH: bind_learnings_schema(schema, learning_packs)}


def _collect_outbox(
    root: Path,
    request: ComputerRequest,
    context_files: Mapping[str, str],
    schema_overrides: Mapping[str, Mapping[str, Any]],
) -> tuple[list[_OutboxFile], list[_Rejected]]:
    """What in the outbox would ingest cleanly, and why the rest was left out.

    Nothing here fails the run: the run's answer is the envelope, and a bad side
    file or a malformed line costs only itself. Each line is held to the same
    rules the writeback tools apply, so ingestion never sees what it would refuse.
    """

    rejected: list[_Rejected] = []
    configured = _configured_writeback(request)
    files = _outbox_paths(root, rejected)
    citations = [str(value) for value in request.citation_ids]
    result: list[_OutboxFile] = []
    total = 0
    for path in files:
        if path == request.output_path:
            continue
        declaration = next(
            (
                item
                for item in configured
                if path == item.path
                or (item.type == "maintained_state" and path.startswith(f"{item.path}/"))
            ),
            None,
        )
        if declaration is None or declaration.type == "final_result":
            rejected.append(_Rejected(path=path, reason="not a declared writeback file"))
            continue
        target = root / path.lstrip("/")
        try:
            text = target.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            rejected.append(_Rejected(path=path, reason=f"unreadable: {exc}"))
            continue
        schema = (
            schema_overrides.get(declaration.path)
            or destination_schema(context_files, declaration.path)
            or {}
        )
        if declaration.type == "observations":
            kept: list[str] = []
            for number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                problems = _line_problems(line, schema, citations)
                if problems:
                    rejected.append(_Rejected(path=path, line=number, reason="; ".join(problems)))
                else:
                    kept.append(
                        json.dumps(normalize_candidate(json.loads(line)), ensure_ascii=False)
                    )
            if not kept:
                continue
            content = "\n".join(kept) + "\n"
        else:
            problems = (
                ["proposal is not a .json file"]
                if not path.endswith(".json")
                else _line_problems(text, schema, citations)
            )
            if problems:
                rejected.append(_Rejected(path=path, reason="; ".join(problems)))
                continue
            content = json.dumps(normalize_candidate(json.loads(text)), ensure_ascii=False)
        data = content.encode("utf-8")
        if len(result) >= _OUTBOX_MAX_FILES or total + len(data) > _OUTBOX_MAX_BYTES:
            rejected.append(_Rejected(path=path, reason="outbox size limit reached"))
            continue
        total += len(data)
        result.append(
            _OutboxFile(
                path=path,
                type=declaration.type,
                content=content,
                sha256=hashlib.sha256(data).hexdigest(),
                bytes=len(data),
            )
        )
    return result, rejected


def _outbox_citations(outbox: list[_OutboxFile]) -> frozenset[UUID]:
    cited: set[UUID] = set()
    for file in outbox:
        documents = (
            [json.loads(line) for line in file.content.splitlines() if line.strip()]
            if file.type == "observations"
            else [json.loads(file.content)]
        )
        for document in documents:
            cited.update(UUID(str(value)) for value in document.get("citations", []))
    return frozenset(cited)


def _line_problems(line: str, schema: Mapping[str, Any], citations: list[str]) -> list[str]:
    try:
        candidate = json.loads(line)
    except json.JSONDecodeError as exc:
        return [f"not JSON: {exc.msg}"]
    return check_candidate(candidate, schema, citations)


__all__ = ["LocalComputerProvider", "RunOptions"]
