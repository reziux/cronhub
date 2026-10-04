#!/usr/bin/env bash
# Run every cronhub test suite. Non-zero exit if any fails.
#   bash /mnt/data/cronhub/bin/run_all_tests.sh          # summary
#   VERBOSE=1 bash ...                                   # full output
set -u
cd "$(dirname "$0")" || exit 2

SUITES=(
    "python3 test_yaml_io_strict.py"
    "bash test_openclaw_heartbeat.sh"
    "bash test_wrap_duration.sh"
    "node /home/reziux/.openclaw/workspace/scripts/test_memory_consolidator_race.js"
)
# The two JSON-line suites have no "passed:" line; count their own ok/FAIL.

R=0
for t in "${SUITES[@]}"; do
    out=$($t 2>&1); rc=$?
    [ -n "${VERBOSE:-}" ] && printf '%s\n' "$out"
    # Suites that print their own "passed:" line win; the rest are counted from
    # their per-assertion ok/FAIL lines. (grep exiting 1 on no-match is a normal
    # result here, not an error — hence the `|| true` and the string test.)
    summary=$(printf '%s' "$out" | grep -E '^passed:' | tail -1 || true)
    if [ -z "$summary" ]; then
        ok=$(printf '%s' "$out" | grep -cE '^  ok ' || true)
        bad=$(printf '%s' "$out" | grep -cE '^  FAIL' || true)
        summary="passed: $ok  failed: $bad"
    fi
    printf '%-58s rc=%d  %s\n' "$t" "$rc" "$summary"
    [ "$rc" -ne 0 ] && R=1
done
printf -- '---- OVERALL rc=%d ----\n' "$R"
exit "$R"
