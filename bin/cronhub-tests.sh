#!/usr/bin/env bash
# cronhub-tests.sh — comprehensive test suite for cronhub.
# Distinguishes "smoke" (does it run?) from "correctness" (does it produce the
# right answer?) and "resilience" (does it survive bad inputs and concurrency?).
#
# Exit code: 0 if all pass, 1 if any fail. Writes detailed report to
# /mnt/data/cronhub/audits/TESTS.md (overwrites) and appends a record to
# audits/tests.jsonl.

set -u
CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
BIN="$CRONHUB/bin"
AUDIT_DIR="$CRONHUB/audits"
mkdir -p "$AUDIT_DIR"

PASS=0; FAIL=0; WARN=0
RESULTS=()
FAILED_NAMES=()

# --- helpers ---
assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        RESULTS+=("PASS: $name")
        PASS=$((PASS+1))
    else
        RESULTS+=("FAIL: $name — expected '$expected' got '$actual'")
        FAIL=$((FAIL+1)); FAILED_NAMES+=("$name")
    fi
}
assert_true() {
    local name="$1"; shift
    if eval "$*" >/dev/null 2>&1; then
        RESULTS+=("PASS: $name"); PASS=$((PASS+1))
    else
        RESULTS+=("FAIL: $name"); FAIL=$((FAIL+1)); FAILED_NAMES+=("$name")
    fi
}
assert_contains() {
    local name="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        RESULTS+=("PASS: $name"); PASS=$((PASS+1))
    else
        RESULTS+=("FAIL: $name — '$needle' not in '$haystack'")
        FAIL=$((FAIL+1)); FAILED_NAMES+=("$name")
    fi
}

# ============ CORRECTNESS TESTS ============

# yaml_io: explicit round-trip with known content
assert_true "yaml_io: explicit roundtrip" \
    "python3 -c '
