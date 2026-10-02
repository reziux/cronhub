#!/usr/bin/env bash
# openclaw-heartbeat.sh - refresh openclaw gateway heartbeat.
#
# Run from system cron every minute.
#
# Resolves the gateway PID from systemd (authoritative) rather than by
# pattern-matching the command line. The previous implementation used
# `pgrep -f '^openclaw-gateway$'`, which never matched the real invocation
# (`node .../openclaw/dist/index.js gateway --port ...`), so the heartbeat
# silently stopped and the arbiter demoted openclaw to standby.
#
# Env overrides (used by bin/test_openclaw_heartbeat.sh):
#   CRONHUB           cronhub root            (default /mnt/data/cronhub)
#   OPENCLAW_UNIT     systemd user unit       (default openclaw-gateway)
#   OPENCLAW_PIDFILE  pidfile path             (default ~/.openclaw/gateway.pid)

set -u

CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
UNIT="${OPENCLAW_UNIT:-openclaw-gateway}"
STATE_DIR="$CRONHUB/state"
OC_PIDFILE="${OPENCLAW_PIDFILE:-/home/reziux/.openclaw/gateway.pid}"
OC_HB="$STATE_DIR/openclaw.heartbeat"
OC_STATUS="$STATE_DIR/openclaw.heartbeat.status"

# systemctl --user needs a session bus. Cron supplies neither variable, and
# the crontab line ends in >/dev/null 2>&1, so without this the call fails
# silently and we are back to a heartbeat that never advances.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

write_status() { printf '%s\n' "$1" >"$OC_STATUS" 2>/dev/null; }

if ! mkdir -p "$STATE_DIR" "$(dirname "$OC_PIDFILE")" 2>/dev/null; then
    printf 'unknown mkdir-failed\n' >"$OC_STATUS" 2>/dev/null
    exit 1
fi

show_prop() { systemctl --user show "$UNIT" --property="$1" --value 2>/dev/null; }

LOAD_STATE="$(show_prop LoadState)"
MAIN_PID="$(show_prop MainPID)"

if [ -n "$LOAD_STATE" ]; then
    # systemd answered. LoadState is the only thing that separates
    # "unit does not exist" from "unit is stopped" - both report MainPID 0.
    if [ "$LOAD_STATE" != "loaded" ]; then
        write_status "unknown no-pid"
        exit 3
    fi
    if ! printf '%s' "$MAIN_PID" | grep -qE '^[0-9]+$'; then
        write_status "unknown no-pid"
        exit 3
    fi
    if [ "$MAIN_PID" -eq 0 ]; then
        write_status "down"
        exit 0
    fi
    PID="$MAIN_PID"
    SUFFIX=""
else
    # User bus unreachable. Degraded path: pgrep against the real signature.
    # Only meaningful for the default unit - never claim another unit is up
    # because the openclaw process happens to be running.
    if [ "$UNIT" = "openclaw-gateway" ]; then
        PID="$(pgrep -f 'openclaw/dist/index\.js gateway' 2>/dev/null | head -1)"
        if [ -z "$PID" ]; then
            PID="$(ps -eo pid,args 2>/dev/null | awk '/openclaw\/dist\/index\.js gateway/ {print $1; exit}')"
        fi
        if printf '%s' "$PID" | grep -qE '^[0-9]+$'; then
            SUFFIX=" pgrep-fallback"
        else
            PID=""
        fi
    else
        PID=""
    fi
    if [ -z "$PID" ]; then
        write_status "unknown dbus"
        exit 3
    fi
fi

# Refresh the pidfile when it disagrees with systemd. It is only ever written
# here, which is why it stayed frozen at a dead PID after the last restart.
if [ ! -f "$OC_PIDFILE" ] || ! grep -q "\"pid\": $PID" "$OC_PIDFILE" 2>/dev/null; then
    python3 - "$OC_PIDFILE" "$PID" <<'PYEOF'
import json, sys, time
from pathlib import Path
p = Path(sys.argv[1]); pid = int(sys.argv[2])
p.write_text(json.dumps({
    "pid": pid,
    "kind": "openclaw-gateway",
    "argv": ["openclaw-gateway"],
    "start_time": int(time.time()),
    "openclaw_home": "/home/reziux/.openclaw",
}, indent=2))
PYEOF
fi

if ! touch "$OC_HB" 2>/dev/null; then
    write_status "unknown touch-failed"
    exit 3
fi

write_status "ok $PID$SUFFIX"
exit 0
