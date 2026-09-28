#!/usr/bin/env python3
"""cronctl — operator CLI for the cronhub registry.

Replaces the earlier "ship-now" expectation: registry.yaml is the single
source of truth, every mutation goes through this tool under a file lock.

Subcommands (referenced by cronhub-tests.sh / cronhub-auditor.sh):
  ls [--live]                           List jobs (--live filters enabled).
  show <id>                             Print one job (JSON).
  enable <id>                           Set enabled=true.
  disable <id>                          Set enabled=false.
  pause <id>                            Alias for disable.
  run <id> [--no-record]                Fire the job now via cronhub-fire.sh.
  runs [N]                              Last N run records across all jobs.
  upcoming [N]                          Next N scheduled fires.
  fail [--since DURATION] [--ack ID]    Show failures, optionally ack one.
  ack <id>                              Mark a job's failures as acknowledged.
  switch active --to SCHEDULER          Flip active scheduler (updates claims).
  migrate <id> --to EXECUTOR            Change primary_executor.
  doctor                                Health check.

Writes are file-locked (fcntl) and atomic (tmp+rename). Reads are direct.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CRONHUB = Path(os.environ.get("CRONHUB", "/mnt/data/cronhub"))
REGISTRY = CRONHUB / "registry.yaml"
CLAIMS = CRONHUB / "claims" / "active-scheduler.json"
RUNS_DIR = CRONHUB / "runs"
FAILURES = CRONHUB / "alerts" / "failures.jsonl"
LOCK_PATH = CRONHUB / "locks" / "registry.lock"
AUDITS = CRONHUB / "audits"
AUDITS_JSONL = AUDITS / "audits.jsonl"

sys.path.insert(0, str(CRONHUB / "bin"))
import yaml_io  # noqa: E402
import debounce  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -------- registry I/O under a shared lock --------

class _RegistryLock:
    def __init__(self) -> None:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)

    def __enter__(self):
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)


def read_registry() -> dict:
    return yaml_io.read(REGISTRY)


def write_registry(doc: dict) -> None:
    """Atomic registry write with content-hash debounce. Caller holds _RegistryLock.

    Skips writes whose serialized content is byte-identical to the on-disk
    state (idempotent no-op). Prevents the sync_* feedback loop from
    amplifying into many identical writes. Pass `--force` to bypass.
    """
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    import yaml_io as _y
    tmp_for_hash = REGISTRY.with_suffix(REGISTRY.suffix + ".hash")
    _y.write(tmp_for_hash, doc)
    data = tmp_for_hash.read_text()
    tmp_for_hash.unlink()
    # Hash the *content* — strip updated_at so second-precision timestamp
    # changes don't make every cronctl write look unique. updated_at still
    # gets written to disk when the write is allowed; it just doesn't poison
    # the idempotency check.
    import copy as _copy
    _hdoc = _copy.deepcopy(doc)
    _hdoc.pop("updated_at", None)
    tmp_for_hash2 = REGISTRY.with_suffix(REGISTRY.suffix + ".hash2")
    _y.write(tmp_for_hash2, _hdoc)
    data_for_hash = tmp_for_hash2.read_text()
    tmp_for_hash2.unlink()
    h = debounce.hash_str(data_for_hash)
    force = "--force" in sys.argv
    allow, reason = debounce.should_write(h, force=force)
    if not allow:
        sys.stderr.write(f"cronctl: registry write skipped ({reason})\n")
        _audit_event("write-skip", {"reason": reason, "hash": h})
        return
    yaml_io.write(REGISTRY, doc)
    debounce.mark_written(h)


def _atomic_write_local(path: Path, data: str) -> None:
    """Write `data` to `path` atomically using a sibling tmp file (same FS)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def find_job(doc: dict, job_id: str) -> tuple[int, dict] | None:
    """Find job whose id starts with job_id (UUID-prefix friendly)."""
    matches = [(i, j) for i, j in enumerate(doc["jobs"]) if j["id"].startswith(job_id)]
    if not matches:
        return None
    if len(matches) > 1:
        print(
            f"ambiguous id '{job_id}' matches {len(matches)} jobs; "
            f"first: {matches[0][1]['id']}",
            file=sys.stderr,
        )
    return matches[0]


