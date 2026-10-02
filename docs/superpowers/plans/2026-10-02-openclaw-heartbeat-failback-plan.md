# openclaw Heartbeat Failback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `openclaw-heartbeat.sh` resolve the real gateway PID from systemd so openclaw regains primary scheduler status within one arbiter tick.

**Architecture:** Replace brittle `pgrep`-anchored command-line matching with `systemctl --user show`, which is authoritative and immune to how the gateway binary is invoked. Resolve the PID from a systemd user bus that must be bootstrapped explicitly, because cron supplies neither `XDG_RUNTIME_DIR` nor `DBUS_SESSION_BUS_ADDRESS`. Write a three-state sidecar so "gateway is down" is never conflated with "could not measure". Leave `arbiter.sh` untouched — its rule already prefers openclaw.

**Tech Stack:** bash 5, systemd user manager, python3 (pidfile JSON only), D-Bus session bus.

## Global Constraints

- Target: `/mnt/data/cronhub/bin/openclaw-heartbeat.sh`, in git repo `/mnt/data/cronhub` (branch `main`, remote `git@github.com:reziux/cronhub.git`).
- **Do not modify `bin/arbiter.sh`.** Its rule "openclaw fresh → openclaw, elif hermes fresh → hermes" is correct and fails back on its own once the heartbeat moves.
- **Do not modify any crontab entry.** The existing `* * * * * ... openclaw-heartbeat.sh` line stays as-is, including its `>/dev/null 2>&1`.
- Only `ok` states may touch `openclaw.heartbeat`. The arbiter reads that file's mtime and nothing else.
- `LoadState` is the discriminator for "does not exist" vs "stopped". `MainPID` alone is NOT — both return `0`.
- Exit codes: `0` for `ok` and `down`; non-zero for every `unknown` state.
- Every task commits to git on `main` in `/mnt/data/cronhub`.

## File Structure

| File | Responsibility |
|---|---|
| `bin/openclaw-heartbeat.sh` (modify) | Resolve gateway PID, refresh pidfile, touch heartbeat, write status sidecar. Sole production change. |
| `bin/test_openclaw_heartbeat.sh` (create) | Black-box test harness. Drives the script through every state via env overrides. |

The script gains three env overrides so it is testable without touching the live pidfile or the real heartbeat. `CRONHUB` already existed; `OPENCLAW_UNIT` and `OPENCLAW_PIDFILE` are new.

---

### Task 1: Status sidecar and state derivation

**Files:**
- Modify: `/mnt/data/cronhub/bin/openclaw-heartbeat.sh`
- Create: `/mnt/data/cronhub/bin/test_openclaw_heartbeat.sh`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: env overrides `CRONHUB`, `OPENCLAW_UNIT`, `OPENCLAW_PIDFILE`; status file at `$CRONHUB/state/openclaw.heartbeat.status` containing one of `ok <pid>`, `ok <pid> pgrep-fallback`, `down`, `unknown no-pid`, `unknown dbus`, `unknown touch-failed`, `unknown mkdir-failed`. Later tasks and the test harness rely on these exact strings.

- [ ] **Step 1: Write the failing test harness**

Create `/mnt/data/cronhub/bin/test_openclaw_heartbeat.sh`:

```bash
#!/usr/bin/env bash
# Test harness for openclaw-heartbeat.sh. Drives every state via env
# overrides; never touches the live heartbeat or pidfile.
set -u
SCRIPT="/mnt/data/cronhub/bin/openclaw-heartbeat.sh"
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

run_case() { # run_case <unit> <extra-env...> ; sets RC, STATUS, HB_EXISTS
    local unit="$1"; shift
    rm -rf "$SANDBOX"; mkdir -p "$SANDBOX"
    env CRONHUB="$SANDBOX" OPENCLAW_PIDFILE="$SANDBOX/gateway.pid" \
        OPENCLAW_UNIT="$unit" "$@" bash "$SCRIPT"
    RC=$?
    STATUS="$(cat "$SANDBOX/state/openclaw.heartbeat.status" 2>/dev/null || echo '<missing>')"
    if [ -f "$SANDBOX/state/openclaw.heartbeat" ]; then HB_EXISTS=yes; else HB_EXISTS=no; fi
}

echo "== active unit resolves to ok =="
run_case openclaw-gateway
check "status is ok <pid>"  "ok" "$(printf '%s' "$STATUS" | cut -d' ' -f1)"
check "pid is numeric"     "yes" "$(printf '%s' "$STATUS" | awk '{print $2 ~ /^[0-9]+$/ ? "yes" : "no"}')"
check "rc is 0"            "0"   "$RC"
check "heartbeat touched"  "yes" "$HB_EXISTS"
check "pidfile written"    "yes" "$([ -f "$SANDBOX/gateway.pid" ] && echo yes || echo no)"

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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
chmod +x /mnt/data/cronhub/bin/test_openclaw_heartbeat.sh
bash /mnt/data/cronhub/bin/test_openclaw_heartbeat.sh
```

