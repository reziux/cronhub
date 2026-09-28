#!/usr/bin/env python3
"""Append a single run record to /mnt/data/cronhub/runs/YYYY-MM-DD.jsonl.
Used by wrap.sh; also callable directly for ad-hoc runs.
Usage: record_run.py <job_id> <status> [--duration N] [--scheduler X] [--exit N] [--error MSG] [--output PATH]
"""
from __future__ import annotations
import sys, json, os, time, fcntl, argparse
from pathlib import Path
from datetime import datetime, timezone

CRONHUB = Path(os.environ.get("CRONHUB", "/mnt/data/cronhub"))

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("job_id")
    p.add_argument("status", choices=["ok", "error", "started", "skipped"])
    p.add_argument("--duration", type=float)
    p.add_argument("--scheduler", default=os.environ.get("SCHEDULER", "unknown"))
    p.add_argument("--exit", type=int)
    p.add_argument("--error", default="")
    p.add_argument("--output", default="")
    p.add_argument("--note", default="")
    args = p.parse_args()
    rec = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "id": args.job_id,
        "scheduler": args.scheduler,
        "status": args.status,
    }
    if args.duration is not None:
        rec["duration_s"] = round(args.duration, 3)
    if args.exit is not None:
        rec["exit_code"] = args.exit
    if args.error:
        rec["error"] = args.error[:500]
    if args.output:
        rec["output_path"] = args.output
    if args.note:
        rec["note"] = args.note[:200]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = CRONHUB / "runs" / f"{today}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    # File lock so concurrent appends don't interleave
    lock_path = CRONHUB / "locks" / "runs.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with target.open("a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # Mirror failure to alerts sink
    if args.status == "error":
        alert_path = CRONHUB / "alerts" / "failures.jsonl"
        alert_path.parent.mkdir(parents=True, exist_ok=True)
        os.makedirs(CRONHUB / "locks", exist_ok=True); fd2 = os.open(CRONHUB / "locks" / "alerts.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX)
            with alert_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(fd2, fcntl.LOCK_UN)
            os.close(fd2)
    # Write per-job health back into registry.yaml (last_status /
    # consecutive_errors / last_run_at). Best-effort; never fails the run.
    if args.status in ("ok", "error"):
        try:
            sys.path.insert(0, str(CRONHUB / "bin"))
            import health_writeback
            health_writeback.writeback(args.job_id, args.status)
        except Exception:
            pass
    return 0

if __name__ == "__main__":
    sys.exit(main())