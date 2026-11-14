#!/usr/bin/env bash
# Shared helpers for the dummy MCP test server and ngrok static-domain tunnel.
# shellcheck disable=SC2034

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/venv"
PYTHON="$VENV/bin/python"
NGROK="${NGROK:-$(command -v ngrok || true)}"
if [[ -z "$NGROK" && -x /home/box/.local/bin/ngrok ]]; then
  NGROK=/home/box/.local/bin/ngrok
fi
NGROK_API="http://127.0.0.1:4040/api/tunnels"
# debug briefly so the next drop leaves a trail; set NGROK_LOG_LEVEL=info to quiet it.
NGROK_LOG_LEVEL="${NGROK_LOG_LEVEL:-debug}"

SERVER_LOG="$DIR/server.log"
TUNNEL_LOG="$DIR/tunnel.log"
WATCHDOG_LOG="$DIR/watchdog.log"
OUTAGES_LOG="$DIR/outages.log"
OUTAGE_STATE="$DIR/outage_state"
SERVER_PID_FILE="$DIR/server.pid"
TUNNEL_PID_FILE="$DIR/tunnel.pid"
WATCHDOG_PID_FILE="$DIR/watchdog.pid"
CURRENT_URL_FILE="$DIR/current_url.txt"
PUBLIC_URL_FILE="$DIR/public-url.txt"
URL_HISTORY="$DIR/url_history.log"
SERVICE_LOCK="$DIR/service.singleton"
WATCHDOG_LOCK="$DIR/watchdog.singleton"
PORT=8000
LOG_MAX_BYTES=$((1024 * 1024))

