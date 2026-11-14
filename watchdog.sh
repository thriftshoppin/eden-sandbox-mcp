#!/usr/bin/env bash
# Restart the local MCP server or the ngrok tunnel when health checks fail.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
source "$DIR/common.sh"

exec 9>"$WATCHDOG_LOCK"
if ! flock -n 9; then
  echo "$(now_cst) watchdog already running; exiting" >&2
  exit 0
fi

echo $$ > "$WATCHDOG_PID_FILE"
trap 'rm -f "$WATCHDOG_PID_FILE"' EXIT

log() { echo "$(now_cst) $*" >> "$WATCHDOG_LOG"; }

# Seed current_url.txt from public-url.txt if needed, without a history line
# unless this is the first time we have ever recorded a URL.
if [[ ! -s "$CURRENT_URL_FILE" && -s "$PUBLIC_URL_FILE" ]]; then
  printf '%s\n' "$(normalize_url "$(tr -d '[:space:]' < "$PUBLIC_URL_FILE")")" > "$CURRENT_URL_FILE"
fi
if [[ -s "$CURRENT_URL_FILE" && ! -s "$URL_HISTORY" ]]; then
  printf '%s %s\n' "$(now_cst)" "$(current_url)" >> "$URL_HISTORY"
fi

log "watchdog started pid=$$ url=$(current_url) interval=10s"

public_fails=0
INTERVAL=10

restart_tunnel() {
  local reason="$1"
  outage_start "$reason"
  log "$reason; restarting ngrok tunnel (static domain should stay the same)"
  if start_tunnel; then
    log "tunnel restarted pid=$(cat "$TUNNEL_PID_FILE" 2>/dev/null || echo ?) url=$(current_url)"
    public_fails=0
    outage_end "recovered"
    return 0
  fi
  log "tunnel restart failed"
  public_fails=0
  return 1
}

restart_server() {
  local reason="$1"
  log "$reason; restarting server only"
  if start_server; then
    log "server restarted pid=$(cat "$SERVER_PID_FILE" 2>/dev/null || echo ?)"
    return 0
  fi
  log "server restart failed"
  return 1
}

while true; do
  rotate_logs

  if ! mcp_check "http://127.0.0.1:${PORT}/mcp"; then
    outage_start "local_mcp_down"
    restart_server "local health check failed"
  fi

  url="$(current_url)"
  api="$(ngrok_api_status || true)"

  if ! pidfile_alive "$TUNNEL_PID_FILE"; then
    restart_tunnel "ngrok process is down"
  elif [[ "$api" == "no_api" || "$api" == "no_tunnel" ]]; then
    # Process may still be alive while the agent has lost its tunnel session.
    restart_tunnel "ngrok agent API reports $api"
  elif [[ -z "$url" ]]; then
    public_fails=$((public_fails + 1))
    log "no current public URL (fail $public_fails/2)"
    if [[ "$public_fails" -ge 2 ]]; then
      restart_tunnel "no public URL after 2 checks"
    fi
  else
    status="$(mcp_get_status "$url" || true)"
    case "$status" in
      ok)
        if [[ "$public_fails" -gt 0 ]]; then
          log "public check recovered url=$url"
        fi
        public_fails=0
        outage_end "recovered"
        ;;
      ngrok_error)
        log "public check got an ngrok error page for $url"
        restart_tunnel "ngrok error page (endpoint offline)"
        ;;
      timeout)
        public_fails=$((public_fails + 1))
        log "public check timed out for $url (fail $public_fails/2)"
        if [[ "$public_fails" -ge 2 ]]; then
          restart_tunnel "public check timed out 2 times"
        fi
        ;;
      *)
        public_fails=$((public_fails + 1))
        log "public check failed for $url status=${status:-fail} (fail $public_fails/2)"
        if [[ "$public_fails" -ge 2 ]]; then
          restart_tunnel "public check failed 2 times"
        fi
        ;;
    esac
  fi

  # Sleep 10s total, but poll the ngrok pid and agent API every 5s so a
  # dead process or vanished tunnel is noticed quickly.
  _slept=0
  while [[ "$_slept" -lt "$INTERVAL" ]]; do
    sleep 5
    _slept=$((_slept + 5))
    if ! pidfile_alive "$TUNNEL_PID_FILE"; then
      break
    fi
    api_now="$(ngrok_api_status || true)"
    if [[ "$api_now" != "ok" ]]; then
      break
    fi
  done
done
