#!/usr/bin/env bash

# Run the live Cloudflare Agent canary against `wrangler dev` instead of a
# deployed Worker. Nothing is deployed, but the AI binding is always remote,
# so the Workers AI turns are real and billable.
#
# The secret comes from cloudflare/computer-runtime/.dev.vars, so this path
# never touches the deployed Worker's secret or the repository .env.

set -euo pipefail

runtime_dir="cloudflare/computer-runtime"
port="${COMPUTER_RUNTIME_PORT:-8799}"
log="${TMPDIR:-/tmp}/memseek-cloudflare-smoke-dev.log"

[ -d "$runtime_dir" ] || { printf 'error: run from the repository root\n' >&2; exit 1; }
command -v npx >/dev/null || {
  printf 'error: npx not found; node is not on PATH\n' >&2
  exit 1
}

secret=$(sed -n 's/^MEMSEEK_RUNTIME_SECRET=//p' "$runtime_dir/.dev.vars" | tr -d "\"'")
[ -n "$secret" ] || { printf 'error: MEMSEEK_RUNTIME_SECRET missing from %s/.dev.vars\n' "$runtime_dir" >&2; exit 1; }

pid=""
cleanup() { [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

printf 'Starting wrangler dev on port %s (log: %s)\n' "$port" "$log"
( cd "$runtime_dir" && npx wrangler dev --port "$port" ) >"$log" 2>&1 &
pid=$!

for _ in $(seq 1 180); do
  grep -q "Ready on" "$log" 2>/dev/null && break
  kill -0 "$pid" 2>/dev/null || { printf 'error: wrangler dev exited early; see %s\n' "$log" >&2; exit 1; }
  sleep 1
done
grep -q "Ready on" "$log" 2>/dev/null || { printf 'error: wrangler dev never became ready; see %s\n' "$log" >&2; exit 1; }

COMPUTER_RUNTIME_URL="http://127.0.0.1:$port" \
COMPUTER_RUNTIME_TOKEN="$secret" \
  "${UV:-uv}" run python -m memseek.cloudflare_smoke "$@"