# Load a gitignored local config if present (copy .env.example to .env).
if [[ -f "$DIR/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  source "$DIR/.env"
  set +a
fi
# Public ngrok (or other tunnel) hostname only — no scheme, no /mcp path.
# Set EXPECTED_HOST or NGROK_DOMAIN in .env or the environment.
EXPECTED_HOST="${EXPECTED_HOST:-${NGROK_DOMAIN:-}}"

now_cst() {
  TZ=America/Chicago date '+%Y-%m-%d %H:%M:%S %Z'
}

rotate_log() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  local sz
  sz=$(wc -c < "$f" 2>/dev/null | tr -d ' ' || echo 0)
  if [[ "${sz:-0}" -gt "$LOG_MAX_BYTES" ]]; then
    mv -f "$f" "${f}.old"
    : > "$f"
  fi
}

rotate_logs() {
  rotate_log "$SERVER_LOG"
  rotate_log "$TUNNEL_LOG"
  rotate_log "$WATCHDOG_LOG"
  rotate_log "$URL_HISTORY"
  rotate_log "$OUTAGES_LOG"
}

stop_pidfile() {
  local file="$1"
  local kill_group="${2:-}"
  if [[ -f "$file" ]]; then
    local pid
    pid="$(cat "$file" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      if [[ "$kill_group" == "group" ]]; then
        kill -TERM -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
      else
        kill "$pid" 2>/dev/null || true
      fi
      local _i
      for _i in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.2
      done
      if [[ "$kill_group" == "group" ]]; then
        kill -KILL -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
      else
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi
    rm -f "$file"
  fi
}

pidfile_alive() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  local pid
  pid="$(cat "$file" 2>/dev/null || true)"
  [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null
}

normalize_url() {
  local u="${1:-}"
  u="${u%%$'\n'}"
  u="${u%/}"
  if [[ -n "$u" && "$u" != */mcp ]]; then
    u="${u}/mcp"
  fi
  printf '%s' "$u"
}

current_url() {
  local u=""
  if [[ -s "$CURRENT_URL_FILE" ]]; then
    u="$(tr -d '[:space:]' < "$CURRENT_URL_FILE")"
  elif [[ -s "$PUBLIC_URL_FILE" ]]; then
    u="$(tr -d '[:space:]' < "$PUBLIC_URL_FILE")"
  fi
  normalize_url "$u"
}

record_url() {
  local new
  new="$(normalize_url "${1:-}")"
  [[ -n "$new" ]] || return 1
  local old
  old="$(current_url)"
  printf '%s\n' "$new" > "$CURRENT_URL_FILE"
  printf '%s\n' "$new" > "$PUBLIC_URL_FILE"
  if [[ "$new" != "$old" ]]; then
    printf '%s %s\n' "$(now_cst)" "$new" >> "$URL_HISTORY"
    return 0
  fi
  return 1
}

# Serialize start_server / start_tunnel across watchdog, start.sh, and ensure_up.sh.
with_service_lock() {
  if [[ "${SERVICE_LOCK_HELD:-0}" == 1 ]]; then
    "$@"
    return $?
  fi
  flock -x "$SERVICE_LOCK" bash -c '
    set -u
    source "$1"
    SERVICE_LOCK_HELD=1
    shift
    "$@"
  ' bash "$DIR/common.sh" "$@"
}

outage_start() {
  local reason="${1:-unknown}"
  if [[ -s "$OUTAGE_STATE" ]]; then
    return 0
  fi
  local ts epoch
  ts="$(now_cst)"
  epoch="$(date +%s)"
  printf '%s\t%s\t%s\n' "$ts" "$epoch" "$reason" > "$OUTAGE_STATE"
  printf 'START %s reason=%s\n' "$ts" "$reason" >> "$OUTAGES_LOG"
}

outage_end() {
  [[ -s "$OUTAGE_STATE" ]] || return 0
  local start_ts start_epoch start_reason end_ts end_epoch dur
  IFS=$'\t' read -r start_ts start_epoch start_reason < "$OUTAGE_STATE" || true
  end_ts="$(now_cst)"
  end_epoch="$(date +%s)"
  dur=$((end_epoch - ${start_epoch:-end_epoch}))
  printf 'END   %s duration_s=%s started=%s reason=%s\n' \
    "$end_ts" "$dur" "$start_ts" "${start_reason:-unknown}" >> "$OUTAGES_LOG"
  rm -f "$OUTAGE_STATE"
}

# Classify a health-check body+http code.
#   ok | ngrok_error | timeout | fail
_classify_http() {
  local http="$1"
  local body="$2"
  local rc="${3:-0}"
  if [[ $rc -ne 0 || -z "$http" || "$http" == "000" ]]; then
    if [[ $rc -eq 28 || $rc -eq 7 || $rc -eq 6 || "$http" == "000" ]]; then
      printf 'timeout'
      return 1
    fi
  fi
  if [[ "$http" == "200" ]] && grep -qE 'protocolVersion|"ok"[[:space:]]*:[[:space:]]*true|mcp-test|streamable-http' "$body"; then
    printf 'ok'
    return 0
  fi
  if [[ "$http" == "000" ]]; then
    printf 'timeout'
    return 1
  fi
  if grep -qiE 'ERR_NGROK_|endpoint offline|ngrok.com/docs/errors|ERR_NGROK_3200' "$body" \
     || { [[ "$http" == "404" || "$http" == "502" || "$http" == "503" ]] && grep -qiE '<html|ngrok' "$body"; }; then
    printf 'ngrok_error'
    return 1
  fi
  printf 'fail'
  return 1
}

# Fast public/local probe: GET, 5s timeout. Enough to see ngrok error pages
# and the /mcp JSON greeting. Prints ok|ngrok_error|timeout|fail.
mcp_get_status() {
  local url="$1"
  local body http rc
  body="$(mktemp)"
  set +e
  http="$(curl -sS -o "$body" -w '%{http_code}' --max-time 5 \
    -X GET "$url" \
    -H 'Accept: application/json' \
    -H 'User-Agent: mcp-watchdog' \
    2>/dev/null)"
  rc=$?
  set -e
  if [[ $rc -ne 0 || -z "$http" ]]; then
    http="000"
  fi
  local st
  st="$(_classify_http "$http" "$body" "$rc" || true)"
  rm -f "$body"
  printf '%s' "${st:-fail}"
  [[ "$st" == "ok" ]]
}

# POST initialize (heavier; used for local server readiness after a start).
mcp_check_status() {
  local url="$1"
  local body http rc
  body="$(mktemp)"
  set +e
  http="$(curl -sS -o "$body" -w '%{http_code}' --max-time 15 \
    -X POST "$url" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"mcp-watchdog","version":"1.0"}}}' \
    2>/dev/null)"
  rc=$?
  set -e
  if [[ $rc -ne 0 || -z "$http" ]]; then
    http="000"
  fi
  local st
  st="$(_classify_http "$http" "$body" "$rc" || true)"
  rm -f "$body"
  printf '%s' "${st:-fail}"
  [[ "$st" == "ok" ]]
}

mcp_check() {
  local status
  status="$(mcp_get_status "$1" || true)"
  if [[ "$status" == "ok" ]]; then
    return 0
  fi
  # Fall back to initialize for servers that do not answer GET the same way.
  status="$(mcp_check_status "$1" || true)"
  [[ "$status" == "ok" ]]
}

# Local ngrok agent API: ok | no_api | no_tunnel
ngrok_api_status() {
  local js
  js="$(curl -sf --max-time 2 "$NGROK_API" 2>/dev/null || true)"
  if [[ -z "$js" ]]; then
    printf 'no_api'
    return 1
  fi
  if python3 -c '
import json,sys
host=sys.argv[1]
try:
    data=json.loads(sys.argv[2])
except Exception:
    raise SystemExit(2)
for t in data.get("tunnels") or []:
    u=t.get("public_url") or ""
    proto=t.get("proto") or ""
    if proto=="https" and host in u:
        raise SystemExit(0)
raise SystemExit(1)
' "$EXPECTED_HOST" "$js" 2>/dev/null; then
    printf 'ok'
    return 0
  fi
  printf 'no_tunnel'
  return 1
}

ngrok_public_url() {
  local js
  js="$(curl -sf --max-time 2 "$NGROK_API" 2>/dev/null || true)"
  [[ -n "$js" ]] || return 1
  python3 -c 'import json,sys
try:
    data=json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
for t in data.get("tunnels") or []:
    u=t.get("public_url") or ""
    if u.startswith("https://"):
        print(u)
        raise SystemExit(0)
raise SystemExit(1)
' "$js"
}

start_server() {
  if [[ "${SERVICE_LOCK_HELD:-0}" != 1 ]]; then
    with_service_lock start_server
    return $?
  fi
  if [[ ! -x "$PYTHON" ]]; then
    echo "venv python missing: $PYTHON" >&2
    return 1
  fi
  stop_pidfile "$SERVER_PID_FILE" group
  local _i
  for _i in $(seq 1 30); do
    if ! ss -tln 2>/dev/null | grep -qE ":${PORT}\\b"; then
      break
    fi
    sleep 0.1
  done
  if ss -tln 2>/dev/null | grep -qE ":${PORT}\\b"; then
    # If the port already answers MCP (another locked starter won), treat as up.
    if mcp_check "http://127.0.0.1:${PORT}/mcp"; then
      return 0
    fi
    echo "Port ${PORT} still in use after stopping our server" >&2
    return 1
  fi
  rotate_log "$SERVER_LOG"
  # 8>&- 9>&- so service/watchdog lock fds are not inherited (would block a later watchdog).
  export DISPLAY="${DISPLAY:-:4}"
  local spid
  spid="$("$PYTHON" - "$PYTHON" "$DIR/server.py" "$SERVER_LOG" <<'PY'
import os, sys
from subprocess import Popen, DEVNULL, STDOUT
python, script, log_path = sys.argv[1], sys.argv[2], sys.argv[3]
log = open(log_path, "ab", buffering=0)
env = os.environ.copy()
env.setdefault("DISPLAY", ":4")
p = Popen([python, script], stdin=DEVNULL, stdout=log, stderr=STDOUT,
          start_new_session=True, close_fds=True, env=env, cwd=os.path.dirname(script))
print(p.pid)
PY
)"
  echo "$spid" > "$SERVER_PID_FILE"
  for _i in $(seq 1 60); do
    if mcp_check "http://127.0.0.1:${PORT}/mcp"; then
      return 0
    fi
    if ! kill -0 "$(cat "$SERVER_PID_FILE" 2>/dev/null)" 2>/dev/null; then
      echo "MCP server failed to start; see $SERVER_LOG" >&2
      return 1
    fi
    sleep 0.5
  done
  echo "MCP server started but initialize did not succeed; see $SERVER_LOG" >&2
  return 1
}

