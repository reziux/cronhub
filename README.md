# cronhub — single source of truth for cron scheduling

This directory is the **only** authoritative record of cron jobs for both
OpenClaw (`~/.openclaw/`) and Hermes (`~/.hermes/`). Both schedulers derive
their internal job lists from `registry.yaml` on boot and on heartbeat.

## Layout

```
registry.yaml            # The single source of truth (~46 jobs)
runs/                    # Unified, append-only run history (jsonl per day)
locks/                   # Advisory flock per job
claims/                  # active-scheduler heartbeat
alerts/                  # rate-limited failure sink (jsonl)
status/                  # materialized views (by-job.md, upcoming.md)
state/                   # last-known per-job state (one .json per job)
bin/                     # cronctl, arbiter, wrap.sh, record_run.py
observers/               # generated observer-job templates
```

## Rules (binding for both agents)

1. **Active scheduler wins.** Only the scheduler named in
   `claims/active-scheduler.json` may fire LLM-driven jobs. The other
   scheduler only fires jobs tagged `fallback_strategy: run-anyway`
   (monitoring, backups, observers).
2. **Primary executor wins.** Each job has a `primary_executor`. Even if the
   active scheduler is the backup side, if the job's primary is offline for
   >5 minutes the backup may fire it (with a heartbeat-stale flag in run
   log).
3. **Every run writes a jsonl record** to `runs/YYYY-MM-DD.jsonl` via
   `bin/wrap.sh`. Skipping the wrapper = silent failure = doctor's
   `unsanctioned-write` finding.
4. **Failures hit the sink.** Jobs whose run status is `error` must append
   to `alerts/failures.jsonl`. Rate limiting: max 1 Discord post per job
   per 6 hours, max 3 per job per day.
5. **Registry writes are file-locked.** The cronhub daemon (or cronctl)
   holds `locks/registry.lock` for the duration of any write.
6. **Symlink:** `~/.cronhub` → `/mnt/data/cronhub`. Both names work; pick
   one and stick with it per session.

## Tools

- `cronctl ls|show|enable|disable|pause|run|runs|upcoming|fail|ack|switch|migrate|doctor`
- `bin/arbiter.sh` — runs from system cron @ `* * * * *`, writes
  `claims/active-scheduler.json` and posts failover notices.
- `bin/wrap.sh <job_id> -- <command...>` — wraps any command with run
  recording, alerting, and timeout.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Jobs fire but no runs/*.jsonl | wrapper bypassed | rebuild cron payload to invoke `wrap.sh` |
| Same job fires twice | both schedulers active | check `claims/active-scheduler.json` freshness; run `cronctl doctor` |
| OpenClaw jobs all `error: auth` | Minimax M3 outage | wait + check circuit-breaker state in `state/` |
| Hermes cron jobs say `Unknown Channel` | `openclaw-bridge` skill missing | see Phase 2 docs — skill is at `~/.hermes/skills/software-development/openclaw-bridge` |

## Migration from old locations

- `~/.hermes/cron/output/<id>/` is **read-only** after cutover. New runs
  go to `runs/YYYY-MM-DD.jsonl`.
- `~/.openclaw/cron/jobs.json` is **derived state** — rewritable by
  OpenClaw gateway on heartbeat from `registry.yaml`.
- Existing per-scheduler output blobs are preserved for forensic queries.