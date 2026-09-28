#!/usr/bin/env bash
# cronhub-fire.sh — invoked by OpenClaw cron (and Hermes cron if it ever
# dispatches there) for each job whose primary_executor matches the active
# scheduler. Routes to the real command via wrap.sh; records to the unified
# log; respects the circuit breaker for LLM jobs.
#
# Usage: cronhub-fire.sh <job_id>
#
# Decision logic (read from registry.yaml):
#   - if ! enabled:           log 'skipped' (job disabled)
#   - if primary != active:   log 'skipped' (backed off, backup only on heartbeat-stale)
#   - if breaker is OPEN:     log 'error' with reason
#   - else:                   invoke owner_script via wrap.sh

set -u
CRONHUB="${CRONHUB:-/mnt/data/cronhub}"
JOB_ID="${1:-}"
[ -z "$JOB_ID" ] && { echo "cronhub-fire.sh: missing job_id" >&2; exit 2; }

REG="$CRONHUB/registry.yaml"
CLAIMS="$CRONHUB/claims/active-scheduler.json"
SCHEDULER="${SCHEDULER:-$(hostname -s)}"
export CRONHUB SCHEDULER JOB_ID REG CLAIMS

# Single-pass lookup. Output (via env, no shell interpolation):
#   line 1: <enabled> <primary> <fallback> <is_llm> <active>
#   line 2: <owner_script>     (may contain spaces; treat as remainder)
OUT=$(CRONHUB="$CRONHUB" REG="$REG" CLAIMS="$CLAIMS" JOB_ID="$JOB_ID" python3 <<'PYEOF'
import os, json, sys
sys.path.insert(0, os.environ["CRONHUB"] + "/bin")
from pathlib import Path
import yaml_io
try:
    d = yaml_io.read(Path(os.environ["REG"]))
except Exception as e:
    print(f"# error reading registry: {e}", file=sys.stderr)
    sys.exit(1)
target = None
for j in d["jobs"]:
    if j["id"].startswith(os.environ["JOB_ID"]):
        target = j
        break
if not target:
    print("NOTFOUND")
    sys.exit(0)
try:
    claims = json.loads(Path(os.environ["CLAIMS"]).read_text())
except Exception:
    claims = {"active_scheduler": "openclaw"}
# When SCHEDULER is set to a known scheduler name (openclaw/hermes), treat it
# as authoritative for tests and operator runs. Production launches don't
# pass SCHEDULER as one of these names — they default to hostname.
_sched = os.environ.get("SCHEDULER", "")
if _sched in ("openclaw", "hermes"):
    active = _sched
else:
    active = claims.get("active_scheduler", "openclaw")
enabled = str(bool(target.get("enabled", False))).lower()
primary = target.get("primary_executor", "")
fallback = target.get("fallback_strategy", "skip")
is_llm = str(bool(target.get("is_llm_turn", False))).lower()
script = target.get("owner_script", "") or ""
print(f"{enabled} {primary} {fallback} {is_llm} {active}")
print(script)
PYEOF
)
# If python failed, OUT will be empty or contain error
if [ -z "$OUT" ] || [ "$(printf '%s\n' "$OUT" | head -1)" = "NOTFOUND" ]; then
    bash "$CRONHUB/bin/wrap.sh" "$JOB_ID" error --error "job_id not in registry.yaml"
    exit 1
fi

# Parse line 1 (5 fields) and line 2 (script)
LINE1=$(printf '%s\n' "$OUT" | head -1)
SCRIPT=$(printf '%s\n' "$OUT" | tail -n +2)
read -r ENABLED PRIMARY FALLBACK IS_LLM ACTIVE <<< "$LINE1"

# Normalize primary: strip '-cron' suffix to compare against active scheduler
PRIMARY_AGENT="${PRIMARY%-cron}"

# Decision: disabled?
if [ "$ENABLED" != "true" ]; then
    bash "$CRONHUB/bin/wrap.sh" "$JOB_ID" skipped --note "job disabled"
    exit 0
fi

# Decision: primary mismatch?
if [ "$PRIMARY_AGENT" != "$ACTIVE" ]; then
    if [ "$FALLBACK" = "run-anyway" ]; then
        : # proceed
    else
        bash "$CRONHUB/bin/wrap.sh" "$JOB_ID" skipped --note "primary=$PRIMARY, active=$ACTIVE, fallback=skip"
        exit 0
    fi
fi

# Circuit breaker for LLM jobs
if [ "$IS_LLM" = "true" ]; then
    if ! bash "$CRONHUB/bin/breaker.sh"; then
        bash "$CRONHUB/bin/wrap.sh" "$JOB_ID" error --error "circuit breaker OPEN"
        exit 99
    fi
fi

# No script (systemEvent or empty)
if [ -z "$SCRIPT" ]; then
    bash "$CRONHUB/bin/wrap.sh" "$JOB_ID" ok --note "systemEvent (no script)"
    exit 0
fi

# Pre-exec security: tirith scan of the owner_script. We never block (fail-open
# convention matches Hermes's default) — instead, append findings to
# audits/audits.jsonl so operator-side observability catches suspicious scripts.
# Set CRONHUB_TIRITH_REQUIRE=1 to upgrade to fail-closed.
TIRITH_BIN="${TIRITH_BIN:-/mnt/data/userlib/.hermes/bin/tirith}"
if [ -x "$TIRITH_BIN" ] && [ -n "$SCRIPT" ]; then
    TIRITH_OUT=$(CRONHUB="$CRONHUB" "$TIRITH_BIN" check "$SCRIPT" 2>&1) || true
    TIRITH_RC=$?
    TIRITH_STATUS="clean"
    if echo "$TIRITH_OUT" | grep -qE 'BLOCKED|HIGH|CRITICAL'; then
        TIRITH_STATUS="flagged"
    fi
    python3 - <<PYEOF
import json, os
from datetime import datetime, timezone
rec = {
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "kind": "tirith-scan",
    "job_id": "${JOB_ID}",
    "status": "${TIRITH_STATUS}",
    "rc": ${TIRITH_RC},
    "findings": """${TIRITH_OUT}""".strip()[:1000],
}
with open("/mnt/data/cronhub/audits/audits.jsonl", "a") as f:
    f.write(json.dumps(rec) + "\n")
PYEOF
    # Honor fail-closed upgrade if explicitly requested
    if [ "${CRONHUB_TIRITH_REQUIRE:-0}" = "1" ] && [ "$TIRITH_STATUS" = "flagged" ]; then
        bash "$CRONHUB/bin/wrap.sh" "$JOB_ID" error --error "tirith BLOCKED script: $(echo "$TIRITH_OUT" | head -3 | tr '\n' ' ' | cut -c1-300)"
        exit 99
    fi
fi

# Invoke via wrap.sh using bash -c "<script>" so multi-line scripts and shell
# features (pipes, redirects) work. Script comes from registry.yaml which is
# controlled by cronhub itself.
bash "$CRONHUB/bin/wrap.sh" "$JOB_ID" bash -c "$SCRIPT"
RC=$?

# Update breaker based on outcome
if [ "$IS_LLM" = "true" ]; then
    if [ $RC -eq 0 ]; then
        bash "$CRONHUB/bin/breaker_record.sh" success "ok"
    else
        bash "$CRONHUB/bin/breaker_record.sh" failure "exit=$RC"
    fi
fi
exit $RC