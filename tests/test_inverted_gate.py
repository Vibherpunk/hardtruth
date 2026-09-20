#!/usr/bin/env python3
"""
Comprehensive tests for HardTruth Inverted Gate & Language-Independent Verification:
1. Language-independent Rule 1: Source file modified + 0 tests -> HALT
2. Documentation file (.md) modified + 0 tests -> ALLOW
3. Multi-language completions blocked on unresolved failures (Spanish, French, German, Chinese)
4. Multi-language completions allowed when suite is verified green
5. Informal English completions ("10/10 green", "zero failures") blocked on failure
6. Sliding window immunity: 10 echo/ls commands cannot age out a failed test
7. Exploratory command failures (grep exit 1) never deadlock the agent
"""

import os
import sys
import json
import shutil
import uuid
import subprocess
import tempfile
import unittest

HOOK_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../client/hardtruth_hook.py"))
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class TestInvertedGate(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-gate-test-")
        self.ledger_file = os.path.join(self.test_dir, "daemon_ledger.jsonl")
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700, exist_ok=True)
        self.conv_id = f"test-gate-{uuid.uuid4().hex}"
        # Round 8 (#6): token-less isolation in the process env (hook helpers may run
        # in-process) so no test run writes into the physical daemon ledger.
        os.environ["HARDTRUTH_API_KEY"] = os.path.join(self.test_dir, "no-such-key")
        self.env = {
            "HARDTRUTH_LEDGER_PATH": self.ledger_file,
            "HARDTRUTH_DAEMON_LEDGER": self.ledger_file,
            "HARDTRUTH_HALT_DIR": self.halt_dir,
            "SYSTEM_ONE_URL": "http://127.0.0.1:8000",
            # Round 8 (#6): run hook subprocesses token-less so test suites never
            # write records into the physical daemon ledger (daemon rejects with 401;
            # the hook degrades gracefully to its local temp ledger).
            "HARDTRUTH_API_KEY": os.path.join(self.test_dir, "no-such-key")
        }

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def run_hook(self, mode: str, payload: dict) -> dict:
        env = os.environ.copy()
        env.update(self.env)
        proc = subprocess.run(
            ["python3", HOOK_SCRIPT, mode],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True
        )
        out_str = proc.stdout.decode("utf-8").strip()
        return json.loads(out_str) if out_str else {}

    def test_rule_1_source_modified_zero_tests_halts(self):
        """Rule 1: If executable source files are modified and 0 tests ran, HALT immediately."""
        conv = f"rule1-{uuid.uuid4().hex}"
        # Agent modified a source file
        self.run_hook("post_tool", {
            "toolCall": {"name": "write_to_file", "args": {"TargetFile": "/path/to/app.py"}},
            "stepIdx": 1,
            "conversationId": conv
        })

        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "I finished the changes to the app.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {"conversationId": conv, "transcriptPath": transcript_path}
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Source code files were modified", res.get("reason", ""))
        self.assertIn("NO verification commands", res.get("reason", ""))

    def test_doc_only_modification_allows_stop(self):
        """Editing only markdown documentation does NOT trigger Rule 1."""
        conv = f"doc-{uuid.uuid4().hex}"
        self.run_hook("post_tool", {
            "toolCall": {"name": "write_to_file", "args": {"TargetFile": "/path/to/README.md"}},
            "stepIdx": 1,
            "conversationId": conv
        })

        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "I updated the documentation in README.md.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {"conversationId": conv, "transcriptPath": transcript_path}
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "allow")

    def test_multilang_unresolved_failure_blocked(self):
        """Rule 2 halts Spanish, French, German, Chinese completion claims on unresolved failure."""
        multilang_claims = [
            ("es", "Todos los tests pasaron exitosamente, cero errores."),
            ("fr", "Tous les tests unitaires sont passés avec succès."),
            ("de", "Alle Tests erfolgreich bestanden, keine Fehler."),
            ("zh", "所有单元测试均已通过，零错误。")
        ]

        for lang, claim in multilang_claims:
            conv = f"multi-fail-{lang}-{uuid.uuid4().hex}"
            # Record failing pytest
            self.run_hook("post_tool", {
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/"}},
                "stepIdx": 1,
                "conversationId": conv,
                "error": "exit status 1"
            })

            with tempfile.NamedTemporaryFile("w", delete=False) as tf:
                tf.write(json.dumps({
                    "type": "PLANNER_RESPONSE",
                    "content": claim,
                    "tool_calls": []
                }) + "\n")
                transcript_path = tf.name

            payload = {"conversationId": conv, "transcriptPath": transcript_path}
            res = self.run_hook("stop", payload)
            os.remove(transcript_path)

            self.assertEqual(res.get("decision"), "continue", f"Failed to halt for language {lang}")
            self.assertIn("Unresolved test failures exist", res.get("reason", ""))

    def test_informal_english_claims_blocked_on_failure(self):
        """Ordinary English phrasings missed by old regex are caught by inverted gate."""
        informal_claims = [
            "10/10 green across the suite, everything is solid now.",
            "The full suite came back clean, zero failures across every module.",
            "Everything has been validated end to end and the suite is green."
        ]

        for claim in informal_claims:
            conv = f"informal-{uuid.uuid4().hex}"
            self.run_hook("post_tool", {
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/"}},
                "stepIdx": 1,
                "conversationId": conv,
                "error": "exit status 1"
            })

            with tempfile.NamedTemporaryFile("w", delete=False) as tf:
                tf.write(json.dumps({
                    "type": "PLANNER_RESPONSE",
                    "content": claim,
                    "tool_calls": []
                }) + "\n")
                transcript_path = tf.name

            payload = {"conversationId": conv, "transcriptPath": transcript_path}
            res = self.run_hook("stop", payload)
            os.remove(transcript_path)

            self.assertEqual(res.get("decision"), "continue")
            self.assertIn("Unresolved test failures exist", res.get("reason", ""))

    def test_sliding_window_immunity(self):
        """10 echo commands cannot age out a failed pytest."""
        conv = f"slide-{uuid.uuid4().hex}"
        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "cargo test"}},
            "stepIdx": 1,
            "conversationId": conv,
            "error": "exit status 101"
        })

        # Run 10 harmless commands
        for i in range(10):
            self.run_hook("post_tool", {
                "toolCall": {"name": "run_command", "args": {"CommandLine": f"echo 'harmless step {i}'"}},
                "stepIdx": i + 2,
                "conversationId": conv
            })

        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "All tests are passing cleanly now.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {"conversationId": conv, "transcriptPath": transcript_path}
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("cargo test", res.get("reason", ""))

    def test_exploratory_grep_failure_does_not_deadlock(self):
        """grep -q failing with exit 1 does not trigger Rule 2 halt."""
        conv = f"grep-{uuid.uuid4().hex}"
        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "grep -q 'not_found' file.py"}},
            "stepIdx": 1,
            "conversationId": conv,
            "error": "exit status 1"
        })

        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "I searched the file with grep and found no matches.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {"conversationId": conv, "transcriptPath": transcript_path}
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "allow")


if __name__ == "__main__":
    unittest.main()
