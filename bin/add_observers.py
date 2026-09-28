#!/usr/bin/env python3
"""add_observers.py — add observer jobs to registry.yaml for high-value jobs
the user wants monitored. Each observer:
  - runs as a systemEvent in OpenClaw cron (low LLM cost)
  - reports liveness to Discord #alerts-system
  - schedules a few minutes after the target job's expected fire time

Per user decision: add observers for the BLADE suite (5 jobs) + the inventory
refresh + discord-system-report (already observer-like) + cronhub self-check.
"""
from __future__ import annotations
import json, sys, hashlib
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, '/mnt/data/cronhub/bin')
import yaml_io

REG = Path('/mnt/data/cronhub/registry.yaml')
WARN_CH = '1538617880072159373'  # #alerts-system

def stable(s): return hashlib.sha1(s.encode()).hexdigest()[:8]

OBSERVER_TEMPLATES = [
    {
        "name": "blade-greenhouse-observer",
        "watches": "BLADE Greenhouse",
        "schedule": "15 6,10,14,18 * * *",
        "tz": "UTC",
        "script": "/home/reziux/.openclaw/workspace/scripts/bluesky-cron.sh",  # placeholder, real script below
    },
    {
        "name": "blade-ashby-observer",
        "watches": "BLADE Ashby",
        "schedule": "15 8,14,20 * * *",
        "tz": "UTC",
    },
    {
        "name": "blade-himalayas-observer",
        "watches": "BLADE Himalayas",
        "schedule": "15 7,11,15,19 * * *",
        "tz": "UTC",
    },
    {
        "name": "blade-wttj-observer",
        "watches": "BLADE WTTJ Pipeline",
        "schedule": "15 9,15,21 * * *",
        "tz": "UTC",
    },
    {
        "name": "blade-workable-observer",
        "watches": "BLADE Workable",
        "schedule": "15 9 * * 1",
        "tz": "UTC",
    },
    {
        "name": "linkedin-blade-observer",
        "watches": "linkedin-blade-mode",
        "schedule": "15 9 * * *",
        "tz": "Europe/London",
    },
    {
        "name": "reed-blade-observer",
        "watches": "reed-blade-mode",
        "schedule": "15 8 * * *",
        "tz": "Europe/London",
    },
    {
        "name": "opus-degradation-observer",
        "watches": "opus-degradation-monitor",
        "schedule": "15 9 * * *",
        "tz": "UTC",
    },
    {
        "name": "inventory-refresh-observer",
        "watches": "inventory-refresh",
        "schedule": "15 */6 * * *",
        "tz": "UTC",
    },
    {
        "name": "system-report-observer",
        "watches": "System Report to #alerts",
        "schedule": "15 */4 * * *",
        "tz": "Europe/London",
    },
    {
        "name": "cronhub-self-check",
        "watches": "cronhub self",
        "schedule": "*/5 * * * *",
        "tz": "UTC",
        "script": "/mnt/data/cronhub/bin/cronctl doctor",
    },
]

def main():
    d = yaml_io.read(REG)
    existing_ids = {j["id"][:8] for j in d["jobs"]}
    existing_names = {j["name"] for j in d["jobs"]}
    added = 0
    for tpl in OBSERVER_TEMPLATES:
        if tpl["name"] in existing_names:
            continue
        # build a verifier command
        target = tpl["watches"]
        verifier = (
            f"Verifier for '{target}'. "
            f"Check that the most recent run for '{target}' in "
            f"/mnt/data/cronhub/runs/ is within the expected window. "
            f"If no run is present OR the last run is older than the schedule period, "
            f"post a single-line failure to Discord channel {WARN_CH} via the message tool. "
            f"Format: '⚠️ {target} did not run on schedule (last seen: <age>)'. "
            f"Do not post anything if the last run is fresh."
        )
        job = {
            "id": "obs-" + stable(tpl["name"]),
            "name": tpl["name"],
            "enabled": True,
            "schedule_expr": tpl["schedule"],
            "schedule_kind": "cron",
            "tz": tpl.get("tz", "UTC"),
            "is_llm_turn": True,
            "owner_script": tpl.get("script", ""),
            "primary_executor": "openclaw-cron",
            "backup_executor": "hermes-cron",
            "fallback_strategy": "run-anyway",
            "discord_channel": WARN_CH,
            "tags": ["observer"],
            "source": "cronhub.observers",
            "last_status": None,
            "consecutive_errors": 0,
            "watches": target,
            "verifier_prompt": verifier,
        }
        d["jobs"].append(job)
        added += 1
    d["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    yaml_io.write(REG, d)
    print(f"added {added} observers; total jobs now: {len(d['jobs'])}")
    return 0

if __name__ == "__main__":
    sys.exit(main())