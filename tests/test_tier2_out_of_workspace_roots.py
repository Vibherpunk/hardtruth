#!/usr/bin/env python3
"""
Regression tests: Tier 2 target resolution for files edited OUTSIDE the session workspace.

Incident (2026-10-11): a Claude Code session started in /Users/ai/vibehard-build (not a git
repo, no test runner) edited /Users/ai/dev/ht-gofix/daemon/tier2_runner.py. Resolution was
bounded by the session's starting directory, so the edit resolved to no project, the
workspace had no runner, and the Stop hook halted with "no canonical test suite or runner
could be detected" -- a halt no amount of running the repo's tests could clear.

Fix: an edited source file outside the workspace resolves to its own project root, bounded
by that file's git top-level. Files in no git repository never resolve (fail closed as
before). Every resolved project is verified and every one must pass.

No docker, network, or live daemon required.
"""

import os
import sys
import json
import shutil
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
DAEMON_DIR = os.path.join(REPO_ROOT, "daemon")
if DAEMON_DIR not in sys.path:
    sys.path.insert(0, DAEMON_DIR)

from daemon.tier2_runner import (
    resolve_target_project_dirs,
    resolve_target_project_dir,
    run_independent_verification,
)
import client.hardtruth_hook as hardtruth_hook


def _git_init(path):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _make_pytest_project(path):
    _write(os.path.join(path, "pytest.ini"), "[pytest]\n")
    _write(os.path.join(path, "tests", "test_ok.py"), "def test_ok(): pass\n")


def _real(p):
    return os.path.realpath(p)


