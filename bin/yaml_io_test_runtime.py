import sys
sys.path.insert(0, "/mnt/data/cronhub/bin")
from pathlib import Path
import yaml_io

d = yaml_io.read(Path("/mnt/data/cronhub/registry.yaml"))
print("jobs count:", len(d["jobs"]))
print("first 5 jobs:")
for j in d["jobs"][:5]:
    print(" ", j.get("id", "???")[:14], "|", j.get("name", "?")[:50], "| enabled=", j.get("enabled"))

print()
print("enabled/disabled counts:")
e = sum(1 for j in d["jobs"] if j.get("enabled") is True)
dis = sum(1 for j in d["jobs"] if j.get("enabled") is False)
print(" enabled:", e, "disabled:", dis, "total:", len(d["jobs"]))

# round-trip test
import tempfile
with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as t:
    out = t.name
yaml_io.write(Path(out), d)
d2 = yaml_io.read(Path(out))
print("round-trip jobs count:", len(d2["jobs"]))

mismatches = 0
for j1, j2 in zip(d["jobs"], d2["jobs"]):
    if j1 != j2:
        mismatches += 1
        if mismatches <= 2:
            print("mismatch:")
            print("  orig:", repr(j1)[:200])
            print("  rt  :", repr(j2)[:200])
print("mismatches:", mismatches)


# =============================================================================
# Registry invariants check (D0-D5)
#
# Runs against a COPY of the WORKTREE registry. It never reads
# /mnt/data/cronhub/registry.yaml: that is the live production file, it has
# different content, and it is rewritten on a schedule. The worktree copy is
# the artefact under repair, and this is what gets checked.
#
# Violations are REPORTED, not repaired. This file is a detector: it exits
# non-zero while the registry is dirty so the count can be driven to zero.
# =============================================================================
import importlib.util
import re
import shutil

WORKTREE_BIN = "/mnt/data/cronhub-work/bin"
WORKTREE_REG = "/mnt/data/cronhub-work/registry.yaml"

# Load the WORKTREE yaml_io explicitly by path. The round-trip check above
# already bound the name `yaml_io` to the live module at import time; binding
# the worktree module under its own name keeps both checks independent and
# leaves the section above untouched.
_spec = importlib.util.spec_from_file_location(
    "yaml_io_worktree", WORKTREE_BIN + "/yaml_io.py")
yio = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(yio)

# The canonical key set. A job key outside this is a phantom: the parser
# split a value on an embedded ": " and invented the key.
CANON = {
    "id", "name", "enabled", "schedule_expr", "schedule_kind", "tz",
    "is_llm_turn", "owner_script", "primary_executor", "backup_executor",
    "fallback_strategy", "discord_channel", "tags", "source",
    "last_status", "consecutive_errors", "description",
    "log_jobname_hint", "log_file", "watches", "last_run_at",
}

_NAMES = {
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
}
_EVERY = re.compile(r"^@every \d+ms$")
_FIELD = re.compile(r"^[*0-9/,-]+$")
# A run of two or more apostrophes at either end of the value.
_APOS_RUN = re.compile(r"^'{2,}|'{2,}$")


def _schedule_ok(se):
    """True if se is empty, an @every Nms interval, or a 5-field cron."""
    if not isinstance(se, str) or not se.strip():
        return True
    s = se.strip()
    if _EVERY.match(s):
        return True
    parts = s.split()
    if len(parts) != 5:
        return False
    return all(_FIELD.match(f) or f.lower() in _NAMES for f in parts)


def _leaves(v):
    """Yield each scalar leaf of a job value (the value itself, or its items)."""
    if isinstance(v, list):
        for x in v:
            yield x
    else:
        yield v


violations = []  # (code, detail)
counts = {}      # code -> number of violations


def flag(code, detail):
    violations.append((code, detail))
    counts[code] = counts.get(code, 0) + 1


# --- the copy under test ----------------------------------------------------
_tmpdir = tempfile.mkdtemp(prefix="cronhub-registry-invariants-")
COPY = Path(_tmpdir) / "registry.yaml"
shutil.copy2(WORKTREE_REG, COPY)

print()
print("=" * 72)
print("registry invariants")
print("  source:", WORKTREE_REG)
print("  copy  :", COPY)
print("=" * 72)

# --- D0-unreadable / D1-unparseable -----------------------------------------
# scan_problems never raises. An unreadable file comes back as a single entry
# with line number 0; that sentinel is skipped here and reported as its own
# check, otherwise an unreadable registry would masquerade as a clean one.
for lineno, raw in yio.scan_problems(COPY):
    if lineno == 0:
        flag("D0-unreadable", raw)
    else:
        flag("D1-unparseable", "line %d: %s" % (lineno, raw.strip()[:100]))

jobs = yio.read(COPY).get("jobs") or []
print("jobs parsed from copy:", len(jobs))

# --- D1-phantom-key ---------------------------------------------------------
for j in jobs:
    bad = sorted(str(k) for k in j if k not in CANON)
    if bad:
        flag("D1-phantom-key", "%s: %s"
             % (str(j.get("id"))[:24], "; ".join(repr(k)[:70] for k in bad)))

# --- D2-apos / D3-str-none --------------------------------------------------
for j in jobs:
    jid = str(j.get("id"))[:24]
    for k, v in j.items():
        for s in _leaves(v):
            if not isinstance(s, str):
                continue
            if _APOS_RUN.search(s):
                flag("D2-apos", "%s.%s: %r" % (jid, k, s[:70]))
            if s == "None":
                flag("D3-str-none", "%s.%s: %r" % (jid, k, s[:70]))

# --- D4-schedule ------------------------------------------------------------
for j in jobs:
    se = j.get("schedule_expr")
    if not _schedule_ok(se):
        flag("D4-schedule", "%s: %r" % (str(j.get("id"))[:24], se))

# --- D5-ghost ---------------------------------------------------------------
for j in jobs:
    if j.get("enabled") is True and j.get("owner_script") == []:
        flag("D5-ghost", "%s: enabled with empty owner_script"
             % str(j.get("id"))[:24])

for code, detail in violations:
    print(" %-18s %s" % (code, detail))

print()
print("violations by code:")
for code in sorted(counts):
    print(" %-18s %d" % (code, counts[code]))
print(" %-18s %d" % ("TOTAL", len(violations)))

print()
if violations:
    print("INVARIANTS FAILED: %d violation(s). Registry is NOT clean." % len(violations))
    sys.exit(1)
print("INVARIANTS OK: no violations.")
