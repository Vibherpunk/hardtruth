#!/usr/bin/env python3
"""
Regression tests: Tier 2 container verification for Go projects and projects whose tests
need a database (2026-10-11).

Before the fix, every Go module with an external dependency failed Tier 2 on any machine:
the golang:alpine container ran with --network none and no module cache, so `go test`
died fetching modules from proxy.golang.org. Tests that read TEST_DATABASE_URL had no
database either.

The fix (daemon/tier2_runner.py):
- mounts the host's Go module cache read-only, proxy off, toolchain pinned to go.mod;
- keeps the build cache in tmpfs (no cross-run cache the code under test could poison);
- gives tests that read TEST_DATABASE_URL a throwaway Postgres on an internal network
  (no internet route), always torn down;
- raises the Go timeout floor (cold builds) within the Stop hook's 180 s budget.

The live tests need Docker and are skipped without it.
"""

import os
import sys
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from daemon import tier2_runner as t2


def _write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(body)


def _docker_ok():
    d = shutil.which("docker")
    if not d:
        return False
    try:
        return subprocess.run([d, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0
    except Exception:
        return False


class TestGoImage(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ht-go-img-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_go_directive(self):
        _write(os.path.join(self.d, "go.mod"), "module x\n\ngo 1.23.0\n")
        self.assertEqual(t2.go_container_image(self.d), "golang:1.23-alpine")

    def test_toolchain_wins(self):
        _write(os.path.join(self.d, "go.mod"), "module x\n\ngo 1.22\n\ntoolchain go1.24.3\n")
        self.assertEqual(t2.go_container_image(self.d), "golang:1.24-alpine")

    def test_fallback(self):
        self.assertEqual(t2.go_container_image(self.d), t2.GO_IMAGE_FALLBACK)


class TestNeedsTestDatabase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ht-db-detect-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_detects_go_test_marker(self):
        _write(os.path.join(self.d, "internal", "x", "x_test.go"), 'package x\nvar u = os.Getenv("TEST_DATABASE_URL")\n')
        self.assertTrue(t2.needs_test_database(self.d))

    def test_ignores_non_test_files_and_vendor(self):
        _write(os.path.join(self.d, "main.go"), 'var u = os.Getenv("TEST_DATABASE_URL")\n')
        _write(os.path.join(self.d, "vendor", "y", "y_test.go"), 'os.Getenv("TEST_DATABASE_URL")\n')
        self.assertFalse(t2.needs_test_database(self.d))

    def test_opt_out(self):
        _write(os.path.join(self.d, "x_test.go"), 'os.Getenv("TEST_DATABASE_URL")\n')
        with mock.patch.dict(os.environ, {"HARDTRUTH_TIER2_DB": "0"}):
            self.assertFalse(t2.needs_test_database(self.d))


class TestModcache(unittest.TestCase):
    def test_env_override(self):
        d = tempfile.mkdtemp(prefix="ht-modcache-")
        try:
            with mock.patch.dict(os.environ, {"GOMODCACHE": d}):
                self.assertEqual(t2.host_go_modcache(), os.path.abspath(d))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class _FakeProc:
    def __init__(self, rc=0, out=""):
        self.returncode = rc
        self.stdout = out


class TestContainerArgs(unittest.TestCase):
    """Captures the docker command lines without a docker daemon."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ht-go-args-")
        _write(os.path.join(self.d, "go.mod"), "module x\n\ngo 1.23.0\n")
        self.cache = tempfile.mkdtemp(prefix="ht-go-cache-")
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
        shutil.rmtree(self.cache, ignore_errors=True)

    def _fake_run(self, run_rc=0, raise_on_run=False):
        def fake(args, **kw):
            self.calls.append((list(args), kw))
            if args[1:3] == ["run", "--rm"] and "-d" not in args:
                if raise_on_run:
                    raise RuntimeError("boom")
                return _FakeProc(run_rc, "ok")
            return _FakeProc(0, "")
        return fake

    def _run(self, **kw):
        with mock.patch.object(t2.shutil, "which", side_effect=lambda b: "/usr/bin/docker" if b == "docker" else None), \
             mock.patch.object(t2.subprocess, "run", side_effect=self._fake_run(**kw)), \
             mock.patch.dict(os.environ, {"GOMODCACHE": self.cache}):
            return t2.run_container_verification(self.d, "go test ./...", timeout_sec=30)

    def _test_run_call(self):
        return [c for c in self.calls if c[0][1:3] == ["run", "--rm"] and "-d" not in c[0]][0]

    def test_go_without_db_is_offline_with_ro_modcache(self):
        res = self._run()
        self.assertTrue(res["success"])
        args, kw = self._test_run_call()
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn(f"{self.cache}:/go/pkg/mod:ro", args)
        for env in ("GOPROXY=off", "GOTOOLCHAIN=local", "GOCACHE=/tmp/go-build", "GOMODCACHE=/go/pkg/mod"):
            self.assertIn(env, args)
        self.assertIn("golang:1.23-alpine", args)
        self.assertGreaterEqual(kw["timeout"], 90)
        self.assertFalse(any(a.startswith("TEST_DATABASE_URL=") for a in args))

    def test_db_sidecar_internal_network_and_teardown(self):
        _write(os.path.join(self.d, "a_test.go"), 'os.Getenv("TEST_DATABASE_URL")\n')
        self._run()
        net_create = [c[0] for c in self.calls if c[0][1:3] == ["network", "create"]]
        self.assertEqual(len(net_create), 1)
        self.assertIn("--internal", net_create[0])
        network = net_create[0][-1]
        args, _ = self._test_run_call()
        self.assertEqual(args[args.index("--network") + 1], network)
        self.assertTrue(any(a.startswith("TEST_DATABASE_URL=postgres://") for a in args))
        cmds = [c[0][1:3] for c in self.calls]
        self.assertIn(["rm", "-f"], cmds)
        self.assertIn(["network", "rm"], cmds)

    def test_db_torn_down_even_if_test_run_raises(self):
        _write(os.path.join(self.d, "a_test.go"), 'os.Getenv("TEST_DATABASE_URL")\n')
        self._run(raise_on_run=True)
        cmds = [c[0][1:3] for c in self.calls]
        self.assertIn(["rm", "-f"], cmds)
        self.assertIn(["network", "rm"], cmds)

    def test_failed_tests_still_fail(self):
        res = self._run(run_rc=1)
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "failed")


@unittest.skipUnless(_docker_ok() and shutil.which("go"), "needs docker and a host Go toolchain")
class TestLiveGoContainer(unittest.TestCase):
    """End to end: a Go module whose test needs the DB reaches it, cannot reach the
    internet, and a failing assertion still fails the gate."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ht-go-live-")
        _write(os.path.join(self.d, "go.mod"), "module htlive\n\ngo 1.23\n")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _test(self, body):
        _write(os.path.join(self.d, "live_test.go"), body)
        return t2.run_container_verification(self.d, "go test ./...", timeout_sec=30)

    def test_db_reachable_internet_not(self):
        res = self._test('''package htlive
import ("net"; "os"; "testing"; "time")
func TestDB(t *testing.T) {
	if os.Getenv("TEST_DATABASE_URL") == "" { t.Fatal("TEST_DATABASE_URL not provided") }
	c, err := net.DialTimeout("tcp", "db:5432", 5*time.Second)
	if err != nil { t.Fatalf("db unreachable: %v", err) }
	c.Close()
	if c2, err := net.DialTimeout("tcp", "1.1.1.1:443", 3*time.Second); err == nil { c2.Close(); t.Fatal("internet reachable from sealed container") }
}
''')
        self.assertTrue(res["success"], res.get("output"))
        self.assertEqual(res["isolation"], "container_read_only")

    def test_failing_test_fails_gate(self):
        res = self._test('package htlive\nimport "testing"\nfunc TestNo(t *testing.T) { t.Fatal("expected failure") }\n')
        self.assertFalse(res["success"])

    def test_no_leftover_networks(self):
        self._test('package htlive\nimport ("os";"testing")\nfunc TestE(t *testing.T){ _ = os.Getenv("TEST_DATABASE_URL") }\n')
        out = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"], stdout=subprocess.PIPE, text=True).stdout
        mine = [n for n in out.split() if n.startswith(f"hardtruth-t2-{os.getpid()}-")]
        self.assertEqual(mine, [])


if __name__ == "__main__":
    unittest.main()
