#!/usr/bin/env bash
# Lightweight session hook: if the watchdog is not running, start it in the background.
# Safe to source or exec from bashrc/profile; exits immediately when already up.
DIR="/workspace/mcp-test"
PIDFILE="$DIR/watchdog.pid"
if [[ -f "$PIDFILE" ]]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    return 0 2>/dev/null || exit 0
  fi
fi
if [[ -x "$DIR/ensure_up.sh" ]]; then
  nohup "$DIR/ensure_up.sh" >> "$DIR/ensure_up.log" 2>&1 &
  disown 2>/dev/null || true
fi
return 0 2>/dev/null || exit 0
