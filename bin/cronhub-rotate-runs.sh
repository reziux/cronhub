#!/bin/bash
# cronhub-rotate-runs.sh -- keep N days of run history.
#
# WHY (2026-09-28)
# -----------------
# runs/ had no retention at all. The destructive rebuild wiped it, and nothing
# preserved it since, so every "7d"/"30d" figure on the dashboard was really
# just the last 24h. /api/runs/timeseries?hours=24 and ?hours=168 returned
# almost identical totals because there was only one day of data.
#
# Analytics can only get better once history accumulates, and history only
# accumulates if something stops deleting it and nothing keeps it forever.
#
# Keeps RUN_KEEP_DAYS (default 30) shards, prunes the rest, and reports.
set -uo pipefail

CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
RUNS="$CRONHUB/runs"
KEEP="${RUN_KEEP_DAYS:-30}"

[ -d "$RUNS" ] || { echo "rotate-runs: no $RUNS"; exit 0; }

total=$(find "$RUNS" -maxdepth 1 -name '*.jsonl' -type f | wc -l)

# Prune shards older than the cutoff. Filenames are YYYY-MM-DD so lexical
# order is chronological order.
cutoff=$(date -u -d "$KEEP days ago" +%Y-%m-%d 2>/dev/null)
if [ -z "$cutoff" ]; then
  echo "rotate-runs: cannot compute cutoff date" >&2
  exit 0
fi

removed=0
kept=0
for f in "$RUNS"/*.jsonl; do
  [ -e "$f" ] || continue
  base=$(basename "$f" .jsonl)
  case "$base" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    *) kept=$((kept+1)); continue ;;     # not a date shard, leave alone
  esac
  if [[ "$base" < "$cutoff" ]]; then
    sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
    if rm -f "$f"; then
      removed=$((removed+1))
      echo "rotate-runs: pruned $base (${sz} B)"
    fi
  else
    kept=$((kept+1))
  fi
done

echo "rotate-runs: $total shard(s) before; kept $kept, pruned $removed (keep ${KEEP}d, cutoff < $cutoff)"
