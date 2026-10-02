# cronhub Registry Repair — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the 7 measured defects in `registry.yaml`, make `yaml_io.read()` report corruption instead of silently skipping it, and add an invariants check that fails on any regression of those defects.

**Architecture:** The repair is a one-shot transform: read the damaged document, fix it in memory, write it back through `yaml_io.write()`. Because the serializer is already correct, output is canonical by construction and the `''''` artifacts cannot survive. The reader gains a `strict` flag and a `scan_problems()` helper; `strict` defaults to **False** until the data is clean, because flipping it earlier would make `cronctl.py` throw on the live registry before the repair has run.

**Tech Stack:** python3, PyYAML (for cross-checking only — the writer stays hand-rolled), existing `yaml_io` module.

## Global Constraints

- Repo `/mnt/data/cronhub`, branch `main`, remote `git@github.com:reziux/cronhub.git`.
- **Do not change `yaml_io.write()`.** It round-trips correctly (`yaml_io_test_runtime.py` reports 0 mismatches). The "avoids pyyaml dependency" choice is deliberate.
- **Do not change any crontab entry.** Disabling the 9 ghost jobs in the registry must not stop the work — 8 of them run from `cronhub-wraps/*.wrap.sh` under system cron.
- **Job count must remain 126** after repair. Repairs change values and keys, never remove records.
- `registry.yaml`, `bin/yaml_io.py`, `bin/yaml_io_test_runtime.py` are all git-tracked; `git checkout HEAD -- <path>` is a valid rollback for each.
- Take an explicit `registry.yaml.pre-repair-<UTC ts>` snapshot before any `--apply`.
- Alert template text is never auto-invented. Task 4 requires human approval per record.

## Deviation from the design spec

The spec said to make `read()` raise on unrecognised lines. Taken literally that breaks `cronctl.py` against the live damaged registry *before* the repair runs — the tool would be unusable during the window when it is needed most. The reader therefore gains `strict=False` as the default, plus `scan_problems()`, and the default flips to strict only in Task 5, after the data is clean.

## File Structure

| File | Responsibility |
|---|---|
| `bin/yaml_io.py` (modify) | Add `RegistryParseError`, `strict` param, `scan_problems()`. `write()` untouched. |
| `bin/test_yaml_io_strict.py` (create) | Unit tests for the reader's strict/lenient behaviour. |
| `bin/registry_repair.py` (create) | One-shot repair transform with `--dry-run` / `--apply`. |
| `bin/yaml_io_test_runtime.py` (modify) | Add invariants check alongside the existing round-trip check. |
| `registry.yaml` (data) | Repaired output. |
| `disabled-jobs.yaml` (data) | Gains 9 audit entries. |

---

### Task 1: Make the reader report what it cannot represent

**Files:**
- Modify: `/mnt/data/cronhub/bin/yaml_io.py` — add exception class near the top; thread `strict` and `problems` through `read()`; add `scan_problems()`.
- Create: `/mnt/data/cronhub/bin/test_yaml_io_strict.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `yaml_io.RegistryParseError` — raised on unrecognised lines when `strict=True`.
  - `yaml_io.read(path, strict=False, problems=None) -> dict` — same signature as before plus two optional params. Existing single-arg callers are unaffected.
  - `yaml_io.scan_problems(path) -> list[tuple[int, str]]` — `(lineno, raw_line)` for every line `read()` could not represent.
  - Later tasks call `scan_problems()` and `RegistryParseError`.

- [ ] **Step 1: Write the failing test**

Create `/mnt/data/cronhub/bin/test_yaml_io_strict.py`:

```python
"""Tests for yaml_io strict-mode parsing. Run: python3 test_yaml_io_strict.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/mnt/data/cronhub/bin")
import yaml_io

GOOD = """jobs:
  - id: aaa
    name: Alpha
    enabled: true
  - id: bbb
    name: Beta
    enabled: false
"""

# A description split across real newlines: the second line looks like a key.
DAMAGED = """jobs:
  - id: aaa
    name: Alpha
    description: "first line: with a colon"
    tool. Format: "orphan fragment"
