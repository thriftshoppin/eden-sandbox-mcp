#!/usr/bin/env bash
# Print every pending transfer manifest as a human-readable summary
# (items, directions, sizes, reason, expiry) so it can be shown for approval.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$DIR/xfer_tools.py" pending
