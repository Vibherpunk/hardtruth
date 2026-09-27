#!/usr/bin/env python3
"""
Unit tests for the approved "automatic scoped host verification" policy for macOS-native
projects (follow-up to Bug #6, hardtruth-claude-code-fixes task).

Host (non-container) Tier 2 execution is permitted WITHOUT HARDTRUTH_TIER2_ALLOW_SUBPROCESS=1
only when ALL of:
  1. host is Darwin AND is_macos_native_project(target) is true;
  2. the resolved target project dir is inside an allowed root (default ~/dev, overridable
     via HARDTRUTH_TIER2_NATIVE_HOST_ROOTS), checked with realpath on both sides and a real
     path-prefix check (no string-prefix trick, no symlink escape);
  3. the target came from files this session actually edited (session_evidence=True,
     asserted only by the caller -- resolve_target_project_dir returning it from evidence).

These tests exercise the decision logic and env/command construction as pure functions.
They do NOT run the real reachyd test suite (no docker, network, or live daemon; no cargo
invocation).
"""

import os
import sys
import shutil
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from daemon.tier2_runner import (
    get_native_host_allowed_roots,
    is_within_native_host_allowed_roots,
    scoped_native_host_permitted,
    get_native_host_timeout_sec,
    _cargo_can_run_offline,
    apply_native_host_env,
)


