#!/usr/bin/env bash
# cronhub-check-job-fresh — non-LLM staleness check for an observer.
# Usage: cronhub-check-job-fresh.sh <target_name_or_id> [max_age_seconds]
#
# Reads /mnt/data/cronhub/runs/*.jsonl and emits one of:
#   ok          — most recent run for <target> was within max_age_seconds
#                 (default max_age = period_seconds × 2)
#   warn        — last run is older than max_age_seconds
#   unknown     — no run records found for <target>
#
# Exit codes:
#   0  fresh (last run within window)
#   1  stale (last run older than window)
#   2  unknown (no records)
#   3  bad args
#
# The "max_age" default assumes hourly cadence (3600s × 2 = 7200s). Pass a
# custom max_age for faster/slower cadences.
set -u
CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
TARGET="${1:-}"
MAX_AGE="${2:-7200}"  # 2 hours default
[ -z "$TARGET" ] && { echo "usage: $0 <target> [max_age_s]" >&2; exit 3; }

# Look for the most recent run record whose 'id' or 'name' contains TARGET.
# We don't try to be clever — bash + python is plenty for a per-minute observer.
RECORD=$(
    python3 - "$TARGET" "$MAX_AGE" <<'PY'
import json, glob, os, sys, time
from pathlib import Path
target = sys.argv[1]
max_age = int(sys.argv[2])
runs_dir = Path("/mnt/data/cronhub/runs")
# Tolerate partial matches against id (UUID prefix) or name (job label).
target_l = target.lower()
latest_ts = None
latest_id = None
for f in sorted(runs_dir.glob("*.jsonl")):
    for line in f.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        rid = (rec.get("id") or "").lower()
        nm = (rec.get("id") or "").lower()  # we only have id in runs.jsonl
        if target_l in rid:
            ts = rec.get("ts")
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
                latest_id = rec.get("id")
# Compute age
if latest_ts is None:
    print("unknown")
else:
    from datetime import datetime, timezone
    try:
        t = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
    except Exception:
        print("unknown")
        sys.exit(0)
    age = (datetime.now(timezone.utc) - t).total_seconds()
    if age <= max_age:
        print(f"ok age={int(age)}s")
    else:
        print(f"warn age={int(age)}s last_id={latest_id[:14] if latest_id else '?'}")
PY
)

if [ "$RECORD" = "ok" ] || [[ "$RECORD" == ok* ]]; then
    exit 0
elif [ "$RECORD" = "warn" ] || [[ "$RECORD" == warn* ]]; then
    exit 1
else
    exit 2
fi
