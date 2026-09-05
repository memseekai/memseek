#!/usr/bin/env bash

# Interactive setup for the live Cloudflare Agent canary.
#
# This script writes .env (used by MemSeek's Settings) and .env.sh (a
# sourceable export file). It never prints the secret. Run it directly to
# configure the files; source .env.sh afterwards when you need shell exports.

set -euo pipefail

env_file="${MEMSEEK_ENV_FILE:-.env}"
export_file="${MEMSEEK_EXPORT_FILE:-.env.sh}"

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

is_valid_runtime_url() {
  case "$1" in
    https://?*|http://localhost:?*|http://127.0.0.1:?*|http://\[::1\]:?*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

escape_single_quoted() {
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
}

upsert_dotenv() {
  local path="$1"
  local url="$2"
  local token="$3"
  local directory
  directory=$(dirname -- "$path")
  [ -d "$directory" ] || die "directory does not exist: $directory"

  local temporary
  temporary=$(mktemp "${path}.tmp.XXXXXX")
  trap 'rm -f -- "$temporary"' RETURN
  if [ -f "$path" ]; then
    grep -v -E '^(COMPUTER_RUNTIME_URL|COMPUTER_RUNTIME_TOKEN)=' "$path" >"$temporary" || true
  fi
  {
    cat "$temporary"
    printf "COMPUTER_RUNTIME_URL='%s'\n" "$(escape_single_quoted "$url")"
    printf "COMPUTER_RUNTIME_TOKEN='%s'\n" "$(escape_single_quoted "$token")"
  } >"${temporary}.next"
  mv -- "${temporary}.next" "$path"
  chmod 600 "$path"
  trap - RETURN
  rm -f -- "$temporary"
}

upsert_exports() {
  local path="$1"
  local url="$2"
  local token="$3"
  local directory
  directory=$(dirname -- "$path")
  [ -d "$directory" ] || die "directory does not exist: $directory"

  local temporary
  temporary=$(mktemp "${path}.tmp.XXXXXX")
  trap 'rm -f -- "$temporary"' RETURN
  if [ -f "$path" ]; then
    grep -v -E '^export (COMPUTER_RUNTIME_URL|COMPUTER_RUNTIME_TOKEN)=' "$path" >"$temporary" || true
  fi
  {
    cat "$temporary"
    printf "export COMPUTER_RUNTIME_URL='%s'\n" "$(escape_single_quoted "$url")"
    printf "export COMPUTER_RUNTIME_TOKEN='%s'\n" "$(escape_single_quoted "$token")"
  } >"${temporary}.next"
  mv -- "${temporary}.next" "$path"
  chmod 600 "$path"
  trap - RETURN
  rm -f -- "$temporary"
}

printf '%s\n' "Cloudflare Agent smoke setup"
printf '%s\n' "The Worker must already be deployed and configured with the same secret."
printf '%s' "Deployed Worker URL (https://...): "
IFS= read -r runtime_url
[ -n "$runtime_url" ] || die "Worker URL is required"
is_valid_runtime_url "$runtime_url" || die "use an HTTPS URL (HTTP is allowed only for localhost)"

printf '%s' "Shared runtime secret (input hidden): "
IFS= read -r -s runtime_token
printf '\n'
[ -n "$runtime_token" ] || die "runtime secret is required"

upsert_dotenv "$env_file" "$runtime_url" "$runtime_token"
upsert_exports "$export_file" "$runtime_url" "$runtime_token"
printf 'Saved runtime settings to %s and shell exports to %s (both mode 600).\n' "$env_file" "$export_file"

printf '%s' "Run the billable smoke test now? [y/N] "
IFS= read -r run_now
case "$run_now" in
  y|Y|yes|YES)
    export COMPUTER_RUNTIME_URL="$runtime_url"
    export COMPUTER_RUNTIME_TOKEN="$runtime_token"
    exec "${UV:-uv}" run python -m memseek.cloudflare_smoke
    ;;
  *)
    printf 'Next: source %s, then run make cloudflare-agent-smoke.\n' "$export_file"
    ;;
esac
