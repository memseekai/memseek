#!/usr/bin/env python3
"""Small, dependency-free Memseek HTTP client used by the Claude Code plugin."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_NAME = "memseek-claude-code"


class MemseekError(RuntimeError):
    """Base error for errors safe to display without leaking credentials."""


class ConfigurationError(MemseekError):
    """The local plugin configuration is incomplete or invalid."""


class HttpError(MemseekError):
    """Memseek returned an HTTP error."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"Memseek HTTP {status}: {message}")


@dataclass(frozen=True)
class PluginConfig:
    """Resolved runtime configuration. Secrets are deliberately omitted from repr."""

    base_url: str
    api_key: str
    entity: str
    skill_entity: str
    context_artifact: str
    capture_mode: str
    timeout: float
    state_dir: Path
    project_root: Path

    def __repr__(self) -> str:
        return (
            "PluginConfig("
            f"base_url={self.base_url!r}, api_key='<redacted>', entity={self.entity!r}, "
            f"skill_entity={self.skill_entity!r}, context_artifact={self.context_artifact!r}, "
            f"capture_mode={self.capture_mode!r}, timeout={self.timeout!r}, "
            f"state_dir={str(self.state_dir)!r}, project_root={str(self.project_root)!r})"
        )


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def find_project_root(cwd: Path) -> Path:
    root = _git(["rev-parse", "--show-toplevel"], cwd)
    return Path(root).resolve() if root else cwd.resolve()


def _normalise_remote(remote: str) -> str:
    """Return a stable remote identity with embedded credentials removed."""

    value = remote.strip()
    scp_match = re.fullmatch(r"(?:[^@/]+@)?([^:]+):(.+)", value)
    if scp_match and "://" not in value:
        host, path = scp_match.groups()
        value = f"ssh://{host}/{path}"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme and parsed.hostname:
        port = f":{parsed.port}" if parsed.port else ""
        value = f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}{parsed.path}"
    else:
        value = str(Path(value).expanduser().resolve())
    return value.rstrip("/").removesuffix(".git")


def project_entity(project_root: Path, override: str | None = None) -> str:
    """Build a non-secret, cross-agent entity from the project remote or root."""

    if override and override.strip():
        return override.strip()
    remote = _git(["remote", "get-url", "origin"], project_root)
    identity = _normalise_remote(remote) if remote else str(project_root.resolve())
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    name_source = identity.rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name_source).strip("-.") or "project"
    return f"project:{name[:48]}:{digest}"


def _validate_entity(value: str, setting_name: str) -> str:
    if len(value) > 128:
        raise ConfigurationError(f"{setting_name} must be at most 128 characters")
    if any(ord(character) < 32 for character in value):
        raise ConfigurationError(f"{setting_name} cannot contain control characters")
    return value


def _project_overrides(project_root: Path) -> dict[str, str]:
    path = project_root / ".memseek-project.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"invalid project configuration at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError(f"project configuration at {path} must be a JSON object")
    return {
        key: value.strip()
        for key, value in payload.items()
        if key in {"entity", "skill_entity"} and isinstance(value, str) and value.strip()
    }


