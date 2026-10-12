#!/usr/bin/env python3
"""
Unit tests for Bug #4 (Tier 2 targets the wrong project) and Bug #5 (duplicate
definitions in daemon/tier2_runner.py) from the hardtruth-claude-code-fixes task.

Bug #4: resolve_target_project_dir must derive its target exclusively from files this
session actually edited (the modified_files list / the session's own ledger), never from
"some repo under cwd with git changes" -- so it must NOT wander into an unrelated sibling
project that merely has uncommitted changes lying around, and must return None (meaning
"nothing for Tier 2 to verify") when the session edited nothing at all.

Bug #5: find_project_root_for_file, resolve_target_project_dir, and is_macos_native_project
must each be defined exactly once in daemon/tier2_runner.py.

No docker, network, or live daemon required.
"""

import os
import sys
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
DAEMON_DIR = os.path.join(REPO_ROOT, "daemon")
if DAEMON_DIR not in sys.path:
    sys.path.insert(0, DAEMON_DIR)  # so tier2_runner's bare "from ledger import _ledger" resolves

from daemon.tier2_runner import resolve_target_project_dir, find_project_root_for_file, is_macos_native_project
import daemon.tier2_runner as tier2_runner_module


def _git_init(path):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)


class TestBug5NoDuplicateDefinitions(unittest.TestCase):

    def test_each_helper_defined_exactly_once(self):
        src = open(os.path.join(REPO_ROOT, "daemon", "tier2_runner.py"), "r", encoding="utf-8").read()
        for name in ("find_project_root_for_file", "resolve_target_project_dir", "is_macos_native_project"):
            count = src.count(f"def {name}(")
            self.assertEqual(count, 1, f"{name} should be defined exactly once, found {count}")


