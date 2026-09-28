#!/usr/bin/env python3
"""sync_hermes_jobs.py — keep ~/.hermes/cron/jobs.json consistent with the
registry. Updates each existing job's `prompt` so a Hermes agent waking on
that job knows to delegate execution to the cronhub hook via the local
`bluesky_monitor.sh` / similar script, but does NOT overwrite Hermes's own
`script` field (which Hermes invokes with the prompt as arg).

Strategy: update prompt + monitor_script only. Hermes's cron engine reads
script to determine what executable to run; if we replace it with the hook
itself, Hermes would invoke the hook with no args (or wrong args), so we
leave it alone and just inform the agent via the prompt.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
from datetime import datetime, timezone

HERMES_CRON = Path("/home/reziux/.hermes/cron/jobs.json")
CRONHUB = Path("/mnt/data/cronhub")
REGISTRY = CRONHUB / "registry.yaml"

sys.path.insert(0, str(CRONHUB / "bin"))
import yaml_io

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def main() -> int:
    reg = yaml_io.read(REGISTRY)
    he_data = json.loads(HERMES_CRON.read_text())
    reg_by_id = {j["id"]: j for j in reg["jobs"]}
    n_changed = n_skipped = n_enabled = 0
    for job in he_data["jobs"]:
        rid = job["id"]
        rj = reg_by_id.get(rid)
        if not rj:
            continue
        if not rj["enabled"]:
            new_text = f"[cronhub] disabled — see registry.yaml ({rid})"
            n_skipped += 1
        elif rj["primary_executor"] != "hermes-cron":
            new_text = f"[cronhub] primary executor is {rj['primary_executor']}, not hermes-cron — skipping"
            n_skipped += 1
        else:
            new_text = (
                f"[cronhub] enabled for {rid}.\n"
                f"Schedule: {rj['schedule_expr']}.\n"
                f"This job is intended to be invoked via the cronhub fire hook. "
                f"If you are an LLM agent receiving this prompt, just acknowledge "
                f"with 'fire-and-forget' and let the cronhub hook handle execution. "
                f"The hook command is: bash /mnt/data/cronhub/bin/cronhub-fire.sh {rid}"
            )
            n_enabled += 1
        job["prompt"] = new_text
        # Update origin's last_flip_reason marker so we know we synced
        job.setdefault("meta", {})["cronhub_synced_at"] = now_iso()
        n_changed += 1
    he_data["_cronhub_synced_at"] = now_iso()
    he_data["_cronhub_registry_jobs"] = len(reg["jobs"])
    tmp = HERMES_CRON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(he_data, indent=2))
    tmp.replace(HERMES_CRON)
    print(f"synced {n_changed} hermes jobs: {n_enabled} enabled, {n_skipped} skipped")
    return 0

if __name__ == "__main__":
    sys.exit(main())