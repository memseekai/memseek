"""Operator-run canary for the live Cloudflare Agent + Computer runtime.

The canary deliberately bypasses PostgreSQL and the invocation service. It
exercises the smallest useful production seam: MemSeek's signed remote
provider, a real Workers AI tool loop, and two tasks sharing one durable
Computer session.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx

from memseek.computers import (
    ComputerExecutionError,
    ComputerRequest,
    ComputerResult,
    RemoteComputerProvider,
)
from memseek.config import Settings
from memseek.definitions import load_definition_catalog

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPOSITORY_ROOT / "examples" / "computer_renewal_catalog"
WORKSPACE = "cloudflare-agent-smoke"
COMPUTER_REF = "research_workspace@1"
AGENT_REF = "renewal_analyst@1"
CONTEXT_POLICY_REF = "evidence_spine@1"
PROOF_PATH = "/workspace/proof.json"
OUTPUT_PATH = "/outbox/final-result.json"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SmokeFailure(RuntimeError):
    """A live canary invariant was not satisfied."""


@dataclass(frozen=True, slots=True)
class CloudflareSmokePlan:
    """The two requests and identities used by one canary run."""

    run_id: str
    marker: str
    evidence_id: UUID
    session_key: str
    model_target: str
    write_request: ComputerRequest
    read_request: ComputerRequest


def _catalog_settings(settings: Settings) -> Settings:
    root = CATALOG_ROOT
    return settings.model_copy(
        update={
            "models_file": root / "conf/models.yaml",
            "processors_file": root / "conf/processors.yaml",
            "collections_dir": root / "collections",
            "derivations_dir": root / "derivations",
            "triggers_dir": None,
            "views_dir": None,
            "artifacts_dir": root / "artifacts",
            "computers_dir": root / "computers",
            "programs_dir": root / "programs",
            "agents_dir": root / "agents",
            "context_policies_dir": root / "context_policies",
            "mcp_dir": root / "mcp",
            "packages_dir": root / "packages",
            "search_profiles_file": root / "conf/search_profiles.yaml",
            "rank_default_file": root / "conf/rank_default.yaml",
        }
    )


def _session_key(run_id: str) -> str:
    return hashlib.sha256(f"{WORKSPACE}\0{run_id}\0{COMPUTER_REF}".encode()).hexdigest()


def _context_files(evidence_id: UUID) -> dict[str, str]:
    evidence_text = (
        "This is a runtime canary, not customer data. The only authorized source "
        f"record is {evidence_id}. Cite that UUID after completing the requested "
        "filesystem operation."
    )
    manifest = {
        "kind": "cloudflare_agent_smoke",
        "definitions": {
            "computer": COMPUTER_REF,
            "agent": AGENT_REF,
            "context_policy": CONTEXT_POLICY_REF,
        },
        "source_records": [
            {
                "id": str(evidence_id),
                "sha256": hashlib.sha256(evidence_text.encode()).hexdigest(),
            }
        ],
    }
    return {
        "/.memseek/instructions.md": (
            "Perform the runtime canary exactly as described by the task input. "
            "A named filesystem tool is mandatory; do not merely claim it was used. "
            "Return the MemSeek JSON envelope only after its tool result succeeds."
        ),
        "/.memseek/context.md": evidence_text,
        "/.memseek/manifest.json": json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    }


def build_cloudflare_smoke_plan(settings: Settings, run_id: str) -> CloudflareSmokePlan:
    """Resolve the fixture and build two live requests sharing one session."""

    if not _RUN_ID.fullmatch(run_id):
        raise SmokeFailure("run id must contain 1-64 letters, digits, dots, dashes, or underscores")

    catalog = load_definition_catalog(_catalog_settings(settings))
    source_computer = catalog.resolve_computer(COMPUTER_REF)
    # The committed fixture remains deterministic for CI. The operator-run
    # canary changes only the provider at the invocation boundary.
    computer = source_computer.model_copy(update={"provider": "cloudflare"})
    agent = catalog.resolve_agent(AGENT_REF)
    context_policy = catalog.resolve_context_policy(CONTEXT_POLICY_REF)
    model_alias = catalog.models.aliases[agent.model]
    model_target = model_alias.targets[0]
    if not model_target.startswith(("workers_ai:@cf/", "workers-ai:@cf/")):
        raise SmokeFailure(f"smoke Agent model is not a Workers AI target: {model_target}")

    evidence_id = uuid5(NAMESPACE_URL, f"https://memseek.dev/cloudflare-smoke/{run_id}")
    marker = f"memseek-cloudflare-smoke-{run_id}"
    session_key = _session_key(run_id)
    context_files = _context_files(evidence_id)
    model = {
        "alias": agent.model,
        "targets": list(model_alias.targets),
        "params": dict(model_alias.params),
    }
    common: dict[str, Any] = {
        "mode": "invocation",
        "workspace": WORKSPACE,
        "entity": f"smoke:{run_id}",
        "session_key": session_key,
        "computer_ref": COMPUTER_REF,
        "computer": computer,
        "source_ids": frozenset({evidence_id}),
        "citation_ids": frozenset({evidence_id}),
        "output_path": OUTPUT_PATH,
        "agent_ref": AGENT_REF,
        "agent": agent,
        "context_policy_ref": CONTEXT_POLICY_REF,
        "context_policy": context_policy,
        "context_files": context_files,
        "model": model,
    }
    proof = json.dumps(
        {"evidence_id": str(evidence_id), "marker": marker},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    write_value = {
        "evidence_id": str(evidence_id),
        "marker": marker,
        "phase": "write",
        "proof_path": PROOF_PATH,
    }
    write_request = ComputerRequest(
        **common,
        task_id=f"cloudflare-smoke-write:{run_id}",
        input={
            "kind": "cloudflare_agent_smoke_write",
            "required_action": {
                "tool": "write",
                "arguments": {"path": PROOF_PATH, "content": proof},
            },
            "rules": [
                "You MUST call the write tool with exactly the path and content above.",
                "Do not use exec or merely describe the write.",
                "After the write tool succeeds, return the exact required_final_value.",
                "The envelope citation_ids must contain the one visible source UUID.",
            ],
            "required_final_value": write_value,
        },
    )
    # Deliberately omit the marker from the second request and from all context
    # files. The Agent can return it only after opening durable session state.
    read_request = ComputerRequest(
        **common,
        task_id=f"cloudflare-smoke-read:{run_id}",
        input={
            "kind": "cloudflare_agent_smoke_read",
            "required_action": {
                "tool": "read",
                "arguments": {"path": PROOF_PATH},
            },
            "rules": [
                "You MUST call the read tool on the exact path above.",
                "Parse the JSON returned by that tool; do not guess or use recall instead.",
                "Return phase='read', proof_path, and the marker and evidence_id from the file.",
                "The envelope citation_ids must contain the one visible source UUID.",
            ],
        },
    )
    return CloudflareSmokePlan(
        run_id=run_id,
        marker=marker,
        evidence_id=evidence_id,
        session_key=session_key,
        model_target=model_target,
        write_request=write_request,
        read_request=read_request,
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _tool_events(receipt: Mapping[str, Any], kind: str, tool_name: str) -> list[Mapping[str, Any]]:
    matches: list[Mapping[str, Any]] = []
    events = receipt.get("events")
    if not isinstance(events, list):
        return matches
    for event in events:
        if not isinstance(event, Mapping) or event.get("kind") != kind:
            continue
        payload = event.get("payload")
        if isinstance(payload, Mapping) and payload.get("tool_name") == tool_name:
            matches.append(payload)
    return matches


def validate_cloudflare_smoke_result(
    result: ComputerResult,
    plan: CloudflareSmokePlan,
    phase: Literal["write", "read"],
) -> dict[str, Any]:
    """Validate model output, proof events, and provider receipt for one phase."""

    value = result.value
    _require(isinstance(value, Mapping), f"{phase} Agent value is not an object")
    assert isinstance(value, Mapping)
    _require(value.get("phase") == phase, f"{phase} Agent returned the wrong phase")
    _require(value.get("proof_path") == PROOF_PATH, f"{phase} Agent returned the wrong path")
    _require(value.get("marker") == plan.marker, f"{phase} Agent did not return the proof marker")
    _require(
        value.get("evidence_id") == str(plan.evidence_id),
        f"{phase} Agent did not return the proof evidence id",
    )
    _require(
        result.citation_ids == frozenset({plan.evidence_id}),
        f"{phase} Agent did not preserve the exact visible citation set",
    )
    _require(not result.awaiting_input, f"{phase} Agent unexpectedly requested user input")
    _require(result.steps >= 2, f"{phase} Agent completed without a tool round trip")

    receipt = result.receipt
    _require(receipt.get("provider") == "cloudflare", f"{phase} receipt is not Cloudflare")
    _require(
        receipt.get("backend") == "worker-javascript",
        f"{phase} receipt did not use worker-javascript",
    )
    _require(
        receipt.get("session_key") == plan.session_key,
        f"{phase} receipt changed session",
    )
    _require(receipt.get("resumed") is False, f"{phase} unexpectedly replayed a cached task")
    output_hash = receipt.get("output_sha256")
    _require(
        isinstance(output_hash, str) and re.fullmatch(r"[0-9a-f]{64}", output_hash) is not None,
        f"{phase} receipt has no valid output hash",
    )

    calls = _tool_events(receipt, "tool_call", phase)
    _require(bool(calls), f"{phase} receipt has no {phase} tool call")
    tool_input = calls[0].get("input")
    _require(
        isinstance(tool_input, Mapping) and tool_input.get("path") == PROOF_PATH,
        f"{phase} tool call did not target {PROOF_PATH}",
    )
    _require(
        bool(_tool_events(receipt, "tool_result", phase)),
        f"{phase} receipt has no {phase} tool result",
    )
    if phase == "write":
        files = receipt.get("files")
        proof_in_diff = isinstance(files, list) and any(
            isinstance(entry, Mapping) and entry.get("path") == PROOF_PATH for entry in files
        )
        _require(proof_in_diff, f"write receipt has no filesystem diff for {PROOF_PATH}")

    return {
        "task_id": receipt.get("task_id"),
        "steps": result.steps,
        "output_sha256": output_hash,
        "tool": phase,
    }


async def _check_health(settings: Settings) -> None:
    base = settings.computer_runtime_url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SmokeFailure("COMPUTER_RUNTIME_URL must be an absolute HTTP(S) URL")
    try:
        async with httpx.AsyncClient(
            timeout=min(settings.computer_request_timeout_s, 15)
        ) as client:
            response = await client.get(f"{base}/health")
        if response.status_code != 200:
            detail = " ".join(response.text.split())[:240]
            error_code = re.search(r"error code:\s*\d+", response.text, re.IGNORECASE)
            if error_code:
                detail = error_code.group(0)
            suffix = f": {detail}" if detail else ""
            raise SmokeFailure(f"health endpoint returned HTTP {response.status_code}{suffix}")
        payload = response.json()
    except SmokeFailure:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise SmokeFailure(f"Cloudflare runtime health check failed: {type(exc).__name__}") from exc
    if payload != {"ok": True, "provider": "cloudflare"}:
        raise SmokeFailure("Cloudflare runtime returned an unexpected health payload")


async def run_cloudflare_agent_smoke(
    settings: Settings,
    *,
    run_id: str,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the health, write, and reopen phases against a deployed runtime."""

    if not settings.computer_runtime_url or not settings.computer_runtime_token:
        raise SmokeFailure(
            "set COMPUTER_RUNTIME_URL and COMPUTER_RUNTIME_TOKEN before running the canary"
        )
    plan = build_cloudflare_smoke_plan(settings, run_id)
    report = progress or (lambda _message: None)
    report("[1/3] checking the deployed Cloudflare runtime")
    await _check_health(settings)
    provider = RemoteComputerProvider(settings)
    report("[2/3] asking Workers AI to write the durable proof file")
    write_result = await provider.execute(plan.write_request)
    write_summary = validate_cloudflare_smoke_result(write_result, plan, "write")
    report("[3/3] starting a fresh task that must reopen the same proof file")
    read_result = await provider.execute(plan.read_request)
    read_summary = validate_cloudflare_smoke_result(read_result, plan, "read")
    return {
        "ok": True,
        "runtime": settings.computer_runtime_url.rstrip("/"),
        "run_id": plan.run_id,
        "session_key": plan.session_key,
        "computer": COMPUTER_REF,
        "agent": AGENT_REF,
        "model": plan.model_target,
        "backend": "worker-javascript",
        "proof": {"path": PROOF_PATH, "marker": plan.marker},
        "phases": {"write": write_summary, "read": read_summary},
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a real Workers AI Agent twice against one Cloudflare Computer session and "
            "prove that the second task can reopen the first task's file."
        )
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="unique canary id (default: a new random id; reuse is rejected as a cache replay)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run_id = args.run_id or uuid4().hex[:12]
    try:
        summary = asyncio.run(
            run_cloudflare_agent_smoke(
                Settings(),
                run_id=run_id,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
        )
    except (ComputerExecutionError, SmokeFailure) as exc:
        print(f"Cloudflare Agent smoke failed: {exc}", file=sys.stderr)
        print(
            "Inspect the deployed Worker with wrangler tail for the underlying exception.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Cloudflare Agent smoke cancelled", file=sys.stderr)
        return 130
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
