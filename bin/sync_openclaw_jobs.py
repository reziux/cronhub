#!/usr/bin/env python3
"""sync_openclaw_jobs.py — mutate ~/.openclaw/cron/jobs.json so each
job's payload points at the cronhub hook instead of running inline.

For each job in registry.yaml with primary_executor=openclaw-cron and enabled:
  - Set payload.kind = "systemEvent"
  - Set payload.text = "Run via cronhub hook: bash /mnt/data/cronhub/bin/cronhub-fire.sh <id>"

For jobs whose primary_executor != openclaw-cron (e.g. hermes-cron):
  - Set payload.kind = "systemEvent"
  - Set payload.text = "Skipped: this job's primary executor is hermes-cron, see /mnt/data/cronhub/registry.yaml"
  - (OpenClaw gateway should short-circuit systemEvent with this exact text)

For jobs that are disabled:
  - Set payload.text = "Skipped: job disabled in cronhub registry"
  - effective = same as primary mismatch

Then we never need to touch jobs.json again — the cronhub hook handles routing.
"""
from __future__ import annotations
import json, sys, os
from pathlib import Path
from datetime import datetime, timezone

OPENCLAW_CRON = Path("/home/reziux/.openclaw/cron/jobs.json")
CRONHUB = Path("/mnt/data/cronhub")
REGISTRY = CRONHUB / "registry.yaml"

sys.path.insert(0, str(CRONHUB / "bin"))
import yaml_io

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _payload_for(job: dict, text: str) -> dict:
    """Isolated-session jobs must use agentTurn (message); main-session
    jobs use systemEvent (text). Mismatched kinds cause the gateway to
    skip the job with 'isolated job requires payload.kind=agentTurn'."""
    if job.get("sessionTarget") == "isolated":
        return {"kind": "agentTurn", "message": text}
    return {"kind": "systemEvent", "text": text}

def main() -> int:
    reg = yaml_io.read(REGISTRY)
    if not OPENCLAW_CRON.exists():
        # OpenClaw gateway is offline / hasn't written jobs.json yet. Skipping
        # is correct behavior — registry is the source of truth, sync is one-way.
        print(f"[sync_openclaw_jobs] {OPENCLAW_CRON} missing; openclaw gateway appears offline — skipping (registry remains canonical).")
        return 0
    try:
        oc_data = json.loads(OPENCLAW_CRON.read_text())
    except Exception as e:
        print(f"[sync_openclaw_jobs] failed to read {OPENCLAW_CRON}: {e}; skipping.")
        return 0
    # Build registry by id
    reg_by_id = {j["id"]: j for j in reg["jobs"]}
    n_changed = 0
    n_skipped = 0
    n_enabled = 0
    n_added = 0
    # Map existing openclaw jobs by id
    existing_oc = {j["id"]: j for j in oc_data["jobs"]}
    # For each registry job with primary_executor=openclaw-cron and enabled:
    #   if exists in oc -> update payload
    #   else -> add new
    # For registry jobs that are NOT primary openclaw-cron or disabled:
    #   ensure existing oc entry (if any) gets a "skip" payload
    # Step 1: upsert each registry job into oc_data
    for rj in reg["jobs"]:
        rid = rj["id"]
        if rj["primary_executor"] == "openclaw-cron" and rj["enabled"]:
            if rid in existing_oc:
                job = existing_oc[rid]
            else:
                # Brand new observer or new job — create a job entry
                job = {
                    "id": rid,
                    "agentId": "main",
                    "name": rj["name"],
                    "enabled": True,
                    "createdAtMs": int(datetime.now(timezone.utc).timestamp() * 1000),
                    "updatedAtMs": int(datetime.now(timezone.utc).timestamp() * 1000),
                    "sessionTarget": "isolated",
                    "wakeMode": "now",
                    "schedule": {
                        "kind": rj["schedule_kind"],
                        "expr": rj["schedule_expr"],
                        "tz": rj["tz"],
                    },
                }
                oc_data["jobs"].append(job)
                existing_oc[rid] = job
                n_added += 1
            # build hook payload
            if rj.get("verifier_prompt"):
                # Observer — has verifier instructions
                new_text = (
                    f"[cronhub] observer for {rj.get('watches','?')}.\n\n"
                    f"INSTRUCTIONS:\n{rj['verifier_prompt']}\n\n"
                    f"If you need to invoke the script: bash /mnt/data/cronhub/bin/cronhub-fire.sh {rid}"
                )
            else:
                new_text = (
                    f"[cronhub] enabled — invoking hook for {rid}.\n"
                    f"Run: bash /mnt/data/cronhub/bin/cronhub-fire.sh {rid}"
                )
            # OpenClaw's cron engine rejects systemEvent payloads for
            # isolated-session jobs ("isolated job requires
            # payload.kind=agentTurn") — match payload kind to sessionTarget.
            job["payload"] = _payload_for(job, new_text)
            job["enabled"] = True  # registry is source of truth
            n_enabled += 1
            n_changed += 1
        elif rid in existing_oc:
            job = existing_oc[rid]
            if not rj["enabled"]:
                job["payload"] = _payload_for(job, f"[cronhub] disabled — see registry.yaml ({rid})")
                job["enabled"] = False  # registry is source of truth
                n_skipped += 1
            else:
                job["payload"] = _payload_for(job, f"[cronhub] primary executor is {rj['primary_executor']}, not openclaw-cron — skipping")
                n_skipped += 1
            n_changed += 1
        # else: registry job not relevant to openclaw; skip silently
    # Step 2: any openclaw jobs not in registry -> mark orphan
    for job in oc_data["jobs"]:
        if job["id"] not in reg_by_id:
            job["payload"] = _payload_for(job, f"[cronhub] ORPHAN — job {job['id']} not in registry")
    # add metadata
    oc_data["_cronhub_synced_at"] = now_iso()
    oc_data["_cronhub_registry_jobs"] = len(reg["jobs"])
    # Write back atomically
    tmp = OPENCLAW_CRON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(oc_data, indent=2))
    tmp.replace(OPENCLAW_CRON)
    print(f"synced {n_changed} jobs: {n_enabled} enabled (hook), {n_skipped} skipped, {n_added} new (added)")
    return 0

if __name__ == "__main__":
    sys.exit(main())