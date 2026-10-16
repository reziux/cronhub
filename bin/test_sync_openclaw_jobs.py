#!/usr/bin/env python3
"""Tests for sync_openclaw_jobs.py.

Run:
    cd /mnt/data/cronhub && /usr/bin/python3 -m unittest bin/test_sync_openclaw_jobs.py -v

The module is imported (not run) so we can exercise its public functions with
fake runners. Real cron state is never touched.
"""
from __future__ import annotations

import io
import os
import unittest.mock
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# Import the module under test. The fix plan puts the file at
# `bin/sync_openclaw_jobs.py`; the task description's `CH/bin/...` path is
# just shorthand for the cronhub workdir (`/mnt/data/cronhub`).
sys.path.insert(0, "/mnt/data/cronhub/bin")
import sync_openclaw_jobs as soj  # noqa: E402


def _cp(returncode=0, stdout="", stderr=""):
    """Build a CompletedProcess-shaped object for fake runs."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class FakeRunner:
    """A minimal fake for `subprocess.run` that records calls and serves canned
    responses based on simple predicates over the command list."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self._rules: list[tuple] = []

    # --- response helpers ---------------------------------------------------
    def respond_cron_list_json(self, jobs):
        pred = lambda c: (
            len(c) >= 5
            and c[0] == soj.OPENCLAW_BIN
            and c[1:5] == ["cron", "list", "--all", "--json"]
        )
        self._rules.append((
            pred,
            {"returncode": 0, "stdout": json.dumps({"jobs": jobs}), "stderr": ""},
        ))
        return self

    def respond_cron_list_fail(self, returncode=2, stderr="cli error"):
        pred = lambda c: (
            len(c) >= 5
            and c[0] == soj.OPENCLAW_BIN
            and c[1:5] == ["cron", "list", "--all", "--json"]
        )
        self._rules.append((pred, {"returncode": returncode, "stdout": "", "stderr": stderr}))
        return self

    def respond_cron_list_bad_json(self):
        pred = lambda c: (
            len(c) >= 5
            and c[0] == soj.OPENCLAW_BIN
            and c[1:5] == ["cron", "list", "--all", "--json"]
        )
        self._rules.append((pred, {"returncode": 0, "stdout": "not-json{", "stderr": ""}))
        return self

    def respond_disable_ok(self):
        pred = lambda c: (
            len(c) >= 3
            and c[0] == soj.OPENCLAW_BIN
            and c[1:3] == ["cron", "disable"]
        )
        self._rules.append((pred, {"returncode": 0, "stdout": "", "stderr": ""}))
        return self

    def respond_disable_fail(self):
        pred = lambda c: (
            len(c) >= 3
            and c[0] == soj.OPENCLAW_BIN
            and c[1:3] == ["cron", "disable"]
        )
        self._rules.append((pred, {"returncode": 1, "stdout": "", "stderr": "disable failed"}))
        return self

    # --- subprocess.run protocol -------------------------------------------
    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        for pred, k in self._rules:
            try:
                ok = pred(cmd)
            except Exception:
                ok = False
            if ok:
                return _cp(**k)
        return _cp()


# Shared fixture used by every plan_actions / main test.
REG_JOBS = [
    # enabled=False in registry → action "disable"
    {
        "id": "reg-disabled-1",
        "name": "Cron Deduplicator",
        "enabled": False,
        "primary_executor": "openclaw-cron",
    },
    # BLADE Workable: enabled=True, executor=system-cron, OC enabled → "disable"
    {
        "id": "ed958381-01d0-4e0a-8e03-3d32e0768620",
        "name": "BLADE Workable",
        "enabled": True,
        "primary_executor": "system-cron",
    },
    # enabled, openclaw-cron, OC disabled → "drift"
    {
        "id": "drift-1",
        "name": "blade-fix-watchdog",
        "enabled": True,
        "primary_executor": "openclaw-cron",
    },
    # enabled, openclaw-cron, OC enabled → "noop"
    {
        "id": "noop-1",
        "name": "Daily Usage Stats",
        "enabled": True,
        "primary_executor": "openclaw-cron",
    },
]

OC_JOBS = [
    {"id": "reg-disabled-1", "name": "Cron Deduplicator", "enabled": True},
    {"id": "ed958381-01d0-4e0a-8e03-3d32e0768620", "name": "BLADE Workable", "enabled": True},
    {"id": "drift-1", "name": "blade-fix-watchdog", "enabled": False},
    {"id": "noop-1", "name": "Daily Usage Stats", "enabled": True},
    # orphan: in OC, not in registry
    {"id": "skill-collection-review-main", "name": "skill-collection-review-main", "enabled": True},
]


