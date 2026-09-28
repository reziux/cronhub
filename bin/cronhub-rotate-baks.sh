#!/usr/bin/env bash
# cronhub-rotate-baks — keep last N bak/registry.yaml.bak-*, prune the rest.
# Run daily. Default N=10.
set -u
CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
KEEP="${KEEP_N:-10}"
cd "$CRONHUB" || exit 0

# Match both styles: registry.yaml.bak-<ts> and registry.yaml.bak-<label>
ls -1t registry.yaml.bak-* 2>/dev/null \
    | awk -v keep="$KEEP" '
        { n[NR]=$0 }
        END {
            for (i = keep + 1; i <= length(n); i++) print n[i]
        }' \
    | while read -r f; do
        [ -f "$f" ] && rm -- "$f" && echo "pruned: $f"
    done
echo "rotate-baks: done (kept last $KEEP)"
