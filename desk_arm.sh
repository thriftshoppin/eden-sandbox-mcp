#!/usr/bin/env bash
# Arm or disarm the sandbox desktop tools.
#   desk_arm.sh            # arm for 15 minutes
#   desk_arm.sh 20         # arm for 20 minutes (max 60)
#   desk_arm.sh off        # disarm now
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
STATE="$DIR/desk_arm.state"
LOG="$DIR/desk_actions.log"
IDLE_S=300
DEFAULT=15
MAX=60

stamp() { TZ=America/Chicago date '+%Y-%m-%d %H:%M:%S %Z'; }

arg="${1:-$DEFAULT}"
if [[ "$arg" == "off" || "$arg" == "disarm" || "$arg" == "0" ]]; then
  python3 - "$STATE" "$LOG" <<'PY'
import json, os, sys, time, subprocess
state, log = sys.argv[1], sys.argv[2]
data = {"armed": False, "disarmed_reason": "desk_arm.sh off", "disarmed_at": int(time.time())}
open(state, "w").write(json.dumps(data) + "\n")
env = dict(os.environ); env["TZ"] = "America/Chicago"
ts = subprocess.check_output(["date", "+%Y-%m-%d %H:%M:%S %Z"], env=env, text=True).strip()
with open(log, "a") as f:
    f.write(f"{ts} tool=disarm reason=desk_arm.sh_off\n")
print("DISARMED")
PY
  exit 0
fi

if ! [[ "$arg" =~ ^[0-9]+$ ]]; then
  echo "usage: $0 [minutes|off]   (default ${DEFAULT}, max ${MAX})" >&2
  exit 2
fi
minutes="$arg"
if [[ "$minutes" -lt 1 ]]; then
  echo "minutes must be at least 1 (or use 'off')" >&2
  exit 2
fi
if [[ "$minutes" -gt "$MAX" ]]; then
  minutes="$MAX"
fi

python3 - "$STATE" "$LOG" "$minutes" "$IDLE_S" <<'PY'
import json, os, sys, time, subprocess
state, log, minutes, idle = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
now = int(time.time())
data = {
    "armed": True,
    "expires": now + minutes * 60,
    "last_activity": now,
    "armed_minutes": minutes,
    "idle_s": idle,
    "armed_at": now,
}
open(state, "w").write(json.dumps(data) + "\n")
env = dict(os.environ); env["TZ"] = "America/Chicago"
ts = subprocess.check_output(["date", "+%Y-%m-%d %H:%M:%S %Z"], env=env, text=True).strip()
with open(log, "a") as f:
    f.write(f"{ts} tool=arm minutes={minutes} expires_in_s={minutes*60}\n")
print(f"ARMED for {minutes} minutes (idle disarm after {idle // 60} minutes of no actions)")
PY
