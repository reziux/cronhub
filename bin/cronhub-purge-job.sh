#!/usr/bin/env bash
# cronhub-purge-job.sh — remove all records for a job id from cronhub's logs.
#
# Why this exists: cronhub's test suites deliberately fire deliberately-nonexistent
# job ids (e.g. `nonexistent_$$`, `totally-fake-$$`) to exercise the
# "job_id not in registry.yaml" negative path. Those probes fail *by design*,
# and record_run.py faithfully mirrors the failure into the production
# alerts/failures.jsonl. That pollutes the real alert stream (and would page
# a human once a webhook is wired) with failures that are not real.
#
# Usage: cronhub-purge-job.sh <id-substring> [id-substring ...]
#
# Idempotent and safe to run when nothing matches.
set -u

CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
LOCKS="$CRONHUB/locks"

if [ "$#" -eq 0 ]; then
  echo "usage: cronhub-purge-job.sh <id-substring> [...]" >&2
  exit 2
fi

mkdir -p "$LOCKS"

# Take the same locks record_run.py uses, so we never truncate a file mid-append.
_runs_lock=$(mktemp "$LOCKS/purge-runs.lock.XXXXXX")
_alerts_lock=$(mktemp "$LOCKS/purge-alerts.lock.XXXXXX")
trap 'rm -f "$_runs_lock" "$_alerts_lock"' EXIT
exec 9>"$_runs_lock"
exec 8>"$_alerts_lock"
flock -w 10 9 || { echo "purge: could not lock runs" >&2; exit 1; }
flock -w 10 8 || { echo "purge: could not lock alerts" >&2; exit 1; }

python3 - "$@" <<'PYEOF'
import glob, json, os, sys
from pathlib import Path

CRONHUB = Path(os.environ.get("CRONHUB", "/mnt/data/cronhub"))
needles = sys.argv[1:]

total_removed = 0
files_touched = 0

# runs/YYYY-MM-DD.jsonl
for path in sorted(glob.glob(str(CRONHUB / "runs" / "*.jsonl"))):
    p = Path(path)
    if not p.exists():
        continue
    lines = p.read_text().splitlines()
    kept, removed = [], 0
    for line in lines:
        drop = False
        for n in needles:
            if n and n in line:
                drop = True
                break
        if drop:
            removed += 1
        else:
            kept.append(line)
    if removed:
        p.write_text("\n".join(kept) + ("\n" if kept else ""))
        total_removed += removed
        files_touched += 1
        print(f"  runs: {p.name} -{removed}")

# alerts/failures.jsonl
p = CRONHUB / "alerts" / "failures.jsonl"
if p.exists():
    lines = p.read_text().splitlines()
    kept, removed = [], 0
    for line in lines:
        drop = False
        for n in needles:
            if n and n in line:
                drop = True
                break
        if drop:
            removed += 1
        else:
            kept.append(line)
    if removed:
        p.write_text("\n".join(kept) + ("\n" if kept else ""))
        total_removed += removed
        files_touched += 1
        print(f"  alerts/failures.jsonl -{removed}")

print(f"purge-job: removed {total_removed} record(s) across {files_touched} file(s)")
PYEOF
