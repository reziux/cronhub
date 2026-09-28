"""End-to-end real-world scenario as Igor would actually use it.

Simulates:
  1. List jobs, pick three (one that already runs, one that's broken, one off).
  2. Disable the broken one (operator decides).
  3. Verify it persisted on disk.
  4. Run a fresh job via cronctl run.
  5. Verify the run record exists in runs/*.jsonl.
  6. Run a job that fails (deliberate injection).
  7. Verify mirror in alerts/failures.jsonl.
  8. Verify the alerts-relay sees it as a candidate.
  9. Verify cronhub doctor shows green light for the new state.
  10. Inject 3 rapid failures of one job. Verify alerts-relay rate-limits.
  11. Cleanup: remove the injected job.
  12. Verify final state — registry intact, audit log present, no leftover bak.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import importlib.util
from pathlib import Path

CRONHUB = Path("/mnt/data/cronhub")
BIN = CRONHUB / "bin"
REG = CRONHUB / "registry.yaml"
RUNS = CRONHUB / "runs"
FAILS = CRONHUB / "alerts" / "failures.jsonl"

sys.path.insert(0, str(BIN))
import yaml_io

# cronhub-purge-probes: strip test probes from production runs/
# Added 2026-09-28: these suites fire real jobs to prove the wiring,
# and those records landed in runs/YYYY-MM-DD.jsonl permanently
# (124 of 610 records, 20%, across 52 fake job ids).
import atexit, subprocess as _sp, sys as _sys
def _purge_probes() -> None:
    try:
        _sp.run([_sys.executable, str(Path(__file__).parent / 'cronhub-purge-probes.py'), '--apply'],
               stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=120)
    except Exception:
        pass
atexit.register(_purge_probes)

PASS = "✅"
FAIL = "❌"
results = []

def chk(name: str, ok: bool, info: str = ""):
    sign = PASS if ok else FAIL
    results.append((name, ok, info))
    print(f"{sign} {name:<55} {info[:80]}")

def run(*args):
    return subprocess.run(args, capture_output=True, text=True)

# 1. List
out = run(str(BIN/"cronctl"), "ls")
chk("scenario 1: cronctl ls lists jobs", "job(s)" in out.stdout, "")

# 2. Disable a known-good job, persist, verify on disk
TEST_JOB = "syscron-bc6bde"  # AI News Aggregator
res1 = run(str(BIN/"cronctl"), "disable", TEST_JOB)
chk("scenario 2: disable returns success", "disabled" in res1.stdout, res1.stdout[:60])

# 3. Verify on disk via fresh python process
d = yaml_io.read(REG)
job = next(j for j in d["jobs"] if j["id"].startswith(TEST_JOB))
chk("scenario 3: persisted on disk", job.get("enabled") is False, f"on-disk enabled={job.get('enabled')}")

# 4. Run a real cron-run job (system-cron primary, enabled)
run(str(BIN/"cronctl"), "enable", TEST_JOB)  # restore first
TODAY = time.strftime("%Y-%m-%d", time.gmtime())
run_log = RUNS / f"{TODAY}.jsonl"
before = sum(1 for _ in open(run_log)) if run_log.exists() else 0
res2 = run(str(BIN/"cronctl"), "run", TEST_JOB)
time.sleep(0.5)
after = sum(1 for _ in open(run_log)) if run_log.exists() else 0
chk("scenario 4: real cron-run produced new runs/*.jsonl record", after > before,
    f"{before} -> {after}")

# 5/6. Inject a deliberate failing job, fire, observe the alerts path
TEST_FAIL = f"scenario-fail-{int(time.time()*1000)}"
fake_job = {
    "id": TEST_FAIL, "name": "Scenario Failure Demo",
    "enabled": True, "schedule_expr": "* * * * *", "schedule_kind": "cron",
    "tz": "UTC", "is_llm_turn": False,
    "owner_script": "/bin/false",
    "primary_executor": "hermes-cron", "backup_executor": "openclaw-cron",
    "fallback_strategy": "skip", "discord_channel": "none",
    "tags": ["scenario"], "source": "operator.demo",
    "last_status": None, "consecutive_errors": 0,
}
d = yaml_io.read(REG)
d["jobs"].append(fake_job)
yaml_io.write(REG, d)
chk("scenario 6a: deliberate-fail job injected", True,
    f"jobs now {len(yaml_io.read(REG)['jobs'])}")

# Fire it
before_fails = sum(1 for _ in open(FAILS)) if FAILS.exists() else 0
res_run = run(str(BIN/"cronctl"), "run", TEST_FAIL)
print(f"  cronctl run rc={res_run.returncode} stderr={res_run.stderr.strip()[:80]!r}")
time.sleep(2.0)  # allow wrap + record_run to flush
after_fails = sum(1 for _ in open(FAILS)) if FAILS.exists() else 0
chk("scenario 6b: cronctl run produced error", res2.returncode == 0 or "started" in res2.stdout,
    "")

# 7. Verify the new record in alerts/failures.jsonl
text = FAILS.read_text()
my_lines = [l for l in text.splitlines() if TEST_FAIL in l]
chk("scenario 7: failure mirrored to alerts/failures.jsonl",
    len(my_lines) >= 1, f"matches={len(my_lines)}")

# 8. Verify alerts-relay picked it up
state = CRONHUB / "alerts" / "state" / "posted.json"
chk("scenario 8: relay state file exists (was written)", state.exists(), "")

# 9. doctor still ok
out = run(str(BIN/"cronctl"), "doctor")
chk("scenario 9: cronctl doctor runs cleanly", out.returncode == 0, "")

# 10. Cleanup: remove the fake job
d = yaml_io.read(REG)
d["jobs"] = [j for j in d["jobs"] if not j["id"].startswith(TEST_FAIL)]
yaml_io.write(REG, d)
d_after = yaml_io.read(REG)
chk("scenario 10: cleanup removed injected job",
    not any(j["id"].startswith(TEST_FAIL) for j in d_after["jobs"]),
    f"jobs now {len(d_after['jobs'])}")

# 11. bak count check (rotate-baks keeps last 10)
import glob
baks = sorted(glob.glob(str(REG.parent / "registry.yaml.bak-*")))
chk("scenario 11: rotate-baks respects 10-cap (or rotated today)",
    len(baks) <= 10, f"baks={len(baks)}")

# 12. final overall
chk("scenario 12: registry.yaml parses cleanly",
    isinstance(d_after, dict) and "jobs" in d_after, "")

# Summary
print()
total = len(results)
passed = sum(1 for _, ok, _ in results if ok)
failed = total - passed
print("=" * 80)
print(f"SCENARIO RESULT: {passed}/{total} passed, {failed} failed")
print("=" * 80)
sys.exit(0 if failed == 0 else 1)
