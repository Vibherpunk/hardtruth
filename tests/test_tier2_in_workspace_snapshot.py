#!/usr/bin/env python3
"""
Regression tests: repositories INSIDE the workspace are verified against their session-start
snapshot (follow-up r3 M1 to PR #3).

get_or_set_session_baseline() is lazy: it records HEAD after whatever tool ran first and
falls back to a git ref and a halt-dir cache, all agent-writable. PostToolUse calls it for
every tool cwd, so a shell commit made before the first edit became the baseline, and a
committed conftest tamper in the workspace repository passed Phase 5, Phase 8 and the Tier 2
tamper check. When SessionStart snapshotted the repository, Stop now uses that snapshot for
Phases 5, 6 and 8 and applies check_outside_repo_integrity to in-workspace targets.

Without a snapshot the in-workspace checks keep the lazy baseline (documented in
client/hardtruth_hook.py) unless HARDTRUTH_REQUIRE_SESSION_SNAPSHOT=1.

End to end through handle_session_start and handle_stop with a fake first-write-wins
daemon, as in tests/test_tier2_out_of_workspace_roots.py. No docker or live daemon.
"""

import os
import sys
import json
import hashlib
import subprocess
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import test_tier2_out_of_workspace_roots as oow  # module import: pytest must not re-collect its classes
from test_tier2_out_of_workspace_roots import _commit, _git_init, _write, TEST_BODY
import client.hardtruth_hook as hardtruth_hook