class TestPlanActions(unittest.TestCase):
    """plan_actions: pure dict-in / dict-out, no I/O."""

    def _plan(self):
        return soj.plan_actions(REG_JOBS, OC_JOBS)

    def test_disable_when_registry_disabled(self):
        actions = self._plan()
        by_id = {a["id"]: a for a in actions}
        self.assertEqual(by_id["reg-disabled-1"]["action"], "disable")
        # Every action dict carries id/name/action/reason.
        self.assertEqual(by_id["reg-disabled-1"]["name"], "Cron Deduplicator")
        self.assertIsInstance(by_id["reg-disabled-1"]["reason"], str)
        self.assertTrue(by_id["reg-disabled-1"]["reason"])

    def test_disable_when_executor_not_openclaw(self):
        """BLADE Workable: system-cron in registry, OC enabled → disable."""
        actions = self._plan()
        by_id = {a["id"]: a for a in actions}
        a = by_id["ed958381-01d0-4e0a-8e03-3d32e0768620"]
        self.assertEqual(a["action"], "disable")
        # The reason should mention the executor that does not match.
        self.assertIn("system-cron", a["reason"])

    def test_drift_is_never_enabled(self):
        """apply_actions never calls an `enable` subcommand — only `disable`.

        Drift, orphan and noop actions must produce zero CLI calls.
        """
        r = FakeRunner()
        actions = [
            {"id": "d1", "name": "D1", "action": "drift", "reason": "x"},
            {"id": "d2", "name": "D2", "action": "drift", "reason": "y"},
            {"id": "o1", "name": "O1", "action": "orphan", "reason": "x"},
            {"id": "n1", "name": "N1", "action": "noop", "reason": "x"},
        ]
        n = soj.apply_actions(actions, runner=r)
        self.assertEqual(n, 0)
        self.assertEqual(r.calls, [])
        for c in r.calls:
            self.assertNotIn("enable", c)

    def test_orphan_untouched(self):
        """skill-collection-review-main is in OC, not in registry → orphan, no runner call.

        plan_actions alone does not invoke the runner (it's pure). We exercise
        apply_actions on the orphan action alone to confirm it never reaches
        the CLI.
        """
        actions = self._plan()
        by_id = {a["id"]: a for a in actions}
        a = by_id["skill-collection-review-main"]
        self.assertEqual(a["action"], "orphan")
        self.assertEqual(a["name"], "skill-collection-review-main")
        # Orphan must not be acted on by apply_actions.
        r = FakeRunner()
        n = soj.apply_actions([a], runner=r)
        self.assertEqual(n, 0)
        self.assertEqual(r.calls, [])


class TestApplyActions(unittest.TestCase):
    def test_disable_runs_cli_and_returns_count(self):
        r = FakeRunner().respond_disable_ok()
        actions = [
            {"id": "a", "name": "A", "action": "disable", "reason": "x"},
            {"id": "b", "name": "B", "action": "disable", "reason": "y"},
        ]
        n = soj.apply_actions(actions, runner=r)
        self.assertEqual(n, 2)
        self.assertEqual(len(r.calls), 2)
        self.assertEqual(r.calls[0], [soj.OPENCLAW_BIN, "cron", "disable", "a"])
        self.assertEqual(r.calls[1], [soj.OPENCLAW_BIN, "cron", "disable", "b"])

    def test_disable_raises_on_nonzero_rc(self):
        r = FakeRunner().respond_disable_fail()
        actions = [{"id": "a", "name": "A", "action": "disable", "reason": "x"}]
        with self.assertRaises(soj.SyncError):
            soj.apply_actions(actions, runner=r)


class TestLoadOpenclawJobs(unittest.TestCase):
    def test_returns_jobs_list(self):
        jobs = [{"id": "x", "enabled": True}, {"id": "y", "enabled": False}]
        r = FakeRunner().respond_cron_list_json(jobs)
        out = soj.load_openclaw_jobs(runner=r)
        self.assertEqual(out, jobs)
        self.assertEqual(len(r.calls), 1)
        c = r.calls[0]
        self.assertEqual(c[0], soj.OPENCLAW_BIN)
        self.assertEqual(c[1:5], ["cron", "list", "--all", "--json"])

    def test_raises_on_nonzero_rc(self):
        r = FakeRunner().respond_cron_list_fail(returncode=2, stderr="oops")
        with self.assertRaises(soj.SyncError):
            soj.load_openclaw_jobs(runner=r)

    def test_raises_on_unparseable_json(self):
        r = FakeRunner().respond_cron_list_bad_json()
        with self.assertRaises(soj.SyncError):
            soj.load_openclaw_jobs(runner=r)