"""

FAILED = []


def check(label, cond):
    if cond:
        print("  ok   %s" % label)
    else:
        FAILED.append(label)
        print("  FAIL %s" % label)


def write_tmp(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return Path(f.name)


print("== clean file ==")
p = write_tmp(GOOD)
d = yaml_io.read(p)
check("lenient read returns 2 jobs", len(d["jobs"]) == 2)
check("scan_problems is empty", yaml_io.scan_problems(p) == [])
check("strict read does not raise", yaml_io.read(p, strict=True) is not None)

print("== damaged file ==")
p2 = write_tmp(DAMAGED)
probs = yaml_io.scan_problems(p2)
check("lenient read still returns data", len(yaml_io.read(p2)["jobs"]) == 1)
check("scan_problems finds the orphan line", len(probs) == 1)
check("problem carries the line number", probs and probs[0][0] == 6)
check("problem carries the raw text", probs and "tool. Format" in probs[0][1])

raised = False
try:
    yaml_io.read(p2, strict=True)
except yaml_io.RegistryParseError:
    raised = True
check("strict read raises RegistryParseError", raised)

print("== '' is a string, not a list ==")
p3 = write_tmp("jobs:\n  - id: aaa\n    notes: ''\n")
d3 = yaml_io.read(p3)
check("'' does not become a list", d3["jobs"][0].get("notes") == "" or
      d3["jobs"][0].get("notes") == "''")
check("'' is not an empty list", d3["jobs"][0].get("notes") != [])

print()
if FAILED:
    print("FAILED: %d" % len(FAILED))
    sys.exit(1)
print("all tests passed")
```

- [ ] **Step 2: Run it to verify it fails**

```bash
python3 /mnt/data/cronhub/bin/test_yaml_io_strict.py
```

Expected: `AttributeError: module 'yaml_io' has no attribute 'scan_problems'`.

- [ ] **Step 3: Add the exception class**

Insert immediately after the `_SPECIAL_CHARS` definition in
`/mnt/data/cronhub/bin/yaml_io.py`:

```python
class RegistryParseError(ValueError):
    """Raised in strict mode when the document contains lines that this
    schema cannot represent.

    Previously such lines were silently skipped, which is how multi-line
    descriptions split into phantom keys went unnoticed.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        detail = "\n".join("  line %d: %s" % (n, t) for n, t in self.problems[:10])
        super().__init__("registry has %d unparseable line(s):\n%s"
                         % (len(self.problems), detail))
```

- [ ] **Step 4: Thread `strict` and `problems` through `read()`**

Change the signature of `read()` from:

```python
def read(path: Path) -> dict:
```

to:

```python
def read(path: Path, strict: bool = False, problems: list | None = None) -> dict:
```

Add at the top of the body, after `text = path.read_text()`:

```python
    if problems is None:
        problems = []
```

Change the loop header from:

```python
    for raw in text.splitlines():
```

to:

```python
    for lineno, raw in enumerate(text.splitlines(), 1):
```

Replace the trailing comment block:

```python
        # else: silently skip (e.g. trailing comment lines we've already filtered,
        # or lines we don't recognise in our schema)

    return out
```

with:

```python
        else:
            # A line we cannot represent. Comment and blank lines are filtered
            # above, so anything reaching here is real content: most often a
            # multi-line value that was hand-edited into the file and split on
            # its embedded ": ". Record it instead of dropping it silently.
            problems.append((lineno, raw))

    if strict and problems:
        raise RegistryParseError(problems)

    return out