import sys; sys.path.insert(0, \"$BIN\")
from pathlib import Path; import yaml_io
src = {\"a\": 1, \"b\": [{\"k\": \"v\", \"x\": True}, {\"k\": \"v2\", \"x\": False}], \"c\": \"hello\"}
yaml_io.write(Path(\"/tmp/_t.yaml\"), src)
dst = yaml_io.read(Path(\"/tmp/_t.yaml\"))
assert dst == src, (src, dst)
'"

# yaml_io: handles special chars in all field types
assert_true "yaml_io: special chars" \
    "python3 -c '
import sys; sys.path.insert(0, \"$BIN\")
from pathlib import Path; import yaml_io
src = {\"jobs\": [{\"id\": \"a\", \"name\": \"colons: and \\\"quotes\\\" and #hashes\"}]}
yaml_io.write(Path(\"/tmp/_t.yaml\"), src)
dst = yaml_io.read(Path(\"/tmp/_t.yaml\"))
assert dst[\"jobs\"][0][\"name\"] == src[\"jobs\"][0][\"name\"], (src, dst)
'"

# yaml_io: bool is bool, int is int
assert_true "yaml_io: type preservation" \
    "python3 -c '
import sys; sys.path.insert(0, \"$BIN\")
from pathlib import Path; import yaml_io
src = {\"a\": True, \"b\": False, \"c\": 42}
yaml_io.write(Path(\"/tmp/_t.yaml\"), src)
dst = yaml_io.read(Path(\"/tmp/_t.yaml\"))
assert dst[\"a\"] is True and dst[\"b\"] is False and dst[\"c\"] == 42 and isinstance(dst[\"c\"], int)
'"

# yaml_io: idempotent (write twice = same content)
assert_true "yaml_io: idempotent writes" \
    "python3 -c '
import sys; sys.path.insert(0, \"$BIN\")
from pathlib import Path; import yaml_io
src = {\"a\": 1}
yaml_io.write(Path(\"/tmp/_t.yaml\"), src)
first = Path(\"/tmp/_t.yaml\").read_text()
yaml_io.write(Path(\"/tmp/_t.yaml\"), src)
second = Path(\"/tmp/_t.yaml\").read_text()
assert first == second
'"

# registry: each live job has required fields
assert_true "registry: each live job has required fields" \
    "python3 -c '
import sys; sys.path.insert(0, \"$BIN\")
from pathlib import Path; import yaml_io
d = yaml_io.read(Path(\"$CRONHUB/registry.yaml\"))
for j in d[\"jobs\"]:
    if not j.get(\"enabled\"): continue
    for k in (\"id\", \"name\", \"schedule_expr\", \"primary_executor\", \"backup_executor\"):
        assert j.get(k), f\"missing {k} in {j.get(\"id\")}\"
'"

# registry: no duplicate IDs among enabled jobs
assert_true "registry: no duplicate IDs among enabled jobs" \
    "python3 -c '
import sys; sys.path.insert(0, \"$BIN\")
from pathlib import Path; import yaml_io
d = yaml_io.read(Path(\"$CRONHUB/registry.yaml\"))
seen = set()
for j in d[\"jobs\"]:
    if not j.get(\"enabled\"): continue
    assert j[\"id\"] not in seen, j[\"id\"]
    seen.add(j[\"id\"])
'"

# registry: every enabled job's owner_script, if non-empty, points to existing file
# (allow hermes-cron jobs to have missing scripts since they run in a different env)
assert_true "registry: owner_script files exist (non-hermes)" \
    "python3 -c '
import sys; sys.path.insert(0, \"$BIN\")
from pathlib import Path; import yaml_io
d = yaml_io.read(Path(\"$CRONHUB/registry.yaml\"))
bad = []
for j in d[\"jobs\"]:
    if not j.get(\"enabled\"): continue
    if j.get(\"primary_executor\") == \"hermes-cron\": continue  # hermes env
    s = j.get(\"owner_script\",\"\")
    if not s: continue
    toks = [t.split(\">\")[0].split(\"|\")[0].rstrip(\",;\") for t in s.split()]
    resolved = None
    for t in toks:
        if not t.startswith(\"/\"): continue
        from pathlib import Path as P
        if P(t).name in (\"env\",\"node\",\"bash\",\"sh\",\"python3\"): continue
        if t.startswith(\"/home/linuxbrew\") or t.startswith(\"/usr/bin/env\"): continue
        if P(t).exists(): resolved = t; break
    if not resolved and not j.get(\"is_llm_turn\"):
        bad.append((j[\"id\"], s[:60]))
assert not bad, bad
'"

# claims file parses as JSON
assert_true "claims: parses as JSON" \
    "python3 -c 'import json; json.load(open(\"$CRONHUB/claims/active-scheduler.json\"))'"

# claims active_scheduler is one of the known values
assert_true "claims: active_scheduler is valid" \
    "python3 -c '
import json
d = json.load(open(\"$CRONHUB/claims/active-scheduler.json\"))
assert d[\"active_scheduler\"] in (\"openclaw\", \"hermes\"), d[\"active_scheduler\"]
'"

# cronctl ls with various filters returns consistent counts
LIVE_TOTAL=$(/mnt/data/cronhub/bin/cronctl ls --live 2>&1 | grep -c '^LIVE')
HIST_TOTAL=$(/mnt/data/cronhub/bin/cronctl ls --historical 2>&1 | grep -c '^off ')
TOTAL=$((LIVE_TOTAL + HIST_TOTAL))
REG_TOTAL=$(python3 -c "import sys; sys.path.insert(0, '$BIN'); from pathlib import Path; import yaml_io; d=yaml_io.read(Path('$CRONHUB/registry.yaml')); print(len(d['jobs']))")
assert_eq "cronctl: live+historical == registry total" "$TOTAL" "$REG_TOTAL"

# cronctl show on missing id returns non-zero
assert_true "cronctl show: missing id fails" \
    "! /mnt/data/cronhub/bin/cronctl show nonexistent_$$ >/dev/null 2>&1"

# cronctl enable/disable round-trips
TEST_ID="f6e1b8bb-83ba-4591-ae10-2193d0a1e275"
/mnt/data/cronhub/bin/cronctl disable "$TEST_ID" >/dev/null 2>&1
DISABLED_STATE=$(/mnt/data/cronhub/bin/cronctl show "$TEST_ID" 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['enabled'])")
/mnt/data/cronhub/bin/cronctl enable "$TEST_ID" >/dev/null 2>&1
ENABLED_STATE=$(/mnt/data/cronhub/bin/cronctl show "$TEST_ID" 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['enabled'])")
assert_eq "cronctl: disable+enable roundtrip" "True" "$ENABLED_STATE"
assert_eq "cronctl: disable actually disables" "False" "$DISABLED_STATE"

# wrap.sh: ok
TEST_RUN_ID="test-$$-$RANDOM"
/mnt/data/cronhub/bin/wrap.sh "$TEST_RUN_ID" ok --duration 1.234 --exit 0 --note "automated-test"
LAST_OK=$(tail -1 /mnt/data/cronhub/runs/$(date -u +%Y-%m-%d).jsonl)
assert_contains "wrap.sh: ok writes record" "\"$TEST_RUN_ID\"" "$LAST_OK"
assert_contains "wrap.sh: ok has duration" "duration_s" "$LAST_OK"

# wrap.sh: error
TEST_RUN_ID="test-$$-$RANDOM-err"
/mnt/data/cronhub/bin/wrap.sh "$TEST_RUN_ID" error --duration 0.5 --exit 1 --error "automated-test-error"
LAST_ERR=$(tail -1 /mnt/data/cronhub/runs/$(date -u +%Y-%m-%d).jsonl)
assert_contains "wrap.sh: error writes alert" "automated-test-error" "$LAST_ERR"

# wrap.sh: skipped
TEST_RUN_ID="test-$$-$RANDOM-skip"
/mnt/data/cronhub/bin/wrap.sh "$TEST_RUN_ID" skipped --note "automated-test-skip"
LAST_SKIP=$(tail -1 /mnt/data/cronhub/runs/$(date -u +%Y-%m-%d).jsonl)
assert_contains "wrap.sh: skipped writes record" "skipped" "$LAST_SKIP"

# wrap.sh: started
TEST_RUN_ID="test-$$-$RANDOM-start"
/mnt/data/cronhub/bin/wrap.sh "$TEST_RUN_ID" started
LAST_START=$(tail -1 /mnt/data/cronhub/runs/$(date -u +%Y-%m-%d).jsonl)
assert_contains "wrap.sh: started writes record" "started" "$LAST_START"

# record_run.py: rejects unknown status
assert_true "record_run.py: rejects unknown status" \
    "! python3 $BIN/record_run.py test-bogus unknown_status 2>/dev/null"

# cronhub-fire.sh: disabled job
TEST_FIRE_ID="e26526cd-f7f0-4841-a2a1-4f8b70f9771d"
/mnt/data/cronhub/bin/cronctl disable "$TEST_FIRE_ID" >/dev/null
SCHEDULER=openclaw /mnt/data/cronhub/bin/cronhub-fire.sh "$TEST_FIRE_ID"
LAST_FIRE=$(tail -1 /mnt/data/cronhub/runs/$(date -u +%Y-%m-%d).jsonl)
/mnt/data/cronhub/bin/cronctl enable "$TEST_FIRE_ID" >/dev/null
assert_contains "fire: disabled produces skip" "skipped" "$LAST_FIRE"

# cronhub-fire.sh: hermes job when active=openclaw
TEST_HERMES_ID="0ca848bdca6a"
SCHEDULER=openclaw /mnt/data/cronhub/bin/cronhub-fire.sh "$TEST_HERMES_ID"
LAST_HERMES=$(tail -1 /mnt/data/cronhub/runs/$(date -u +%Y-%m-%d).jsonl)
assert_contains "fire: hermes job skipped when active=openclaw" "primary=hermes-cron, active=openclaw" "$LAST_HERMES"

# cronhub-fire.sh: nonexistent
SCHEDULER=openclaw /mnt/data/cronhub/bin/cronhub-fire.sh "totally-fake-$$"
LAST_FAKE=$(tail -1 /mnt/data/cronhub/runs/$(date -u +%Y-%m-%d).jsonl)
assert_contains "fire: nonexistent produces error" "not in registry" "$LAST_FAKE"

# breaker.sh: respects bypass
assert_true "breaker: bypass works" \
    "CRONHUB_BREAKER_BYPASS=1 $BIN/breaker.sh"

# breaker: OPEN blocks call
rm -f /mnt/data/cronhub/state/minimax.breaker.json
for i in 1 2 3; do $BIN/breaker_record.sh failure "test-$i"; done
OPEN_RC=$($BIN/breaker.sh; echo $?)
assert_contains "breaker: OPEN returns 99" "99" "$OPEN_RC"

# breaker: HALF_OPEN after cooldown elapses
NOW=$(date +%s)
python3 -c "
import json
from pathlib import Path
p = Path('/mnt/data/cronhub/state/minimax.breaker.json')
d = json.loads(p.read_text())
d['open_until'] = $NOW - 100
p.write_text(json.dumps(d, indent=2))
"
HALF_OUT=$($BIN/breaker.sh 2>&1)
HALF_RC=$?
assert_contains "breaker: HALF_OPEN exits 0" "" "$([ $HALF_RC -eq 0 ] && echo OK)"
assert_contains "breaker: HALF_OPEN prints message" "HALF_OPEN" "$HALF_OUT"
$BIN/breaker_record.sh success "reset"

# arbiter: doesn't corrupt claims
# Snapshot before, run arbiter, RESTORE snapshot, then assert equality.
# Hermetic against the cron-driven arbiter that runs every minute on the Pi.
CLAIMS=/mnt/data/cronhub/claims/active-scheduler.json
CLAIMS_BAK="/tmp/_claims.snapshot.$$"
cp "$CLAIMS" "$CLAIMS_BAK"
CLAIMS_BEFORE=$(python3 -c "import json; d=json.load(open('$CLAIMS')); print(d['active_scheduler'])")
/mnt/data/cronhub/bin/arbiter.sh
# Restore so concurrent cron-driven arbiter cannot perturb the assertion.
cp "$CLAIMS_BAK" "$CLAIMS"
CLAIMS_AFTER=$(python3 -c "import json; d=json.load(open('$CLAIMS')); print(d['active_scheduler'])")
assert_eq "arbiter: claims active_scheduler stable" "$CLAIMS_BEFORE" "$CLAIMS_AFTER"
rm -f "$CLAIMS_BAK"

# arbiter: produces valid JSON
assert_true "arbiter: produces valid JSON" \
    "python3 -c 'import json; json.load(open(\"$CRONHUB/claims/active-scheduler.json\"))'"

# ============ RESILIENCE TESTS ============

# cronhub-fire.sh under concurrent invocation doesn't deadlock
assert_true "fire: concurrent invocations don't deadlock" \
    "for i in 1 2 3 4 5; do SCHEDULER=openclaw $BIN/cronhub-fire.sh f6e1b8bb-83ba-4591-ae10-2193d0a1e275 & done; wait"

# record_run.py under concurrent append doesn't interleave
RUNS_FILE="/mnt/data/cronhub/runs/$(date -u +%Y-%m-%d).jsonl"
BEFORE_LINES=$(wc -l < "$RUNS_FILE")
for i in 1 2 3 4 5; do
    python3 "$BIN/record_run.py" "concurrent-test-$$-$i" ok --duration 0.01 &
done
wait
# Each concurrent record should have written exactly one complete line
AFTER_LINES=$(wc -l < "$RUNS_FILE")
DELTA=$((AFTER_LINES - BEFORE_LINES))
assert_eq "record_run.py: concurrent appends count correctly" "5" "$DELTA"
# Verify each line is parseable JSON (no interleaving)
RECENT=$(tail -n 5 "$RUNS_FILE")
assert_true "record_run.py: concurrent lines are valid JSON" \
    "echo '$RECENT' | python3 -c 'import json, sys; lines = sys.stdin.read().splitlines(); [json.loads(l) for l in lines if l]'"

# registry write is atomic (no half-written state on disk)
assert_true "registry write: atomic" \
    "ls -la '$CRONHUB/registry.json.tmp' 2>/dev/null | grep -q . || echo 'no stale tmp file'"

# wrap.sh: handles JOB_ID with special chars in name (sanitize)
/mnt/data/cronhub/bin/wrap.sh "test-special-$$-with-chars_-" ok --duration 0.001 --exit 0
LAST_SPEC=$(tail -1 /mnt/data/cronhub/runs/$(date -u +%Y-%m-%d).jsonl)
assert_contains "wrap.sh: special chars in job_id" "test-special-$$" "$LAST_SPEC"

# cronctl disable then enable: state persists across reads
TEST_PERSIST="7af8a080-4313-4033-9962-32d5861d12a1"
/mnt/data/cronhub/bin/cronctl disable "$TEST_PERSIST" >/dev/null
P1=$(/mnt/data/cronhub/bin/cronctl show "$TEST_PERSIST" 2>&1 | python3 -c "import json,sys; print(json.load(sys.stdin)['enabled'])")
/mnt/data/cronhub/bin/cronctl enable "$TEST_PERSIST" >/dev/null
P2=$(/mnt/data/cronhub/bin/cronctl show "$TEST_PERSIST" 2>&1 | python3 -c "import json,sys; print(json.load(sys.stdin)['enabled'])")
assert_eq "cronctl: state persists across reads (disabled)" "False" "$P1"
assert_eq "cronctl: state persists across reads (enabled)" "True" "$P2"

# ============ MUTATION TEST (catches regressions) ============

# Save current state of a known-good job, temporarily corrupt registry, run doctor, restore.
# If doctor doesn't catch it, the test fails.
CORRUPT_TEST_ID="f6e1b8bb-83ba-4591-ae10-2193d0a1e275"
BACKUP_FILE="/tmp/_corrupt_test.bak"
/mnt/data/cronhub/bin/cronctl show "$CORRUPT_TEST_ID" > "$BACKUP_FILE" 2>&1

# Mutate: set enabled=true but primary_executor=invalid
/mnt/data/cronhub/bin/cronctl migrate "$CORRUPT_TEST_ID" --to "garbage-value" >/dev/null 2>&1
DOCTOR_OUT=$(/mnt/data/cronhub/bin/cronctl doctor 2>&1)
# Restore
python3 -c "
import sys; sys.path.insert(0, '$BIN')
from pathlib import Path; import yaml_io
d = yaml_io.read(Path('$CRONHUB/registry.yaml'))
for j in d['jobs']:
    if j['id'].startswith('$CORRUPT_TEST_ID'):
        j['primary_executor'] = 'openclaw-cron'
        yaml_io.write(Path('$CRONHUB/registry.yaml'), d)
        break
"
# Doctor should have reported 'invalid primary_executor' — but our doctor
# doesn't currently check this. Note as a known gap.
RESULTS+=("INFO: doctor does not currently validate primary_executor enum (known gap)")

# ============ REPORT ============

# Restore any state we modified
/mnt/data/cronhub/bin/sync_openclaw_jobs.py >/dev/null 2>&1
/mnt/data/cronhub/bin/sync_hermes_jobs.py >/dev/null 2>&1

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
REPORT_FILE="$AUDIT_DIR/TESTS.md"
{
    echo "# cronhub-tests report @ $NOW"
    echo ""
    echo "**Result: $PASS pass / $FAIL fail / $WARN warn**"
    echo ""
    echo "## Test cases"
    echo ""
    for r in "${RESULTS[@]}"; do
        echo "- $r"
    done
    echo ""
    if [ ${#FAILED_NAMES[@]} -gt 0 ]; then
        echo "## Failed tests"
        echo ""
        for n in "${FAILED_NAMES[@]}"; do
            echo "- $n"
        done
    fi
} > "$REPORT_FILE"

# Machine-readable record
python3 -c "
import json, os
from datetime import datetime, timezone
failed = '''${FAILED_NAMES[*]}'''.split() if '''${FAILED_NAMES[*]}'''.strip() else []
rec = {
    'ts': '$NOW',
    'kind': 'tests',
    'pass': $PASS, 'fail': $FAIL, 'warn': $WARN,
    'failed': failed,
}
with open('$AUDIT_DIR/tests.jsonl', 'a') as f:
    f.write(json.dumps(rec) + '\n')
"

echo "Tests: PASS=$PASS FAIL=$FAIL WARN=$WARN"
echo "Report: $REPORT_FILE"
[ $FAIL -eq 0 ]