class TestBug4TargetResolution(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-tier2-target-test-")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_no_edits_returns_none(self):
        """No modified_files and no conv_id (no session edits at all) -> None: nothing for
        Tier 2 to verify, even if the workspace itself is a valid project."""
        proj = os.path.join(self.test_dir, "hardtruth-fix-like")
        _git_init(proj)
        with open(os.path.join(proj, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        os.makedirs(os.path.join(proj, "tests"), exist_ok=True)

        result = resolve_target_project_dir(proj, modified_files=None, conv_id=None)
        self.assertIsNone(result)

    def test_does_not_wander_into_unrelated_sibling_with_git_changes(self):
        """A sibling project with its own uncommitted changes must NOT be selected just
        because it sits under the same parent directory -- this reproduces the real
        incident (npm test in an unrelated `goose` repo, cargo test in reachyd)."""
        parent = os.path.join(self.test_dir, "dev")
        os.makedirs(parent, exist_ok=True)

        # The actual workspace: no runner at its root, no edits point anywhere useful.
        workspace = os.path.join(parent, "hardtruth-fix")
        _git_init(workspace)

        # An unrelated sibling project with a real runner AND uncommitted changes --
        # this is what the old git-status-scanning fallback would have wrongly targeted.
        sibling = os.path.join(parent, "goose")
        _git_init(sibling)
        with open(os.path.join(sibling, "package.json"), "w") as f:
            f.write('{"name": "goose", "scripts": {"test": "echo ok"}}')
        subprocess.run(["git", "add", "package.json"], cwd=sibling, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=sibling, check=True)
        with open(os.path.join(sibling, "package.json"), "a") as f:
            f.write("\n// dirty, unrelated to this session\n")

        result = resolve_target_project_dir(parent, modified_files=[], conv_id=None)
        self.assertIsNone(result)
        self.assertNotEqual(result, sibling)

    def test_resolves_to_project_containing_an_actually_edited_file(self):
        """A file the session actually edited, nested under the workspace, correctly
        resolves to ITS project root (not the parent, not an unrelated sibling).

        Uses pytest/Python fixtures rather than npm/cargo projects so this test is
        hermetic in any environment that can run this test suite at all (e.g. a Tier 2
        verification container, which has pytest but not necessarily node or cargo).
        """
        parent = os.path.join(self.test_dir, "dev")
        os.makedirs(parent, exist_ok=True)

        target_proj = os.path.join(parent, "vibehard", "apps", "reachyd")
        _git_init(os.path.join(parent, "vibehard"))
        os.makedirs(os.path.join(target_proj, "tests"), exist_ok=True)
        with open(os.path.join(target_proj, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        with open(os.path.join(target_proj, "tests", "test_x.py"), "w") as f:
            f.write("def test_x(): pass\n")

        edited_file = os.path.join(target_proj, "src", "main.py")
        os.makedirs(os.path.dirname(edited_file), exist_ok=True)
        with open(edited_file, "w") as f:
            f.write("def main(): pass\n")

        # An unrelated sibling project -- ALSO pytest-runnable -- that should NOT be
        # picked even though it exists and has its own detectable runner, proving
        # selection follows the actually-edited file rather than "any runnable project".
        unrelated = os.path.join(parent, "goose")
        _git_init(unrelated)
        os.makedirs(os.path.join(unrelated, "tests"), exist_ok=True)
        with open(os.path.join(unrelated, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        with open(os.path.join(unrelated, "tests", "test_y.py"), "w") as f:
            f.write("def test_y(): pass\n")

        result = resolve_target_project_dir(parent, modified_files=[edited_file], conv_id=None)
        self.assertEqual(os.path.abspath(result), os.path.abspath(target_proj))

    def test_workspace_itself_used_when_it_has_a_runner_and_was_edited(self):
        """If the workspace_dir itself has a runner and a session-edited file is inside it
        (even if that file's own nearest root doesn't resolve to a runner), workspace_dir
        is a safe, session-scoped fallback."""
        proj = os.path.join(self.test_dir, "hardtruth-fix")
        _git_init(proj)
        os.makedirs(os.path.join(proj, "tests"), exist_ok=True)
        with open(os.path.join(proj, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")

        edited_file = os.path.join(proj, "client", "hardtruth_hook.py")
        os.makedirs(os.path.dirname(edited_file), exist_ok=True)
        with open(edited_file, "w") as f:
            f.write("# edited\n")

        result = resolve_target_project_dir(proj, modified_files=[edited_file], conv_id=None)
        self.assertEqual(os.path.abspath(result), os.path.abspath(proj))

    def test_relative_modified_file_paths_resolved_against_workspace(self):
        """modified_files may be relative paths (as stored in the ledger/premise) --
        these must resolve against workspace_dir, not cwd.

        Uses a pytest/Python fixture rather than an npm project so this test is hermetic
        in any environment that can run this test suite at all (e.g. a Tier 2
        verification container, which has pytest but not necessarily node installed).
        """
        parent = os.path.join(self.test_dir, "dev")
        os.makedirs(parent, exist_ok=True)
        target_proj = os.path.join(parent, "myproj")
        _git_init(target_proj)
        os.makedirs(os.path.join(target_proj, "tests"), exist_ok=True)
        with open(os.path.join(target_proj, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        with open(os.path.join(target_proj, "tests", "test_index.py"), "w") as f:
            f.write("def test_index(): pass\n")
        os.makedirs(os.path.join(target_proj, "src"), exist_ok=True)
        with open(os.path.join(target_proj, "src", "index.py"), "w") as f:
            f.write("# x\n")

        result = resolve_target_project_dir(
            target_proj, modified_files=["src/index.py"], conv_id=None
        )
        self.assertEqual(os.path.abspath(result), os.path.abspath(target_proj))

    def test_find_project_root_for_file_stops_at_boundary(self):
        parent = os.path.join(self.test_dir, "dev")
        os.makedirs(parent, exist_ok=True)
        with open(os.path.join(parent, "Cargo.toml"), "w") as f:
            f.write("[workspace]\n")
        nested = os.path.join(parent, "sub", "file.py")
        os.makedirs(os.path.dirname(nested), exist_ok=True)
        with open(nested, "w") as f:
            f.write("x = 1\n")

        # Boundary excludes the parent's Cargo.toml -- should find nothing (returns None)
        # rather than escaping the boundary.
        root = find_project_root_for_file(nested, boundary=os.path.join(parent, "sub"))
        self.assertIsNone(root)


if __name__ == "__main__":
    unittest.main()
