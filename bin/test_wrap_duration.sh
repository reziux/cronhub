#!/usr/bin/env bash
# Regression test: wrap.sh must actually write its ok/error run-record.
#
# Bug (2026-10-04): `awk -v s=… -e=…` — `-e` is awk's program-text flag, so
# mawk printed a usage error and emitted empty stdout. DURATION="" then made
# record_run.py reject `--duration ''`, so the terminal row was never written.
# Jobs kept running correctly; only the record vanished. Silent for 13h.
#
# Fully sandboxed: CRONHUB points at a temp dir, so nothing here reads the real
# registry or appends to the real runs/*.jsonl. bin/ is symlinked in because
# wrap.sh invokes "$CRONHUB/bin/record_run.py" — this keeps the live recorder
# under test while confining every write to $SANDBOX.
#
# Usage: bash test_wrap_duration.sh    [WRAP=/path/to/other/wrap.sh]

set -u
WRAP="${WRAP:-/mnt/data/cronhub/bin/wrap.sh}"
BIN="${BIN:-$(cd "$(dirname "$WRAP")" && pwd)}"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
mkdir -p "$SANDBOX"/{runs,audits,locks,alerts}
ln -s "$BIN" "$SANDBOX/bin"

JOB=$(date -u +%Y-%m-%d).jsonl
: > "$SANDBOX/runs/$JOB"

run() { CRONHUB="$SANDBOX" SCHEDULER=hermes bash "$WRAP" "$@" >/dev/null 2>&1; }

run wrap-ok  bash -c 'echo hi; exit 0'
run wrap-err bash -c 'echo boom >&2; exit 7'
run wrap-rc  bash -c 'exit 7'; RC=$?

python3 - "$SANDBOX/runs/$JOB" "$RC" <<'PY'
import collections, json, sys

rows = [json.loads(l) for l in open(sys.argv[1])]
by_id = collections.defaultdict(list)
for r in rows:
    by_id[r["id"]].append(r)
rc = int(sys.argv[2])

def row(job, status):
    return next((r for r in by_id.get(job, []) if r.get("status") == status), None)

results = []
def check(label, ok, detail=""):
    results.append((label, bool(ok), detail))

ok = row("wrap-ok", "ok")
check("success writes an ok row", ok is not None)
check("ok row carries a float duration_s", ok and isinstance(ok.get("duration_s"), (int, float)),
      f"got {ok and ok.get('duration_s')!r}")

err = row("wrap-err", "error")
check("failure writes an error row", err is not None)
check("error row has exit_code 7", err and err.get("exit_code") == 7, f"got {err and err.get('exit_code')!r}")
check("error row carries the message", err and "boom" in (err.get("error") or ""))
check("exit code propagates to the caller", rc == 7, f"got {rc}")

# The regression itself: a started row with no terminal counterpart. This is
# exactly what production looked like for 13h — jobs fine, records absent.
orphans = {j: len(v) for j, v in by_id.items()
           if j.startswith("wrap-")
           and sum(1 for r in v if r["status"] == "started")
               > sum(1 for r in v if r["status"] in ("ok", "error"))}
check("no started-without-terminal rows", not orphans, f"orphans={sorted(orphans)}")

failed = 0
for label, good, detail in results:
    print(f"  {'ok  ' if good else 'FAIL'} {label}" + (f"  ({detail})" if detail and not good else ""))
    failed += not good
print(f"\npassed: {len(results) - failed}  failed: {failed}")
sys.exit(1 if failed else 0)
PY
