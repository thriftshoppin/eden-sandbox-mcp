#!/usr/bin/env bash
# Bring up (or, with --server, restart only) the MCP server and ngrok tunnel.
# Shares service.lock with the watchdog so the two never race on port 8000.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
source "$DIR/common.sh"

if [[ -z "${NGROK:-}" ]]; then
  echo "ngrok not found" >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "venv missing; create it with: python3 -m venv \"$VENV\" && \"$VENV/bin/pip\" install -r \"$DIR/requirements.txt\"" >&2
  exit 1
fi

case "${1:-}" in
  --server|--restart-server)
    # Restart the MCP process only; leave the ngrok tunnel (and URL) alone.
    start_server
    ;;
  --tunnel|--restart-tunnel)
    start_tunnel
    ;;
  *)
    # Default: only start whatever is down. Never yank a healthy tunnel.
    ensure_services
    ;;
esac

start_watchdog_bg || true

echo "MCP server PID: $(cat "$SERVER_PID_FILE")"
echo "Tunnel PID:     $(cat "$TUNNEL_PID_FILE")"
echo "Public connector URL: $(current_url)"
