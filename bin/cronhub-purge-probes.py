#!/usr/bin/env python3
"""Purge test-probe run records out of the production run history.

WHY THIS EXISTS (2026-09-28)
---------------------------
The auditor, stress, scenario and finalpass suites deliberately fire real jobs
through wrap.sh / record_run.py to prove the wiring works, then grep for the
record. Those records land in the PRODUCTION runs/YYYY-MM-DD.jsonl and were
never removed, because `rm -f $CRONHUB/runs/_audit-test.jsonl` targets a file
that never exists (runs are sharded by date).

Measured today: 124 of 610 records (20%) were probes -- 20% of every
analytics number on the dashboard was fake.

Real cronhub job ids are a UUID, or "syscron-<hex>". Anything else that looks
like a probe is removed. The script is conservative: it only removes ids
matching an explicit allowlist of probe shapes, and it reports what it kept.

Usage:
  cronhub-purge-probes.py            # dry run
  cronhub-purge-probes.py --apply    # rewrite the shards
"""
from __future__ import annotations
import json, os, re, sys, glob
from pathlib import Path

CRONHUB = Path(os.environ.get("CRONHUB", "/mnt/data/cronhub"))
RUNS = CRONHUB / "runs"

# Shapes that are definitely test scaffolding. Real ids never look like this:
#   a UUID .......................... xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#   a syscron id ................... syscron-<hex>
#   a plain openclaw/hermes id ...... <hex-ish token>
PROBE_PATTERNS = [
    re.compile(r"^_audit-test"),                 # auditor smoke probes
    re.compile(r"^test-\d+-"),                   # stress/scenario/finalpass
    re.compile(r"^concurrent-test-"),
    re.compile(r"^test-special-"),
    re.compile(r"^finalpass-"),                 # finalpass.py fixtures
    re.compile(r"^scenario-"),                  # cronhub-scenario.py fixtures
    re.compile(r"^stress-"),                    # cronhub-stress.py fixtures
    re.compile(r"^nonexistent[_-]"),             # cronhub-fire unknown-id probe
    re.compile(r"^totally-fake[_-]"),
]

REAL_SHAPES = [
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
    re.compile(r"^syscron-[0-9a-f]+$"),
]


def is_probe(job_id: str) -> bool:
    if not job_id:
        return False
    for r in REAL_SHAPES:
        if r.match(job_id):
            return False          # never touch a real id
    return any(p.match(job_id) for p in PROBE_PATTERNS)


def main() -> int:
    apply = "--apply" in sys.argv
    shards = sorted(glob.glob(str(RUNS / "*.jsonl")))
    if not shards:
        print("no run shards found")
        return 0

    removed_total = kept_total = 0
    per_shard = []
    for s in shards:
        p = Path(s)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        keep, drop, drop_ids = [], 0, {}
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                keep.append(line)          # never delete unparseable data
                continue
            jid = str(rec.get("id") or "")
            if is_probe(jid):
                drop += 1
                drop_ids[jid] = drop_ids.get(jid, 0) + 1
            else:
                keep.append(line)
        kept_total += len(keep)
        removed_total += drop
        if drop:
            per_shard.append((p.name, len(lines), len(keep), drop, drop_ids))
            if apply:
                p.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")

    print(f"mode: {'APPLY' if apply else 'DRY RUN'}")
    print(f"shards scanned: {len(shards)}")
    for name, before, after, drop, ids in per_shard:
        print(f"  {name}: {before} -> {after}  (-{drop})")
        for jid, n in sorted(ids.items(), key=lambda x: -x[1])[:6]:
            print(f"      {n:>3}x {jid}")
        if len(ids) > 6:
            print(f"      ... and {len(ids)-6} more probe ids")
    print(f"\nremoved {removed_total} probe record(s), kept {kept_total} real record(s)")
    if not apply and removed_total:
        print("re-run with --apply to rewrite the shards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
