#!/usr/bin/env python3
"""Tests for regen_wrap.py — Plan Task 8.

Behaviour pinned by this suite (all from
`docs/reorg/20-fix-broken-jobs-plan.md` Task 8):

  * Self-reference: when the registry's `owner_script` IS the requested
    `--out` path, the wrapper would call itself forever. regen_wrap
    must exit 2 and write nothing.
  * Missing `log_file`: the registry entry is incomplete; exit 2, no write.
  * Render envelope: a normal entry renders a wrap whose body contains
    `_emit_envelope.sh` and the registry id.

Run (under cron-style PATH, the way `bin/run_all_tests.sh` invokes it):
    env -i PATH=/usr/bin:/bin python3 -m unittest /mnt/data/cronhub/bin/test_regen_wrap.py -v
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


# The fix plan puts the script at /mnt/data/cronhub/bin/regen_wrap.py.
# That is CH/bin in plan parlance.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# yaml_io lives next to this test; the script under test must also be
# able to import it without surprises when invoked from a fresh PATH.
import yaml_io  # noqa: E402


JOB_ID = "syscron-6424ffeb"           # BLADE Ingest (Umbrella)
JOB_NAME = "BLADE Ingest (Umbrella)"
JOB_OWNER = "/home/reziux/.openclaw/workspace/scripts/blade-ingest-all.sh"
JOB_LOG = "/home/reziux/.openclaw/workspace/logs/blade-ingest-all.log"


def _write_registry(tmpdir: Path, jobs: list[dict]) -> Path:
    """Drop a yaml_io-compatible registry.yaml in tmpdir and return its path."""
    path = tmpdir / "registry.yaml"
    yaml_io.write(path, {"version": 1, "jobs": jobs})
    return path


def _make_tmp_root() -> Path:
    """Fresh tempdir shaped like /mnt/data/cronhub — registry at root."""
    tmp = Path(tempfile.mkdtemp(prefix="regen-wrap-"))
    return tmp


class TestRefusesSelfReference(unittest.TestCase):
    """Self-reference: owner_script == --out path. Must exit 2, no write."""

    def test_dry_run_exits_2_and_does_not_modify_out(self):
        from regen_wrap import main  # type: ignore

        tmp = _make_tmp_root()
        out_path = tmp / "blade-ingest-umbrella-syscron-.wrap.sh"
        # Pre-seed the file with old content. regen must NOT clobber this.
        original = "#!/usr/bin/env bash\n# OLD CONTENTS - must be preserved\n"
        out_path.write_text(original)

        # Build a registry whose owner_script *is* the requested out path.
        # This is the dangerous case: re-generating the wrap would yield a
        # wrap whose inner command invokes itself ad infinitum.
        reg = _write_registry(tmp, [
            {
                "id": JOB_ID,
                "name": JOB_NAME,
                "owner_script": str(out_path),
                "log_file": JOB_LOG,
            },
        ])

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = main(
                ["--job-id", JOB_ID, "--out", str(out_path),
                 "--registry", str(reg), "--dry-run"],
            )
        self.assertEqual(rc, 2,
                         f"expected refusal rc=2, got rc={rc}; stderr={stderr.getvalue()}")
        # Original file content must be untouched.
        self.assertEqual(out_path.read_text(), original,
                         "self-reference refusal must not modify the out file")

    def test_no_dry_run_also_exits_2_and_does_not_modify_out(self):
        """Without --dry-run the same refusal must hold (no live write either)."""
        from regen_wrap import main  # type: ignore

        tmp = _make_tmp_root()
        out_path = tmp / "minimax-m31-monitor-syscron-.wrap.sh"
        original = "#!/usr/bin/env bash\n# OLD CONTENTS - must be preserved\n"
        out_path.write_text(original)

        reg = _write_registry(tmp, [
            {
                "id": "syscron-minimax-m31",
                "name": "MiniMax M3.1 Monitor",
                "owner_script": str(out_path),
                "log_file": "/home/reziux/.openclaw/workspace/data/minimax-monitor/logs/cron.log",
            },
        ])

        rc = main(
            ["--job-id", "syscron-minimax-m31", "--out", str(out_path),
             "--registry", str(reg)],
        )
        self.assertEqual(rc, 2, "live mode must also refuse self-reference")
        self.assertEqual(out_path.read_text(), original,
                         "self-reference refusal must not modify the out file")


class TestRefusesMissingFields(unittest.TestCase):
    """Missing owner_script or log_file: exit 2, no write."""

    def test_missing_log_file_exits_2(self):
        from regen_wrap import main  # type: ignore

        tmp = _make_tmp_root()
        out_path = tmp / "somejob-syscron-.wrap.sh"
        original = "#!/usr/bin/env bash\n# OLD CONTENTS\n"
        out_path.write_text(original)

        reg = _write_registry(tmp, [
            {
                "id": JOB_ID,
                "name": JOB_NAME,
                "owner_script": JOB_OWNER,
                # log_file intentionally omitted.
            },
        ])

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = main(
                ["--job-id", JOB_ID, "--out", str(out_path),
                 "--registry", str(reg), "--dry-run"],
            )
        self.assertEqual(rc, 2, "missing log_file must exit 2")
        self.assertEqual(out_path.read_text(), original,
                         "missing log_file must not modify out")

    def test_missing_owner_script_exits_2(self):
        from regen_wrap import main  # type: ignore

        tmp = _make_tmp_root()
        out_path = tmp / "somejob-syscron-.wrap.sh"
        original = "#!/usr/bin/env bash\n# OLD CONTENTS\n"
        out_path.write_text(original)

        reg = _write_registry(tmp, [
            {
                "id": JOB_ID,
                "name": JOB_NAME,
                # owner_script intentionally omitted.
                "log_file": JOB_LOG,
            },
        ])

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = main(
                ["--job-id", JOB_ID, "--out", str(out_path),
                 "--registry", str(reg), "--dry-run"],
            )
        self.assertEqual(rc, 2, "missing owner_script must exit 2")
        self.assertEqual(out_path.read_text(), original,
                         "missing owner_script must not modify out")


class TestRendersEnvelope(unittest.TestCase):
    """Successful render: output contains the envelope helper and the job id."""

    def test_renders_envelope_block_with_job_id(self):
        from regen_wrap import main  # type: ignore

        tmp = _make_tmp_root()
        out_path = tmp / "blade-ingest-umbrella-syscron-.wrap.sh"
        # Pre-existing file (simulating the envelope-less legacy wrap).
        out_path.write_text("#!/usr/bin/env bash\n# LEGACY ENVELOPE-LESS\n")

        reg = _write_registry(tmp, [
            {
                "id": JOB_ID,
                "name": JOB_NAME,
                "owner_script": JOB_OWNER,
                "log_file": JOB_LOG,
            },
        ])

        # Live render (no --dry-run) so we can read the file back.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = main(
                ["--job-id", JOB_ID, "--out", str(out_path),
                 "--registry", str(reg)],
            )
        self.assertEqual(rc, 0, f"successful render should exit 0, got rc={rc}")

        body = out_path.read_text()
        self.assertIn("_emit_envelope.sh", body,
                      f"rendered wrap missing envelope helper; got:\n{body}")
        self.assertIn(JOB_ID, body,
                      f"rendered wrap missing job id; got:\n{body}")
        # Also: a start-epoch + end-epoch pair (the JSONL envelope uses them).
        self.assertIn("_start_epoch", body,
                      "rendered wrap missing _start_epoch (envelope stage 1)")
        self.assertIn("_end_epoch", body,
                      "rendered wrap missing _end_epoch")

    def test_dry_run_prints_diff_without_writing(self):
        """--dry-run must surface changes without touching the file."""
        from regen_wrap import main  # type: ignore

        tmp = _make_tmp_root()
        out_path = tmp / "minimax-m31-monitor-syscron-.wrap.sh"
        # Real-world legacy content (no envelope helper line).
        legacy = (
            "#!/usr/bin/env bash\n"
            "# Auto-generated by cronhub-dashboard wrapper pattern.\n"
            "set -uo pipefail\n"
            "mkdir -p /home/reziux/.openclaw/workspace/data/minimax-monitor/logs\n"
        )
        out_path.write_text(legacy)

        reg = _write_registry(tmp, [
            {
                "id": "syscron-minimax-m31",
                "name": "MiniMax M3.1 Monitor",
                "owner_script":
                    "/home/reziux/.openclaw/workspace/scripts/cron-minimax-m31-monitor.sh",
                "log_file":
                    "/home/reziux/.openclaw/workspace/data/minimax-monitor/logs/cron.log",
            },
        ])

        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            rc = main(
                ["--job-id", "syscron-minimax-m31", "--out", str(out_path),
                 "--registry", str(reg), "--dry-run"],
            )
        self.assertEqual(rc, 0, f"dry-run should exit 0; got rc={rc}")
        # The diff should mention the helper (it's the new bit) and the
        # other file content must be byte-identical.
        rendered = buf.getvalue()
        self.assertIn("_emit_envelope.sh", rendered,
                      f"dry-run output missing envelope helper; got:\n{rendered}")
        self.assertEqual(out_path.read_text(), legacy,
                         "dry-run must not modify the out file")
        # The file is unchanged.
        self.assertFalse("_emit_envelope.sh" in out_path.read_text(),
                         "dry-run should not have added the envelope helper to the file")


class TestWritesExecutableFile(unittest.TestCase):
    """Live (non-dry-run) write must chmod 755."""

    def test_live_write_modes_file_755(self):
        from regen_wrap import main  # type: ignore

        tmp = _make_tmp_root()
        out_path = tmp / "blade-ingest-umbrella-syscron-.wrap.sh"

        reg = _write_registry(tmp, [
            {
                "id": JOB_ID,
                "name": JOB_NAME,
                "owner_script": JOB_OWNER,
                "log_file": JOB_LOG,
            },
        ])

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = main(
                ["--job-id", JOB_ID, "--out", str(out_path),
                 "--registry", str(reg)],
            )
        self.assertEqual(rc, 0)
        mode = out_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o755,
                         f"live write must chmod 755; got {oct(mode)}")


class TestUnknownJobId(unittest.TestCase):
    """An unknown --job-id must exit 2, never silently render nothing."""

    def test_unknown_id_exits_2(self):
        from regen_wrap import main  # type: ignore

        tmp = _make_tmp_root()
        out_path = tmp / "no-such-job-syscron-.wrap.sh"
        out_path.write_text("#!/usr/bin/env bash\n")

        reg = _write_registry(tmp, [
            {
                "id": JOB_ID,
                "name": JOB_NAME,
                "owner_script": JOB_OWNER,
                "log_file": JOB_LOG,
            },
        ])

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = main(
                ["--job-id", "no-such-job-id", "--out", str(out_path),
                 "--registry", str(reg), "--dry-run"],
            )
        self.assertEqual(rc, 2, "unknown job id must exit 2")


if __name__ == "__main__":
    unittest.main()
