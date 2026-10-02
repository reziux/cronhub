#!/usr/bin/env bash
# Test harness for openclaw-heartbeat.sh. Drives every state via env
# overrides; never touches the live heartbeat or pidfile.
set -u
SCRIPT="/mnt/data/cronhub-work/bin/openclaw-heartbeat.sh"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0; FAIL=0
check() { # check <label> <expected> <actual>
    if [ "$2" = "$3" ]; then
        PASS=$((PASS+1)); printf '  ok   %s\n' "$1"
    else
        FAIL=$((FAIL+1)); printf '  FAIL %s\n       expected: %s\n       actual:   %s\n' "$1" "$2" "$3"
    fi
}

run_case() { # run_case <unit>; sets RC, STATUS, HB_EXISTS
    local unit="$1"
    rm -rf "$SANDBOX"; mkdir -p "$SANDBOX"
    env CRONHUB="$SANDBOX" OPENCLAW_PIDFILE="$SANDBOX/gateway.pid" \
        OPENCLAW_UNIT="$unit" bash "$SCRIPT"
    RC=$?
    STATUS="$(cat "$SANDBOX/state/openclaw.heartbeat.status" 2>/dev/null || echo '<missing>')"
    if [ -f "$SANDBOX/state/openclaw.heartbeat" ]; then HB_EXISTS=yes; else HB_EXISTS=no; fi
}

echo "== active unit resolves to ok =="
run_case openclaw-gateway
check "status is ok"       "ok"   "$(printf '%s' "$STATUS" | cut -d' ' -f1)"
check "pid is numeric"     "yes"  "$(printf '%s' "$STATUS" | awk '{print $2 ~ /^[0-9]+$/ ? "yes" : "no"}')"
check "rc is 0"            "0"    "$RC"
check "heartbeat touched"  "yes"  "$HB_EXISTS"
check "pidfile written"    "yes"  "$([ -f "$SANDBOX/gateway.pid" ] && echo yes || echo no)"

echo "== loaded but stopped unit is down =="
run_case claw-watchdog.service
check "status is down"     "down" "$STATUS"
check "rc is 0"            "0"    "$RC"
check "heartbeat NOT touched" "no" "$HB_EXISTS"

echo "== nonexistent unit is unknown, not down =="
run_case definitely-not-a-unit
check "status unknown no-pid" "unknown no-pid" "$STATUS"
check "rc is non-zero"     "nonzero" "$([ "$RC" -ne 0 ] && echo nonzero || echo zero)"
check "heartbeat NOT touched" "no" "$HB_EXISTS"

echo "== cron-like empty environment still resolves =="
# Minimal env, exactly as cron provides it: PATH and HOME only, no
# XDG_RUNTIME_DIR and no DBUS_SESSION_BUS_ADDRESS. The script's own bootstrap
# must make systemctl --user work anyway. Not routed through run_case,
# because a nested `env -i` would wipe the harness's own path overrides too.
rm -rf "$SANDBOX"; mkdir -p "$SANDBOX"
env -i PATH="$PATH" HOME="$HOME" \
    CRONHUB="$SANDBOX" OPENCLAW_PIDFILE="$SANDBOX/gateway.pid" \
    OPENCLAW_UNIT=openclaw-gateway \
    bash "$SCRIPT"
RC=$?
STATUS="$(cat "$SANDBOX/state/openclaw.heartbeat.status" 2>/dev/null || echo '<missing>')"
if [ -f "$SANDBOX/state/openclaw.heartbeat" ]; then HB_EXISTS=yes; else HB_EXISTS=no; fi
check "status is ok"       "ok"   "$(printf '%s' "$STATUS" | cut -d' ' -f1)"
check "rc is 0"            "0"    "$RC"
check "heartbeat touched"  "yes"  "$HB_EXISTS"

echo
echo "passed: $PASS  failed: $FAIL"
[ "$FAIL" -eq 0 ]