class TestStopHookInWorkspaceRepo(oow.TestStopHookOutsideRepos):
    """The session's cwd is the repository it edits. Inherits the fake daemon, _stop and
    _session_start; the outside-repo tests are not re-run here."""

    def setUp(self):
        super().setUp()
        self._old["HARDTRUTH_REQUIRE_SESSION_SNAPSHOT"] = os.environ.pop("HARDTRUTH_REQUIRE_SESSION_SNAPSHOT", None)
        os.rmdir(self.workspace)
        self.repo = self._repo(os.path.basename(self.workspace), parent=os.path.dirname(self.workspace))
        self.app = os.path.join(self.repo, "app.py")

    def _post_tool_use_baseline(self):
        """What PostToolUse does for a tool's cwd (get_or_set_session_baseline)."""
        with mock.patch.object(hardtruth_hook, "call_system_one", side_effect=self._fake_call):
            return hardtruth_hook.get_or_set_session_baseline(self.repo, self.conv)

    def _edit(self):
        _write(self.app, "def f():\n    return 'broken'\n")

    # --- the snapshot is the baseline ---------------------------------------------------

    def test_clean_in_workspace_edit_is_verified_and_allowed(self):
        self._session_start()
        _write(self.app, "def f():\n    return 2\n")
        res = self._stop([self.app])
        self.assertEqual(res.get("decision"), "allow", res)
        self.assertEqual(self.handed_off, [self.repo])

    def test_shell_commit_of_conftest_tamper_before_first_edit_halts(self):
        """r3 M1: the lazy baseline is recorded after the tamper commit; the snapshot is not.
        (SessionStart seeds the halt-dir cache with the cwd's HEAD, but the agent can delete it.)"""
        self._session_start()
        tampered = self._tamper_conftest(self.repo)
        os.remove(hardtruth_hook.get_session_start_baseline_file(self.conv))
        self.assertEqual(self._post_tool_use_baseline(), tampered)  # the lazy baseline is chosen by the agent
        self._edit()
        self._assert_halted_on_conftest(self._stop([self.app]))

    def test_planted_baseline_ref_and_cache_files_are_ignored(self):
        self._session_start()
        tampered = self._tamper_conftest(self.repo)
        conv_hash = hashlib.sha256(self.conv.encode("utf-8")).hexdigest()[:16]
        subprocess.run(["git", "update-ref", f"refs/hardtruth/baseline/{conv_hash}", tampered], cwd=self.repo, check=True)
        self._post_tool_use_baseline()
        cache_files = [n for n in os.listdir(self.halt_dir) if n.startswith("baseline_")]
        self.assertTrue(cache_files)
        for name in cache_files:
            with open(os.path.join(self.halt_dir, name), "w") as f:
                json.dump({"baseline_sha": tampered, "workspace_path": self.repo}, f)
        self._edit()
        self._assert_halted_on_conftest(self._stop([self.app]))

    def test_backdated_tamper_commit_halts(self):
        self._session_start()
        self._tamper_conftest(self.repo, when=time.time() - 3600)
        self._post_tool_use_baseline()
        self._edit()
        self._assert_halted_on_conftest(self._stop([self.app]))

    def test_committed_assertion_deletion_before_first_edit_halts(self):
        """Phase 8 only scans files the session is known to have edited; the in-workspace
        integrity check diffs the whole repository against the snapshot."""
        self._session_start()
        _write(os.path.join(self.repo, "tests", "test_app.py"), "def test_a():\n    assert f() is not None\n")
        _commit(self.repo, "x")
        self._post_tool_use_baseline()
        _write(self.app, "def f():\n    return 2\n")
        res = self._stop([self.app])
        self.assertIn("Severe assertion deletion", res.get("reason", ""))
        self.assertEqual(self.handed_off, [])

    def test_backdated_stash_halts(self):
        """Phase 5 compares refs/stash with its session-start value, not the stash's %ct."""
        self._session_start()
        _write(self.app, "def f():\n    return 2\n")
        _write(os.path.join(self.repo, "tests", "test_app.py"), "def test_a():\n    pass\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = f"{int(time.time()) - 3600} +0000"
        subprocess.run(["git", "stash", "push", "--", "tests/test_app.py"], cwd=self.repo, env=env, check=True,
                       stdout=subprocess.PIPE)
        res = self._stop([self.app])
        self.assertIn("stash", res.get("reason", ""))
        self.assertEqual(self.handed_off, [])

    def test_history_rewrite_below_snapshot_halts_despite_lazy_baseline(self):
        """E4 against the snapshot: resetting to an older commit, then letting PostToolUse
        record the lazy baseline there, does not move the lineage anchor."""
        _write(os.path.join(self.repo, "README.md"), "second\n")
        _commit(self.repo, "second")
        self._session_start()
        subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=self.repo, check=True)
        self._post_tool_use_baseline()
        _write(self.app, "def f():\n    return 2\n")
        res = self._stop([self.app])
        self.assertIn("EVASION E4", res.get("reason", ""))
        self.assertEqual(self.handed_off, [])

    def test_in_workspace_subproject_target_uses_its_repository_snapshot(self):
        """A Tier 2 target in a subdirectory of the workspace repository is checked against
        the repository's snapshot, with manifests anchored at the git top-level."""
        sub = os.path.join(self.repo, "svc")
        _write(os.path.join(sub, "pytest.ini"), "[pytest]\n")
        _write(os.path.join(sub, "tests", "test_svc.py"), TEST_BODY)
        _write(os.path.join(sub, "svc.py"), "def g():\n    return 1\n")
        _commit(self.repo, "svc")
        self._session_start()
        self._tamper_conftest(sub)
        self._post_tool_use_baseline()
        _write(os.path.join(sub, "svc.py"), "def g():\n    return 'broken'\n")
        self._assert_halted_on_conftest(self._stop([os.path.join(sub, "svc.py")]))

    # --- no snapshot: documented fallback ---------------------------------------------

    def test_without_snapshot_lazy_baseline_is_kept(self):
        """No SessionStart (another harness, hook installed mid-session): today's behaviour.
        The lazy baseline is recorded after the tamper commit, so this tamper is not seen;
        that is the gap HARDTRUTH_REQUIRE_SESSION_SNAPSHOT=1 closes."""
        self._tamper_conftest(self.repo)
        self._post_tool_use_baseline()
        self._edit()
        res = self._stop([self.app])
        self.assertEqual(res.get("decision"), "allow", res)
        self.assertEqual(self.handed_off, [self.repo])

    def test_without_snapshot_strict_mode_fails_closed(self):
        os.environ["HARDTRUTH_REQUIRE_SESSION_SNAPSHOT"] = "1"
        self._post_tool_use_baseline()
        _write(self.app, "def f():\n    return 2\n")
        res = self._stop([self.app])
        self.assertIn("no session-start snapshot", res.get("reason", ""))
        self.assertIn(self.repo, res.get("reason", ""))
        self.assertEqual(self.handed_off, [])

    def test_strict_mode_with_snapshot_allows_clean_edit(self):
        os.environ["HARDTRUTH_REQUIRE_SESSION_SNAPSHOT"] = "1"
        self._session_start()
        _write(self.app, "def f():\n    return 2\n")
        res = self._stop([self.app])
        self.assertEqual(res.get("decision"), "allow", res)

    def test_id_less_session_snapshot_is_never_trusted(self):
        """Every id-less session shares one daemon key, so its snapshot may be another
        session's HEAD (the anton-go worktree incident)."""
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
                              stdout=subprocess.PIPE, text=True).stdout.strip()
        for cid in ("unknown", "default"):
            self.daemon[(cid, hardtruth_hook.SESSION_START_HEAD_NS + self.repo)] = head
            with mock.patch.object(hardtruth_hook, "call_system_one", side_effect=self._fake_call):
                self.assertIsNone(hardtruth_hook.trusted_repo_baseline(self.repo, cid))
        with mock.patch.object(hardtruth_hook, "call_system_one", side_effect=self._fake_call):
            hardtruth_hook.snapshot_repos_at_session_start(self.conv, self.repo)
            self.assertEqual(hardtruth_hook.trusted_repo_baseline(self.repo, self.conv), head)


