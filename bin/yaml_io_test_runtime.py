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
