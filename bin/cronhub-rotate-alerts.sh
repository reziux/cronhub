#!/usr/bin/env bash
# cronhub-rotate-alerts — daily rotation for alerts/*.jsonl and alerts/failover.jsonl.
# Compresses files older than 7 days, deletes compressed files older than 90 days.
set -u
CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
ALERTS="$CRONHUB/alerts"
KEEP_DAYS="${KEEP_DAYS:-90}"
COMPRESS_OLDER_DAYS="${COMPRESS_OLDER_DAYS:-7}"
cd "$ALERTS" || exit 0

# 1. Compress any .jsonl older than COMPRESS_OLDER_DAYS that's not already .gz
find . -maxdepth 1 -type f -name "*.jsonl" -mtime +"$COMPRESS_OLDER_DAYS" | while read -r f; do
    if [ ! -f "${f}.gz" ]; then
        gzip -9 "$f" && echo "rotated: $(basename "$f").gz"
    fi
done

# 2. Remove .gz files older than KEEP_DAYS
find . -maxdepth 1 -type f -name "*.jsonl.gz" -mtime +"$KEEP_DAYS" -delete -print | sed 's/^/pruned: /'