```

- [ ] **Step 5: Add `scan_problems()`**

Append to `/mnt/data/cronhub/bin/yaml_io.py`:

```python
def scan_problems(path: Path) -> list:
    """Return [(lineno, raw_line)] for every line read() cannot represent.

    Never raises. Use this to audit a registry without breaking callers.
    """
    problems: list = []
    read(path, strict=False, problems=problems)
    return problems
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
python3 /mnt/data/cronhub/bin/test_yaml_io_strict.py
```

Expected: `all tests passed`, exit 0.

- [ ] **Step 7: Confirm no existing caller broke**

```bash
python3 /mnt/data/cronhub/bin/yaml_io_test_runtime.py 2>&1 | tail -n 5
cd /mnt/data/cronhub && python3 bin/cronctl.py list 2>&1 | tail -n 5
```

Expected: round-trip still reports `mismatches: 0`, and `cronctl.py list`
still prints jobs. The `''` case is expected to still fail its assertion until
Step 8 — if the last test fails, continue; it is fixed next.

- [ ] **Step 8: Remove the lossy `''` coercions**

In `/mnt/data/cronhub/bin/yaml_io.py`, inside `read()`, replace:

```python
            if v in ("", "''", '""', '"\'\''):
                # Empty value: could be scalar-list decl. Initialize as empty list
                # and let a subsequent dash line activate it.
                cur_item[parsed_k] = []
                sub_list_key = parsed_k
                sub_list_parent_indent = indent
            else:
                parsed_v = _parse_scalar(v)
                # `''` (literal two single quotes that wasn't a quoted-empty) —
                # normalize to actual empty string for consistency.
                if parsed_v == "''":
                    parsed_v = ""
                cur_item[parsed_k] = parsed_v
                sub_list_key = None
                sub_list_parent_indent = -1
```

with:

```python
            if v == "":
                # Empty value: could be a scalar-list declaration. Initialize as
                # an empty list and let a subsequent dash line activate it.
                cur_item[parsed_k] = []
                sub_list_key = parsed_k
                sub_list_parent_indent = indent
            else:
                cur_item[parsed_k] = _parse_scalar(v)
                sub_list_key = None
                sub_list_parent_indent = -1
