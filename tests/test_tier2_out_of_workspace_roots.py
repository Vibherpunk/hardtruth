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
import hashlib
import shutil
import subprocess
import tempfile
import time
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
    resolve_tier2_targets,
    check_manifest_tampering,
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


def _commit(repo, msg="c", when=None):
    env = dict(os.environ)
    for k in [k for k in env if k.startswith("GIT_")]:
        env.pop(k)
    if when is not None:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = f"{int(when)} +0000"
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env, stdout=subprocess.PIPE)
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", msg], cwd=repo, check=True, env=env,
                   stdout=subprocess.PIPE)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, env=env,
                          stdout=subprocess.PIPE, text=True).stdout.strip()


TEST_BODY = "def test_a():\n" + "".join(f"    assert {i} == {i}\n" for i in range(8))


class TestBaselineAwareResolution(unittest.TestCase):
    """resolve_tier2_targets with a baseline lookup, as the Stop hook calls it."""

    def setUp(self):
        self.test_dir = _real(tempfile.mkdtemp(prefix="hardtruth-oow-bl-"))
        self.workspace = os.path.join(self.test_dir, "ws")
        os.makedirs(self.workspace)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _repo(self, name):
        repo = os.path.join(self.test_dir, name)
        _git_init(repo)
        _make_pytest_project(repo)
        _write(os.path.join(repo, "app.py"), "x = 1\n")
        return repo, _commit(repo)

    def test_outside_repo_without_baseline_is_unresolved(self):
        repo, _ = self._repo("r")
        res = resolve_tier2_targets(self.workspace, [os.path.join(repo, "app.py")], baseline_lookup=lambda top: None)
        self.assertEqual(res["targets"], [])
        self.assertEqual(len(res["unresolved"]), 1)
        self.assertIn("no session-start snapshot", res["unresolved"][0]["reason"])

    def test_outside_target_carries_repo_and_baseline(self):
        repo, sha = self._repo("r")
        res = resolve_tier2_targets(self.workspace, [os.path.join(repo, "app.py")], baseline_lookup=lambda top: sha)
        self.assertEqual(res["unresolved"], [])
        self.assertEqual(res["targets"], [{"root": _real(repo), "outside": True, "repo_root": _real(repo), "baseline": sha}])

    def test_root_fabricated_during_session_is_not_chosen(self):
        """A pytest.ini + trivial tests/ created next to the edited file after the baseline
        cannot become the verification root; the root existing at baseline is used."""
        repo, sha = self._repo("r")
        sub = os.path.join(repo, "pkg")
        _write(os.path.join(sub, "core.py"), "y = 1\n")
        _commit(repo)
        _make_pytest_project(sub)  # fabricated after the baseline
        res = resolve_tier2_targets(self.workspace, [os.path.join(sub, "core.py")], baseline_lookup=lambda top: sha)
        self.assertEqual([t["root"] for t in res["targets"]], [_real(repo)])

    def test_repo_without_runner_is_unresolved_even_with_a_decoy(self):
        """M3: an edited repo with no runner is reported, not masked by a passing decoy."""
        real = os.path.join(self.test_dir, "norunner")
        _git_init(real)
        _write(os.path.join(real, "lib.py"), "z = 1\n")
        real_sha = _commit(real)
        decoy, decoy_sha = self._repo("decoy")
        shas = {_real(real): real_sha, _real(decoy): decoy_sha}
        res = resolve_tier2_targets(self.workspace, [os.path.join(real, "lib.py"), os.path.join(decoy, "app.py")],
                                    baseline_lookup=lambda top: shas.get(top))
        self.assertEqual([u["file"] for u in res["unresolved"]], [os.path.join(real, "lib.py")])
        self.assertIn("no test runner", res["unresolved"][0]["reason"])

    def test_edit_through_symlink_resolves_to_the_real_repo(self):
        """M4: an edit made through a symlink is attributed to the repository holding the file."""
        b, b_sha = self._repo("b")
        a = os.path.join(self.test_dir, "a")
        _git_init(a)
        os.symlink(os.path.join(b, "app.py"), os.path.join(a, "link.py"))
        res = resolve_tier2_targets(self.workspace, [os.path.join(a, "link.py")],
                                    baseline_lookup=lambda top: b_sha if top == _real(b) else None)
        self.assertEqual(res["unresolved"], [])
        self.assertEqual([t["root"] for t in res["targets"]], [_real(b)])


