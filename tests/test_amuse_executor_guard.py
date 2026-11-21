#!/usr/bin/env python3
"""Unit tests for the Amuse terminal executor HardTruth guard."""

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from client.adapters.amuse import executor_guard as guard


class TestAmuseExecutorGuard(unittest.TestCase):
    def test_blocks_exit_code_masking_and_test_pipes(self):
        for bad in (
            "pytest tests/ || true",
            "cargo test || exit 0",
            "npm test; true",
            "vitest run && exit 0",
            "set +e && pytest tests/",
            "pytest tests/ | tail -n 20",
            "go test ./... | grep PASS",
        ):
            ok, reason = guard.evaluate_command(bad)
            self.assertFalse(ok, f"Expected block for {bad!r}")
            self.assertIn("exit-code masking", reason or "")

    def test_allows_normal_inspection_pipes_and_pipefail_tests(self):
        for good in (
            "ls -R ~/Downloads | head -n 20",
            "df -h / | awk 'NR==2 {print $4}'",
            "set -o pipefail && pytest tests/ | tail -n 20",
            "pytest tests/",
        ):
            ok, reason = guard.evaluate_command(good)
            self.assertTrue(ok, f"Expected allow for {good!r}, got {reason}")

    def test_port_8000_quarantine_vs_remote_windmill(self):
        for bad in (
            "curl http://127.0.0.1:8000/health",
            "uvicorn app:app --port 8000",
            "python3 -m http.server -p 8000",
            "lsof -i :8000",
            "kill $(lsof -t -i:8000)",
        ):
            ok, reason = guard.evaluate_command(bad)
            self.assertFalse(ok, f"Expected block for {bad!r}")
            self.assertIn("Port 8000", reason or "")

        ok, reason = guard.evaluate_command("curl -sf http://umbrel-dell.tail3b031e.ts.net:8000/api/version")
        self.assertTrue(ok, f"Remote Windmill :8000 should be allowed, got {reason}")

    def test_blocks_hardtruth_tamper_vectors(self):
        for bad in (
            "cat ~/.hardtruth/daemon_api.key",
            "cat ~/.hardtruth/daemon_hmac.key",
            "rm -rf ~/.hardtruth/halts",
            "python3 -c 'purge_records([\"goose\"])'",
            "git update-ref refs/hardtruth/baseline/abc HEAD",
            "HARDTRUTH_TIER2_ALLOW_SUBPROCESS=1 python3 script.py",
        ):
            ok, reason = guard.evaluate_command(bad)
            self.assertFalse(ok, f"Expected block for {bad!r}")
            self.assertIn("integrity guard", reason or "")

    def test_records_execution_with_bearer_token(self):
        captured = []
        server_logs = []

        class MockHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                server_logs.append(fmt % args if args else fmt)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                captured.append((self.path, self.headers.get("Authorization"), body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"recorded"}')

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        fd, key_path = tempfile.mkstemp(prefix="ht-amuse-key-")
        os.close(fd)
        try:
            with open(key_path, "w", encoding="utf-8") as f:
                f.write("test-amuse-secret-token\n")
            ok = guard.record_execution(
                conversation_id="amuse-chat-1-run-2",
                command="echo hello",
                cwd="/tmp",
                exit_code=0,
                stdout="hello\n",
                stderr="",
                daemon_url=f"http://127.0.0.1:{srv.server_address[1]}",
                api_key_path=key_path,
            )
            self.assertTrue(ok)
            self.assertEqual(len(captured), 1)
            path, auth, body = captured[0]
            self.assertEqual(path, "/v1/ledger/record")
            self.assertEqual(auth, "Bearer test-amuse-secret-token")
            self.assertEqual(body["conversationId"], "amuse-chat-1-run-2")
            self.assertEqual(body["observed_exit_code"], 0)
            self.assertEqual(body["harness_status"], "no_error")
        finally:
            srv.shutdown()
            srv.server_close()
            os.remove(key_path)


if __name__ == "__main__":
    unittest.main()
