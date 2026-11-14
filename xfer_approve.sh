#!/usr/bin/env bash
# Approve, trim, or deny a pending transfer manifest.
# Approval is only allowed here (or an equivalent CLI), never via an MCP tool.
#   xfer_approve.sh <id> all
#   xfer_approve.sh <id> 0,2
#   xfer_approve.sh <id> deny
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ $# -lt 2 ]]; then
  echo "usage: $0 <manifest-id> all|<comma item indexes>|deny" >&2
  exit 2
fi
exec python3 "$DIR/xfer_tools.py" approve "$1" "$2"