# The inherited outside-repo tests already run in their own module.
for _name in [n for n in dir(oow.TestStopHookOutsideRepos) if n.startswith("test_")]:
    if _name not in TestStopHookInWorkspaceRepo.__dict__:
        setattr(TestStopHookInWorkspaceRepo, _name, None)


class TestSessionStartSnapshotBudget(unittest.TestCase):
    """delta LOW-1: a worker running at the deadline makes two sequential daemon calls
    (HEAD, then stash), so the hook process outlives the budget by up to two call timeouts."""

    def test_in_flight_worker_finishes_both_calls_after_the_deadline(self):
        import tempfile
        import shutil
        test_dir = os.path.realpath(tempfile.mkdtemp(prefix="hardtruth-budget-"))
        self.addCleanup(shutil.rmtree, test_dir, True)
        repo = os.path.join(test_dir, "r")
        _git_init(repo)
        _write(os.path.join(repo, "a.py"), "x = 1\n")
        _commit(repo)
        ends = []

        def slow(endpoint, data, timeout=None):
            time.sleep(1.2)  # under the 1.5s per-call timeout
            ends.append(time.time())
            return {"baseline_sha": data.get("commit_sha")}

        with mock.patch.dict(os.environ, {"HARDTRUTH_BASELINE_ROOTS": ""}), \
             mock.patch.object(hardtruth_hook, "call_system_one", side_effect=slow):
            t0 = time.time()
            n = hardtruth_hook.snapshot_repos_at_session_start("budget-conv", repo, time_budget_sec=0.3)
            returned = time.time() - t0
            time.sleep(3.0)
        self.assertEqual(n, 0)
        self.assertLess(returned, 1.0)
        self.assertEqual(len(ends), 2)
        self.assertGreater(ends[-1] - t0, 0.3 + 1.5)  # longer than one in-flight call
        self.assertLess(ends[-1] - t0, 0.3 + 3.0 + 0.5)  # within two


if __name__ == "__main__":
    unittest.main()