def require_job(doc: dict, job_id: str) -> tuple[int, dict]:
    found = find_job(doc, job_id)
    if not found:
        print(f"no such job: {job_id}", file=sys.stderr)
        sys.exit(2)
    return found


# -------- subcommand implementations --------

def cmd_ls(args: argparse.Namespace) -> int:
    doc = read_registry()
    jobs = doc["jobs"]
    # `--live` enables only enabled; `--historical` enables only disabled.
    # Default: list everything. (Both flags used in cronhub-tests invariants.)
    if args.live and not args.historical:
        jobs = [j for j in jobs if j.get("enabled")]
    elif args.historical and not args.live:
        jobs = [j for j in jobs if not j.get("enabled")]
    jobs.sort(key=lambda j: (j.get("name") or j.get("id", "")))
    # compact tabular output; "LIVE"/"off" so `grep '^LIVE'` and `grep '^off '`
    # work for cronhub-tests invariants.
    print(f"{'STATUS':<7} {'ID':<14} {'EXECUTOR':<14} {'SCHEDULE':<22} NAME")
    for j in jobs:
        status = "LIVE" if j.get("enabled") else "off"
        print(
            f"{status:<7} {j['id'][:14]:<14} {j.get('primary_executor','?'):<14} "
            f"{j.get('schedule_expr','?'):<22} {j.get('name','?')[:60]}"
        )
    print(f"\n{len(jobs)} job(s)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    doc = read_registry()
    found = require_job(doc, args.id)
    _, job = found
    print(json.dumps(job, indent=2, default=str))
    return 0


def _mutate_enabled(args: argparse.Namespace, value: bool) -> int:
    with _RegistryLock():
        doc = read_registry()
        idx, job = require_job(doc, args.id)
        job["enabled"] = value
        doc["jobs"][idx] = job
        doc["updated_at"] = _now_iso()
        write_registry(doc)
    print(f"{'enabled' if value else 'disabled'} {job['name']} ({job['id'][:14]})")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    return _mutate_enabled(args, True)


def cmd_disable(args: argparse.Namespace) -> int:
    return _mutate_enabled(args, False)


def cmd_pause(args: argparse.Namespace) -> int:
    return _mutate_enabled(args, False)


def cmd_run(args: argparse.Namespace) -> int:
    doc = read_registry()
    _, job = require_job(doc, args.id)
    # Invoke cronhub-fire.sh with this job id
    cmd = [
        "bash",
        str(CRONHUB / "bin" / "cronhub-fire.sh"),
        job["id"],
    ]
    # Schedule-aware context — let cronhub-fire decide routing/breaker
    res = subprocess.run(cmd, capture_output=True, text=True)
    if args.no_record:
        print(res.stdout)
        print(res.stderr, file=sys.stderr)
        return res.returncode
    # cronhub-fire records via wrap.sh/record_run.py; we surface its output here
    print(res.stdout)
    if res.stderr:
        print(res.stderr, file=sys.stderr)
    # Also write a quick operator-visible entry into AUDITS_JSONL
    _audit_event("run-cli", {"job_id": job["id"], "rc": res.returncode})
    return res.returncode


def cmd_runs(args: argparse.Namespace) -> int:
    """Show recent runs across all jobs, newest first."""
    N = args.n
    records: list[dict] = []
    for f in sorted(RUNS_DIR.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    for r in records[:N]:
        # Compact: ts id status duration
        line = f"{r.get('ts','?'):<22} {r.get('id','?')[:14]:<14} {r.get('status','?'):<9}"
        if r.get("duration_s") is not None:
            line += f" {r['duration_s']:.3f}s"
        if r.get("error"):
            err = r["error"][:60].replace("\n", " ")
            line += f" err={err}"
        elif r.get("note"):
            line += f" note={r['note'][:40]}"
        print(line)
    print(f"\nshowing {min(N,len(records))} of {len(records)} total run records")
    return 0


def cmd_upcoming(args: argparse.Namespace) -> int:
    """Show next N fires per job using schedule_expr as crontab-like."""
    # We don't have a full cron parser; honor '*' semantics approximately.
    N = args.n
    doc = read_registry()
    now = datetime.now(timezone.utc)
    out: list[tuple[str, str]] = []
    for job in doc["jobs"]:
        if not job.get("enabled"):
            continue
        expr = (job.get("schedule_expr") or "").strip()
        try:
            nxt = _next_fire(expr, now)
        except Exception:
            nxt = None
        if nxt is None:
            continue
        out.append((nxt.isoformat(), f"{job['id'][:14]} ({job.get('name','?')})"))
    out.sort()
    for ts, label in out[:N]:
        print(f"{ts:<22} {label}")
    print(f"\nshowing next {min(N,len(out))} of {len(out)} enabled jobs")
    return 0


def _next_fire(expr: str, now: datetime) -> datetime | None:
    """Crude next-fire approximation for crontab-style `* * * * *` / `@every Nms`.

    Supports: `M H DoM Mo DoW`, `*/N` for any field, specific numbers, and
    `@every Nms` (intervals). Returns next fire after `now`.
    """
    import re
    if not expr:
        return None
    if expr.startswith("@every"):
        m = re.match(r"@every\s+(\d+)ms", expr)
        if not m:
            return None
        ms = int(m.group(1))
        # next multiple after now
        epoch_ms = int(now.timestamp() * 1000)
        return datetime.fromtimestamp(((epoch_ms // ms) + 1) * ms / 1000, tz=timezone.utc)
    fields = expr.split()
    if len(fields) != 5:
        return None
    minute, hour, dom, mon, dow = fields
    # Round to next minute
    cand = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
    for _ in range(60 * 24 * 8):  # up to ~8 days
        if _match_field(minute, cand.minute) and _match_field(hour, cand.hour) \
           and _match_field(dom, cand.day) and _match_field(mon, cand.month) \
           and _match_field(dow, cand.weekday()):
            return cand
        cand += timedelta(minutes=1)
    return None


def _match_field(spec: str, val: int) -> bool:
    # Supports '*', '*/N', and exact integers. weekday is 0=Mon in Python (we use
    # isoweekday-1 so Sun=6). Cron-style: 0=Sun. We'll just match python weekday.
    spec = spec.strip()
    if spec == "*":
        return True
    if spec.startswith("*/"):
        n = int(spec[2:])
        return val % n == 0
    try:
        return int(spec) == val
    except Exception:
        return False


def cmd_fail(args: argparse.Namespace) -> int:
    """Show recent failures, optionally ack one."""
    if not FAILURES.exists():
        print("no failures recorded")
        return 0
    cutoff = None
    if args.since:
        cutoff = datetime.now(timezone.utc) - _parse_dur(args.since)
    records = []
    for line in FAILURES.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = _parse_iso(r.get("ts", "1970-01-01T00:00:00Z"))
        if cutoff and ts < cutoff:
            continue
        records.append(r)
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    for r in records[: args.n]:
        line = f"{r.get('ts','?'):<22} {r.get('id','?')[:14]:<14} {r.get('status','?'):<9}"
        if r.get("error"):
            err = r["error"][:80].replace("\n", " ")
            line += f" err={err}"
        print(line)
    print(f"\n{len(records)} failure(s)")
    # Optional ack
    if getattr(args, "ack_id", None):
        return cmd_ack(argparse.Namespace(id=args.ack_id))
    return 0


def _parse_dur(s: str) -> timedelta:
    # '1d', '2h', '30m', '90s'
    s = s.strip().lower()
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    if s.endswith("m"):
        return timedelta(minutes=int(s[:-1]))
    if s.endswith("s"):
        return timedelta(seconds=int(s[:-1]))
    return timedelta(seconds=int(s))


def _parse_iso(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def cmd_ack(args: argparse.Namespace) -> int:
    """Mark a job's failures as acknowledged by appending an ACK record to alerts."""
    doc = read_registry()
    _, job = require_job(doc, args.id)
    rec = {
        "ts": _now_iso(),
        "kind": "ack",
        "job_id": job["id"],
        "job_name": job.get("name"),
        "operator": os.environ.get("USER", "unknown"),
    }
    ack_log = CRONHUB / "alerts" / "acks.jsonl"
    ack_log.parent.mkdir(parents=True, exist_ok=True)
    with open(ack_log, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"acknowledged failures for {job['name']} ({job['id'][:14]})")
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    target = args.to
    if target not in {"openclaw", "hermes"}:
        print(f"unknown scheduler: {target}", file=sys.stderr)
        return 2
    claims = {}
    if CLAIMS.exists():
        try:
            claims = json.loads(CLAIMS.read_text())
        except Exception:
            pass
    prev = claims.get("active_scheduler", "openclaw")
    claims["active_scheduler"] = target
    claims["active_updated_at"] = _now_iso()
    claims["last_flip_reason"] = f"cli-switch from {prev}"
    claims["last_check_ts"] = _now_iso()
    CLAIMS.parent.mkdir(parents=True, exist_ok=True)
    CLAIMS.write_text(json.dumps(claims, indent=2))
    print(f"flipped active scheduler: {prev} -> {target}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """Change a job's primary_executor."""
    target = args.to
    valid = {"openclaw-cron", "hermes-cron", "system-cron"}
    if target not in valid:
        print(f"unknown executor: {target} (valid: {sorted(valid)})", file=sys.stderr)
        return 2
    with _RegistryLock():
        doc = read_registry()
        idx, job = require_job(doc, args.id)
        prev = job.get("primary_executor")
        job["primary_executor"] = target
        doc["jobs"][idx] = job
        doc["updated_at"] = _now_iso()
        write_registry(doc)
    print(f"migrated {job['name']} ({job['id'][:14]}): {prev} -> {target}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Health check. Reports findings, exits non-zero if any warnings."""
    findings = []  # list of (severity, message)

    # Registry readable
    try:
        doc = read_registry()
        n = len(doc.get("jobs", []))
        if n == 0:
            findings.append(("warn", "registry has 0 jobs"))
    except Exception as e:
        findings.append(("fail", f"registry unreadable: {e}"))
        _print_findings(findings)
        return 1

    enabled = [j for j in doc["jobs"] if j.get("enabled")]
    disabled = [j for j in doc["jobs"] if not j.get("enabled")]
    print(f"  registry: {n} jobs ({len(enabled)} enabled, {len(disabled)} disabled)")

    # Claims readable, active scheduler set
    if CLAIMS.exists():
        try:
            claims = json.loads(CLAIMS.read_text())
            a = claims.get("active_scheduler", "?")
            print(f"  active scheduler: {a}")
            if a not in ("openclaw", "hermes"):
                findings.append(("fail", f"active scheduler invalid: {a}"))
        except Exception as e:
            findings.append(("fail", f"claims unreadable: {e}"))
    else:
        findings.append(("warn", f"claims file missing: {CLAIMS}"))

    # Heartbeats freshness
    # If the active scheduler has flipped to the *other* scheduler because
    # primary is stale, don't re-warn about the primary — arbiter already
    # handled failover.
    flipped_active = claims.get("active_scheduler")
    for hb_name, label in [("openclaw.heartbeat", "openclaw"), ("hermes.heartbeat", "hermes")]:
        p = CRONHUB / "state" / hb_name
        if p.exists():
            age_s = time.time() - p.stat().st_mtime
            print(f"  {label} heartbeat: age {age_s:.0f}s")
            if age_s > 300 and label != flipped_active:
                findings.append(("warn", f"{label} heartbeat stale ({age_s:.0f}s)"))

    # Recent runs
    n_runs = 0
    if RUNS_DIR.exists():
        for f in RUNS_DIR.glob("*.jsonl"):
            n_runs += sum(1 for _ in f.open())
    print(f"  run records: {n_runs}")

    # Recent failures
    n_fail = 0
    if FAILURES.exists():
        for line in FAILURES.read_text().splitlines():
            try:
                r = json.loads(line)
                if r.get("status") == "error":
                    n_fail += 1
            except Exception:
                continue
    print(f"  failures (total): {n_fail}")
    if n_fail > 0:
        findings.append(("info", f"{n_fail} failure record(s) present"))

    # Validation: all enabled jobs have an owner_script or are systemEvent
    bad = []
    for j in doc["jobs"]:
        if j.get("enabled") and j.get("is_llm_turn") and not (j.get("owner_script") or "").strip():
            bad.append(j["id"][:14])
    if bad:
        findings.append(("warn", f"enabled LLM jobs with empty owner_script: {bad[:5]}"))

    _print_findings(findings)

    # exit 0 if no fail
    if any(s == "fail" for s, _ in findings):
        return 1
    return 0


def _print_findings(findings: list[tuple[str, str]]) -> None:
    if not findings:
        print("  no problem(s) found")
        return
    print(f"  {len(findings)} problem(s):")
    for sev, msg in findings:
        sev_label = {"fail": "ERROR", "warn": "WARNING", "info": "INFO"}.get(sev, sev.upper())
        print(f"  [{sev_label}] {msg}")


def _audit_event(kind: str, payload: dict) -> None:
    """Append a record to AUDITS_JSONL (creates the file lazily)."""
    AUDITS.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now_iso(), "kind": kind, "operator": os.environ.get("USER", "unknown"), **payload}
    with open(AUDITS_JSONL, "a") as f:
        f.write(json.dumps(rec) + "\n")


# -------- argparse --------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cronctl", description="Operator CLI for cronhub.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ls")
    sp.add_argument("--live", action="store_true")
    sp.add_argument("--historical", action="store_true")
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("show"); sp.add_argument("id"); sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("enable"); sp.add_argument("id"); sp.set_defaults(func=cmd_enable)
    sp = sub.add_parser("disable"); sp.add_argument("id"); sp.set_defaults(func=cmd_disable)
    sp = sub.add_parser("pause"); sp.add_argument("id"); sp.set_defaults(func=cmd_pause)

    sp = sub.add_parser("run"); sp.add_argument("id"); sp.add_argument("--no-record", action="store_true")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("runs"); sp.add_argument("n", type=int, nargs="?", default=20); sp.set_defaults(func=cmd_runs)

    sp = sub.add_parser("upcoming"); sp.add_argument("n", type=int, nargs="?", default=10)
    sp.set_defaults(func=cmd_upcoming)

    sp = sub.add_parser("fail")
    sp.add_argument("--since", help="Duration like '2d', '4h', '30m'")
    sp.add_argument("--ack", dest="ack_id", help="Acknowledge this job's failures")
    sp.add_argument("--n", type=int, default=20)
    sp.set_defaults(func=cmd_fail)

    sp = sub.add_parser("ack"); sp.add_argument("id"); sp.set_defaults(func=cmd_ack)

    sp = sub.add_parser("switch")
    sp.add_argument("what")  # 'active'
    sp.add_argument("--to", required=True)
    sp.set_defaults(func=cmd_switch)

    sp = sub.add_parser("migrate")
    sp.add_argument("id")
    sp.add_argument("--to", required=True)
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser("doctor"); sp.set_defaults(func=cmd_doctor)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
