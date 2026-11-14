#!/usr/bin/env bash
# Idempotent: start the dummy MCP connector if needed, then print one status line.
# Goes through the same service.lock as the watchdog / start.sh.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
source "$DIR/common.sh"

status_line() {
  local st="$1"
  local url
  url="$(current_url)"
  [[ -n "$url" ]] || url="none"
  printf 'URL=%s STATUS=%s\n' "$url" "$st"
}

url="$(current_url)"
if mcp_check "http://127.0.0.1:${PORT}/mcp" \
   && pidfile_alive "$TUNNEL_PID_FILE" \
   && [[ "$(ngrok_api_status || true)" == "ok" ]] \
   && [[ -n "$url" ]] && mcp_check "$url"; then
  start_watchdog_bg >/dev/null 2>&1 || true
  status_line ok
  exit 0
fi

# Something is down. Fix it under the shared lock (serializes with the watchdog).
ensure_services >> "$WATCHDOG_LOG" 2>&1 || true
start_watchdog_bg >> "$WATCHDOG_LOG" 2>&1 || true

url="$(current_url)"
for _i in $(seq 1 20); do
  url="$(current_url)"
  if [[ -n "$url" ]] && mcp_check "$url"; then
    status_line ok
    exit 0
  fi
  sleep 1
done
status_line fail
exit 1
