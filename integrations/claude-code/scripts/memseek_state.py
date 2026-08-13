#!/usr/bin/env python3
"""Concurrency-safe session state and retry spool for the Claude Code plugin."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memseek_client import HttpError, MemseekClient, PluginConfig

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback is exercised on Windows.
    fcntl = None  # type: ignore[assignment]
    try:
        import msvcrt
    except ImportError:
        msvcrt = None  # type: ignore[assignment]
else:
    msvcrt = None  # type: ignore[assignment]


def utc_now() -> str:
    # datetime.UTC arrived in Python 3.11; the standalone plugin supports Python 3.10.
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def hook_session_id(payload: dict[str, Any]) -> str:
    """Return one stable, schema-bounded identifier for a Claude session."""

    native = payload.get("session_id")
    if isinstance(native, str) and native.strip():
        source = native.strip()
    else:
        transcript = payload.get("transcript_path")
        cwd = payload.get("cwd")
        source = str(transcript or cwd or "unknown-session")
    candidate = f"claude:{source}"
    if len(candidate) <= 128:
        return candidate
    return f"claude:sha256:{hashlib.sha256(source.encode('utf-8')).hexdigest()}"


def _secure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with suppress(OSError):
        path.chmod(0o700)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _secure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
    finally:
        with suppress(FileNotFoundError):
            temporary_path.unlink()


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    _secure_directory(path.parent)
    path.touch(mode=0o600, exist_ok=True)
    with suppress(OSError):
        path.chmod(0o600)
    with path.open("r+", encoding="utf-8") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows only.
            if path.stat().st_size == 0:
                stream.write("\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows only.
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


@dataclass(frozen=True)
class EventAllocation:
    ordinal: int
    dedupe_key: str
    replay: bool


class SessionStore:
    """Persist the minimum state needed across independent hook processes."""

    def __init__(self, config: PluginConfig, session_id: str) -> None:
        self.config = config
        self.session_id = session_id
        _secure_directory(config.state_dir)
        key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        self.path = config.state_dir / "sessions" / f"{key}.json"
        self.lock_path = config.state_dir / "locks" / f"{key}.lock"

    def _default(self) -> dict[str, Any]:
        return {
            "version": 1,
            "session_id": self.session_id,
            "entity": self.config.entity,
            "skill_entity": self.config.skill_entity,
            "project_root": str(self.config.project_root),
            "next_ordinal": 0,
            "events": {},
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._default()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._default()
        return value if isinstance(value, dict) else self._default()

    def initialise(self) -> dict[str, Any]:
        with _lock(self.lock_path):
            state = self._read()
            state.update(
                {
                    "session_id": self.session_id,
                    "entity": self.config.entity,
                    "skill_entity": self.config.skill_entity,
                    "project_root": str(self.config.project_root),
                    "updated_at": utc_now(),
                }
            )
            _atomic_json(self.path, state)
            return state

    def load(self) -> dict[str, Any]:
        with _lock(self.lock_path):
            return self._read()

    def allocate_event(
        self, *, role: str, text: str, turn_id: str | int | None = None
    ) -> EventAllocation:
        """Allocate an ordinal, coalescing a hook retry without merging later repeats."""

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        now = time.time()
        with _lock(self.lock_path):
            state = self._read()
            events = state.setdefault("events", {})
            explicit_key = f"{role}:turn:{turn_id}" if turn_id is not None else None
            recent = state.get("last_event", {})
            retry = (
                explicit_key is None
                and recent.get("role") == role
                and recent.get("digest") == digest
                and now - float(recent.get("allocated_at", 0)) <= 8
            )
            event_key = explicit_key or (recent.get("key") if retry else None)
            if event_key and event_key in events:
                event = events[event_key]
                return EventAllocation(
                    ordinal=int(event["ordinal"]),
                    dedupe_key=str(event["dedupe_key"]),
                    replay=True,
                )
            # Epoch milliseconds preserve order with explicit MCP writes, while the
            # persisted floor makes two events in the same millisecond deterministic.
            ordinal = max(int(state.get("next_ordinal", 0)), int(now * 1000))
            state["next_ordinal"] = ordinal + 1
            token = str(turn_id) if turn_id is not None else str(ordinal)
            session_hash = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()[:16]
            dedupe_key = f"claude:{session_hash}:{role}:{token}"
            event_key = explicit_key or f"{role}:ordinal:{ordinal}"
            events[event_key] = {
                "ordinal": ordinal,
                "dedupe_key": dedupe_key,
                "digest": digest,
            }
            if len(events) > 256:
                for old_key in list(events)[: len(events) - 256]:
                    del events[old_key]
            state["last_event"] = {
                "key": event_key,
                "role": role,
                "digest": digest,
                "allocated_at": now,
            }
            state["updated_at"] = utc_now()
            _atomic_json(self.path, state)
            return EventAllocation(ordinal=ordinal, dedupe_key=dedupe_key, replay=False)

    def remember_context(
        self,
        *,
        task: str,
        use_id: str | None = None,
        truncated: bool | None = None,
    ) -> None:
        with _lock(self.lock_path):
            state = self._read()
            state["last_task"] = task
            if use_id:
                state["last_use_id"] = use_id
            if truncated is not None:
                state["last_render_truncated"] = truncated
            state["updated_at"] = utc_now()
            _atomic_json(self.path, state)


class PendingSpool:
    """Atomic local queue: a short Memseek outage never blocks Claude Code."""

    def __init__(self, config: PluginConfig) -> None:
        self.config = config
        _secure_directory(config.state_dir)
        self.pending_dir = config.state_dir / "pending"
        self.failed_dir = config.state_dir / "failed"

    def enqueue(self, *, kind: str, dedupe_key: str, payload: dict[str, Any]) -> Path:
        key = hashlib.sha256(f"{kind}:{dedupe_key}".encode()).hexdigest()
        path = self.pending_dir / f"{key}.json"
        if not path.exists():
            _atomic_json(
                path,
                {
                    "version": 1,
                    "kind": kind,
                    "dedupe_key": dedupe_key,
                    "payload": payload,
                    "queued_at": utc_now(),
                },
            )
        return path

    def counts(self) -> tuple[int, int]:
        pending = len(list(self.pending_dir.glob("*.json"))) if self.pending_dir.exists() else 0
        failed = len(list(self.failed_dir.glob("*.json"))) if self.failed_dir.exists() else 0
        return pending, failed

    def flush(self, client: MemseekClient, *, limit: int = 50) -> dict[str, Any]:
        sent = 0
        errors: list[str] = []
        if not self.pending_dir.exists():
            return {"sent": 0, "remaining": 0, "errors": []}
        for path in sorted(self.pending_dir.glob("*.json"))[:limit]:
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                kind = envelope["kind"]
                payload = envelope["payload"]
                if kind == "records":
                    client.records(payload["records"])
                elif kind == "feedback":
                    client.feedback(payload["use_id"], payload["feedback"])
                else:
                    raise ValueError(f"unknown queue kind {kind!r}")
            except HttpError as exc:
                # Authentication and throttling errors may become valid later. A malformed
                # payload will not, so quarantine it without blocking later queue entries.
                if exc.status in {400, 404, 409, 422}:
                    _secure_directory(self.failed_dir)
                    path.replace(self.failed_dir / path.name)
                else:
                    errors.append(str(exc))
                    break
                errors.append(str(exc))
                continue
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                _secure_directory(self.failed_dir)
                path.replace(self.failed_dir / path.name)
                errors.append(f"invalid queued event {path.name}: {exc}")
                continue
            except Exception as exc:  # Network/config failures stay queued for the next hook.
                errors.append(str(exc))
                break
            path.unlink(missing_ok=True)
            sent += 1
        remaining, _ = self.counts()
        return {"sent": sent, "remaining": remaining, "errors": errors[:5]}
