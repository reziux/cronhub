"""Tests for yaml_io key-shape detection. Run: python3 test_yaml_io_strict.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/mnt/data/cronhub-work/bin")
import yaml_io

GOOD = """jobs:
  - id: aaa
    name: Alpha
    enabled: true
  - id: bbb
    name: Beta
    enabled: false
"""

# Line 5 mimics the real damage: a value split on an embedded ": " leaves a
# second line whose LEFT side ("tool. Format") cannot be a real key.
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
check("keys are real identifiers",
      all(yaml_io.is_identifier(k) for j in d["jobs"] for k in j))

print("== damaged file ==")
p2 = write_tmp(DAMAGED)
probs = yaml_io.scan_problems(p2)
check("scan_problems finds the orphan key line", len(probs) == 1)
check("problem is on line 5", probs and probs[0][0] == 5)
check("problem carries the raw text", probs and "tool. Format" in probs[0][1])
check("is_identifier rejects the orphan",
      not yaml_io.is_identifier("tool. Format"))
check("is_identifier accepts real keys",
      yaml_io.is_identifier("log_jobname_hint"))

raised = False
try:
    yaml_io.read(p2, strict=True)
except yaml_io.RegistryParseError:
    raised = True
check("strict read raises RegistryParseError", raised)

d2 = yaml_io.read(p2)
check("lenient read still returns the job", len(d2["jobs"]) == 1)

print("== '' is a string, not a list ==")
p3 = write_tmp("jobs:\n  - id: aaa\n    notes: ''\n")
d3 = yaml_io.read(p3)
check("'' is not an empty list", d3["jobs"][0].get("notes") != [])

print()
if FAILED:
    print("FAILED: %d" % len(FAILED))
    sys.exit(1)
print("all tests passed")