ensure_ngrok_auth() {
  local cfg="${HOME}/.config/ngrok/ngrok.yml"
  if [[ -s "$cfg" ]]; then
    chmod 600 "$cfg" 2>/dev/null || true
    return 0
  fi
  local raw="${NGROK_AUTHTOKEN:-}"
  local token="${raw##* }"
  if [[ -z "$token" ]]; then
    echo "ngrok is not configured (missing config and NGROK_AUTHTOKEN)" >&2
    return 1
  fi
  mkdir -p "$(dirname "$cfg")"
  if ! "$NGROK" config add-authtoken "$token" >/dev/null 2>&1; then
    echo "ngrok config add-authtoken failed" >&2
    return 1
  fi
  chmod 600 "$cfg"
}

start_tunnel() {
  if [[ "${SERVICE_LOCK_HELD:-0}" != 1 ]]; then
    with_service_lock start_tunnel
    return $?
  fi
  if [[ -z "$NGROK" ]]; then
    echo "ngrok not found" >&2
    return 1
  fi
  ensure_ngrok_auth || return 1
  if [[ -z "$EXPECTED_HOST" ]]; then
    echo "EXPECTED_HOST is not set. Copy .env.example to .env and set your static domain (for example your-static-domain.ngrok-free.dev)." >&2
    return 1
  fi
  stop_pidfile "$TUNNEL_PID_FILE"
  rotate_log "$TUNNEL_LOG"
  printf '\n===== tunnel restart %s =====\n' "$(now_cst)" >> "$TUNNEL_LOG"
  "$PYTHON" "$DIR/update_ngrok_hosts.py" >> "$TUNNEL_LOG" 2>&1
  if [[ ! -s "$DIR/ngrok-hosts" ]]; then
    echo "Failed to write $DIR/ngrok-hosts" >&2
    return 1
  fi
  # System DNS rewrites ngrok hosts to 198.18.0.1, which breaks TLS/CRL
  # fetches. Bind-mount freshly DoH-resolved hosts over /etc/hosts in a
  # user namespace (refreshed on every restart).
  nohup unshare --user --map-root-user --mount bash -c "
    mount --bind '$DIR/ngrok-hosts' /etc/hosts
    exec '$NGROK' http ${PORT} --url=${EXPECTED_HOST} --log=stdout --log-format=term --log-level=${NGROK_LOG_LEVEL}
  " >> "$TUNNEL_LOG" 2>&1 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- &
  echo $! > "$TUNNEL_PID_FILE"
  local url="" _i
  for _i in $(seq 1 80); do
    url="$(ngrok_public_url || true)"
    if [[ -n "$url" ]]; then
      record_url "${url}/mcp" || true
      local _j
      for _j in $(seq 1 15); do
        if mcp_check "$(current_url)"; then
          return 0
        fi
        sleep 1
      done
      echo "ngrok started at $url but public initialize failed" >&2
      return 1
    fi
    if ! kill -0 "$(cat "$TUNNEL_PID_FILE" 2>/dev/null)" 2>/dev/null; then
      echo "ngrok failed to start; see $TUNNEL_LOG" >&2
      return 1
    fi
    sleep 0.25
  done
  echo "Timed out waiting for ngrok public URL; see $TUNNEL_LOG" >&2
  return 1
}

