#!/usr/bin/env bash
# cronhub-record.sh -- run a command, then record the result. Output-safe.
#
# WHY NOT wrap.sh FOR INFRASTRUCTURE JOBS (2026-09-28)
# ---------------------------------------------------
# wrap.sh captures the command's stdout+stderr into a temp file and passes it
# to record_run as --output. That is correct for payload jobs, but the
# cronhub infrastructure lines rely on their own append-only log files
# (state/arbiter.log, state/sync.log, audits/auditor-cron.log, ...), and the
# dashboard derives a liveness signal from those files' mtime
# (last_seen_via_log_mtime). Wrapping them would silently stop writing those
# logs and break that signal.
#
# This variant lets the command's output flow straight through to whatever
# redirect the crontab line already has, so existing log files are untouched,
# and records the result afterwards. One record per run (terminal only, no
# separate `started` marker) which also keeps python startups down on the
# every-minute jobs.
#
# Usage:  cronhub-record.sh <job_id> -- <command...>
#
# The command's exit code is propagated unchanged, so cron still sees the
# same status it sees today.
set -u

JOB_ID="${1:-}"
if [ -z "$JOB_ID" ]; then
    echo "cronhub-record.sh: missing job_id" >&2
    exit 2
fi
shift
[ "${1:-}" = "--" ] && shift

if [ "$#" -eq 0 ]; then
    echo "cronhub-record.sh: no command given for $JOB_ID" >&2
    exit 2
fi

"$@"
RC=$?

CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
if [ "$RC" -eq 0 ]; then
    python3 "$CRONHUB/bin/record_run.py" "$JOB_ID" ok --note infra >/dev/null 2>&1 || true
else
    python3 "$CRONHUB/bin/record_run.py" "$JOB_ID" error --exit "$RC" \
        --error "exit=$RC" --note infra >/dev/null 2>&1 || true
fi

exit $RC
