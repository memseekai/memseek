#!/usr/bin/env bash
# Share runtime health checks, persistent logs, and process cleanup with the demo.
# MODE is explicit; this local canary never selects an inherited deployment URL.
set -euo pipefail
unset COMPUTER_RUNTIME_URL COMPUTER_RUNTIME_TOKEN
exec "${UV:-uv}" run python scripts/run_computer_demo.py --mode cloudflare --smoke "$@"