def load_config(cwd: str | Path | None = None) -> PluginConfig:
    """Resolve environment and non-secret project configuration."""

    workdir = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
    project_root = find_project_root(workdir)

    def setting(name: str, default: str = "") -> str:
        # During a normal plugin install, Claude Code prompts for userConfig and exports
        # each answer to hooks as CLAUDE_PLUGIN_OPTION_<KEY>. Prefer that source so hooks
        # and the bundled MCP connection cannot silently use different credentials.
        value = os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name}")
        if value is None:
            # Shell exports remain useful to standalone operator scripts and tests.
            value = os.environ.get(name, default)
        return value.strip()

    project = _project_overrides(project_root)
    entity = _validate_entity(
        project_entity(project_root, setting("MEMSEEK_ENTITY") or project.get("entity")),
        "MEMSEEK_ENTITY",
    )
    skill_entity = setting("MEMSEEK_SKILL_ENTITY") or project.get("skill_entity")
    if not skill_entity:
        skill_entity = f"skill:{entity}:coding"
    skill_entity = _validate_entity(skill_entity, "MEMSEEK_SKILL_ENTITY")
    capture_mode = (setting("MEMSEEK_CAPTURE_MODE", "conversation") or "conversation").lower()
    if capture_mode not in {"conversation", "explicit", "off"}:
        raise ConfigurationError(
            "MEMSEEK_CAPTURE_MODE must be one of conversation, explicit, or off"
        )
    try:
        timeout = float(setting("MEMSEEK_TIMEOUT_SECONDS", "5"))
    except ValueError as exc:
        raise ConfigurationError("MEMSEEK_TIMEOUT_SECONDS must be a number") from exc
    if timeout <= 0 or timeout > 30:
        raise ConfigurationError("MEMSEEK_TIMEOUT_SECONDS must be greater than 0 and at most 30")
    base_url = setting("MEMSEEK_URL", "http://127.0.0.1:8000").rstrip("/")
    parsed_url = urllib.parse.urlsplit(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ConfigurationError("MEMSEEK_URL must be an absolute http:// or https:// URL")
    if parsed_url.username or parsed_url.password:
        raise ConfigurationError("MEMSEEK_URL cannot contain credentials; use MEMSEEK_API_KEY")
    state_value = setting("MEMSEEK_PLUGIN_STATE_DIR")
    state_dir = (
        Path(state_value).expanduser()
        if state_value
        else Path.home() / ".memseek" / "plugin" / "claude-code"
    )
    return PluginConfig(
        base_url=base_url,
        api_key=setting("MEMSEEK_API_KEY"),
        entity=entity,
        skill_entity=skill_entity,
        context_artifact=setting("MEMSEEK_CONTEXT_ARTIFACT", "agent_context"),
        capture_mode=capture_mode,
        timeout=timeout,
        state_dir=state_dir,
        project_root=project_root,
    )


class MemseekClient:
    """Narrow HTTP adapter for the routes used by the plugin."""

    def __init__(self, config: PluginConfig) -> None:
        self.config = config

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        if authenticated and not self.config.api_key:
            raise ConfigurationError(
                "MEMSEEK_API_KEY is not set; export it before starting Claude Code"
            )
        body = None
        headers = {"Accept": "application/json", "User-Agent": PLUGIN_NAME}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = urllib.request.Request(
            f"{self.config.base_url}{path}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read(2048)
            try:
                detail = json.loads(raw).get("detail", raw.decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = raw.decode("utf-8", "replace")
            raise HttpError(exc.code, str(detail)[:500]) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise MemseekError(f"cannot reach Memseek at {self.config.base_url}: {reason}") from exc
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemseekError("Memseek returned a non-JSON response") from exc
        if not isinstance(value, dict):
            raise MemseekError("Memseek returned an unexpected response shape")
        return value

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", authenticated=False)

    def tools(self) -> dict[str, Any]:
        return self._request("GET", "/tools")

    def bind_context(self, *, task: str) -> dict[str, Any]:
        artifact = urllib.parse.quote(self.config.context_artifact, safe="@")
        return self._request(
            "POST",
            f"/artifacts/{artifact}/uses",
            {
                "parameters": {
                    "entity": self.config.entity,
                    "task": task,
                    "skill": self.config.skill_entity,
                },
                "snapshot": False,
            },
        )

    def render_context(self, *, task: str) -> dict[str, Any]:
        artifact = urllib.parse.quote(self.config.context_artifact, safe="@")
        return self._request(
            "POST",
            f"/artifacts/{artifact}/render",
            {
                "entity": self.config.entity,
                "task": task,
                "skill": self.config.skill_entity,
            },
        )

    def records(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", "/records", {"records": records})

    def feedback(self, use_id: str, feedback: dict[str, Any]) -> dict[str, Any]:
        identity = urllib.parse.quote(use_id, safe="")
        return self._request("POST", f"/artifact-uses/{identity}/feedback", feedback)

    def publish_catalog(
        self,
        *,
        package: str,
        files: dict[str, str],
        dry_run: bool,
    ) -> dict[str, Any]:
        suffix = "?dry_run=true" if dry_run else ""
        return self._request(
            "POST",
            f"/catalog{suffix}",
            {"package": package, "files": files},
        )
