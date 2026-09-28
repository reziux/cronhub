#!/usr/bin/env python3
"""Build registry.yaml from 4 sources:
  - ~/.openclaw/cron/jobs.json       (33 jobs)
  - ~/.hermes/cron/jobs.json          (2 jobs)
  - ~/.openclaw/crontab-backup        (1 job)
  - ~/.openclaw/cron-jobs/*           (2 jobs: medium-rss, lobsters-tracker)
  - ~/.openclaw/crons/*               (1 job: generate_collapses_log.sh + shadow-v3 scripts)

Output: /mnt/data/cronhub/registry.yaml
"""
from __future__ import annotations
import json, os, re, sys, hashlib
from pathlib import Path
from datetime import datetime, timezone

OPENCLAW_HOME = Path("/home/reziux/.openclaw")
HERMES_HOME   = Path("/home/reziux/.hermes")
CRONHUB       = Path("/mnt/data/cronhub")
WORKSPACE     = OPENCLAW_HOME / "workspace"
sys.path.insert(0, str(CRONHUB / "bin"))
import yaml_io

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def stable_id(*parts: str) -> str:
    h = hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]
    return f"{parts[0]}-{h}" if parts else h

def from_openclaw_cron_json() -> list[dict]:
    data = json.loads((OPENCLAW_HOME / "cron/jobs.json").read_text())
    out = []
    for j in data["jobs"]:
        sched = j.get("schedule", {})
        if sched.get("kind") == "cron":
            expr = sched.get("expr", "")
            sched_kind = "cron"
        elif sched.get("kind") == "every":
            ms = sched.get("everyMs", 0)
            expr = f"@every {ms}ms"
            sched_kind = "interval"
        else:
            expr = sched.get("expr", "")
            sched_kind = "cron"
        payload = j.get("payload", {})
        is_llm = payload.get("kind") == "agentTurn"
        owner_script = ""
        if is_llm:
            msg = payload.get("message") or payload.get("text") or ""
            m = re.search(r"(?:node|bash)\s+(/[^\s`'\"]+|~?/[^\s`'\"]+)", msg)
            if m:
                owner_script = m.group(1).replace("~", str(OPENCLAW_HOME))
            elif "openclaw-backup" in (j.get("name") or "").lower():
                owner_script = ""  # systemEvent handled by gateway hook
            else:
                owner_script = ""  # observer / wrapper-internal
        delivery = j.get("delivery") or {}
        discord = delivery.get("to") if delivery.get("mode") in ("announce","silent-announce") else None
        if discord and discord.startswith("channel:"):
            discord = discord.split(":",1)[1]
        out.append({
            "id": j["id"],
            "name": j["name"],
            "enabled": j.get("enabled", False),
            "schedule_expr": expr,
            "schedule_kind": sched_kind,
            "tz": sched.get("tz", "UTC"),
            "is_llm_turn": is_llm,
            "owner_script": owner_script,
            "primary_executor": "openclaw-cron",
            "backup_executor": "hermes-cron",
            "fallback_strategy": "skip",
            "discord_channel": discord,
            "tags": [],
            "source": "openclaw.cron.json",
            "last_status": j.get("state", {}).get("lastStatus"),
            "consecutive_errors": j.get("state", {}).get("consecutiveErrors", 0),
        })
    return out

