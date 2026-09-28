#!/usr/bin/env bash
# breaker_record.sh — record outcome of an LLM call into the breaker state file.
# Usage: breaker_record.sh success|failure [reason]
# Exit code is preserved as caller-provided (passed via $EXIT_CODE if set)

set -u
STATE="${CRONHUB:-/mnt/data/cronhub}/state/minimax.breaker.json"
THRESHOLD="${BREAKER_THRESHOLD:-3}"
COOLDOWN="${BREAKER_COOLDOWN:-600}"

OUTCOME="${1:-success}"
REASON="${2:-unspecified}"
NOW=$(date +%s)

mkdir -p "$(dirname "$STATE")"

python3 - "$STATE" "$OUTCOME" "$REASON" "$NOW" "$THRESHOLD" "$COOLDOWN" <<'PYEOF'
import sys, json
from pathlib import Path
state_path, outcome, reason, now, threshold, cooldown = sys.argv[1:7]
now = int(now); threshold = int(threshold); cooldown = int(cooldown)
existing = {}
if Path(state_path).exists():
    try: existing = json.loads(Path(state_path).read_text())
    except: pass
if outcome == "failure":
    fail_count = existing.get("fail_count", 0) + 1
    existing["fail_count"] = fail_count
    existing["last_fail_ts"] = now
    existing["last_fail_reason"] = reason[:200]
    if fail_count >= threshold:
        existing["open_until"] = now + cooldown
        existing["last_state"] = "OPEN"
    else:
        existing["last_state"] = "CLOSED"
else:
    existing["fail_count"] = 0
    existing["last_success_ts"] = now
    existing["last_state"] = "CLOSED"
    existing.pop("open_until", None)
Path(state_path).write_text(json.dumps(existing, indent=2))
print(json.dumps({"last_state": existing["last_state"], "fail_count": existing.get("fail_count", 0)}))
PYEOF