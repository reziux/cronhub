#!/usr/bin/env bash
# wrap.sh — wrap any cron command with timing, run-record, and exit-code capture.
#
# Usage:  wrap.sh <job_id> [-- <command...>]
#         wrap.sh <job_id> started
#         wrap.sh <job_id> ok|error [--error "msg"] [--duration N]
#
# Idempotent. Designed to be called from both OpenClaw and Hermes cron payloads.

set -u
CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
export CRONHUB
SCHEDULER="${SCHEDULER:-unknown}"
export SCHEDULER

JOB_ID="${1:-}"
[ -z "$JOB_ID" ] && { echo "wrap.sh: missing job_id" >&2; exit 2; }

if [ "${2:-}" = "started" ]; then
    python3 "$CRONHUB/bin/record_run.py" "$JOB_ID" started
    exit 0
fi

# Sentinel: ok|error with optional fields
if [ "${2:-}" = "ok" ] || [ "${2:-}" = "error" ] || [ "${2:-}" = "skipped" ]; then
    STATUS="$2"; shift 2
    DURATION=""
    ERROR=""
    NOTE=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --duration) DURATION="$2"; shift 2;;
            --error)    ERROR="$2"; shift 2;;
            --note)     NOTE="$2"; shift 2;;
            *) shift;;
        esac
    done
    # Build args as an array to handle spaces safely
    set -- "$JOB_ID" "$STATUS"
    [ -n "$DURATION" ] && set -- "$@" --duration "$DURATION"
    [ -n "$ERROR" ]    && set -- "$@" --error "$ERROR"
    [ -n "$NOTE" ]     && set -- "$@" --note "$NOTE"
    python3 "$CRONHUB/bin/record_run.py" "$@"
    exit 0
fi

# Otherwise: full command to wrap
shift
START_TS=$(date +%s.%N)
python3 "$CRONHUB/bin/record_run.py" "$JOB_ID" started

# Sanitize JOB_ID for mktemp template — strip anything not [A-Za-z0-9._-]
SAFE_JOB_ID=$(printf '%s' "$JOB_ID" | tr -cd '[:alnum:]._-')
[ -z "$SAFE_JOB_ID" ] && SAFE_JOB_ID="anon"
TMP_OUT="$(umask 077 && mktemp -t "wrap.${SAFE_JOB_ID}.XXXXXX")"
# FIX (2026-10-03): the previous `trap 'rm -f "$TMP_OUT"' EXIT` deleted the
# very file we hand to record_run.py as --output, so every output_path in
# runs/*.jsonl was a dead link. Failures were unreviewable after the fact.
# Now we copy stdout to a durable per-day path and only then unlink the temp.
DURABLE_DIR="${CRONHUB:-/mnt/data/cronhub}/audits/wrap-output"
mkdir -p "$DURABLE_DIR"
DURABLE_OUT="$DURABLE_DIR/$(date -u +%Y-%m-%d)/${SAFE_JOB_ID}-$(date -u +%H%M%S)-$$.log"
mkdir -p "$(dirname "$DURABLE_OUT")"
trap 'rm -f "$TMP_OUT"' EXIT

EXIT_CODE=0
"$@" >"$TMP_OUT" 2>&1 || EXIT_CODE=$?

# Persist before the trap fires.
cp -p "$TMP_OUT" "$DURABLE_OUT" 2>/dev/null || true

END_TS=$(date +%s.%N)
# FIX (2026-10-04): this was `-e="$END_TS"`. `-e` is awk's PROGRAM text
# flag, so mawk treated `-e=1791077042.5` as an option, printed a usage error
# to stderr and produced EMPTY output -> DURATION="". record_run.py then
# rejected `--duration ''` and the `ok`/`error` line was NEVER written. Every
# job still ran correctly (stdout was durable, exit codes propagated); only
# the run-record half of wrap.sh was silently dead since 2026-10-03 21:45.
DURATION=$(awk -v s="$START_TS" -v e="$END_TS" 'BEGIN{printf "%.3f", e-s}')

if [ "$EXIT_CODE" -eq 0 ]; then
    python3 "$CRONHUB/bin/record_run.py" "$JOB_ID" ok --duration "$DURATION" --exit 0 --output "$DURABLE_OUT"
else
    ERR_TAIL=$(tail -c 500 "$TMP_OUT" | tr '\n' ' ' | tr -d '"' | tr -d "'" | cut -c1-400)
    python3 "$CRONHUB/bin/record_run.py" "$JOB_ID" error --duration "$DURATION" --exit "$EXIT_CODE" --error "$ERR_TAIL" --output "$DURABLE_OUT"
fi
exit $EXIT_CODE