def from_hermes_cron_json() -> list[dict]:
    data = json.loads((HERMES_HOME / "cron/jobs.json").read_text())
    out = []
    for j in data.get("jobs", []):
        sched = j.get("schedule", {})
        expr = sched.get("expr", "") if sched.get("kind") == "cron" else f"@every {sched.get('everyMs',0)}ms"
        is_llm = not j.get("script")  # prompt-based jobs are LLM
        prompt = j.get("prompt","")
        script = j.get("script")
        owner_script = script if script else ""
        out.append({
            "id": j["id"],
            "name": j["name"],
            "enabled": j.get("enabled", False),
            "schedule_expr": expr,
            "schedule_kind": "cron" if sched.get("kind") == "cron" else "interval",
            "tz": "Europe/London",
            "is_llm_turn": is_llm,
            "owner_script": owner_script,
            "primary_executor": "hermes-cron",
            "backup_executor": "openclaw-cron",
            "fallback_strategy": "skip",
            "discord_channel": (j.get("origin") or {}).get("chat_id"),
            "tags": ["hermes"],
            "source": "hermes.cron.json",
            "last_status": j.get("last_status"),
            "consecutive_errors": 0,
        })
    return out

def from_openclaw_crontab_backup() -> list[dict]:
    """Read crontab lines and convert each into a job entry.
    NOTE: crontab-backup is a historical snapshot (Mar 2026). Tag as
    'historical' so registry reflects reality: only openclaw.cron.json and
    hermes.cron.json are live."""
    f = OPENCLAW_HOME / "crontab-backup"
    if not f.exists():
        return []
    jobs = []
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        expr, cmd = " ".join(parts[:5]), parts[5]
        # extract the script path: skip /usr/bin/env, node, bash interpreters,
        # take the first token that exists on disk
        import os as _os
        tokens = cmd.split()
        script = ""
        skip = {"/usr/bin/env", "env", "node", "bash", "sh", "python3"}
        i = 0
        if tokens and tokens[0] in skip:
            i = 1
        if i < len(tokens) and tokens[i].startswith("/"):
            # it's an absolute path
            script = tokens[i]
        elif i < len(tokens):
            script = tokens[i]
        jobs.append({
            "id": stable_id("crontab", expr, cmd)[:8],
            "name": Path(script).name if script else "news-aggregator",
            "enabled": False,
            "schedule_expr": expr,
            "schedule_kind": "cron",
            "tz": "Europe/London",
            "is_llm_turn": False,
            "owner_script": script or cmd,
            "primary_executor": "system-cron",
            "backup_executor": "openclaw-cron",
            "fallback_strategy": "run-anyway",
            "discord_channel": None,
            "tags": ["system-cron", "openclaw-backup", "historical"],
            "source": "openclaw.crontab-backup",
            "last_status": None,
            "consecutive_errors": 0,
        })
    return jobs

def from_openclaw_cron_jobs_dir() -> list[dict]:
    """Parse ~/.openclaw/cron-jobs/*. These are formatted as
    'twig <cmd> > log 2>&1\\n<cron-expr>' per line, separated by blanks/comments."""
    out = []
    d = OPENCLAW_HOME / "cron-jobs"
    if not d.exists():
        return out
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        text = f.read_text()
        # Each block is: optional comment + cron-expr + command
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or line.startswith("#"):
                i += 1
                continue
            # could be 'twig <cmd> > log 2>&1' OR a bare cron expression
            if line.startswith("twig "):
                cmd = line[5:].split(">")[0].strip()
                if i + 1 < len(lines):
                    expr = lines[i+1].strip()
                else:
                    expr = ""
                i += 2
            else:
                # bare cron expr with no command (skip)
                i += 1
                continue
            if not expr or not cmd:
                continue
            out.append({
                "id": stable_id(f.stem, expr, cmd)[:8],
                "name": f"{f.stem}:{Path(cmd.split()[0]).name}",
                "enabled": False,
                "schedule_expr": expr,
                "schedule_kind": "cron",
                "tz": "Europe/London",
                "is_llm_turn": False,
                "owner_script": cmd,
                "primary_executor": "system-cron",
                "backup_executor": "openclaw-cron",
                "fallback_strategy": "run-anyway",
                "discord_channel": None,
                "tags": ["system-cron", f.stem, "historical"],
                "source": f"openclaw.cron-jobs/{f.name}",
                "last_status": None,
                "consecutive_errors": 0,
            })
    return out

