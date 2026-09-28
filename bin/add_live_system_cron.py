#!/usr/bin/env python3
"""add_live_system_cron.py — query the user's actual live crontab and add
each entry as a live registry job with primary_executor=system-cron."""
from __future__ import annotations
import json, sys, hashlib, subprocess, re
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, '/mnt/data/cronhub/bin')
import yaml_io

REG = Path('/mnt/data/cronhub/registry.yaml')

def stable(s): return hashlib.sha1(s.encode()).hexdigest()[:8]

def parse_crontab_line(line: str) -> dict | None:
    """Parse a crontab line into {expr, cmd, script_path}."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(None, 5)
    if len(parts) < 6:
        return None
    expr = " ".join(parts[:5])
    cmd = parts[5]
    tokens = cmd.split()
    skip_interp = {"/usr/bin/env", "env", "node", "bash", "sh", "python3", "find"}
    script = ""
    # Find the LAST existing file path in the command — that's the actual script
    # (e.g. "/home/linuxbrew/.linuxbrew/bin/node /home/reziux/.openclaw/workspace/scripts/x.js"
    #  -> "x.js" is the script)
    for tok in reversed(tokens):
        cleaned = tok.split(">")[0].split("|")[0].rstrip(",;").strip()
        if not cleaned.startswith("/"):
            continue
        if Path(cleaned).name in skip_interp:
            continue
        if cleaned.startswith("/home/linuxbrew"):
            # linuxbrew node binary — skip
            continue
        if Path(cleaned).exists():
            script = cleaned
            break
    if not script:
        # fallback: first path-like token after interpreter
        i = 0
        if tokens and (tokens[0] in skip_interp or "/linuxbrew" in tokens[0]):
            i = 1
        if i < len(tokens) and tokens[i].startswith("/"):
            script = tokens[i].split(">")[0].split("|")[0].strip()
    return {"expr": expr, "cmd": cmd, "script": script}

def main():
    # Read user's actual crontab
    out = subprocess.run(["crontab", "-u", "reziux", "-l"], capture_output=True, text=True)
    if out.returncode != 0:
        print(f"failed to read crontab: {out.stderr}", file=sys.stderr); sys.exit(1)
    d = yaml_io.read(REG)
    existing_ids = {j["id"] for j in d["jobs"]}
    added = 0
    for raw_line in out.stdout.splitlines():
        parsed = parse_crontab_line(raw_line)
        if not parsed: continue
        # Build a stable id from the expr + script (or cmd if no script)
        basis = parsed["expr"] + "|" + (parsed["script"] or parsed["cmd"])
        new_id = "syscron-" + stable(basis)
        if new_id in existing_ids: continue
        job = {
            "id": new_id,
            "name": Path(parsed["script"]).name if parsed["script"] else parsed["cmd"][:40],
            "enabled": True,
            "schedule_expr": parsed["expr"],
            "schedule_kind": "cron",
            "tz": "Europe/London",
            "is_llm_turn": False,
            "owner_script": parsed["cmd"],
            "primary_executor": "system-cron",
            "backup_executor": "openclaw-cron",
            "fallback_strategy": "run-anyway",
            "discord_channel": None,
            "tags": ["system-cron", "live-crontab"],
            "source": "user.crontab",
            "last_status": None,
            "consecutive_errors": 0,
        }
        d["jobs"].append(job); added += 1
    d["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    yaml_io.write(REG, d)
    print(f"added {added} live system-cron jobs; total: {len(d['jobs'])}")
    return 0

if __name__ == "__main__":
    sys.exit(main())