```

This stops `''` and `""` from being coerced into a list, and stops a
legitimate two-apostrophe value from being rewritten to `""`.

- [ ] **Step 9: Re-run both tests**

```bash
python3 /mnt/data/cronhub/bin/test_yaml_io_strict.py
python3 /mnt/data/cronhub/bin/yaml_io_test_runtime.py 2>&1 | tail -n 3
```

Expected: `all tests passed`; round-trip `mismatches: 0`.

- [ ] **Step 10: Audit the live registry with the new scanner**

```bash
python3 -c "import sys; sys.path.insert(0,'/mnt/data/cronhub/bin'); import yaml_io; from pathlib import Path; p=yaml_io.scan_problems(Path('/mnt/data/cronhub/registry.yaml')); print(len(p),'unparseable lines'); [print('  line',n,':',t[:90]) for n,t in p[:15]]"
```

Expected: a non-zero count, listing the orphan lines. Record the number — Task 3 must drive it to zero.

- [ ] **Step 11: Commit**

```bash
cd /mnt/data/cronhub
git add bin/yaml_io.py bin/test_yaml_io_strict.py
git commit -m "yaml_io: report unparseable lines instead of skipping them silently"
git push origin main
```

---

### Task 2: The repair transform

**Files:**
- Create: `/mnt/data/cronhub/bin/registry_repair.py`

**Interfaces:**
- Consumes: `yaml_io.read`, `yaml_io.write`, `yaml_io.scan_problems` from Task 1.
- Produces: CLI `registry_repair.py --dry-run`, `--apply mechanical`, `--apply ghosts`. Both `--apply` modes require `--yes` and take a timestamped snapshot. Exits 0 on success, non-zero if the job count would change.

- [ ] **Step 1: Write the script**

Create `/mnt/data/cronhub/bin/registry_repair.py`:

```python
#!/usr/bin/env python3
"""One-shot repair of cronhub registry.yaml.

Usage:
  registry_repair.py --dry-run              # print every proposed change
  registry_repair.py --apply mechanical --yes
  registry_repair.py --apply ghosts --yes

Passes:
  mechanical  strip '''' artifacts, 'None' -> None, repair schedule_expr
  ghosts      set enabled=False on enabled jobs with an empty owner_script,
              and append an audit entry to disabled-jobs.yaml

Phantoms (orphan keys from split multi-line values) are NOT handled here;
they need human-approved reconstructions and are done separately.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/mnt/data/cronhub/bin")
import yaml_io  # noqa: E402

CRONHUB = Path("/mnt/data/cronhub")
REGISTRY = CRONHUB / "registry.yaml"
DISABLED = CRONHUB / "disabled-jobs.yaml"
CANON = {
    "id", "name", "enabled", "schedule_expr", "schedule_kind", "tz",
    "is_llm_turn", "owner_script", "primary_executor", "backup_executor",
    "fallback_strategy", "discord_channel", "tags", "source",
    "last_status", "consecutive_errors", "description", "log_jobname_hint",
    "log_file", "watches", "last_run_at",
}
APOS_RUN = re.compile(r"^'{2,}|'{2,}$")
CRON_FIELD = re.compile(r"^[*0-9/,-]+$")
DOW = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
MON = {"jan", "feb", "mar", "apr", "may", "jun",
       "jul", "aug", "sep", "oct", "nov", "dec"}

changes: list[dict] = []


def note(job_id, field, before, after, kind):
    changes.append({"id": job_id, "field": field, "kind": kind,
                    "before": before, "after": after})


def valid_schedule(expr) -> bool:
    """Concrete validity rule from the design spec."""
    if not expr:
        return True
    s = str(expr).strip()
    m = re.fullmatch(r"@every\s+\d+ms", s)
    if m:
        return True
    parts = s.split()
    if len(parts) != 5:
        return False
    for i, p in enumerate(parts):
        low = p.lower()
        if CRON_FIELD.match(p):
            continue
        if i == 4 and (low in DOW or low in MON):
            continue
        return False
    return True


def strip_apos(s: str) -> str:
    return APOS_RUN.sub("", s)


def pass_mechanical(doc: dict) -> dict:
    for job in doc.get("jobs", []):
        if not isinstance(job, dict):
            continue
        jid = str(job.get("id", "?"))[:8]
        for key, val in list(job.items()):
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
            note(jid, "schedule_expr", sched, "<INVALID>", "bad-schedule")
    return doc


def pass_ghosts(doc: dict) -> tuple[dict, list[dict]]:
    ghosts = []
    for job in doc.get("jobs", []):
        if not isinstance(job, dict):
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", choices=["mechanical", "ghosts"])
    ap.add_argument("--yes", action="store_true",
                    help="required with --apply")
    args = ap.parse_args()

    if args.apply and not args.yes:
        print("--apply requires --yes", file=sys.stderr)
        return 2

    before_count = len(yaml_io.read(REGISTRY).get("jobs", []))

    doc = yaml_io.read(REGISTRY)
    doc = pass_mechanical(doc)
    doc, ghosts = pass_ghosts(doc)
    after_count = len(doc.get("jobs", []))

    if after_count != before_count:
        print("ABORT: job count would change %d -> %d"
              % (before_count, after_count), file=sys.stderr)
        return 1

    print("=" * 68)
    print("PROPOSED CHANGES: %d" % len(changes))
    print("=" * 68)
    for c in changes:
        print("  %-8s %-16s %-18s %r -> %r"
              % (c["id"], c["kind"], c["field"],
                 str(c["before"])[:44], str(c["after"])[:44]))
    print()

    problems = yaml_io.scan_problems(REGISTRY)
    print("unparseable lines remaining: %d" % len(problems))
    for n, t in problems[:10]:
        print("  line %d: %s" % (n, t[:88]))
    print()

    if args.apply == "ghosts":
        print("GHOSTS DISABLED: %d" % len(ghosts))
        for g in ghosts:
            print("  %-8s %s" % (str(g.get("id"))[:8], g.get("name")))
    print()

    if not args.apply:
        print("dry run. re-run with --apply %s --yes"
              % (args.apply or "mechanical"))
        return 0

    snap = REGISTRY.with_suffix(".yaml.pre-repair-%s"
                                % time.strftime("%Y%m%dT%H%M%SZ",
                                                time.gmtime()))
    shutil.copy2(REGISTRY, snap)
    print("snapshot: %s" % snap)

    yaml_io.write(REGISTRY, doc)
    print("wrote %s" % REGISTRY)

    if args.apply == "ghosts" and ghosts:
        existing = yaml_io.read(DISABLED) if DISABLED.exists() else {}
        entries = existing.get("disabled", []) if isinstance(existing, dict) else []
        for g in ghosts:
            entries.append({
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
            })
        yaml_io.write(DISABLED, {"disabled": entries})
        print("appended %d entries to %s" % (len(ghosts), DISABLED))
    return 0


def job_source(job: dict) -> str:
    return str(job.get("source", "unknown"))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the dry run and read every line of output**

```bash
python3 /mnt/data/cronhub/bin/registry_repair.py --dry-run
```

Expected: a non-zero change count. Before approving, check that no proposed
`apos-artifact` change strips a value down to the empty string or leaves
leading/trailing punctuation that looks wrong. If any do, that heuristic is
too blunt for this data — stop and report rather than applying.

- [ ] **Step 3: Confirm the job count is untouched**

```bash
python3 -c "import sys;sys.path.insert(0,'/mnt/data/cronhub/bin');import yaml_io;from pathlib import Path;print(len(yaml_io.read(Path('/mnt/data/cronhub/registry.yaml'))['jobs']))"
```

Expected: `126`. The script aborts if this would change, but verify the
precondition yourself.

- [ ] **Step 4: Commit the script before running it**

```bash
cd /mnt/data/cronhub
git add bin/registry_repair.py
git commit -m "Add one-shot registry repair transform (dry-run by default)"
git push origin main
```

---

### Task 3: Apply the mechanical pass

**Files:**
- Modify: `/mnt/data/cronhub/registry.yaml`
- Create: `/mnt/data/cronhub/registry.yaml.pre-repair-<ts>`

**Interfaces:**
- Consumes: `registry_repair.py` from Task 2.
- Produces: a registry with D2, D3, D4 repaired. D5 (ghosts) still open.

- [ ] **Step 1: Apply**

```bash
python3 /mnt/data/cronhub/bin/registry_repair.py --apply mechanical --yes
```

Expected: a snapshot path, `wrote /mnt/data/cronhub/registry.yaml`, exit 0.

- [ ] **Step 2: Verify the job count and that the file still parses**

```bash
python3 -c "import yaml;d=yaml.safe_load(open('/mnt/data/cronhub/registry.yaml'));print('jobs:',len(d['jobs']))"
```

Expected: `jobs: 126`. PyYAML is used here as an independent parser — if the
hand-rolled reader and PyYAML disagree, that is a finding, not a nuisance.

- [ ] **Step 3: Confirm the artifacts are gone**

```bash
grep -c "''''" /mnt/data/cronhub/registry.yaml
grep -c "discord_channel: \"None\"" /mnt/data/cronhub/registry.yaml
```

Expected: `0` for both.

- [ ] **Step 4: Re-run the cross-check analysis**

```bash
python3 /tmp/phase2_recon.py 2>&1 | sed -n '1,12p;/STATUS DISTRIBUTION/,/^====/p'
```

Expected: the `''''` line count is 0 and the job count is still 126.

- [ ] **Step 5: Confirm cronctl still works**

```bash
cd /mnt/data/cronhub && python3 bin/cronctl.py list 2>&1 | tail -n 5
```

Expected: job listing, no traceback.

- [ ] **Step 6: Commit**

```bash
cd /mnt/data/cronhub
git add registry.yaml
git commit -m "Repair registry: strip quote artifacts, null stringified None, flag bad schedules"
git push origin main
```

---

### Task 4: Reconstruct the 12 phantom-key records — approval gate

**Files:**
- Modify: `/mnt/data/cronhub/registry.yaml`
- Create: `/mnt/data/cronhub/registry.yaml.phantom-proposals.json`

**Interfaces:**
- Consumes: `yaml_io.read` from Task 1.
- Produces: `registry.yaml` with the 12 records' orphan keys folded back into `description`.

**This task requires Igor to approve each reconstructed alert template before it is written. Do not proceed past Step 2 without that approval.**

- [ ] **Step 1: Produce the proposals**

```bash
python3 - <<'EOF' > /mnt/data/cronhub/registry.yaml.phantom-proposals.json
import sys, json
sys.path.insert(0, "/mnt/data/cronhub/bin")
import yaml_io
from pathlib import Path

CANON = {"id","name","enabled","schedule_expr","schedule_kind","tz",
         "is_llm_turn","owner_script","primary_executor","backup_executor",
         "fallback_strategy","discord_channel","tags","source","last_status",
         "consecutive_errors","description","log_jobname_hint","log_file",
         "watches","last_run_at"}

doc = yaml_io.read(Path("/mnt/data/cronhub/registry.yaml"))
out = []
for job in doc["jobs"]:
    if not isinstance(job, dict):
        continue
    extra = {k: v for k, v in job.items() if k not in CANON}
    if extra:
        out.append({
            "id": job.get("id"),
            "name": job.get("name"),
            "current_description": job.get("description", ""),
            "orphan_keys": extra,
        })
print(json.dumps(out, indent=2, ensure_ascii=False))
EOF
cat /mnt/data/cronhub/registry.yaml.phantom-proposals.json
```

- [ ] **Step 2: Present the proposals to Igor and get approval per record**

For each of the 12, show the current `description` and the orphan-key
fragments, and propose a rejoined single-line `description`. Ask which are
approved. **If a record's fragments cannot be confidently rejoined, leave it
damaged and say so** — a visibly broken alert template is better than a
silently invented one.

- [ ] **Step 3: Write the approved reconstructions to the proposals file**

Edit `/mnt/data/cronhub/registry.yaml.phantom-proposals.json` into
`{"<job-id>": {"description": "<approved text>", "drop_orphan_keys": true}}`
for the approved records only. Records not approved are omitted entirely.

- [ ] **Step 4: Apply the approved reconstructions**

```bash
python3 - <<'EOF'
import sys, json, time, shutil
sys.path.insert(0, "/mnt/data/cronhub/bin")
import yaml_io
from pathlib import Path

REG = Path("/mnt/data/cronhub/registry.yaml")
PROP = Path("/mnt/data/cronhub/registry.yaml.phantom-proposals.json")
CANON = {"id","name","enabled","schedule_expr","schedule_kind","tz",
         "is_llm_turn","owner_script","primary_executor","backup_executor",
         "fallback_strategy","discord_channel","tags","source","last_status",
         "consecutive_errors","description","log_jobname_hint","log_file",
         "watches","last_run_at"}

approved = json.loads(PROP.read_text())
doc = yaml_io.read(REG)
n = len(doc["jobs"])
for job in doc["jobs"]:
    jid = job.get("id")
    if jid in approved:
        job["description"] = approved[jid]["description"]
        for k in [k for k in job if k not in CANON]:
            del job[k]
assert len(doc["jobs"]) == n, "job count changed"
snap = REG.with_suffix(".yaml.pre-phantom-%s" % time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
shutil.copy2(REG, snap)
yaml_io.write(REG, doc)
print("snapshot:", snap)
print("repaired:", len(approved), "records; job count still", n)
EOF
```

- [ ] **Step 5: Verify no unparseable lines remain**

```bash
python3 -c "import sys;sys.path.insert(0,'/mnt/data/cronhub/bin');import yaml_io;from pathlib import Path;p=yaml_io.scan_problems(Path('/mnt/data/cronhub/registry.yaml'));print(len(p),'remaining');[print(' ',n,t[:80]) for n,t in p[:10]]"
```

Expected: `0 remaining`.

- [ ] **Step 6: Commit**

```bash
cd /mnt/data/cronhub
git add registry.yaml registry.yaml.phantom-proposals.json
git commit -m "Fold orphan keys back into description for approved observer records"
git push origin main
```

---

### Task 5: Disable the 9 ghost jobs

**Files:**
- Modify: `/mnt/data/cronhub/registry.yaml`, `/mnt/data/cronhub/disabled-jobs.yaml`

**Interfaces:**
- Consumes: `registry_repair.py --apply ghosts` from Task 2.
- Produces: 9 jobs with `enabled: False`, each with an audit entry naming the crontab line that actually runs them.

- [ ] **Step 1: Record the current crontab so you can prove it is untouched**

```bash
crontab -l > /tmp/crontab-before-ghosts.txt
wc -l /tmp/crontab-before-ghosts.txt
```

- [ ] **Step 2: Dry run and review**

```bash
python3 /mnt/data/cronhub/bin/registry_repair.py --dry-run
```

Expected: `GHOSTS DISABLED: 9`.

- [ ] **Step 3: Apply**

```bash
python3 /mnt/data/cronhub/bin/registry_repair.py --apply ghosts --yes
```

- [ ] **Step 4: Verify the registry now claims nothing it does not own**

```bash
python3 -c "
import sys;sys.path.insert(0,'/mnt/data/cronhub/bin');import yaml_io;from pathlib import Path
d=yaml_io.read(Path('/mnt/data/cronhub/registry.yaml'))
g=[j for j in d['jobs'] if j.get('enabled') and isinstance(j.get('owner_script'),list) and not j['owner_script']]
print('enabled ghosts:',len(g))
print('total jobs:',len(d['jobs']))
"
```

Expected: `enabled ghosts: 0`, `total jobs: 126`.

- [ ] **Step 5: Prove the work still runs — the critical check**

8 of the 9 run from `cronhub-wraps/*.wrap.sh`. After disabling them in the
registry, confirm their runs still appear:

```bash
for w in daily-shadow-index wpp-intelligence ai-news-aggregator blade-ingest-umbrella; do
  echo "--- $w"; grep -c "$w" /mnt/data/cronhub/runs/2026-10-02.jsonl 2>/dev/null || echo 0
done
```

Expected: non-zero counts for the wrappers that fire on today's schedule.
If any is zero, the registry disable may have had a side effect — investigate
before proceeding.

- [ ] **Step 6: Prove the crontab did not change**

```bash
crontab -l > /tmp/crontab-after-ghosts.txt
diff /tmp/crontab-before-ghosts.txt /tmp/crontab-after-ghosts.txt && echo CRONTAB_UNCHANGED
```

Expected: `CRONTAB_UNCHANGED`, no diff output.

- [ ] **Step 7: Commit**

```bash
cd /mnt/data/cronhub
git add registry.yaml disabled-jobs.yaml
git commit -m "Disable 9 duplicate registry entries; work continues from system cron"
git push origin main
```

---

### Task 6: Invariants check, then flip strict mode

**Files:**
- Modify: `/mnt/data/cronhub/bin/yaml_io_test_runtime.py`
- Modify: `/mnt/data/cronhub/bin/yaml_io.py` — flip the `read()` default to `strict=True`

**Interfaces:**
- Consumes: `yaml_io.scan_problems`, `CANON` from earlier tasks.
- Produces: a runtime test that fails on any regression of D1–D5, and a reader that refuses corrupt input by default.

- [ ] **Step 1: Add the invariants check**

Append to `/mnt/data/cronhub/bin/yaml_io_test_runtime.py`:

```python
print()
print("=" * 60)
print("INVARIANTS")
print("=" * 60)
import re
from pathlib import Path as _P

REPO = _P("/mnt/data/cronhub")
REG = REPO / "registry.yaml"
CANON = {"id", "name", "enabled", "schedule_expr", "schedule_kind", "tz",
         "is_llm_turn", "owner_script", "primary_executor", "backup_executor",
         "fallback_strategy", "discord_channel", "tags", "source",
         "last_status", "consecutive_errors", "description",
         "log_jobname_hint", "log_file", "watches", "last_run_at"}
APOS = re.compile(r"^'{2,}|'{2,}$")
CRON_FIELD = re.compile(r"^[*0-9/,-]+$")
DOW = {"mon","tue","wed","thu","fri","sat","sun"}
MON = {"jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"}

viol = []


def bad(code, detail):
    viol.append("%-16s %s" % (code, detail))


for n, t in yaml_io.scan_problems(REG):
    bad("D1-unparseable", "line %d: %s" % (n, t[:70]))
for j in doc["jobs"]:
    if not isinstance(j, dict):
        continue
    jid = str(j.get("id", "?"))[:8]
    for k in set(j.keys()) - CANON:
        bad("D1-phantom-key", "%s %r" % (jid, k))
    for k, v in j.items():
        if isinstance(v, str) and APOS.search(v):
            bad("D2-apos", "%s %s" % (jid, k))
        if v == "None":
            bad("D3-str-none", "%s %s" % (jid, k))
    s = j.get("schedule_expr")
    if isinstance(s, str) and s.strip():
        if not (re.fullmatch(r"@every\s+\d+ms", s.strip())
                or (len(s.split()) == 5 and all(
                    CRON_FIELD.match(p) or (i == 4 and p.lower() in DOW | MON)
                    for i, p in enumerate(s.split())))):
            bad("D4-schedule", "%s %r" % (jid, s))
    if j.get("enabled") and isinstance(j.get("owner_script"), list) \
            and not j["owner_script"]:
        bad("D5-ghost", "%s %s" % (jid, j.get("name")))

if viol:
    print("VIOLATIONS: %d" % len(viol))
    for v in viol[:40]:
        print("  " + v)
    sys.exit(1)
print("no violations")
```

- [ ] **Step 2: Run it**

```bash
python3 /mnt/data/cronhub/bin/yaml_io_test_runtime.py 2>&1 | tail -n 20
```

Expected: `no violations`, exit 0. If D6/D7 are added later they will fail
first — they are intentionally not in this set, because the repair does not
address them.

- [ ] **Step 3: Flip strict mode on by default**

In `/mnt/data/cronhub/bin/yaml_io.py`, change:

```python
def read(path: Path, strict: bool = False, problems: list | None = None) -> dict:
```

to:

```python
def read(path: Path, strict: bool = True, problems: list | None = None) -> dict:
```

- [ ] **Step 4: Verify every caller still works under strict**

```bash
python3 /mnt/data/cronhub/bin/yaml_io_test_runtime.py 2>&1 | tail -n 6
python3 /mnt/data/cronhub/bin/test_yaml_io_strict.py 2>&1 | tail -n 6
cd /mnt/data/cronhub && python3 bin/cronctl.py list 2>&1 | tail -n 5
```

Expected: no violations, all tests passed, cronctl lists jobs. The strict
test's damaged-file case calls `read(p2)` without `strict`; it now raises, so
update that one line to `yaml_io.read(p2, strict=False)` if it fails.

- [ ] **Step 5: Confirm the auditor and rotate still parse the registry**

```bash
bash /mnt/data/cronhub/bin/cronhub-auditor.sh 2>&1 | tail -n 10
bash /mnt/data/cronhub/bin/cronhub-rotate-runs.sh
```

Expected: both complete without a parse error.

- [ ] **Step 6: Commit**

```bash
cd /mnt/data/cronhub
git add bin/yaml_io.py bin/yaml_io_test_runtime.py
git commit -m "Add registry invariants check; make read() strict by default"
git push origin main
```

## Rollback

| Change | Rollback |
|---|---|
| `registry.yaml` | `git -C /mnt/data/cronhub checkout HEAD -- registry.yaml` |
| `disabled-jobs.yaml` | `git -C /mnt/data/cronhub checkout HEAD -- disabled-jobs.yaml` |
| `bin/yaml_io.py` | `git -C /mnt/data/cronhub checkout HEAD -- bin/yaml_io.py` |
| `bin/yaml_io_test_runtime.py` | `git -C /mnt/data/cronhub checkout HEAD -- bin/yaml_io_test_runtime.py` |
| `bin/registry_repair.py` | delete the file; it is one-shot |
| pre-repair snapshots | `registry.yaml.pre-repair-<ts>` left on disk by the script |