def from_openclaw_crons_dir() -> list[dict]:
    """Hardcoded crons we know about from prior inventory."""
    out = []
    # generate_collapses_log.sh — appears in /home/reziux/.openclaw/crons/
    p = OPENCLAW_HOME / "crons/generate_collapses_log.sh"
    if p.exists():
        out.append({
            "id": stable_id("collapses-log", "30 4 * * *", str(p))[:8],
            "name": "generate_collapses_log.sh",
            "enabled": False,
            "schedule_expr": "30 4 * * *",
            "schedule_kind": "cron",
            "tz": "Europe/London",
            "is_llm_turn": False,
            "owner_script": str(p),
            "primary_executor": "system-cron",
            "backup_executor": "openclaw-cron",
            "fallback_strategy": "run-anyway",
            "discord_channel": None,
            "tags": ["system-cron", "collapse", "historical"],
            "source": "openclaw.crons/generate_collapses_log.sh",
            "last_status": None,
            "consecutive_errors": 0,
        })
    return out

def from_system_crontab_documented() -> list[dict]:
    """DEPRECATED. The live system cron is captured by add_live_system_cron.py,
    which queries `crontab -u reziux -l`. This stub returns an empty list and
    exists only so older callers don't break."""
    return []

def main():
    jobs = []
    jobs += from_openclaw_cron_json()
    jobs += from_hermes_cron_json()
    jobs += from_openclaw_crontab_backup()
    jobs += from_openclaw_cron_jobs_dir()
    jobs += from_openclaw_crons_dir()
    jobs += from_system_crontab_documented()
    # Dedupe by id
    seen = set()
    deduped = []
    for j in jobs:
        if j["id"] in seen:
            continue
        seen.add(j["id"])
        deduped.append(j)
    # Preserve existing live system-cron jobs (added by add_live_system_cron.py)
    target = CRONHUB / "registry.yaml"
    existing_by_id = {}
    if target.exists():
        try:
            existing = yaml_io.read(target)
            for j in existing.get("jobs", []):
                existing_by_id[j["id"]] = j
        except Exception as e:
            print(f"warn: could not read existing registry: {e}", file=sys.stderr)

    # For each deduped job, if we already have it in existing_by_id with a
    # non-empty owner_script, KEEP that owner_script. This is critical: after
    # sync_openclaw_jobs.py rewrites jobs.json payloads to invoke the hook,
    # the message no longer contains the original script path. The registry's
    # owner_script is the only source of truth.
    final = []
    for j in deduped:
        prev = existing_by_id.get(j["id"])
        if prev and prev.get("owner_script") and not j.get("owner_script"):
            j["owner_script"] = prev["owner_script"]
        # also preserve any added fields like 'watches', 'verifier_prompt', tags
        if prev:
            for k in ("watches", "verifier_prompt", "tags"):
                if prev.get(k) and not j.get(k):
                    j[k] = prev[k]
        final.append(j)

    # Add user.crontab entries from existing
    if existing_by_id:
        for j in existing_by_id.values():
            if j.get("source") == "user.crontab" and j["id"] not in seen:
                final.append(j); seen.add(j["id"])

    deduped = final
    doc = {
        "version": 1,
        "active_scheduler": "openclaw",
        "active_updated_at": now_iso(),
        "updated_at": now_iso(),
        "jobs": deduped,
    }
    yaml_io.write(target, doc)
    print(f"wrote {target} ({len(deduped)} jobs total)")
    by_exec = {}
    by_enabled = {}
    for j in deduped:
        by_exec[j['primary_executor']] = by_exec.get(j['primary_executor'], 0) + 1
        key = "live" if j["enabled"] else "disabled/historical"
        by_enabled[key] = by_enabled.get(key, 0) + 1
    print("primary_executor distribution:", by_exec)
    print("enabled/disabled:", by_enabled)

if __name__ == "__main__":
    main()