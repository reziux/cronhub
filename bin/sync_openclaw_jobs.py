#!/usr/bin/env python3
"""sync_openclaw_jobs.py — keep the OpenClaw cron table consistent with the
cronhub registry.yaml.

Read-only inspection + disable-only mutations, all routed through the
`openclaw` CLI. We never hand-edit `~/.openclaw/cron/jobs.json` or
`openclaw.sqlite` here.

Decisions per OpenClaw job, in this order:

  1. OC job id is not in the registry         → orphan   (no mutation)
  2. registry enabled=false AND OC enabled   → disable  (run `cron disable`)
  3. registry primary_executor != "openclaw-cron" AND OC enabled → disable
  4. registry enabled, openclaw-cron, OC disabled → drift   (no mutation)
  5. otherwise                                → noop    (no mutation)

We deliberately do NOT call `cron enable`. Drift is informational: the
operator can flip it once they have decided the registry is the canonical
"on" state again. This keeps the sync script's blast radius small.

A snapshot of the live table is written on every successful run (including
dry-run) so downstream observers (auditor, inventory-refresh) can diff the
table without re-invoking the CLI.

Usage:
    sync_openclaw_jobs.py [--dry-run]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Make yaml_io importable when called from cron (PATH=/usr/bin:/bin).
sys.path.insert(0, "/mnt/data/cronhub/bin")
import yaml_io  # noqa: E402

# ----- constants ---------------------------------------------------------

OPENCLAW_BIN = "/home/reziux/.local/bin/openclaw"
SNAPSHOT = Path("/mnt/data/cronhub/state/openclaw-jobs.snapshot.json")
REGISTRY = Path("/mnt/data/cronhub/registry.yaml")

LIST_TIMEOUT_S = 180
DISABLE_TIMEOUT_S = 60


# ----- exceptions --------------------------------------------------------


class SyncError(RuntimeError):
    """Raised when the openclaw CLI fails or returns unparseable output."""


# ----- helpers -----------------------------------------------------------


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_capture(runner: Callable[..., Any], cmd: list[str], *, timeout: int) -> Any:
    """Invoke `runner` (defaults to subprocess.run) with capture_output.

    Returns the CompletedProcess. Centralised so tests can stub `runner`.
    """
    return runner(cmd, capture_output=True, text=True, timeout=timeout, env=_openclaw_env())


# The openclaw CLI needs node >= 24 (node:sqlite). Under cron PATH=/usr/bin:/bin it
# would resolve /usr/bin/node v20 and exit 1, so brew's node goes first on PATH.
BREW_BIN = "/home/linuxbrew/.linuxbrew/bin"


def _openclaw_env() -> dict:
    env = dict(os.environ)
    env.setdefault("HOME", "/home/reziux")
    env["PATH"] = BREW_BIN + ":" + env.get("PATH", "/usr/bin:/bin")
    return env


# ----- public API --------------------------------------------------------


def load_openclaw_jobs(runner: Callable[..., Any] = subprocess.run) -> list[dict]:
    """Return the live list of OpenClaw cron jobs.

    Runs `[OPENCLAW_BIN, "cron", "list", "--all", "--json"]` with a 180-second
    timeout and returns the `jobs` list. Raises SyncError on non-zero exit,
    timeout, or unparseable JSON.
    """
    cmd = [OPENCLAW_BIN, "cron", "list", "--all", "--json"]
    try:
        cp = _run_capture(runner, cmd, timeout=LIST_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise SyncError(
            f"{OPENCLAW_BIN} cron list --all --json timed out after "
            f"{LIST_TIMEOUT_S}s: {exc}"
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise SyncError(f"{OPENCLAW_BIN} cron list failed to launch: {exc}") from exc

    if cp.returncode != 0:
        raise SyncError(
            f"{OPENCLAW_BIN} cron list --all --json exited rc={cp.returncode}: "
            f"{(cp.stderr or '').strip()}"
        )
    try:
        data = json.loads(cp.stdout)
        jobs = data["jobs"]
    except Exception as exc:
        raise SyncError(
            f"{OPENCLAW_BIN} cron list --all --json returned unparseable JSON: {exc}"
        ) from exc
    if not isinstance(jobs, list):
        raise SyncError(
            f"{OPENCLAW_BIN} cron list --all --json: 'jobs' is not a list"
        )
    return jobs


def plan_actions(registry_jobs: list[dict], oc_jobs: list[dict]) -> list[dict]:
    """Decide what to do with each OpenClaw job. Pure function: no I/O.

    Returns a list of action dicts:
        {"id": str, "name": str, "action": "disable"|"drift"|"orphan"|"noop",
         "reason": str}

    Rules, evaluated in order:
      1. OC job id is not in the registry       → orphan
      2. registry enabled=false AND OC enabled  → disable
      3. registry primary_executor != "openclaw-cron" AND OC enabled → disable
      4. registry enabled, openclaw-cron, OC disabled → drift
      5. otherwise                              → noop
    """
    reg_by_id = {j["id"]: j for j in registry_jobs}
    actions: list[dict] = []
    for oc in oc_jobs:
        oid = oc.get("id")
        oname = oc.get("name", "")
        oc_enabled = bool(oc.get("enabled"))
        rj = reg_by_id.get(oid)

        if rj is None:
            actions.append({
                "id": oid,
                "name": oname,
                "action": "orphan",
                "reason": "OC job not present in registry.yaml",
            })
            continue

        reg_enabled = bool(rj.get("enabled"))
        executor = rj.get("primary_executor", "")

        if (not reg_enabled) and oc_enabled:
            actions.append({
                "id": oid,
                "name": oname,
                "action": "disable",
                "reason": f"registry: enabled=false (primary_executor={executor})",
            })
        elif executor != "openclaw-cron" and oc_enabled:
            actions.append({
                "id": oid,
                "name": oname,
                "action": "disable",
                "reason": (
                    f"registry: primary_executor={executor}, not openclaw-cron"
                ),
            })
        elif reg_enabled and executor == "openclaw-cron" and (not oc_enabled):
            actions.append({
                "id": oid,
                "name": oname,
                "action": "drift",
                "reason": "registry says enabled/openclaw-cron but OC is disabled",
            })
        else:
            actions.append({
                "id": oid,
                "name": oname,
                "action": "noop",
                "reason": "registry and OC agree",
            })
    return actions


def apply_actions(actions: list[dict], runner: Callable[..., Any] = subprocess.run) -> int:
    """Execute the actionable subset: `disable`.

    Other action types (drift, orphan, noop) are informational and do NOT
    produce a CLI call — especially never `cron enable`. Returns the number
    of disables performed. Raises SyncError on any non-zero exit code.
    """
    n = 0
    for a in actions:
        if a.get("action") != "disable":
            continue
        oid = a.get("id")
        if not oid:
            continue
        cmd = [OPENCLAW_BIN, "cron", "disable", oid]
        try:
            cp = _run_capture(runner, cmd, timeout=DISABLE_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise SyncError(
                f"{OPENCLAW_BIN} cron disable {oid} timed out after "
                f"{DISABLE_TIMEOUT_S}s: {exc}"
            ) from exc
        if cp.returncode != 0:
            raise SyncError(
                f"{OPENCLAW_BIN} cron disable {oid} exited rc={cp.returncode}: "
                f"{(cp.stderr or cp.stdout or '').strip()}"
            )
        n += 1
    return n


def write_snapshot(oc_jobs: list[dict], path: Path | None = None) -> None:
    """Persist `{"synced_at": "<UTC Z>", "jobs": [...]}` to `path` atomically.

    If `path` is None, the module-level SNAPSHOT constant is used (resolved
    at call time so tests can patch `sync_openclaw_jobs.SNAPSHOT`).

    Atomic means: write to `path.tmp` first, then `os.replace` it. The tmp
    file lives next to the target so it stays on the same filesystem.
    """
    if path is None:
        path = SNAPSHOT
    payload = {"synced_at": _now_utc_iso(), "jobs": oc_jobs}
    tmp = Path(str(path) + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=False))
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


# ----- entry point -------------------------------------------------------


def main(
    argv: list[str],
    *,
    registry_jobs: list[dict] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    """Run the sync.

    Args:
        argv: argument list (without program name). Recognised:
            --dry-run   print non-noop actions but disable nothing.
        registry_jobs: optional pre-loaded registry jobs. If None, the
            registry.yaml on disk is read via yaml_io.read.
        runner: optional `subprocess.run`-shaped callable for tests.

    Returns 0 on success, 1 on SyncError. The snapshot is written only after
    a successful load (in both --dry-run and live mode).
    """
    dry_run = "--dry-run" in argv

    # Load registry (allow caller to pre-load it for tests).
    if registry_jobs is None:
        try:
            reg_doc = yaml_io.read(REGISTRY)
            registry_jobs = reg_doc.get("jobs", [])
        except Exception as exc:
            print(f"[sync_openclaw_jobs] failed to read {REGISTRY}: {exc}", file=sys.stderr)
            return 1

    # Load live OC table — failure here aborts before snapshotting.
    try:
        oc_jobs = load_openclaw_jobs(runner=runner)
    except SyncError as exc:
        print(f"[sync_openclaw_jobs] {exc}", file=sys.stderr)
        return 1

    # Plan, then write snapshot regardless of dry-run.
    actions = plan_actions(registry_jobs, oc_jobs)
    try:
        write_snapshot(oc_jobs)
    except Exception as exc:
        print(f"[sync_openclaw_jobs] snapshot write failed: {exc}", file=sys.stderr)
        return 1

    # Print action lines for non-noop entries.
    for a in actions:
        if a["action"] == "noop":
            continue
        print(f"{a['action'].upper():7s} {a['id']} {a['name']} — {a['reason']}")

    # Apply (or just count, in dry-run).
    if dry_run:
        n_disabled = 0
        n_applied = sum(1 for a in actions if a["action"] == "disable")
        label = "would-disable"
    else:
        try:
            n_disabled = apply_actions(actions, runner=runner)
        except SyncError as exc:
            print(f"[sync_openclaw_jobs] {exc}", file=sys.stderr)
            return 1
        n_applied = n_disabled
        label = "disabled"

    n_drift = sum(1 for a in actions if a["action"] == "drift")
    n_orphan = sum(1 for a in actions if a["action"] == "orphan")
    n_total = len(actions)

    if dry_run:
        # Final line in dry-run uses "would-disable" label.
        print(f"synced {n_total} jobs: {n_applied} {label}, {n_drift} drift, {n_orphan} orphan")
    else:
        print(f"synced {n_total} jobs: {n_disabled} {label}, {n_drift} drift, {n_orphan} orphan")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))