# Bring server and/or tunnel up without racing a sibling starter.
# Does not restart a healthy tunnel (keeps the static URL).
ensure_services() {
  if [[ "${SERVICE_LOCK_HELD:-0}" != 1 ]]; then
    with_service_lock ensure_services
    return $?
  fi
  local rc=0
  if ! mcp_check "http://127.0.0.1:${PORT}/mcp"; then
    start_server || rc=1
  fi
  local api
  api="$(ngrok_api_status || true)"
  local need_tunnel=0
  if ! pidfile_alive "$TUNNEL_PID_FILE"; then
    need_tunnel=1
  elif [[ "$api" != "ok" ]]; then
    need_tunnel=1
  else
    local pub
    pub="$(mcp_get_status "$(current_url)" || true)"
    if [[ "$pub" == "ngrok_error" ]]; then
      need_tunnel=1
    fi
  fi
  if [[ "$need_tunnel" -eq 1 ]]; then
    start_tunnel || rc=1
  fi
  return $rc
}

start_watchdog_bg() {
  if pidfile_alive "$WATCHDOG_PID_FILE"; then
    return 0
  fi
  nohup "$DIR/watchdog.sh" >> "$WATCHDOG_LOG" 2>&1 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- &
  local _i
  for _i in $(seq 1 30); do
    if pidfile_alive "$WATCHDOG_PID_FILE"; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}