class TestSnapshotAndMain(unittest.TestCase):
    """write_snapshot, main() and the dry-run contract."""

    def setUp(self):
        # Each test gets its own tempdir so write_snapshot can drop files.
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _patch_snapshot_path(self, monkey_module):
        # Patch SNAPSHOT path on the module so tests don't touch real state.
        sentinel = self.tmpdir / "openclaw-jobs.snapshot.json"
        self._orig_snapshot = soj.SNAPSHOT
        monkey_module.SNAPSHOT = sentinel
        return sentinel

    def test_snapshot_shape(self):
        from unittest.mock import patch as _patch

        sentinel = self._patch_snapshot_path(soj)
        with _patch.object(soj, "SNAPSHOT", sentinel):
            jobs = [{"id": "x", "enabled": True}]
            soj.write_snapshot(jobs, path=sentinel)
        data = json.loads(sentinel.read_text())
        self.assertIn("synced_at", data)
        self.assertIn("jobs", data)
        self.assertEqual(data["jobs"], jobs)
        # synced_at must be a UTC Z string.
        self.assertTrue(data["synced_at"].endswith("Z"))
        self.assertIn("T", data["synced_at"])

    def test_dry_run_no_mutations(self):
        """main(['--dry-run']) prints non-noop actions but disables nothing."""
        from unittest.mock import patch as _patch

        sentinel = self._patch_snapshot_path(soj)
        runner = (
            FakeRunner()
            .respond_cron_list_json(OC_JOBS)
            .respond_disable_ok()
        )
        with _patch.object(soj, "SNAPSHOT", sentinel):
            rc = soj.main(["--dry-run"], registry_jobs=REG_JOBS, runner=runner)
        self.assertEqual(rc, 0)
        # No `cron disable ...` calls in dry-run.
        for c in runner.calls:
            self.assertNotEqual(c[1:3], ["cron", "disable"])
        # Snapshot exists (dry-run still writes snapshot).
        self.assertTrue(sentinel.exists())

    def test_cli_failure_exits_nonzero_no_snapshot(self):
        """runner rc=1 on `cron list` → main returns 1 and no snapshot file."""
        from unittest.mock import patch as _patch

        sentinel = self._patch_snapshot_path(soj)
        runner = FakeRunner().respond_cron_list_fail(returncode=2, stderr="boom")
        # Suppress main()'s stderr write so unittest's per-line `... ok` report
        # is not interleaved with the error message under `unittest -v`.
        with _patch.object(soj, "SNAPSHOT", sentinel), \
             redirect_stdout(io.StringIO()), \
             redirect_stderr(io.StringIO()):
            rc = soj.main([], registry_jobs=REG_JOBS, runner=runner)
        self.assertEqual(rc, 1)
        self.assertFalse(sentinel.exists())


class TestPublicSymbols(unittest.TestCase):
    """Sanity: the module exposes the documented public names."""

    def test_sync_error_exists(self):
        self.assertTrue(hasattr(soj, "SyncError"))

    def test_interfaces_exist(self):
        for name in (
            "OPENCLAW_BIN",
            "SNAPSHOT",
            "load_openclaw_jobs",
            "plan_actions",
            "apply_actions",
            "write_snapshot",
            "main",
        ):
            self.assertTrue(hasattr(soj, name), name)


if __name__ == "__main__":
    unittest.main()

class TestCronEnvironment(unittest.TestCase):
    """Regression (2026-10-16): under cron (PATH=/usr/bin:/bin) the openclaw CLI
    resolved /usr/bin/node v20 and died with 'node:sqlite is unavailable'.
    Every openclaw invocation must put brew's node (v26) first on PATH."""

    def test_openclaw_runs_with_brew_node_first_on_path(self):
        seen = {}

        def runner(cmd, **kw):
            seen.update(kw)
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, stdout='{"jobs": []}', stderr="")

        with unittest.mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            soj.load_openclaw_jobs(runner=runner)
        env = seen.get("env") or {}
        self.assertTrue(env.get("PATH", "").startswith("/home/linuxbrew/.linuxbrew/bin:"), env.get("PATH"))
        self.assertIn("/usr/bin", env["PATH"].split(":"))
