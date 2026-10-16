#!/usr/bin/env python3
"""regen_wrap.py — Regenerate a single cronhub-managed wrap.sh.

Plan context (`docs/reorg/20-fix-broken-jobs-plan.md` Task 8):
Two cronhub wraps on disk were left over from the pre-envelope era and
emit no JSONL run record. Re-rendering them through
`CD/src/cronhub.py:render_wrap_sh()` (with the extracted
`_command_from_owner`) gives them an envelope block identical to the
36 other Tier-1 wraps, so the cronhub dashboard starts picking up
their runs.

Self-protection:
  * `owner_script` may resolve to a path inside `~/.openclaw/workspace`.
    If a typo (or a malicious edit) ever made `owner_script` equal
    the wrapper's own `--out` path, the regenerated wrap would call
    itself forever. We refuse that case loudly: exit 2, no write.

CLI:
    regen_wrap.py --job-id <id> --out <path> [--dry-run]
                  [--registry <path>]

Exit codes:
  0 — success (live write) OR dry-run printed a clean diff
  2 — refused: missing owner_script / log_file, unknown job id,
      or the command resolves to the `--out` path itself

Notes:
  * `PATH=/usr/bin:/bin` is enough — only stdlib + sibling `yaml_io`.
  * `--registry` lets tests inject a fixture. Default is the live
    `/mnt/data/cronhub/registry.yaml` (the operator is responsible for
    not running this against an in-flight registry window).
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path
from typing import Sequence

# Plan §Tech stack: "python stdlib + `CH/bin/yaml_io.py`". So we add the
# sibling bin/ first, then the absolute path to cronhub-dashboard's
# source dir to import render_wrap_sh + _command_from_owner.
sys.path.insert(0, "/mnt/data/cronhub/bin")
import yaml_io  # noqa: E402

sys.path.insert(0, "/home/reziux/cronhub-dashboard/src")
# Importing cronhub.py pulls in `import yaml` (PyYAML) — that's the
# CD-side reader; we only USE `render_wrap_sh` + `_command_from_owner`.
# We do NOT touch cronhub.load_registry; we read the registry ourselves
# via yaml_io to keep the contract on plan Tech stack.
import cronhub  # noqa: E402


DEFAULT_REGISTRY = Path("/mnt/data/cronhub/registry.yaml")

# Exit codes — keep narrow and grep-able.
RC_OK = 0
RC_REFUSED = 2


# ----- public API --------------------------------------------------------


def render_for(
    job_id: str,
    *,
    registry_jobs: Sequence[dict],
) -> tuple[str, str, str] | None:
    """Return (jobname, command, log_file) for a registry entry, else None.

    Returns None when the id is unknown or the entry is missing one of
    `owner_script` / `log_file` / parseable command. Caller passes the
    result to `rendered_body()` and decides what to do with the output.
    """
    for j in registry_jobs:
        if j.get("id") == job_id:
            owner = (j.get("owner_script") or "").strip()
            log_file = (j.get("log_file") or "").strip()
            if not owner or not log_file:
                return None
            cmd = cronhub._command_from_owner(owner)
            if not cmd:
                return None
            jobname = (j.get("name") or job_id).strip()
            return (jobname, cmd, log_file)
    return None


def rendered_body(job_id: str, *, registry_jobs: Sequence[dict]) -> str | None:
    """Render the wrap body via cronhub.render_wrap_sh(); None on refusal.

    The body matches every other Tier-1 wrap on disk (envelope block,
    bracket markers, mode-755 delivery, etc.).
    """
    prepared = render_for(job_id, registry_jobs=registry_jobs)
    if prepared is None:
        return None
    jobname, command, log_file = prepared
    return cronhub.render_wrap_sh(
        jobname, command, log_file,
        job_id=job_id, tier1=True,
    )


def _out_path_is_in_command(cmd: str, out_path: str) -> bool:
    """True iff the rendered command string contains `--out`'s path.

    A regenerate whose inner command invokes its own wrapper path would
    loop forever under cron. Bare `os.path.realpath` comparison would
    miss symlink differences, but the plan keeps the check simple — any
    match in the literal command is enough to halt.
    """
    return out_path in cmd


# ----- entry point -------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="regen_wrap",
        description=(
            "Re-render a single cronhub-managed wrap.sh from the registry. "
            "Use --dry-run to see the unified diff first."
        ),
    )
    p.add_argument("--job-id", required=True,
                   help="Registry id (the `id:` of a registry entry)")
    p.add_argument("--out", required=True,
                   help="Path of the wrapper to (re)write")
    p.add_argument("--registry", default=str(DEFAULT_REGISTRY),
                   help="Path to registry.yaml (default: %(default)s)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print unified diff against the existing file; never write")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    args = _build_argparser().parse_args(list(argv) if argv is not None else None)

    # 1. Load registry via yaml_io (plan §Tech stack).
    try:
        reg_doc = yaml_io.read(Path(args.registry))
    except Exception as exc:
        print(f"[regen_wrap] failed to read {args.registry}: {exc}", file=sys.stderr)
        return 2

    registry_jobs = reg_doc.get("jobs", []) or []

    # 2. Pre-flight: refuse if owner_script/log_file missing OR unknown id.
    body = rendered_body(args.job_id, registry_jobs=registry_jobs)
    if body is None:
        print(
            f"[regen_wrap] refused: missing owner_script/log_file or unknown "
            f"job id {args.job_id!r} (registry={args.registry})",
            file=sys.stderr,
        )
        return RC_REFUSED

    # 3. Self-reference guard: prevent a wrap whose inner command
    #    invokes itself.
    prepared = render_for(args.job_id, registry_jobs=registry_jobs)
    assert prepared is not None  # already checked above
    _jobname, command, _log = prepared
    if _out_path_is_in_command(command, args.out):
        print(
            f"[regen_wrap] refused: --out {args.out} is referenced by the "
            f"regenerated command {command!r} for job {args.job_id!r} "
            f"(would self-invoke under cron)",
            file=sys.stderr,
        )
        return RC_REFUSED

    # 4. Dry-run: print a unified diff against the existing file (if any).
    out_path = Path(args.out)
    if args.dry_run:
        existing = out_path.read_text() if out_path.exists() else ""
        diff = difflib.unified_diff(
            existing.splitlines(keepends=False),
            body.splitlines(keepends=False),
            fromfile=f"{out_path} (existing)",
            tofile=f"{out_path} (rendered)",
            n=3,
        )
        sys.stdout.write("\n".join(diff) + ("\n" if diff else ""))
        return RC_OK

    # 5. Live write: atomic-ish — write to a sibling tmp, rename.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    try:
        tmp.write_text(body)
        os.replace(tmp, out_path)
    except Exception as exc:
        try:
            tmp.unlink()
        except Exception:
            pass
        print(f"[regen_wrap] write failed: {exc}", file=sys.stderr)
        return RC_REFUSED
    os.chmod(out_path, 0o755)
    return RC_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