Expected: FAIL. The current script has no `OPENCLAW_UNIT`/`OPENCLAW_PIDFILE`
overrides, writes no `.status` file, so every status check reports
`<missing>`, and the `claw-watchdog` case still touches a heartbeat.

- [ ] **Step 3: Replace the script with the systemd-based implementation**

Overwrite `/mnt/data/cronhub/bin/openclaw-heartbeat.sh` with exactly this:

```bash
#!/usr/bin/env bash
# openclaw-heartbeat.sh — refresh openclaw gateway heartbeat.
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
#   OPENCLAW_PIDFILE  pidfile path            (default ~/.openclaw/gateway.pid)

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
    # "unit does not exist" from "unit is stopped" — both report MainPID 0.
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
    # Only meaningful for the default unit — never claim another unit is up
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
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
bash /mnt/data/cronhub/bin/test_openclaw_heartbeat.sh
```

Expected: `passed: 14  failed: 0`, exit 0 (5 + 3 + 3 + 3 assertions).

- [ ] **Step 5: Confirm the script is still executable and syntactically valid**

```bash
bash -n /mnt/data/cronhub/bin/openclaw-heartbeat.sh && echo SYNTAX_OK
chmod +x /mnt/data/cronhub/bin/openclaw-heartbeat.sh
```

Expected: `SYNTAX_OK`.

- [ ] **Step 6: Commit**

```bash
cd /mnt/data/cronhub
git add bin/openclaw-heartbeat.sh bin/test_openclaw_heartbeat.sh
git commit -m "Resolve gateway PID from systemd; add three-state heartbeat status"
git push origin main
```

---

### Task 2: Verify the live system recovers

**Files:** none modified. Verification only.

**Interfaces:**
- Consumes: the `ok <pid>` status and heartbeat behaviour from Task 1.
- Produces: evidence that openclaw is primary again.

- [ ] **Step 1: Run the script live and inspect the result**

```bash
bash /mnt/data/cronhub/bin/openclaw-heartbeat.sh
echo "rc=$?"
cat /mnt/data/cronhub/state/openclaw.heartbeat.status
ls -la /mnt/data/cronhub/state/openclaw.heartbeat
cat /home/reziux/.openclaw/gateway.pid
```

Expected: `rc=0`, status `ok <pid>` where `<pid>` is a number greater than
zero, `openclaw.heartbeat` mtime is within the last minute, and the pidfile's
`"pid"` equals the status PID.

- [ ] **Step 2: Confirm the pidfile self-healed off the dead PID**

The pidfile previously recorded `3476375` while the live gateway was
`3569044`. Confirm it now matches:

```bash
systemctl --user show openclaw-gateway --property=MainPID --value
grep '"pid"' /home/reziux/.openclaw/gateway.pid
```

Expected: both print the same number.

- [ ] **Step 3: Wait for the arbiter to re-elect openclaw**

The arbiter runs on its own schedule; give it up to two minutes.

```bash
sleep 90
cat /mnt/data/cronhub/claims/active_scheduler.json 2>/dev/null || find /mnt/data/cronhub -name 'active_scheduler*' -o -name 'claims*' -maxdepth 2 2>/dev/null | head
grep -i flip /mnt/data/cronhub/state/arbiter.log | tail -n 3
```

Expected: the claims file reports `active_scheduler: openclaw`. If the claims
path differs, locate it with the `find` above and read it there.

- [ ] **Step 4: Confirm heartbeat freshness is now sustained, not a one-off**

```bash
stat -c '%y' /mnt/data/cronhub/state/openclaw.heartbeat
sleep 75
stat -c '%y' /mnt/data/cronhub/state/openclaw.heartbeat
```

Expected: two different timestamps, roughly 75s apart. This proves the cron
entry is actually driving the script — a single manual run does not.

- [ ] **Step 5: Confirm the crontab is untouched**

```bash
crontab -l | grep openclaw-heartbeat
```

Expected: the original line, unchanged, still ending in `>/dev/null 2>&1`.

- [ ] **Step 6: Commit any test-harness refinement only if Step 1–5 forced one**

If all steps passed with no change, there is nothing to commit. Do not create
an empty commit.
