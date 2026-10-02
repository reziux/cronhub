#!/usr/bin/env python3
"""One-shot repair of the cronhub registry.yaml (WORKTREE copy only).

Usage:
  registry_repair.py                     # dry run (default), writes nothing
  registry_repair.py --dry-run           # same, explicit
  registry_repair.py --apply mechanical --yes
  registry_repair.py --apply ghosts --yes

Passes (gated -- ONLY the one named by --apply runs):
  mechanical  strip '' apostrophe artifacts from string values; the string
              "None" -> real null for discord_channel; REPORT (never rewrite)
              invalid schedule_expr values.
  ghosts      set enabled=False on enabled jobs with an empty owner_script.
              NOT run by this task. A later task owns it.

Phantoms (orphan keys from values split on an embedded ": ") are NOT handled
here and are NOT touched: a job carrying any non-canonical key is skipped
whole and reported. Reconstructing them needs human-approved alert text.

Safety properties:
  * default mode is a dry run; it never writes
  * --apply requires --yes
  * --apply snapshots registry.yaml -> registry.yaml.pre-repair-<UTC ts>
  * aborts non-zero if the job count would change
  * aborts non-zero if any strip would empty a value
  * aborts loudly if scan_problems() returns the line-number-0 sentinel
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml_io  # noqa: E402

# NEVER point this at /mnt/data/cronhub (live production). Override only
# deliberately, with CRONHUB_ROOT.
ROOT = Path(os.environ.get("CRONHUB_ROOT", "/mnt/data/cronhub-work"))
REGISTRY = ROOT / "registry.yaml"
DISABLED = ROOT / "disabled-jobs.yaml"

# C1: the canonical key set. Anything outside it is a phantom key produced by
# a value split on an embedded ": ". This must NOT be is_identifier() --
# `Format` matches ^[A-Za-z_][A-Za-z0-9_]*$ and is still corruption.
CANON = {
    "id", "name", "enabled", "schedule_expr", "schedule_kind", "tz",
    "is_llm_turn", "owner_script", "primary_executor", "backup_executor",
    "fallback_strategy", "discord_channel", "tags", "source",
    "last_status", "consecutive_errors", "description",
    "log_jobname_hint", "log_file", "watches", "last_run_at",
}

APOS_RUN = re.compile(r"^'{2,}|'{2,}$")
CRON_FIELD = re.compile(r"^[*0-9/,-]+$")
DOW = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
MON = {"jan", "feb", "mar", "apr", "may", "jun",
       "jul", "aug", "sep", "oct", "nov", "dec"}

changes: list[dict] = []
sched_reports: list[dict] = []
skipped_jobs: list[tuple] = []


def note(job_id, field, before, after, kind):
    changes.append({"id": job_id, "field": field, "kind": kind,
                    "before": before, "after": after})


def valid_schedule(expr) -> bool:
    """Concrete validity rule: `@every <digits>ms`, or exactly 5 fields, each
    matching ^[*0-9/,-]+$ or a three-letter month/day name."""
    if not expr:
        return True
    s = str(expr).strip()
    if re.fullmatch(r"@every\s+\d+ms", s):
        return True
    parts = s.split()
    if len(parts) != 5:
        return False
    for i, p in enumerate(parts):
        low = p.lower()
        if CRON_FIELD.match(p):
            continue
        if i == 3 and low in MON:
            continue
        if i == 4 and (low in DOW or low in MON):
            continue
        return False
    return True


def strip_apos(s: str) -> str:
    return APOS_RUN.sub("", s)


def phantom_keys(job: dict) -> list:
    return [k for k in job if k not in CANON]


def pass_mechanical(doc: dict) -> dict:
    """A job carrying a phantom key is skipped WHOLE and reported.

    Rationale: that job is a malformed record awaiting human-approved
    reconstruction. Partially mutating it would leave a hybrid record that is
    neither the corruption nor the repair.
    """
    # Top-level scalars carry the same apostrophe artifacts. "every string
    # value" includes these; the plan's original script missed them.
    for k, v in list(doc.items()):
        if k == "jobs" or not isinstance(v, str):
            continue
        new = strip_apos(v)
        if new != v:
            note("<top>", k, v, new, "apos-artifact")
            doc[k] = new

    for job in doc.get("jobs", []):
        if not isinstance(job, dict):
            continue
        jid = str(job.get("id", "?"))[:8]
        ph = phantom_keys(job)
        if ph:
            skipped_jobs.append((jid, ph))
            continue
        for key, val in list(job.items()):
            if key not in CANON:
                continue
            if isinstance(val, str):
                new = strip_apos(val)
                if new != val:
                    note(jid, key, val, new, "apos-artifact")
                    job[key] = new
                if key == "discord_channel" and val == "None":
                    note(jid, key, val, None, "stringified-none")
                    job[key] = None
        sched = job.get("schedule_expr")
        if isinstance(sched, str) and not valid_schedule(sched):
            # REPORT ONLY. A wrong schedule fires a job at the wrong time.
            sched_reports.append((jid, sched))
    return doc


def pass_ghosts(doc: dict) -> tuple[dict, list[dict]]:
    """GATED PASS -- not run by this task. A later task owns it.

    Verified but deliberately NOT executed here: 18 jobs carry a list-valued
    owner_script, all 18 empty, 9 of them enabled -> 9 ghost candidates,
    matching the plan's Task 5 count. The predicate is sound; this task just
    does not run it (AC6: ghost jobs are not touched).
    """
    ghosts = []
    for job in doc.get("jobs", []):
        if not isinstance(job, dict):
            continue
        if phantom_keys(job):
            continue
        owner = job.get("owner_script")
        if job.get("enabled") and isinstance(owner, list) and not owner:
            ghosts.append(job)
            note(str(job.get("id", "?"))[:8], "enabled", True, False, "ghost")
            job["enabled"] = False
    return doc, ghosts


def crontab_for(job: dict) -> list[str]:
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return []
    words = [w.lower() for w in str(job.get("name", "")).split()
             if len(w) > 3 and w.lower() not in
             {"indexer", "monitor", "aggregator", "converter", "pipeline"}]
    if not words:
        return []
    return [l.strip() for l in out.splitlines()
            if any(w in l.lower() for w in words) and l.strip()]


def job_source(job: dict) -> str:
    return str(job.get("source", "unknown"))


def _emit_entry(entry: dict, indent: int = 0) -> list[str]:
    """Render one audit entry in disabled-jobs.yaml's own style.

    yaml_io.write() takes a dict and emits a top-level KEY whose value is the
    list; it cannot emit a bare top-level list at all, and yaml_io.read()
    silently returns {} for one (its reader only collects a top-level list
    under a key). So the list shape is rendered here, reusing yaml_io's
    canonical scalar quoting. Keys are emitted in insertion order.
    """
    pad = " " * indent
    lines: list[str] = []
    for k, v in entry.items():
        if isinstance(v, dict) and v:
            lines.append("%s%s:" % (pad, k))
            lines.extend(_emit_entry(v, indent + 2))
        elif isinstance(v, list) and v and all(isinstance(i, str) for i in v):
            lines.append("%s%s:" % (pad, k))
            lines.extend("%s- %s" % (pad, yaml_io._emit_scalar(i)) for i in v)
        else:
            lines.append("%s%s: %s" % (pad, k, yaml_io._emit_scalar(v)))
    return lines


def load_disabled() -> tuple[str, int, str]:
    """Return (shape, entry_count, raw_text) for disabled-jobs.yaml.

    shape is "list" for a bare top-level list (what the file actually is) or
    "dict" for a {"disabled": [...]} map. entry_count is counted from the raw
    text, NOT from yaml_io.read(), which returns {} for the list shape and
    would report every existing entry as absent.
    """
    if not DISABLED.exists():
        return "list", 0, ""
    raw = DISABLED.read_text()
    # Shape is decided by the FIRST meaningful line, and only if it starts at
    # column 0: a dict wrapper's entries are indented ("  - _meta:"), so a
    # stripped-prefix test would misread {"disabled": [...]} as a list.
    first = next((l for l in raw.splitlines()
                  if l.strip() and not l.strip().startswith("#")), "")
    if first.startswith("- "):
        count = sum(1 for l in raw.splitlines() if l.startswith("- "))
        return "list", count, raw
    return "dict", len(yaml_io.read(DISABLED).get("disabled", [])), raw


def save_disabled(shape: str, raw: str, new_entries: list[dict]) -> None:
    """Append new_entries to disabled-jobs.yaml, preserving its shape.

    Textual append, never yaml_io.write(). That writer cannot represent this
    file at all: it takes a dict so it cannot emit a bare top-level list, and
    it has no nested-mapping support, so it flattens each entry's `_meta`
    sub-mapping to `_meta: ""` and drops the keys. Keeping the existing bytes
    and appending means no pre-existing entry can be dropped, reordered or
    reflowed in EITHER shape.
    """
    dash_col, key_indent = (0, 2) if shape == "list" else (2, 4)
    block = []
    for e in new_entries:
        # render the entry as a mapping at key_indent (the file's own style:
        # "- _meta:" then "  job:"), then swap that indent for the dash
        lines = _emit_entry(e, key_indent)
        block.append(" " * dash_col + "- " + lines[0][key_indent:])
        block.extend(lines[1:])
    body = raw if raw.endswith("\n") or not raw else raw + "\n"
    DISABLED.write_text(body + "\n".join(block) + "\n")


def scan_and_report() -> list:
    """C2: scan_problems() can return a line-number-0 sentinel for an
    unreadable file. Never index the file with it; report it loudly."""
    problems = yaml_io.scan_problems(REGISTRY)
    sentinel = [t for n, t in problems if n == 0]
    if sentinel:
        print("FATAL: registry.yaml is UNREADABLE; scan_problems sentinel:")
        for t in sentinel:
            print("  %s" % t)
        raise SystemExit(1)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="default mode; writes nothing")
    ap.add_argument("--apply", choices=["mechanical", "ghosts"])
    ap.add_argument("--yes", action="store_true",
                    help="required with --apply")
    args = ap.parse_args()

    if args.apply and not args.yes:
        print("--apply requires --yes", file=sys.stderr)
        return 2

    if not REGISTRY.exists():
        print("FATAL: %s does not exist" % REGISTRY, file=sys.stderr)
        return 1

    before = yaml_io.read(REGISTRY)
    before_count = len(before.get("jobs", []))
    before_apos = REGISTRY.read_text().count("''")

    # Only the requested pass runs. The plan's original called BOTH
    # unconditionally, so `--apply mechanical` would also have disabled
    # ghost jobs -- out of scope for this task.
    doc = yaml_io.read(REGISTRY)
    ghosts: list[dict] = []
    if args.apply in (None, "mechanical"):
        doc = pass_mechanical(doc)
    if args.apply == "ghosts":
        doc, ghosts = pass_ghosts(doc)
    after_count = len(doc.get("jobs", []))

    print("=" * 70)
    print("REGISTRY: %s" % REGISTRY)
    print("MODE: %s" % ("dry-run (writes nothing)"
                        if not args.apply else "APPLY %s" % args.apply))
    print("=" * 70)
    print("job count: %d -> %d" % (before_count, after_count))
    print("'' apostrophe runs in file: %d" % before_apos)
    print()

    print("-" * 70)
    print("CHANGES PROPOSED: %d" % len(changes))
    print("-" * 70)
    for c in changes:
        print("  [%s] %-8s %s" % (c["kind"], c["id"], c["field"]))
        print("      before: %r" % (c["before"],))
        print("      after : %r" % (c["after"],))
    print()

    print("-" * 70)
    print("INVALID schedule_expr (REPORTED, NOT REWRITTEN): %d"
          % len(sched_reports))
    print("-" * 70)
    for jid, s in sched_reports:
        print("  %-8s %r" % (jid, s))
    print()

    print("-" * 70)
    print("JOBS SKIPPED (carry phantom keys -- NOT touched): %d"
          % len(skipped_jobs))
    print("-" * 70)
    for jid, ph in skipped_jobs:
        for k in ph:
            print("  %-8s phantom key: %r" % (jid, k))
    print()

    problems = scan_and_report()
    print("malformed key lines remaining (unchanged by this pass): %d"
          % len(problems))
    for n, t in problems:
        print("  line %d: %s" % (n, t[:96]))
    print()

    if args.apply == "ghosts":
        print("GHOSTS DISABLED: %d" % len(ghosts))
        for g in ghosts:
            print("  %-8s %s" % (str(g.get("id"))[:8], g.get("name")))
        print()

    # ---- safety gates -----------------------------------------------------
    emptied = [c for c in changes
               if isinstance(c["after"], str) and c["after"].strip() == ""]
    if emptied:
        print("ABORT: %d value(s) would be stripped to empty:" % len(emptied),
              file=sys.stderr)
        for c in emptied:
            print("  %s %s: %r" % (c["id"], c["field"], c["before"]),
                  file=sys.stderr)
        return 1

    if after_count != before_count:
        print("ABORT: job count would change %d -> %d"
              % (before_count, after_count), file=sys.stderr)
        return 1

    if not args.apply:
        print("dry run complete. NOTHING WAS WRITTEN.")
        print("re-run with --apply %s --yes to commit these changes."
              % (args.apply or "mechanical"))
        return 0

    # ---- apply ------------------------------------------------------------
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    snap = REGISTRY.parent / ("%s.pre-repair-%s" % (REGISTRY.name, ts))
    shutil.copy2(REGISTRY, snap)
    print("snapshot: %s" % snap)

    yaml_io.write(REGISTRY, doc)
    print("wrote %s" % REGISTRY)
    print("post-write '' count: %d" % REGISTRY.read_text().count("''"))

    if args.apply == "ghosts" and ghosts:
        shape, before, raw = load_disabled()
        new_entries = [{
            "_meta": {
                "archived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime()),
                "reason": ("registry claimed ownership (source=%s) but "
                           "owner_script is empty; the work runs from "
                           "system cron under cronhub-wraps/*.wrap.sh"
                           % job_source(g)),
                "source": "registry_repair.py ghosts pass",
                "remediation": "enabled=False; cron entry left untouched",
            },
            "job": g,
            "crontab_matches": crontab_for(g),
        } for g in ghosts]
        save_disabled(shape, raw, new_entries)
        print("appended %d entries to %s (%s shape; %d pre-existing kept)"
              % (len(new_entries), DISABLED, shape, before))
    return 0


if __name__ == "__main__":
    sys.exit(main())
