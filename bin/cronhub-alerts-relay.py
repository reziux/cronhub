#!/usr/bin/env python3
"""cronhub-alerts-relay — tails alerts/failures.jsonl and POSTs to Discord.

Honours the README contract:
  max 1 Discord post per job per 6 hours
  max 3 Discord posts per job per 24 hours

State is held in alerts/state/posted.json — survives restarts, rate-limited
across the full 24h window.

Webhook discovery (operator-configured — see `SETUP` block):
  - env CRONHUB_ALERTS_WEBHOOK  (preferred)
  - $CRONHUB/config/alerts.json  {"webhook_url": "..."}
  - dry-run mode if neither set: write the payload to alerts/posted-dryrun.jsonl
    instead of POSTing. The operator sees the would-be post for verification.

Exit code: 0 on success (or no work), 1 on error.
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

CRONHUB = Path(os.environ.get("CRONHUB", "/mnt/data/cronhub"))
FAILURES = CRONHUB / "alerts" / "failures.jsonl"
STATE_DIR = CRONHUB / "alerts" / "state"
POST_LOG = STATE_DIR / "posted.json"
DRYRUN_LOG = CRONHUB / "alerts" / "posted-dryrun.jsonl"
CONFIG_PATH = CRONHUB / "config" / "alerts.json"

# Tuning knobs
PER_JOB_WINDOW = timedelta(hours=6)
PER_DAY_WINDOW = timedelta(hours=24)
MAX_PER_WINDOW = 1
MAX_PER_DAY = 3

# Required because the cross-platform assets shouldn't accumulate dryrun
# payloads indefinitely. Once an operator sees the relay works, they can
# delete `posted-dryrun.jsonl` and wire the webhook.
STATE_DIR.mkdir(parents=True, exist_ok=True)
FAILURES.parent.mkdir(parents=True, exist_ok=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_state() -> dict:
    if not POST_LOG.exists():
        return {"posts": []}
    try:
        return json.loads(POST_LOG.read_text())
    except Exception:
        return {"posts": []}


def _save_state(state: dict) -> None:
    POST_LOG.write_text(json.dumps(state, indent=2))


def _recent_for_job(state: dict, job_id: str, now: datetime) -> list[dict]:
    out = []
    for p in state.get("posts", []):
        try:
            ts = datetime.fromisoformat(p["ts"].replace("Z", "+00:00"))
        except Exception:
            continue
        if p["job_id"] != job_id:
            continue
        if now - ts <= PER_DAY_WINDOW:
            out.append(p)
    return out


def _within_window(posts: list[dict], now: datetime) -> bool:
    fresh = [p for p in posts if now - datetime.fromisoformat(p["ts"].replace("Z", "+00:00")) <= PER_JOB_WINDOW]
    return len(fresh) >= MAX_PER_WINDOW


def _over_day_cap(posts: list[dict]) -> bool:
    return len(posts) >= MAX_PER_DAY


def _resolve_webhook() -> str | None:
    env = os.environ.get("CRONHUB_ALERTS_WEBHOOK")
    if env:
        return env
    if CONFIG_PATH.exists():
        try:
            d = json.loads(CONFIG_PATH.read_text())
            url = d.get("webhook_url")
            if url:
                return url
        except Exception:
            return None
    return None


def _format_message(rec: dict) -> dict:
    """Build a Discord webhook payload from a failure record."""
    job = rec.get("name") or rec.get("id", "?")
    err = (rec.get("error") or "(no error message)")[:500]
    ts = rec.get("ts", _now_iso())[:19].replace("T", " ")
    title = f"⚠️ cronhub failure: {job}"
    desc = (
        f"**Job:** `{rec.get('id', '?')[:14]}` ({rec.get('name','?')})\n"
        f"**When:** {ts} UTC\n"
        f"**Scheduler:** {rec.get('scheduler','?')}\n"
        f"**Exit:** {rec.get('exit_code', '?')}\n"
        f"**Duration:** {rec.get('duration_s', '?')}s\n"
        f"**Error:** `{err}`"
    )
    return {
        "username": "cronhub",
        "embeds": [{"title": title, "description": desc, "color": 0xC0392B}],
    }


def _post_to_discord(webhook_url: str, payload: dict) -> bool:
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "cronhub-relay/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"alerts-relay: post failed: {e}", file=__import__("sys").stderr)
        return False


def _append_dryrun(payload: dict, rec: dict) -> None:
    DRYRUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DRYRUN_LOG.open("a") as f:
        f.write(json.dumps({"ts": _now_iso(), "rec": rec, "payload": payload}) + "\n")


def _audit_event(rec: dict, action: str) -> None:
    AUDITS = CRONHUB / "audits" / "audits.jsonl"
    AUDITS.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": _now_iso(), "kind": "alerts-relay", "action": action, "job_id": rec.get("id"), "operator": os.environ.get("USER", "unknown")}
    with AUDITS.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def main() -> int:
    if not FAILURES.exists():
        return 0

    webhook = _resolve_webhook()
    dry_run = webhook is None
    state = _load_state()
    now = _now()
    posted_new = 0
    skipped_rate = 0
    failed_post = 0

    # Tail FAILURES: read all lines, dedupe by id+ts, process unposted ones.
    seen_in_run: set[tuple[str, str]] = set()
    for line in FAILURES.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("status") != "error":
            continue
        key = (rec.get("id", "?"), rec.get("ts", "?"))
        if key in seen_in_run:
            continue
        seen_in_run.add(key)

        # Skip records already known to be posted
        if any(p.get("key") == list(key) for p in state.get("posts", [])):
            continue

        job_id = rec.get("id", "?")
        posts = _recent_for_job(state, job_id, now)
        if _within_window(posts, now):
            skipped_rate += 1
            continue
        if _over_day_cap(posts):
            skipped_rate += 1
            continue

        payload = _format_message(rec)
        ok = True
        if dry_run:
            _append_dryrun(payload, rec)
        else:
            ok = _post_to_discord(webhook, payload)
            if not ok:
                failed_post += 1
                continue

        state.setdefault("posts", []).append({
            "ts": _now_iso(),
            "job_id": job_id,
            "key": list(key),
            "dry_run": dry_run,
        })
        posted_new += 1
        _audit_event(rec, "posted" if not dry_run else "dryrun")

    _save_state(state)
    print(
        f"alerts-relay: posted={posted_new} rate_limited={skipped_rate} "
        f"failed_post={failed_post} dry_run={dry_run}"
    )
    return 0 if failed_post == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
