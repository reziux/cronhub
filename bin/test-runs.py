#!/usr/bin/env python3
"""cronhub-test-runs — exercise the top-N most-run jobs and record outcomes.

Goal: at startup or on demand, run smoke-tests against the actual
`owner_script` of the busiest jobs and record an outcome. Two modes:

  dry (default): invoke owner_script with a `--selftest` flag if it accepts
                 one; otherwise invoke it with a short timeout and capture.
                 Useful for jobs that have a "list/configure" mode.

  live:          invokes owner_script as the real scheduler would. This is
                 expensive (may post to Discord, write outputs, etc.) so
                 it's opt-in.

Top-N ranking is by run-record frequency over runs/*.jsonl. If no run
records exist, falls back to the order they appear in registry.yaml.

Writes one record per invocation into /tmp/cronhub-audit/test-runs.jsonl
AND into cronhub's alerts/audits.jsonl (so observers can see it).
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

CRONHUB = Path(os.environ.get("CRONHUB", "/mnt/data/cronhub"))
REGISTRY = CRONHUB / "registry.yaml"
RUNS_DIR = CRONHUB / "runs"
AUDITS_JSONL = CRONHUB / "audits" / "audits.jsonl"
TEST_LOG = Path("/tmp/cronhub-audit/test-runs.jsonl")

sys.path.insert(0, str(CRONHUB / "bin"))
import yaml_io  # noqa: E402


def _now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _top_n(n: int) -> list[tuple[str, int]]:
    """Return (job_id, run_count) for top n jobs by run-record frequency."""
    counter: Counter = Counter()
    if RUNS_DIR.exists():
        for f in RUNS_DIR.glob("*.jsonl"):
            for line in f.read_text().splitlines():
                try:
                    rec = json.loads(line)
                    counter[rec.get("id", "?")] += 1
                except Exception:
                    continue
    return counter.most_common(n)


def _job(job_id: str) -> dict | None:
    doc = yaml_io.read(REGISTRY)
    for j in doc["jobs"]:
        if j["id"].startswith(job_id):
            return j
    return None


def _run_owner_script(job: dict, *, dry: bool, timeout_s: int = 30) -> tuple[int, str, str]:
    """Invoke job's owner_script in selftest mode if possible.

    Heuristics (dry mode):
      - If owner_script ends in `.sh` or `.py` and contains '--selftest'
        or 'list', append that flag.
      - Otherwise timeout-bound invoke and capture.
    """
    cmd = (job.get("owner_script") or "").strip()
    if not cmd:
        # systemEvent / observer with no script
        return 0, "", "no owner_script (systemEvent)"
    # Split shell-style args. Avoid pulling in shlex complexity.
    import shlex
    tokens = shlex.split(cmd)
    if dry and job.get("is_llm_turn"):
        # Don't actually fire an LLM agent in dry mode; just record noop.
        return 0, "", "skipped (LLM call, dry mode)"
    if dry:
        # Try a small set of "list" commands depending on file extension
        ext = Path(tokens[0]).suffix if tokens else ""
        if ext in (".sh", ".py", ".js"):
            for flag in ("--selftest", "--list", "list", "--ping"):
                tokens.append(flag)
    try:
        res = subprocess.run(
            tokens,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(CRONHUB.parent),
        )
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after {timeout_s}s"
    except Exception as e:
        return 99, "", f"invocation failed: {e}"


def _record(rec: dict) -> None:
    TEST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TEST_LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    AUDITS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with AUDITS_JSONL.open("a") as f:
        f.write(json.dumps({**rec, "kind": "test-runs"}) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=30, help="top-N jobs to test")
    ap.add_argument("--live", action="store_true", help="run real owner_script")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--filter", help="substring match on job name (optional)")
    args = ap.parse_args()

    top = _top_n(args.n * 4)  # overfetch then filter
    if not top:
        # Fallback: first N jobs in registry
        doc = yaml_io.read(REGISTRY)
        top = [(j["id"], 0) for j in doc["jobs"][: args.n]]

    passed = failed = skipped = 0
    rows = []

    for job_id, count in top:
        if len(rows) >= args.n:
            break
        job = _job(job_id)
        if not job:
            continue
        if args.filter and args.filter.lower() not in (job.get("name") or "").lower():
            continue
        rc, out, err = _run_owner_script(job, dry=not args.live, timeout_s=args.timeout)
        if "skipped" in err or "no owner_script" in err:
            status = "skipped"
            skipped += 1
        elif rc == 0:
            status = "ok"
            passed += 1
        else:
            status = "error"
            failed += 1
        rec = {
            "ts": _now(),
            "id": job["id"],
            "name": job.get("name"),
            "status": status,
            "exit_code": rc,
            "hist_runs": count,
            "stdout_tail": out[-300:] if out else "",
            "stderr_tail": err[-300:] if err else "",
            "mode": "live" if args.live else "dry",
        }
        _record(rec)
        rows.append((job.get("name"), status, rc, count))

    # Compact table output
    print(f"\n{'STATUS':<8} {'NAME':<40} {'RC':<5} {'HIST_RUNS':<10}")
    for name, status, rc, count in rows:
        print(f"{status:<8} {(name or '?')[:38]:<40} {rc:<5} {count:<10}")
    print()
    print(f"passed={passed} failed={failed} skipped={skipped} total={len(rows)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
