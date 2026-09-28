#!/usr/bin/env bash
# Circuit breaker for the LLM provider (Minimax M3).
# Reads state file. If provider has returned >= $THRESHOLD consecutive timeouts
# in the last $WINDOW seconds, the breaker is OPEN and any new call will fail-fast.
# After $COOLDOWN seconds, breaker goes HALF_OPEN and allows one test call.
# Successful calls reset the failure count.
#
# Usage in cron payload:
#   bash /mnt/data/cronhub/bin/breaker.sh && <actual command>
#
# Or via env:
#   CRONHUB_BREAKER_BYPASS=1 bash <command>   # skip breaker
#
# State file: /mnt/data/cronhub/state/minimax.breaker.json

set -u
CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
STATE="$CRONHUB/state/minimax.breaker.json"
THRESHOLD="${BREAKER_THRESHOLD:-3}"
WINDOW="${BREAKER_WINDOW:-300}"      # 5 min
COOLDOWN="${BREAKER_COOLDOWN:-600}"   # 10 min

mkdir -p "$(dirname "$STATE")"

# Bypass hook
if [ "${CRONHUB_BREAKER_BYPASS:-0}" = "1" ]; then
    exit 0
fi

# Read breaker state via env, no shell interpolation of path
NOW=$(date +%s)
read -r OPEN_UNTIL FAIL_COUNT LAST_FAIL_TS LAST_STATE < <(
    CRONHUB="$CRONHUB" STATE="$STATE" python3 <<'PYEOF'
import os, json
from pathlib import Path
p = Path(os.environ["STATE"])
if not p.exists():
    print("0 0 0 INIT")
    sys.exit(0)
try:
    d = json.loads(p.read_text())
except Exception:
    print("0 0 0 CORRUPT")
    sys.exit(0)
print(d.get("open_until", 0), d.get("fail_count", 0), d.get("last_fail_ts", 0), d.get("last_state", "CLOSED"))
PYEOF
)

# Decision
if [ -n "$OPEN_UNTIL" ] && [ "$OPEN_UNTIL" -gt 0 ] && [ "$OPEN_UNTIL" -gt "$NOW" ]; then
    # Circuit OPEN — refuse
    REMAINING=$(( OPEN_UNTIL - NOW ))
    echo "[breaker] OPEN: refusing call, ${REMAINING}s until HALF_OPEN" >&2
    exit 99
fi

# If breaker was OPEN but cooldown elapsed, we are HALF_OPEN.
# Reset OPEN marker in state so next failure can re-open.
if [ -n "$OPEN_UNTIL" ] && [ "$OPEN_UNTIL" -gt 0 ] && [ "$OPEN_UNTIL" -le "$NOW" ]; then
    echo "[breaker] HALF_OPEN: allowing one test call" >&2
    CRONHUB="$CRONHUB" STATE="$STATE" python3 <<'PYEOF'
import os, json
from pathlib import Path
p = Path(os.environ["STATE"])
try:
    d = json.loads(p.read_text())
except Exception:
    d = {}
d["open_until"] = 0
d["last_state"] = "HALF_OPEN"
d.pop("fail_count", None)
p.write_text(json.dumps(d, indent=2))
PYEOF
fi

# Allow the call through
exit 0