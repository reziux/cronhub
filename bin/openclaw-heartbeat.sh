#!/usr/bin/env bash
# openclaw-heartbeat.sh — refresh openclaw gateway heartbeat.
# Run from system cron every minute.
# Updates /home/reziux/.openclaw/gateway.pid (creates if missing) and
# touches /mnt/data/cronhub/state/openclaw.heartbeat.
#
# This is the openclaw counterpart to arbiter's automatic hermes heartbeat;
# without it, the arbiter would see openclaw as stale and flip to hermes.

set -u
CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
OC_PIDFILE="/home/reziux/.openclaw/gateway.pid"
OC_HB="$CRONHUB/state/openclaw.heartbeat"

mkdir -p "$(dirname "$OC_HB")" "$(dirname "$OC_PIDFILE")"

# Find the openclaw gateway pid. Match on "openclaw-gateway" command.
PID=$(pgrep -f '^openclaw-gateway$' 2>/dev/null | head -1)

# Fallback: any openclaw process with "gateway" in cmdline
if [ -z "$PID" ]; then
    PID=$(ps -eo pid,comm,args 2>/dev/null | awk '$2 ~ /openclaw/ && /gateway/ {print $1; exit}')
fi

if [ -n "$PID" ] && [ "$PID" -gt 0 ] 2>/dev/null; then
    # Write pid file if missing or stale
    if [ ! -f "$OC_PIDFILE" ] || ! grep -q "\"pid\": $PID" "$OC_PIDFILE" 2>/dev/null; then
        python3 - "$OC_PIDFILE" "$PID" <<'PYEOF'
import json, sys, time
from pathlib import Path
p = Path(sys.argv[1])
pid = int(sys.argv[2])
p.write_text(json.dumps({
    "pid": pid,
    "kind": "openclaw-gateway",
    "argv": ["openclaw-gateway"],
    "start_time": int(time.time()),
    "openclaw_home": "/home/reziux/.openclaw",
}, indent=2))
PYEOF
    fi
    touch "$OC_HB"
fi