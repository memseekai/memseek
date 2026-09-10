"""Launch the renewal example with one explicit runtime mode.

Owns only runtime processes it starts. Docker services and their data remain
available after exit; use the normal compose lifecycle to stop them.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "cloudflare/computer-runtime"


def health(url: str) -> str | None:
    try:
        with urlopen(f"{url.rstrip('/')}/health", timeout=2) as response:
            payload = json.load(response)
            return payload.get("provider") if isinstance(payload, dict) else None
    except OSError, URLError, ValueError:
        return None


def check_port(port: int) -> None:
    with socket.socket() as sock:
        # Match the servers' restart behavior: TIME_WAIT from our previous run
        # is not an occupied listener. A live listener still prevents binding.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as exc:
            provider = health(f"http://127.0.0.1:{port}")
            raise RuntimeError(
                f"Port {port} is occupied (provider: {provider or 'unknown'}). Choose COMPUTER_RUNTIME_PORT or stop its owner."
            ) from exc


def wait_runtime(
    process: subprocess.Popen[bytes], url: str, expected: str, timeout: float = 180
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Runtime exited during startup ({process.returncode})")
        provider = health(url)
        if provider is not None:
            if provider != expected:
                raise RuntimeError(f"Expected {expected}, got {provider} at {url}")
            return
        time.sleep(0.25)
    raise RuntimeError(f"Runtime did not become healthy at {url}")


def stop_owned(process: subprocess.Popen[bytes]) -> None:
    if not hasattr(os, "killpg") or not hasattr(signal, "SIGKILL"):
        raise RuntimeError("The demo launcher requires a POSIX shell (use WSL on Windows)")
    # start_new_session gives this launcher ownership of the whole runtime tree,
    # including Wrangler's workerd child. Never signal a discovered runtime.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def launch(args: argparse.Namespace) -> None:
    if not hasattr(os, "killpg") or not hasattr(signal, "SIGKILL"):
        raise RuntimeError("The demo launcher requires a POSIX shell (use WSL on Windows)")
    smoke = getattr(args, "smoke", False)
    if smoke and args.mode != "cloudflare":
        raise RuntimeError("--smoke requires --mode cloudflare")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    compose = shlex.split(env.get("COMPOSE", "docker compose"))
    if not compose or shutil.which(compose[0]) is None:
        raise RuntimeError("Docker Compose is required for make computer-demo")
    subprocess.run([*compose, "version"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run([*compose, "ls", "--format", "json"], check=True, stdout=subprocess.DEVNULL)
    port = int(env.get("COMPUTER_RUNTIME_PORT", "8799"))
    expected = "cloudflare" if args.mode == "cloudflare" else "local-standin"
    remote = env.get("COMPUTER_RUNTIME_URL", "").strip()
    command: list[str] | None = None
    cwd = ROOT
    if args.mode == "local":
        # MODE is authoritative. Shells often carry a deployed runtime URL;
        # local execution always supplies its own endpoint and matching token.
        remote = ""
        token = env.get("COMPUTER_RUNTIME_SECRET", "local-computer-demo")
        command = [sys.executable, str(ROOT / "examples/_computer_runtime.py"), "--port", str(port)]
    elif remote:
        token = env.get("COMPUTER_RUNTIME_TOKEN", "")
        if not token:
            raise RuntimeError("Remote Cloudflare mode requires COMPUTER_RUNTIME_TOKEN")
        if health(remote) != expected:
            raise RuntimeError(f"Expected cloudflare runtime at {remote}")
    else:
        wrangler = RUNTIME / "node_modules/.bin/wrangler"
        if not wrangler.exists() or shutil.which("node") is None:
            raise RuntimeError("Install Node.js and run npm ci in cloudflare/computer-runtime")
        token = str(dotenv_values(RUNTIME / ".dev.vars").get("MEMSEEK_RUNTIME_SECRET") or "")
        if not token:
            raise RuntimeError(
                "Set MEMSEEK_RUNTIME_SECRET in cloudflare/computer-runtime/.dev.vars and authenticate Wrangler"
            )
        command = [str(wrangler), "dev", "--port", str(port)]
        cwd = RUNTIME
    if not token:
        raise RuntimeError("Computer runtime secret must not be empty")
    if command:
        check_port(port)
    host_url = remote or f"http://127.0.0.1:{port}"
    container_url = remote or f"http://host.docker.internal:{port}"
    env.update(COMPUTER_RUNTIME_TOKEN=token, COMPUTER_RUNTIME_URL=container_url)
    process = None
    log_dir = Path(env.get("COMPUTER_RUNTIME_LOG_DIR", str(ROOT / ".memseek/logs")))
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # NamedTemporaryFile creates a private, unique file (0600), retained for
    # diagnosis after runtime shutdown instead of losing the first exception.
    with (
        tempfile.NamedTemporaryFile(
            prefix="computer-runtime-", suffix=".log", dir=log_dir, delete=False
        )
        if command
        else tempfile.TemporaryFile()
    ) as log:
        if command:
            print(f"Runtime log: {log.name}", flush=True)
            if args.mode == "cloudflare":
                print(
                    "Cloudflare development: Worker and Durable Objects run locally; "
                    "Workers AI runs in your account. No cloud resources are deployed.",
                    flush=True,
                )
        else:
            print(
                "Remote runtime selected; server diagnostics are in its Workers logs.", flush=True
            )
        try:
            if command:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
                wait_runtime(process, host_url, expected)
            print(f"Runtime ready: {expected} at {host_url}", flush=True)
            if not smoke:
                subprocess.run(
                    [*compose, "up", "-d", "--build", "--wait", "api", "worker"],
                    cwd=ROOT,
                    env=env,
                    check=True,
                )
            env.update(
                DATABASE_URL=f"postgresql://postgres:postgres@127.0.0.1:{env.get('MEMSEEK_DB_PORT', '5433')}/memseek",
                COMPUTER_RUNTIME_URL=host_url,
                MEMSEEK_BASE_URL=f"http://127.0.0.1:{env.get('MEMSEEK_PORT', '8000')}",
            )
            # Always create a demo workspace; never publish into an inherited key.
            env.pop("MEMSEEK_API_KEY", None)
            example = "computer_renewal_advanced.py" if args.advanced else "computer_renewal.py"
            cmd = [sys.executable, str(ROOT / "examples" / example)]
            if smoke:
                cmd = [sys.executable, "-m", "memseek.cloudflare_smoke"]
                if getattr(args, "run_id", None):
                    cmd.extend(["--run-id", args.run_id])
            else:
                if args.mode == "cloudflare":
                    cmd.append("--cloudflare")
                if args.scripted:
                    cmd.append("--scripted")
            subprocess.run(cmd, cwd=ROOT, env=env, check=True)
        except BaseException:
            log.seek(0)
            detail = log.read().decode(errors="replace").replace(token, "[redacted]")
            if detail:
                print(detail[-6000:], file=sys.stderr)
            raise
        finally:
            try:
                if process is not None:
                    stop_owned(process)
            finally:
                # Preserve the entire output, including shutdown, with the
                # shared runtime credential removed if any dependency logged it.
                log.seek(0)
                content = log.read().replace(token.encode(), b"[redacted]")
                log.seek(0)
                log.write(content)
                log.truncate()
                log.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("local", "cloudflare"), default="local")
    parser.add_argument("--scripted", action="store_true")
    parser.add_argument("--advanced", action="store_true")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run the real Agent canary without starting the Memseek database/API/worker",
    )
    parser.add_argument("--run-id", help="Unique smoke canary ID (only with --smoke)")
    args = parser.parse_args()
    if args.run_id and not args.smoke:
        parser.error("--run-id requires --smoke")

    # A SIGTERM should unwind the same ownership cleanup as Ctrl-C.
    def interrupted(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        launch(args)
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
