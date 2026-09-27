#!/usr/bin/env python3
"""
Unit tests for Bug #6 (container mount for a cargo-workspace crate, and the macOS-native
gate message) from the hardtruth-claude-code-fixes task.

- find_cargo_workspace_root must locate the enclosing workspace root for a crate that
  inherits shared settings (e.g. edition.workspace = true), so run_container_verification
  can mount the workspace root instead of just the crate subdir.
- A macOS-native project (Cocoa/objc/CoreAudio) must NOT silently bypass the
  HARDTRUTH_TIER2_ALLOW_SUBPROCESS gate just because the host is Darwin -- it must fall
  through to the SAME operator-authorization gate, with a message that clearly explains
  container verification is impossible and names the exact env var to set.

No docker, network, or live daemon required (docker-dependent behavior is exercised only
indirectly via find_cargo_workspace_root, which is pure filesystem logic).
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

from daemon.tier2_runner import find_cargo_workspace_root, run_independent_verification


def _git_init(path):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


class TestFindCargoWorkspaceRoot(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-cargo-ws-test-")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_finds_workspace_root_for_nested_crate(self):
        ws_root = os.path.join(self.test_dir, "vibehard")
        os.makedirs(ws_root, exist_ok=True)
        with open(os.path.join(ws_root, "Cargo.toml"), "w") as f:
            f.write('[workspace]\nmembers = ["apps/reachyd"]\n\n[workspace.package]\nedition = "2021"\n')

        crate_dir = os.path.join(ws_root, "apps", "reachyd")
        os.makedirs(crate_dir, exist_ok=True)
        with open(os.path.join(crate_dir, "Cargo.toml"), "w") as f:
            f.write('[package]\nname = "reachyd"\nedition.workspace = true\n')

        result = find_cargo_workspace_root(crate_dir)
        self.assertEqual(os.path.abspath(result), os.path.abspath(ws_root))

    def test_standalone_crate_returns_itself(self):
        crate_dir = os.path.join(self.test_dir, "standalone_crate")
        os.makedirs(crate_dir, exist_ok=True)
        with open(os.path.join(crate_dir, "Cargo.toml"), "w") as f:
            f.write('[package]\nname = "standalone"\nedition = "2021"\n')

        result = find_cargo_workspace_root(crate_dir)
        self.assertEqual(os.path.abspath(result), os.path.abspath(crate_dir))

    def test_stops_at_unrelated_ancestor_manifest(self):
        """An ancestor Cargo.toml with no [workspace] table is an unrelated project
        boundary -- must not be treated as a workspace root."""
        unrelated_root = os.path.join(self.test_dir, "unrelated")
        os.makedirs(unrelated_root, exist_ok=True)
        with open(os.path.join(unrelated_root, "Cargo.toml"), "w") as f:
            f.write('[package]\nname = "unrelated_root_crate"\n')

        crate_dir = os.path.join(unrelated_root, "sub", "crate")
        os.makedirs(crate_dir, exist_ok=True)
        with open(os.path.join(crate_dir, "Cargo.toml"), "w") as f:
            f.write('[package]\nname = "crate"\n')

        result = find_cargo_workspace_root(crate_dir)
        self.assertEqual(os.path.abspath(result), os.path.abspath(crate_dir))


class TestMacosNativeGateMessage(unittest.TestCase):
    """
    is_macos_native_project detection itself is exercised by test_reachy_circuit_breaker.py.
    These tests target the OTHER half of Bug #6: when container verification is impossible
    for a macOS-native project and the operator has NOT opted into host execution, the gate
    must reject clearly rather than silently running unsandboxed -- and must not silently
    treat "host is Darwin" as the operator's authorization.
    """

    def _run_without_auto_authorization(self, workspace_path):
        """Runs run_independent_verification with CI / PYTEST_CURRENT_TEST / an explicit
        HARDTRUTH_TIER2_ALLOW_SUBPROCESS=1 all removed, so only the real gate default
        applies -- simulating an operator who has not opted in, run outside pytest/CI."""
        saved = {}
        for key in ("CI", "PYTEST_CURRENT_TEST", "HARDTRUTH_TIER2_ALLOW_SUBPROCESS"):
            saved[key] = os.environ.pop(key, None)
        os.environ["HARDTRUTH_TIER2_CONTAINER"] = "0"  # force straight to the subprocess gate
        try:
            return run_independent_verification(workspace_path, timeout_sec=5)
        finally:
            os.environ.pop("HARDTRUTH_TIER2_CONTAINER", None)
            for key, val in saved.items():
                if val is not None:
                    os.environ[key] = val

    def test_macos_native_project_rejected_with_clear_message_when_not_authorized(self):
        reachyd_dir = "/Users/ai/dev/vibehard/apps/reachyd"
        if not os.path.isdir(reachyd_dir):
            self.skipTest("reachyd fixture project not present on this host")

        result = self._run_without_auto_authorization(reachyd_dir)
        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("status"), "unverified_no_isolation")
        output = result.get("output", "")
        self.assertIn("macOS-native project", output)
        self.assertIn("container verification not possible", output)
        self.assertIn("HARDTRUTH_TIER2_ALLOW_SUBPROCESS=1", output)
        self.assertIn("operator decision", output)

    def test_explicit_operator_opt_in_still_permits_execution(self):
        """Setting HARDTRUTH_TIER2_ALLOW_SUBPROCESS=1 explicitly still works (the default
        itself is unchanged -- only the automatic Darwin-host bypass was removed)."""
        reachyd_dir = "/Users/ai/dev/vibehard/apps/reachyd"
        if not os.path.isdir(reachyd_dir):
            self.skipTest("reachyd fixture project not present on this host")

        saved = {}
        for key in ("CI", "PYTEST_CURRENT_TEST"):
            saved[key] = os.environ.pop(key, None)
        os.environ["HARDTRUTH_TIER2_ALLOW_SUBPROCESS"] = "1"
        os.environ["HARDTRUTH_TIER2_CONTAINER"] = "0"
        try:
            result = run_independent_verification(reachyd_dir, timeout_sec=30)
        finally:
            os.environ.pop("HARDTRUTH_TIER2_CONTAINER", None)
            os.environ.pop("HARDTRUTH_TIER2_ALLOW_SUBPROCESS", None)
            for key, val in saved.items():
                if val is not None:
                    os.environ[key] = val

        self.assertNotEqual(result.get("status"), "unverified_no_isolation")


if __name__ == "__main__":
    unittest.main()