class TestAllowedRoots(unittest.TestCase):

    def setUp(self):
        self._saved = os.environ.pop("HARDTRUTH_TIER2_NATIVE_HOST_ROOTS", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["HARDTRUTH_TIER2_NATIVE_HOST_ROOTS"] = self._saved
        else:
            os.environ.pop("HARDTRUTH_TIER2_NATIVE_HOST_ROOTS", None)

    def test_default_root_is_dev(self):
        roots = get_native_host_allowed_roots()
        self.assertEqual(roots, [os.path.realpath(os.path.expanduser("~/dev"))])

    def test_env_override_split_on_pathsep(self):
        a = tempfile.mkdtemp(prefix="hardtruth-root-a-")
        b = tempfile.mkdtemp(prefix="hardtruth-root-b-")
        try:
            os.environ["HARDTRUTH_TIER2_NATIVE_HOST_ROOTS"] = os.pathsep.join([a, b])
            roots = get_native_host_allowed_roots()
            self.assertEqual(set(roots), {os.path.realpath(a), os.path.realpath(b)})
        finally:
            shutil.rmtree(a, ignore_errors=True)
            shutil.rmtree(b, ignore_errors=True)

    def test_inside_allowed_root(self):
        root = tempfile.mkdtemp(prefix="hardtruth-allowed-")
        try:
            target = os.path.join(root, "vibehard", "apps", "reachyd")
            os.makedirs(target, exist_ok=True)
            self.assertTrue(is_within_native_host_allowed_roots(target, allowed_roots=[os.path.realpath(root)]))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_string_prefix_trick_denied(self):
        """'/allowed-evil' must NOT match an allowed root of '/allowed' via string prefix."""
        root = tempfile.mkdtemp(prefix="hardtruth-allowed-")
        try:
            evil = root + "-evil"
            os.makedirs(evil, exist_ok=True)
            self.assertFalse(is_within_native_host_allowed_roots(evil, allowed_roots=[os.path.realpath(root)]))
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(evil, ignore_errors=True)

    def test_outside_allowed_root_denied(self):
        root = tempfile.mkdtemp(prefix="hardtruth-allowed-")
        other = tempfile.mkdtemp(prefix="hardtruth-other-")
        try:
            self.assertFalse(is_within_native_host_allowed_roots(other, allowed_roots=[os.path.realpath(root)]))
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(other, ignore_errors=True)

    def test_symlink_escape_denied(self):
        """A symlink inside the allowed root pointing OUTSIDE it must not be treated as inside."""
        root = tempfile.mkdtemp(prefix="hardtruth-allowed-")
        outside = tempfile.mkdtemp(prefix="hardtruth-outside-")
        try:
            link = os.path.join(root, "escape_link")
            os.symlink(outside, link)
            self.assertFalse(is_within_native_host_allowed_roots(link, allowed_roots=[os.path.realpath(root)]))
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)

    def test_exact_root_match_allowed(self):
        root = tempfile.mkdtemp(prefix="hardtruth-allowed-")
        try:
            self.assertTrue(is_within_native_host_allowed_roots(root, allowed_roots=[os.path.realpath(root)]))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestScopedNativeHostPermitted(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hardtruth-dev-")
        self.inside = os.path.join(self.root, "vibehard", "apps", "reachyd")
        os.makedirs(self.inside, exist_ok=True)
        self.outside = tempfile.mkdtemp(prefix="hardtruth-outside-")
        self._allowed = [os.path.realpath(self.root)]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def _permitted(self, target, is_mac_native, is_host_darwin, session_evidence):
        # scoped_native_host_permitted calls get_native_host_allowed_roots() internally
        # (not parameterized), so patch the env var for the duration of the assertion.
        old = os.environ.get("HARDTRUTH_TIER2_NATIVE_HOST_ROOTS")
        os.environ["HARDTRUTH_TIER2_NATIVE_HOST_ROOTS"] = self._allowed[0]
        try:
            return scoped_native_host_permitted(target, is_mac_native, is_host_darwin, session_evidence)
        finally:
            if old is not None:
                os.environ["HARDTRUTH_TIER2_NATIVE_HOST_ROOTS"] = old
            else:
                os.environ.pop("HARDTRUTH_TIER2_NATIVE_HOST_ROOTS", None)

    def test_allowed_inside_dev_native_darwin_with_evidence(self):
        self.assertTrue(self._permitted(self.inside, is_mac_native=True, is_host_darwin=True, session_evidence=True))

    def test_denied_outside_allowed_roots(self):
        self.assertFalse(self._permitted(self.outside, is_mac_native=True, is_host_darwin=True, session_evidence=True))

    def test_denied_string_prefix_trick(self):
        evil = self.root + "-evil"
        os.makedirs(evil, exist_ok=True)
        try:
            self.assertFalse(self._permitted(evil, is_mac_native=True, is_host_darwin=True, session_evidence=True))
        finally:
            shutil.rmtree(evil, ignore_errors=True)

    def test_denied_no_session_evidence(self):
        """Even inside the allowed root and native+Darwin, no session evidence -> denied."""
        self.assertFalse(self._permitted(self.inside, is_mac_native=True, is_host_darwin=True, session_evidence=False))

    def test_denied_not_native(self):
        """Non-native project inside ~/dev still requires container or the explicit env var."""
        self.assertFalse(self._permitted(self.inside, is_mac_native=False, is_host_darwin=True, session_evidence=True))

    def test_denied_not_darwin_host(self):
        self.assertFalse(self._permitted(self.inside, is_mac_native=True, is_host_darwin=False, session_evidence=True))

    def test_denied_symlink_escape(self):
        outside_target = os.path.join(self.outside, "secret")
        os.makedirs(outside_target, exist_ok=True)
        link = os.path.join(self.root, "escape")
        os.symlink(outside_target, link)
        try:
            self.assertFalse(self._permitted(link, is_mac_native=True, is_host_darwin=True, session_evidence=True))
        finally:
            pass  # cleaned up by tearDown removing self.root


class TestNativeHostTimeout(unittest.TestCase):

    def setUp(self):
        self._saved = os.environ.pop("HARDTRUTH_TIER2_NATIVE_TIMEOUT_SEC", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["HARDTRUTH_TIER2_NATIVE_TIMEOUT_SEC"] = self._saved
        else:
            os.environ.pop("HARDTRUTH_TIER2_NATIVE_TIMEOUT_SEC", None)

    def test_default_is_240_and_overrides_a_smaller_caller_timeout(self):
        self.assertEqual(get_native_host_timeout_sec(30), 240)

    def test_never_shrinks_a_larger_caller_timeout(self):
        self.assertEqual(get_native_host_timeout_sec(600), 600)

    def test_env_override_respected(self):
        os.environ["HARDTRUTH_TIER2_NATIVE_TIMEOUT_SEC"] = "90"
        self.assertEqual(get_native_host_timeout_sec(30), 90)

    def test_other_paths_timeout_untouched(self):
        """This is a pure function -- callers on non-native paths simply never call it,
        so the normal timeout_sec value flows through unchanged (exercised by the fact
        that run_independent_verification only calls get_native_host_timeout_sec when
        scoped_native_host is True; see test_tier2_container_mount.py for the gate itself)."""
        self.assertEqual(get_native_host_timeout_sec(30), max(30, 240))


class TestCargoOfflineDecision(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-cargo-offline-test-")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_offline_when_lock_and_registry_present(self):
        crate = os.path.join(self.test_dir, "crate")
        cargo_home = os.path.join(self.test_dir, "cargo_home")
        os.makedirs(crate, exist_ok=True)
        os.makedirs(os.path.join(cargo_home, "registry"), exist_ok=True)
        with open(os.path.join(crate, "Cargo.lock"), "w") as f:
            f.write("# lock\n")
        self.assertTrue(_cargo_can_run_offline(crate, cargo_home))

    def test_not_offline_when_registry_missing(self):
        crate = os.path.join(self.test_dir, "crate2")
        cargo_home = os.path.join(self.test_dir, "cargo_home2")
        os.makedirs(crate, exist_ok=True)
        os.makedirs(cargo_home, exist_ok=True)
        with open(os.path.join(crate, "Cargo.lock"), "w") as f:
            f.write("# lock\n")
        self.assertFalse(_cargo_can_run_offline(crate, cargo_home))

    def test_not_offline_when_lock_missing(self):
        crate = os.path.join(self.test_dir, "crate3")
        cargo_home = os.path.join(self.test_dir, "cargo_home3")
        os.makedirs(crate, exist_ok=True)
        os.makedirs(os.path.join(cargo_home, "registry"), exist_ok=True)
        self.assertFalse(_cargo_can_run_offline(crate, cargo_home))

    def test_offline_when_lock_at_workspace_root(self):
        """A crate inside a cargo workspace may not have its own Cargo.lock (only the
        workspace root does) -- _cargo_can_run_offline must check both."""
        ws_root = os.path.join(self.test_dir, "workspace")
        crate = os.path.join(ws_root, "apps", "reachyd")
        cargo_home = os.path.join(self.test_dir, "cargo_home4")
        os.makedirs(crate, exist_ok=True)
        os.makedirs(os.path.join(cargo_home, "registry"), exist_ok=True)
        with open(os.path.join(ws_root, "Cargo.toml"), "w") as f:
            f.write("[workspace]\nmembers = [\"apps/reachyd\"]\n")
        with open(os.path.join(ws_root, "Cargo.lock"), "w") as f:
            f.write("# lock\n")
        with open(os.path.join(crate, "Cargo.toml"), "w") as f:
            f.write('[package]\nname = "reachyd"\n')

        self.assertTrue(_cargo_can_run_offline(crate, cargo_home))


class TestApplyNativeHostEnv(unittest.TestCase):

    def test_prepends_cargo_bin_when_present_and_not_already_on_path(self):
        with tempfile.TemporaryDirectory() as fake_home:
            cargo_bin = os.path.join(fake_home, ".cargo", "bin")
            os.makedirs(cargo_bin, exist_ok=True)
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = fake_home
            try:
                env = {"PATH": "/usr/bin:/bin"}
                apply_native_host_env(env)
                self.assertTrue(env["PATH"].startswith(cargo_bin + os.pathsep))
                self.assertIn("/usr/bin", env["PATH"])
                self.assertEqual(env["CARGO_HOME"], os.path.join(fake_home, ".cargo"))
                self.assertEqual(env["RUSTUP_HOME"], os.path.join(fake_home, ".rustup"))
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                else:
                    os.environ.pop("HOME", None)

    def test_does_not_duplicate_cargo_bin_already_on_path(self):
        with tempfile.TemporaryDirectory() as fake_home:
            cargo_bin = os.path.join(fake_home, ".cargo", "bin")
            os.makedirs(cargo_bin, exist_ok=True)
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = fake_home
            try:
                env = {"PATH": f"{cargo_bin}{os.pathsep}/usr/bin"}
                apply_native_host_env(env)
                self.assertEqual(env["PATH"].count(cargo_bin), 1)
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                else:
                    os.environ.pop("HOME", None)

    def test_never_overrides_explicit_cargo_home(self):
        env = {"PATH": "/usr/bin", "CARGO_HOME": "/custom/cargo/home"}
        apply_native_host_env(env)
        self.assertEqual(env["CARGO_HOME"], "/custom/cargo/home")

    def test_no_cargo_bin_dir_leaves_path_unchanged(self):
        with tempfile.TemporaryDirectory() as fake_home:
            # No ~/.cargo/bin created under fake_home.
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = fake_home
            try:
                env = {"PATH": "/usr/bin:/bin"}
                apply_native_host_env(env)
                self.assertEqual(env["PATH"], "/usr/bin:/bin")
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                else:
                    os.environ.pop("HOME", None)


if __name__ == "__main__":
    unittest.main()
