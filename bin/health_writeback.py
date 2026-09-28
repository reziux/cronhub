#!/usr/bin/env python3
"""Write per-job health back into registry.yaml after each terminal run.

The registry is the source of truth for job *definitions*. This module turns
it into a source of truth for job *health* by updating three fields that were
previously dead (always 0 / None / absent):

    last_status         "ok" | "error"   last terminal outcome
    consecutive_errors  int              error streak, reset to 0 on "ok"
    last_run_at         ISO8601          last terminal run timestamp

Semantics
---------
- Only terminal statuses ("ok", "error") write back. "started" and "skipped"
  are lifecycle noise and must not perturb the error streak.
- Skips the write when the health fields are unchanged AND last_run_at is
  less than MIN_WRITE_INTERVAL seconds old. This suppresses the double-write
  you get from back-to-back ok/error pairs within the same second, without
  suppressing a genuine status transition.
- Best-effort: every failure is swallowed. Health telemetry must never be
  able to fail a job run.

Concurrency
-----------
Shares locks/registry.lock with cronctl.py, so write-back serialises against
operator CLI writes. Full read-modify-write under an exclusive lock.
"""
from __future__ import annotations
import os
import sys
import fcntl
import traceback
from datetime import datetime, timezone
from pathlib import Path

CRONHUB = Path(os.environ.get("CRONHUB", "/mnt/data/cronhub"))
BIN = CRONHUB / "bin"
REGISTRY = CRONHUB / "registry.yaml"
LOCK_PATH = CRONHUB / "locks" / "registry.lock"

TERMINAL_OK = "ok"
TERMINAL_ERROR = "error"

# Don't rewrite the registry more often than this per job.
MIN_WRITE_INTERVAL = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def writeback(job_id: str, status: str) -> bool:
    """Update health fields for `job_id`. True if the registry was rewritten."""
    if status not in (TERMINAL_OK, TERMINAL_ERROR):
        return False

    fd = None
    try:
        sys.path.insert(0, str(BIN))
        import yaml_io

        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)

        if not REGISTRY.exists():
            return False
        doc = yaml_io.read(REGISTRY)

        target = None
        for j in doc.get("jobs") or []:
            jid = j.get("id", "")
            if jid == job_id or (job_id and jid.startswith(job_id)):
                target = j
                break
        if target is None:
            return False

        # ---- compute -------------------------------------------------
        try:
            prev_errors = int(target.get("consecutive_errors") or 0)
        except Exception:
            prev_errors = 0
        new_errors = 0 if status == TERMINAL_OK else prev_errors + 1
        now = _now()
        new_run_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        health_changed = (
            target.get("last_status") != status
            or target.get("consecutive_errors") != new_errors
        )
        last_at = _parse_iso(target.get("last_run_at"))
        fresh_enough = last_at is not None and (now - last_at).total_seconds() < MIN_WRITE_INTERVAL

        if not health_changed and fresh_enough:
            return False  # nothing meaningful to persist

        # ---- mutate --------------------------------------------------
        target["last_status"] = status
        target["consecutive_errors"] = new_errors
        target["last_run_at"] = new_run_at
        doc["updated_at"] = new_run_at

        yaml_io.write(REGISTRY, doc)
        return True
    except Exception:
        try:
            sys.stderr.write(
                "health_writeback: swallowed error for %s/%s\n%s\n"
                % (job_id, status, traceback.format_exc())
            )
        except Exception:
            pass
        return False
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.stderr.write("usage: health_writeback.py <job_id> <ok|error>\n")
        sys.exit(2)
    print("wrote=%s" % writeback(sys.argv[1], sys.argv[2]))