class TestOutOfWorkspaceResolution(unittest.TestCase):

    def setUp(self):
        self.test_dir = _real(tempfile.mkdtemp(prefix="hardtruth-oow-"))
        # The session workspace: not a git repo, no test runner (like ~/vibehard-build).
        self.workspace = os.path.join(self.test_dir, "vibehard-build")
        os.makedirs(self.workspace)
        _write(os.path.join(self.workspace, "NOTES.md"), "notes\n")
        self._env = {k: os.environ.pop(k) for k in ("GIT_DIR", "GIT_WORK_TREE") if k in os.environ}

    def tearDown(self):
        os.environ.update(self._env)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_in_repo_edit_outside_workspace_resolves_to_that_repo(self):
        """The incident: edits to a git repo outside the workspace resolve to that repo's
        project root and are verified there."""
        repo = os.path.join(self.test_dir, "dev", "ht-gofix")
        _git_init(repo)
        _make_pytest_project(repo)
        edited = [
            os.path.join(repo, "daemon", "tier2_runner.py"),
            os.path.join(repo, "tests", "test_tier2_go_container.py"),
        ]
        for e in edited:
            _write(e, "x = 1\n")

        roots = resolve_target_project_dirs(self.workspace, modified_files=edited)
        self.assertEqual([_real(r) for r in roots], [_real(repo)])
        self.assertEqual(_real(resolve_target_project_dir(self.workspace, modified_files=edited)), _real(repo))

    def test_resolves_subproject_within_repo(self):
        """Resolution walks up to the NEAREST project root inside the repo (a monorepo
        subproject), not straight to the git top-level."""
        repo = os.path.join(self.test_dir, "dev", "mono")
        _git_init(repo)
        sub = os.path.join(repo, "apps", "svc")
        _make_pytest_project(sub)
        edited = os.path.join(sub, "src", "main.py")
        _write(edited, "x = 1\n")

        roots = resolve_target_project_dirs(self.workspace, modified_files=[edited])
        self.assertEqual([_real(r) for r in roots], [_real(sub)])

    def test_no_repo_edit_outside_workspace_fails_closed(self):
        """An edited file outside the workspace in NO git repository never resolves, even
        if its directory looks runnable -- and verification of the runner-less workspace
        still fails closed."""
        loose = os.path.join(self.test_dir, "scratch")
        _make_pytest_project(loose)  # runnable-looking, but not a git repo
        edited = os.path.join(loose, "tool.py")
        _write(edited, "x = 1\n")

        self.assertEqual(resolve_target_project_dirs(self.workspace, modified_files=[edited]), [])
        self.assertIsNone(resolve_target_project_dir(self.workspace, modified_files=[edited]))

        res = run_independent_verification(self.workspace)
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "unverified_no_runner")

    def test_resolution_never_escapes_git_toplevel(self):
        """A runner in a directory ABOVE the edited file's git top-level is never used."""
        outer = os.path.join(self.test_dir, "outer")
        _make_pytest_project(outer)  # runner above the repo boundary
        repo = os.path.join(outer, "inner-repo")
        _git_init(repo)  # repo itself has no runner
        edited = os.path.join(repo, "lib", "mod.py")
        _write(edited, "x = 1\n")

        self.assertEqual(resolve_target_project_dirs(self.workspace, modified_files=[edited]), [])

    def test_unedited_dirty_sibling_repo_is_never_attributed(self):
        """Only repos containing files this session edited are targeted; a dirty sibling
        repo with a runner is ignored."""
        edited_repo = os.path.join(self.test_dir, "dev", "edited")
        _git_init(edited_repo)
        _make_pytest_project(edited_repo)
        edited = os.path.join(edited_repo, "src", "a.py")
        _write(edited, "x = 1\n")

        sibling = os.path.join(self.test_dir, "dev", "goose")
        _git_init(sibling)
        _make_pytest_project(sibling)
        subprocess.run(["git", "add", "-A"], cwd=sibling, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-m", "init"], cwd=sibling, check=True, stdout=subprocess.PIPE)
        _write(os.path.join(sibling, "src", "dirty.py"), "dirty = True\n")

        roots = [_real(r) for r in resolve_target_project_dirs(self.workspace, modified_files=[edited])]
        self.assertEqual(roots, [_real(edited_repo)])
        self.assertNotIn(_real(sibling), roots)

    def test_multiple_out_of_workspace_projects_all_returned(self):
        repo_a = os.path.join(self.test_dir, "dev", "a")
        repo_b = os.path.join(self.test_dir, "dev", "b")
        for r in (repo_a, repo_b):
            _git_init(r)
            _make_pytest_project(r)
        ea = os.path.join(repo_a, "x.py")
        eb = os.path.join(repo_b, "pkg", "y.py")
        _write(ea, "x = 1\n")
        _write(eb, "y = 1\n")

        roots = [_real(r) for r in resolve_target_project_dirs(self.workspace, modified_files=[ea, eb, ea])]
        self.assertEqual(roots, [_real(repo_a), _real(repo_b)])

    def test_non_source_edit_outside_workspace_is_ignored(self):
        """Editing a note/memory file in another repo does not pull that repo into Tier 2."""
        repo = os.path.join(self.test_dir, "dev", "docs-repo")
        _git_init(repo)
        _make_pytest_project(repo)
        edited = os.path.join(repo, "README.md")
        _write(edited, "# readme\n")

        self.assertEqual(resolve_target_project_dirs(self.workspace, modified_files=[edited]), [])

    def test_deleted_file_outside_workspace_still_resolves(self):
        repo = os.path.join(self.test_dir, "dev", "r")
        _git_init(repo)
        _make_pytest_project(repo)
        edited = os.path.join(repo, "gone", "removed.py")  # directory no longer exists

        roots = resolve_target_project_dirs(self.workspace, modified_files=[edited])
        self.assertEqual([_real(r) for r in roots], [_real(repo)])

    def test_git_dir_env_cannot_redirect_toplevel(self):
        """A GIT_DIR in the environment must not make a non-repo file resolve into
        another repository."""
        other = os.path.join(self.test_dir, "dev", "other")
        _git_init(other)
        _make_pytest_project(other)
        loose = os.path.join(self.test_dir, "loose")
        _make_pytest_project(loose)
        edited = os.path.join(loose, "t.py")
        _write(edited, "x = 1\n")

        with mock.patch.dict(os.environ, {"GIT_DIR": os.path.join(other, ".git"), "GIT_WORK_TREE": other}):
            self.assertEqual(resolve_target_project_dirs(self.workspace, modified_files=[edited]), [])


class _FakeLedger:
    def __init__(self, premise):
        self._premise = premise

    def get_premise(self, conv_id):
        return dict(self._premise)


class TestStopHookVerifiesEveryProject(unittest.TestCase):
    """handle_stop must hand off EVERY resolved project and halt if any one fails."""

    def setUp(self):
        self.test_dir = _real(tempfile.mkdtemp(prefix="hardtruth-oow-hook-"))
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700)
        self.workspace = os.path.join(self.test_dir, "ws")
        os.makedirs(self.workspace)
        self.repo_a = os.path.join(self.test_dir, "a")
        self.repo_b = os.path.join(self.test_dir, "b")
        self._env = {
            "HARDTRUTH_HALT_DIR": self.halt_dir,
            "HARDTRUTH_API_KEY": os.path.join(self.test_dir, "no-such-key"),
            "HARDTRUTH_URL": "http://127.0.0.1:9",
        }
        self._old = {k: os.environ.get(k) for k in list(self._env) + ["HARDTRUTH_SKIP_TIER2", "HARDTRUTH_LEDGER_PATH"]}
        os.environ.update(self._env)
        os.environ.pop("HARDTRUTH_SKIP_TIER2", None)
        os.environ.pop("HARDTRUTH_LEDGER_PATH", None)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _run_stop(self, handoff_results):
        conv = f"oow-{uuid.uuid4().hex}"
        transcript = os.path.join(self.test_dir, "t.jsonl")
        with open(transcript, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"role": "assistant",
                                "content": [{"type": "text", "text": "Done."}]}}) + "\n")
        premise = {
            "tampered": False,
            "conversationId": conv,
            "premise": "MODIFIED FILES: a.py, b.py.",
            "source_files_modified": 2,
            "doc_files_modified": 0,
            "modified_file_paths": [os.path.join(self.repo_a, "a.py"), os.path.join(self.repo_b, "b.py")],
            "verification_commands_executed": 1,
            "test_commands_executed": 1,
            "last_source_mod_step": 1,
            "last_test_step": 2,
            "unresolved_failures": [],
        }
        handed_off = []

        def fake_call(endpoint, data, timeout=None):
            if endpoint == "v1/verify/handoff":
                handed_off.append(data["workspace_path"])
                return handoff_results[data["workspace_path"]]
            return None

        payload = {"conversationId": conv, "transcriptPath": transcript, "cwd": self.workspace,
                   "workspace_path": self.workspace, "executionNum": 1}
        with mock.patch.object(hardtruth_hook, "HARNESS", "claude_code"), \
             mock.patch.object(hardtruth_hook, "get_local_ledger", return_value=_FakeLedger(premise)), \
             mock.patch.object(hardtruth_hook, "get_git_modified_source_files", return_value=([], [], [])), \
             mock.patch.object(hardtruth_hook, "resolve_target_project_dirs", return_value=[self.repo_a, self.repo_b]), \
             mock.patch.object(hardtruth_hook, "is_macos_native_project", return_value=False), \
             mock.patch.object(hardtruth_hook, "call_system_one", side_effect=fake_call):
            res = hardtruth_hook.handle_stop(payload)
        return res, handed_off

    def _setup_repos(self):
        for r in (self.repo_a, self.repo_b):
            _git_init(r)
            _make_pytest_project(r)
        _write(os.path.join(self.repo_a, "a.py"), "a = 1\n")
        _write(os.path.join(self.repo_b, "b.py"), "b = 1\n")

    def test_all_projects_pass_allows(self):
        self._setup_repos()
        ok = {"success": True, "status": "verified", "exit_code": 0, "runner": "pytest"}
        res, handed_off = self._run_stop({self.repo_a: ok, self.repo_b: ok})
        self.assertEqual(handed_off, [self.repo_a, self.repo_b])
        self.assertNotIn("TIER 2 HARD GATE FAILED", json.dumps(res))

    def test_one_failing_project_halts(self):
        self._setup_repos()
        ok = {"success": True, "status": "verified", "exit_code": 0, "runner": "pytest"}
        bad = {"success": False, "status": "failed", "exit_code": 1, "runner": "pytest", "output": "1 failed"}
        res, handed_off = self._run_stop({self.repo_a: ok, self.repo_b: bad})
        self.assertEqual(handed_off, [self.repo_a, self.repo_b])
        dumped = json.dumps(res, ensure_ascii=False)
        self.assertIn("TIER 2 HARD GATE FAILED", dumped)
        self.assertIn(self.repo_b, dumped)


if __name__ == "__main__":
    unittest.main()