class TestTamperCheckAnchoredAtToplevel(unittest.TestCase):
    """M1: a project root that is a subdirectory of its repository is tamper-checked."""

    def setUp(self):
        self.test_dir = _real(tempfile.mkdtemp(prefix="hardtruth-oow-tamper-"))
        self.repo = os.path.join(self.test_dir, "mono")
        _git_init(self.repo)
        self.sub = os.path.join(self.repo, "apps", "svc")
        _make_pytest_project(self.sub)
        _write(os.path.join(self.sub, "main.py"), "x = 1\n")
        self.baseline = _commit(self.repo)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_clean_subproject_is_not_tampered(self):
        self.assertEqual(check_manifest_tampering(self.sub, baseline_sha=self.baseline), (False, []))

    def test_uncommitted_conftest_in_subproject_is_tampered(self):
        _write(os.path.join(self.sub, "conftest.py"), "import pytest\n")
        tampered, files = check_manifest_tampering(self.sub, baseline_sha=self.baseline)
        self.assertTrue(tampered)
        self.assertIn("apps/svc/conftest.py", files)

    def test_committed_pytest_ini_change_in_subproject_is_tampered(self):
        _write(os.path.join(self.sub, "pytest.ini"), "[pytest]\naddopts = -k nothing\n")
        _commit(self.repo)
        tampered, files = check_manifest_tampering(self.sub, baseline_sha=self.baseline)
        self.assertTrue(tampered)
        self.assertIn("apps/svc/pytest.ini", files)


