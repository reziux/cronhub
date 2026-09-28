#!/usr/bin/env bash
# arbiter.sh — runs from system cron every minute.
# Writes claims/active-scheduler.json based on heartbeats.
#
# Inputs (env vars, defaults shown):
#   OPENCLAW_HEARTBEAT_FILE   (default: /mnt/data/cronhub/state/openclaw.heartbeat)
#   HERMES_HEARTBEAT_FILE     (default: /mnt/data/cronhub/state/hermes.heartbeat)
#   OPENCLAW_GATEWAY_PID_FILE (default: /home/reziux/.openclaw/gateway.pid)
#   HERMES_GATEWAY_PID_FILE   (default: /home/reziux/.hermes/gateway.pid)
#
# Decision rules:
#   - If openclaw heartbeat is fresh (< 300s) → active=openclaw
#   - Else if hermes heartbeat is fresh → active=hermes
#   - Else → keep current active (don't flap)
#   - On every flip, append to alerts/failover.jsonl (rate-limited: 1/hr)

set -u
CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
OPENCLAW_HB="${OPENCLAW_HEARTBEAT_FILE:-$CRONHUB/state/openclaw.heartbeat}"
HERMES_HB="${HERMES_HEARTBEAT_FILE:-$CRONHUB/state/hermes.heartbeat}"
CLAIMS="$CRONHUB/claims/active-scheduler.json"
FAILOVER_LOG="$CRONHUB/alerts/failover.jsonl"
LOCK="$CRONHUB/locks/arbiter.lock"
mkdir -p "$CRONHUB/state" "$CRONHUB/alerts" "$CRONHUB/locks"

now_epoch() { date +%s; }
hb_age() {
    local f="$1"
    [ -f "$f" ] || { echo 999999; return; }
    local mt
    mt=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null)
    [ -z "$mt" ] && { echo 999999; return; }
    echo $(( $(now_epoch) - mt ))
}

# Read a pid from a file. The file may contain a JSON object with a "pid"
# field, a bare integer, or be unreadable. Output the pid, or empty.
read_pid_file() {
    local f="$1"
    [ -f "$f" ] || return 1
    CRONHUB="$CRONHUB" PIDFILE="$f" python3 <<'PYEOF'
import os, sys
from pathlib import Path
p = Path(os.environ["PIDFILE"])
try:
    text = p.read_text().strip()
except Exception:
    sys.exit(1)
# Try JSON first
try:
    import json
    d = json.loads(text)
    pid = d.get("pid")
    if pid is not None:
        print(int(pid))
        sys.exit(0)
except Exception:
    pass
# Fall back to first line as bare integer
try:
    pid = int(text.splitlines()[0].strip())
    print(pid)
except Exception:
    sys.exit(1)
PYEOF
}

is_pid_alive() {
    local pidfile="$1"
    local pid
    pid=$(read_pid_file "$pidfile") || return 1
    [ -z "$pid" ] && return 1
    kill -0 "$pid" 2>/dev/null
}

exec 9>"$LOCK"
flock -w 5 9 || exit 0

# Refresh heartbeats from PID files
if is_pid_alive "${OPENCLAW_GATEWAY_PID_FILE:-/home/reziux/.openclaw/gateway.pid}"; then
    touch "$OPENCLAW_HB"
fi
if is_pid_alive "${HERMES_GATEWAY_PID_FILE:-/home/reziux/.hermes/gateway.pid}"; then
    touch "$HERMES_HB"
fi

OC_AGE=$(hb_age "$OPENCLAW_HB")
HE_AGE=$(hb_age "$HERMES_HB")
DESIRED=""
REASON="no-change"
if [ "$OC_AGE" -lt 300 ]; then
    DESIRED="openclaw"; REASON="openclaw-fresh"
elif [ "$HE_AGE" -lt 300 ]; then
    DESIRED="hermes"; REASON="openclaw-stale"
else
    REASON="both-stale"
fi

# Read current active scheduler
CUR="openclaw"
[ -f "$CLAIMS" ] && CUR=$(CRONHUB="$CRONHUB" CLAIMS="$CLAIMS" python3 <<'PYEOF'
import os, json
from pathlib import Path
try:
    d = json.loads(Path(os.environ["CLAIMS"]).read_text())
    print(d.get("active_scheduler", "openclaw"))
except Exception:
    print("openclaw")
PYEOF
)

# Build new claims JSON via env-passed args
NEW_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
FLIP="false"
[ -n "$DESIRED" ] && [ "$DESIRED" != "$CUR" ] && FLIP="true"

CRONHUB="$CRONHUB" CLAIMS="$CLAIMS" NEW_TS="$NEW_TS" \
DESIRED="$DESIRED" CUR="$CUR" OC_AGE="$OC_AGE" HE_AGE="$HE_AGE" \
REASON="$REASON" FLIP="$FLIP" FAILOVER_LOG="$FAILOVER_LOG" python3 <<'PYEOF'
import os, json, sys
from pathlib import Path
from datetime import datetime, timezone
env = os.environ
claim_path = Path(env["CLAIMS"])
ts = env["NEW_TS"]
desired = env["DESIRED"]
prev = env["CUR"]
oc_age = int(env["OC_AGE"])
he_age = int(env["HE_AGE"])
reason = env["REASON"]
flip = env["FLIP"] == "true"
log_path = Path(env["FAILOVER_LOG"])

existing = {}
if claim_path.exists():
    try:
        existing = json.loads(claim_path.read_text())
    except Exception:
        pass
existing.update({
    "active_scheduler": desired or prev,
    "since": ts if flip else existing.get("since", ts),
    "last_flip_reason": reason if flip else existing.get("last_flip_reason", "no-change"),
    "last_check_ts": ts,
    "openclaw_heartbeat_age_s": oc_age,
    "hermes_heartbeat_age_s": he_age,
})
claim_path.write_text(json.dumps(existing, indent=2))

if flip:
    line = {"ts": ts, "from": prev, "to": desired, "reason": reason,
            "oc_age": oc_age, "he_age": he_age}
    rate_ok = True
    if log_path.exists():
        try:
            lines = log_path.read_text().splitlines()
            if lines:
                last = json.loads(lines[-1])
                last_t = datetime.fromisoformat(last["ts"].replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - last_t).total_seconds() < 3600:
                    rate_ok = False
        except Exception:
            pass
    if rate_ok:
        with log_path.open("a") as f:
            f.write(json.dumps(line) + "\n")
        print(f"FLIP {prev} -> {desired} ({reason})")
PYEOF
flock -u 9