class TestStopHookOutsideRepos(unittest.TestCase):
    """End-to-end through handle_session_start and handle_stop with the real resolver.
    The daemon is a fake first-write-wins store (as daemon/ledger.py set_session_baseline);
    every probe from the PR #3 round-2 review is a test here and must halt."""

    def setUp(self):
        self.test_dir = _real(tempfile.mkdtemp(prefix="hardtruth-oow-hook-"))
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700)
        self.workspace = os.path.join(self.test_dir, "ws")
        os.makedirs(self.workspace)
        self.code_root = os.path.join(self.test_dir, "dev")  # a HARDTRUTH_BASELINE_ROOTS entry
        os.makedirs(self.code_root)
        self._env = {
            "HARDTRUTH_HALT_DIR": self.halt_dir,
            "HARDTRUTH_API_KEY": os.path.join(self.test_dir, "no-such-key"),
            "HARDTRUTH_URL": "http://127.0.0.1:9",
            "HARDTRUTH_BASELINE_ROOTS": self.code_root,
        }
        self._old = {k: os.environ.get(k) for k in list(self._env) + ["HARDTRUTH_SKIP_TIER2", "HARDTRUTH_LEDGER_PATH"]}
        os.environ.update(self._env)
        os.environ.pop("HARDTRUTH_SKIP_TIER2", None)
        os.environ.pop("HARDTRUTH_LEDGER_PATH", None)
        self.conv = f"oow-{uuid.uuid4().hex}"
        self.daemon = {}
        self.daemon_up = True
        self.handed_off = []
        self.handoff_status = {}

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _fake_call(self, endpoint, data, timeout=None):
        if not self.daemon_up:
            return None
        if endpoint == "v1/session/baseline":
            key = (data["conversationId"], data["workspace_path"])
            if key not in self.daemon and data.get("commit_sha"):
                self.daemon[key] = data["commit_sha"]
            return {"baseline_sha": self.daemon.get(key)}
        if endpoint == "v1/verify/handoff":
            self.handed_off.append(data["workspace_path"])
            return self.handoff_status.get(data["workspace_path"],
                                           {"success": True, "status": "verified", "exit_code": 0, "runner": "pytest"})
        return None

    def _repo(self, name, parent=None):
        repo = os.path.join(parent or self.code_root, name)
        _git_init(repo)
        _write(os.path.join(repo, "pytest.ini"), "[pytest]\n")
        _write(os.path.join(repo, "tests", "test_app.py"), TEST_BODY)
        _write(os.path.join(repo, "app.py"), "def f():\n    return 1\n")
        _commit(repo)
        return repo

    def _session_start(self):
        with mock.patch.object(hardtruth_hook, "call_system_one", side_effect=self._fake_call):
            hardtruth_hook.handle_session_start({"conversationId": self.conv, "cwd": self.workspace, "source": "startup"})

    def _tamper_conftest(self, repo, when=None):
        _write(os.path.join(repo, "conftest.py"),
               "import pytest\n@pytest.hookimpl(hookwrapper=True)\ndef pytest_runtest_makereport(item, call):\n"
               "    out = yield\n    out.get_result().outcome = 'passed'\n")
        return _commit(repo, "wip", when=when)

    def _stop(self, modified):
        transcript = os.path.join(self.test_dir, "t.jsonl")
        with open(transcript, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"role": "assistant",
                                "content": [{"type": "text", "text": "Done."}]}}) + "\n")
        premise = {
            "tampered": False, "conversationId": self.conv, "premise": "MODIFIED FILES: app.py.",
            "source_files_modified": len(modified), "doc_files_modified": 0, "modified_file_paths": modified,
            "verification_commands_executed": 1, "test_commands_executed": 1,
            "last_source_mod_step": 1, "last_test_step": 2, "unresolved_failures": [],
        }
        payload = {"conversationId": self.conv, "transcriptPath": transcript, "cwd": self.workspace,
                   "workspace_path": self.workspace, "executionNum": 1}
        with mock.patch.object(hardtruth_hook, "HARNESS", "claude_code"), \
             mock.patch.object(hardtruth_hook, "get_local_ledger", return_value=_FakeLedger(premise)), \
             mock.patch.object(hardtruth_hook, "get_git_modified_source_files", return_value=([], [], [])), \
             mock.patch.object(hardtruth_hook, "is_macos_native_project", return_value=False), \
             mock.patch.object(hardtruth_hook, "call_system_one", side_effect=self._fake_call):
            return hardtruth_hook.handle_stop(payload)

    def _assert_halted_on_conftest(self, res):
        self.assertIn("conftest.py", res.get("reason", ""), res)
        self.assertIn("REJECTED", res.get("reason", ""))
        self.assertEqual(self.handed_off, [])

    # --- passing and failing projects -------------------------------------------------

    def test_clean_outside_repo_is_verified_and_allowed(self):
        repo = self._repo("r")
        self._session_start()
        _write(os.path.join(repo, "app.py"), "def f():\n    return 2\n")
        res = self._stop([os.path.join(repo, "app.py")])
        self.assertEqual(res.get("decision"), "allow", res)
        self.assertEqual(self.handed_off, [repo])

    def test_all_outside_projects_verified_and_one_failure_halts(self):
        a, b = self._repo("a"), self._repo("b")
        self._session_start()
        _write(os.path.join(a, "app.py"), "def f():\n    return 2\n")
        _write(os.path.join(b, "app.py"), "def f():\n    return 3\n")
        self.handoff_status[b] = {"success": False, "status": "failed", "exit_code": 1, "runner": "pytest", "output": "1 failed"}
        res = self._stop([os.path.join(a, "app.py"), os.path.join(b, "app.py")])
        self.assertEqual(self.handed_off, [a, b])
        self.assertIn("TIER 2 HARD GATE FAILED", res.get("reason", ""))
        self.assertIn(b, res.get("reason", ""))

    # --- round-1 H1 and its tests ------------------------------------------------------

    def test_committed_conftest_tamper_in_outside_repo_halts(self):
        repo = self._repo("r")
        self._session_start()
        _write(os.path.join(repo, "app.py"), "def f():\n    return 'broken'\n")
        self._tamper_conftest(repo)
        self._assert_halted_on_conftest(self._stop([os.path.join(repo, "app.py")]))

    def test_committed_assertion_deletion_in_outside_repo_halts(self):
        repo = self._repo("r")
        self._session_start()
        _write(os.path.join(repo, "app.py"), "def f():\n    return 2\n")
        _write(os.path.join(repo, "tests", "test_app.py"), "def test_a():\n    assert f() is not None\n")
        _commit(repo, "x")
        res = self._stop([os.path.join(repo, "app.py")])
        self.assertIn("Severe assertion deletion", res.get("reason", ""))
        self.assertEqual(self.handed_off, [])

    # --- round-2 probes: the agent must not be able to choose the baseline -------------

    def test_shell_commit_from_inside_outside_repo_before_first_edit_halts(self):
        """Round-2 H1 / P11: a shell commit run with Cwd = the outside repo makes PostToolUse
        baseline that repo at the post-tamper HEAD (get_or_set_session_baseline). The
        session-start snapshot must still be what Stop compares against."""
        repo = self._repo("r")
        self._session_start()
        self._tamper_conftest(repo)
        with mock.patch.object(hardtruth_hook, "call_system_one", side_effect=self._fake_call):
            hardtruth_hook.get_or_set_session_baseline(repo, self.conv)  # what PostToolUse does for tool_cwd
        _write(os.path.join(repo, "app.py"), "def f():\n    return 'broken'\n")
        self._assert_halted_on_conftest(self._stop([os.path.join(repo, "app.py")]))

    def test_backdated_tamper_commit_halts(self):
        """P1: a GIT_COMMITTER_DATE before session start doesn't move the baseline."""
        repo = self._repo("r")
        self._session_start()
        self._tamper_conftest(repo, when=time.time() - 3600)
        _write(os.path.join(repo, "app.py"), "def f():\n    return 'broken'\n")
        self._assert_halted_on_conftest(self._stop([os.path.join(repo, "app.py")]))

    def test_planted_baseline_ref_and_cache_files_are_ignored(self):
        """P2/P3: refs/hardtruth/baseline/<conv_hash> and halt-dir files set to the tampered HEAD."""
        repo = self._repo("r")
        self._session_start()
        tampered = self._tamper_conftest(repo)
        conv_hash = hashlib.sha256(self.conv.encode("utf-8")).hexdigest()[:16]
        subprocess.run(["git", "update-ref", f"refs/hardtruth/baseline/{conv_hash}", tampered], cwd=repo, check=True)
        for name in os.listdir(self.halt_dir):
            if name.startswith("baseline_"):
                with open(os.path.join(self.halt_dir, name), "w") as f:
                    json.dump({"baseline_sha": tampered}, f)
        _write(os.path.join(repo, "app.py"), "def f():\n    return 'broken'\n")
        self._assert_halted_on_conftest(self._stop([os.path.join(repo, "app.py")]))

    def test_deleted_session_start_file_does_not_weaken_baseline(self):
        """P4: removing the halt-dir session file changes nothing; the snapshot is in the daemon."""
        repo = self._repo("r")
        self._session_start()
        os.remove(hardtruth_hook.get_session_start_baseline_file(self.conv))
        self._tamper_conftest(repo)
        _write(os.path.join(repo, "app.py"), "def f():\n    return 'broken'\n")
        self._assert_halted_on_conftest(self._stop([os.path.join(repo, "app.py")]))

    def test_repo_without_session_start_snapshot_halts_with_guidance(self):
        """P5/L6: a repository outside every snapshot root (or created mid-session) fails
        closed, and the message says why and what to do."""
        elsewhere = os.path.join(self.test_dir, "elsewhere")
        os.makedirs(elsewhere)
        repo = self._repo("r", parent=elsewhere)
        self._session_start()
        _write(os.path.join(repo, "app.py"), "def f():\n    return 2\n")
        res = self._stop([os.path.join(repo, "app.py")])
        self.assertIn("no session-start snapshot", res.get("reason", ""))
        self.assertIn("HARDTRUTH_BASELINE_ROOTS", res.get("reason", ""))
        self.assertEqual(self.handed_off, [])

    def test_repo_created_mid_session_halts(self):
        self._session_start()
        repo = self._repo("late")
        _write(os.path.join(repo, "app.py"), "def f():\n    return 2\n")
        res = self._stop([os.path.join(repo, "app.py")])
        self.assertIn("no session-start snapshot", res.get("reason", ""))

    def test_stash_created_during_session_halts_even_backdated(self):
        """L4: the stash check compares refs/stash with its session-start value, not %ct."""
        repo = self._repo("r")
        self._session_start()
        _write(os.path.join(repo, "app.py"), "def f():\n    return 2\n")
        _write(os.path.join(repo, "tests", "test_app.py"), "def test_a():\n    pass\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = f"{int(time.time()) - 3600} +0000"
        subprocess.run(["git", "stash", "push", "--", "tests/test_app.py"], cwd=repo, env=env, check=True,
                       stdout=subprocess.PIPE)
        res = self._stop([os.path.join(repo, "app.py")])
        self.assertIn("stash", res.get("reason", ""))
        self.assertEqual(self.handed_off, [])

    def test_daemon_unreachable_at_stop_halts(self):
        repo = self._repo("r")
        self._session_start()
        _write(os.path.join(repo, "app.py"), "def f():\n    return 2\n")
        self.daemon_up = False
        res = self._stop([os.path.join(repo, "app.py")])
        self.assertNotEqual(res.get("decision"), "allow", res)
        self.assertEqual(self.handed_off, [])

    def test_snapshot_budget_cancels_queued_registrations(self):
        """With a stalling daemon, registrations not started by the deadline are cancelled
        (the SessionStart hook has a 10s timeout), and repos left without a snapshot fail
        closed rather than being trusted."""
        repos = [self._repo(f"r{i}") for i in range(20)]  # more than the 16 workers
        calls = []

        def slow(endpoint, data, timeout=None):
            time.sleep(0.6)
            calls.append(data["workspace_path"])
            return self._fake_call(endpoint, data, timeout)

        with mock.patch.object(hardtruth_hook, "call_system_one", side_effect=slow):
            t0 = time.time()
            n = hardtruth_hook.snapshot_repos_at_session_start(self.conv, self.workspace, time_budget_sec=1.0)
            elapsed = time.time() - t0
            time.sleep(1.0)  # let in-flight calls finish; cancelled ones must never run
        self.assertLess(elapsed, 2.0)
        self.assertLess(n, len(repos))
        self.assertLess(len(calls), 2 * len(repos))
        missing = [r for r in repos if (self.conv, hardtruth_hook.SESSION_START_HEAD_NS + r) not in self.daemon]
        self.assertTrue(missing)
        _write(os.path.join(missing[0], "app.py"), "def f():\n    return 2\n")
        res = self._stop([os.path.join(missing[0], "app.py")])
        self.assertIn("no session-start snapshot", res.get("reason", ""))

    def test_outside_repo_invisible_to_verifier_halts(self):
        """M2: unverified_no_workspace for an outside target is a failure, not a pass."""
        repo = self._repo("r")
        self._session_start()
        _write(os.path.join(repo, "app.py"), "def f():\n    return 2\n")
        self.handoff_status[repo] = {"success": False, "status": "unverified_no_workspace", "exit_code": 1}
        with mock.patch.object(hardtruth_hook, "run_independent_verification", None):
            res = self._stop([os.path.join(repo, "app.py")])
        self.assertIn("could not see", res.get("reason", ""))


class _FakeLedger:
    def __init__(self, premise):
        self._premise = premise

    def get_premise(self, conv_id):
        return dict(self._premise)


if __name__ == "__main__":
    unittest.main()
