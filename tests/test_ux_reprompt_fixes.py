#!/usr/bin/env python3
"""
Regression suite for HardTruth UX, quote-aware tainting, course-correction prompts,
Antigravity circuit breaker handoff, and prose/Rule 4A false-positive fixes.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HOOK_SCRIPT = os.path.join(REPO_ROOT, "client", "hardtruth_hook.py")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from daemon.ledger import (  # noqa: E402
    DaemonLedger,
    get_taint_reason,
    is_taint_applicable_command,
    is_tainted_shell_command,
)


class TestUxRepromptAndFalsePositiveFixes(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-ux-fix-")
        self.ledger_path = os.path.join(self.test_dir, "ledger.jsonl")
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700, exist_ok=True)
        self.env = os.environ.copy()
        self.env["HARDTRUTH_LEDGER_PATH"] = self.ledger_path
        self.env["HARDTRUTH_HALT_DIR"] = self.halt_dir
        self.env["HARDTRUTH_SKIP_TIER2"] = "1"
        self.env["HARDTRUTH_API_KEY"] = os.path.join(self.test_dir, "missing-key")
        self.env["HARDTRUTH_HARNESS"] = "antigravity"
        self.env["SYSTEM_ONE_URL"] = "http://127.0.0.1:9"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _run_hook(self, mode: str, payload: dict):
        proc = subprocess.run(
            [sys.executable, HOOK_SCRIPT, mode],
            input=json.dumps(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
            check=True,
        )
        out = proc.stdout.strip()
        return (json.loads(out) if out else {}), proc.stderr

    def _write_transcript(self, content: str) -> str:
        tf = tempfile.NamedTemporaryFile("w", delete=False, dir=self.test_dir)
        tf.write(
            json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "content": content,
                    "tool_calls": [],
                }
            )
            + "\n"
        )
        tf.flush()
        tf.close()
        return tf.name

    def test_quote_aware_taint_and_specific_reasons(self):
        self.assertFalse(is_tainted_shell_command('rg -n "TAINTED|Chained shell" file.py'))
        self.assertIsNone(get_taint_reason('rg -n "TAINTED|Chained shell" file.py'))
        self.assertFalse(is_tainted_shell_command('pytest tests/ -k "foo(bar)"'))
        self.assertFalse(is_tainted_shell_command('pytest -k "a or b"'))
        self.assertIsNone(get_taint_reason("pytest -k 'foo;bar'"))

        # Command substitution inside double quotes MUST still be caught
        self.assertTrue(is_tainted_shell_command('pytest -k "$(echo hi)"'))
        self.assertTrue(is_tainted_shell_command('pytest -k "`echo hi`"'))
        self.assertTrue(is_tainted_shell_command("pytest || true"))

        env_reason = get_taint_reason("PYTHONPATH=/tmp/evil pytest")
        self.assertIsNotNone(env_reason)
        self.assertIn("Dangerous environment override", env_reason)

        chain_reason = get_taint_reason("pytest || true")
        self.assertIsNotNone(chain_reason)
        self.assertIn("Chained shell operators", chain_reason)

    def test_non_verification_exploratory_commands_not_tainted(self):
        self.assertFalse(is_taint_applicable_command("docker ps -a && docker images"))
        self.assertFalse(is_taint_applicable_command("ps aux | grep docker"))
        self.assertFalse(is_taint_applicable_command('rg -n "A|B" f.py'))
        self.assertTrue(is_taint_applicable_command("pytest || true"))

        ledger = DaemonLedger(self.ledger_path)
        conv = "conv-explore"
        ledger.record_entry(conv, 1, "run_command", "docker ps -a && docker images", observed_exit_code=0)
        ledger.record_entry(conv, 2, "run_command", "ps aux | grep docker", observed_exit_code=0)
        ledger.record_entry(conv, 3, "run_command", 'rg -n "A|B" f.py', observed_exit_code=0)

        premise = ledger.get_premise(conv)
        self.assertEqual(premise["unresolved_failures"], [])
        self.assertNotIn("TAINTED", premise["premise"])

    def test_no_double_tainted_prefix(self):
        conv = "conv-no-double"
        self._run_hook(
            "post_tool",
            {
                "conversationId": conv,
                "stepIdx": 1,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/ || true"}},
                "exitCode": 0,
            },
        )
        ledger = DaemonLedger(self.ledger_path)
        premise = ledger.get_premise(conv)
        self.assertEqual(len(premise["unresolved_failures"]), 1)
        err = premise["unresolved_failures"][0]["error"]
        self.assertEqual(err.count("TAINTED:"), 1, f"Expected single TAINTED prefix, got: {err}")

    def test_exploratory_pytest_no_collect_does_not_latch_failure_or_count_as_pass(self):
        ledger = DaemonLedger(self.ledger_path)
        conv = "conv-nocollect"
        ledger.record_entry(
            conv,
            1,
            "run_command",
            'pytest /Users/ai/rooms/devops -k "not_a_test"',
            observed_exit_code=5,
            error="exit status 5",
            stdout_tail="no tests ran",
            cwd=self.test_dir,
        )
        ledger.record_entry(
            conv,
            2,
            "run_command",
            "pytest nonexistent_test.py",
            observed_exit_code=4,
            error="exit status 4",
            stdout_tail="ERROR: file or directory not found: nonexistent_test.py",
            cwd=self.test_dir,
        )
        p1 = ledger.get_premise(conv)
        self.assertEqual(p1["test_commands_executed"], 0)
        self.assertEqual(p1["unresolved_failures"], [])

        # Real exit 1 failure MUST still latch
        ledger.record_entry(
            conv,
            3,
            "run_command",
            "pytest tests/test_real.py",
            observed_exit_code=1,
            error="exit status 1",
            stdout_tail="1 failed",
            cwd=self.test_dir,
        )
        p2 = ledger.get_premise(conv)
        self.assertEqual(len(p2["unresolved_failures"]), 1)
        self.assertEqual(p2["unresolved_failures"][0]["command"], "pytest tests/test_real.py")

    def test_tainted_halt_includes_actionable_course_correction(self):
        conv = "conv-course-correct"
        self._run_hook(
            "post_tool",
            {
                "conversationId": conv,
                "stepIdx": 1,
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "PYTHONPATH=/tmp pytest tests/test_foo.py || true"},
                },
                "exitCode": 0,
            },
        )
        tp = self._write_transcript("Done verifying.")
        res, _ = self._run_hook(
            "stop",
            {"conversationId": conv, "transcriptPath": tp, "workspacePaths": [self.test_dir]},
        )
        self.assertEqual(res.get("decision"), "continue")
        reason = res.get("reason", "")
        self.assertIn("Course-Correction:", reason)
        self.assertIn("`pytest tests/test_foo.py`", reason)

    def test_antigravity_escalation_and_circuit_breaker_handoff(self):
        conv = "conv-agy-breaker"
        ws_dir = os.path.join(self.test_dir, "ws")
        os.makedirs(ws_dir, exist_ok=True)
        self._run_hook(
            "post_tool",
            {
                "conversationId": conv,
                "stepIdx": 1,
                "cwd": ws_dir,
                "toolCall": {"name": "replace_file_content", "args": {"TargetFile": "/tmp/app.py"}},
            },
        )
        tp = self._write_transcript("I updated the server.")
        for attempt in range(1, 4):
            res, _ = self._run_hook(
                "stop",
                {"conversationId": conv, "transcriptPath": tp, "workspacePaths": [ws_dir]},
            )
            self.assertEqual(res.get("decision"), "continue")
            if attempt == 3:
                reason = res.get("reason", "")
                self.assertIn("HARDTRUTH ESCALATION HALT", reason)
                self.assertIn("Final automated course-correction", reason)
                self.assertNotIn("rm ", reason)

        # 4th attempt (>3 without state change) must allow and report CIRCUIT BREAKER on stderr
        res_4, err_4 = self._run_hook(
            "stop",
            {"conversationId": conv, "transcriptPath": tp, "workspacePaths": [ws_dir]},
        )
        self.assertEqual(res_4.get("decision"), "allow")
        self.assertIn("CIRCUIT BREAKER", err_4)

    def test_prose_and_rule4a_false_positives_resolved(self):
        # 1. Descriptive Q&A about halt prompts + pure questions must allow cleanly
        tp_qa = self._write_transcript(
            'The halt prompt only says "Fix the failures and re-run tests before stopping."\nDid all tests pass?'
        )
        res_qa, _ = self._run_hook(
            "stop",
            {"conversationId": "conv-qa-1", "transcriptPath": tp_qa, "workspacePaths": [self.test_dir]},
        )
        self.assertEqual(res_qa.get("decision"), "allow")

        # 2. Source file legitimately created & committed in-session passes Rule 4A even with clean working tree
        repo_dir = os.path.join(self.test_dir, "repo")
        os.makedirs(repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True)
        with open(os.path.join(repo_dir, "init.py"), "w") as f:
            f.write("x = 1\n")
        subprocess.run(["git", "add", "init.py"], cwd=repo_dir, check=True, capture_output=True)
        init_env = os.environ.copy()
        init_env["GIT_AUTHOR_DATE"] = "2025-01-01T00:00:00Z"
        init_env["GIT_COMMITTER_DATE"] = "2025-01-01T00:00:00Z"
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, env=init_env, check=True, capture_output=True)

        conv = "conv-committed-file"
        # First tool call in session anchors created_at before adapter.py is written and committed
        self._run_hook(
            "post_tool",
            {
                "conversationId": conv,
                "stepIdx": 1,
                "cwd": repo_dir,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
                "exitCode": 0,
            },
        )

        new_src = os.path.join(repo_dir, "adapter.py")
        with open(new_src, "w") as f:
            f.write("def run(x):\n    return x + 1\n")
        subprocess.run(["git", "add", "adapter.py"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add adapter"], cwd=repo_dir, check=True, capture_output=True)

        # Record clean test run in the session
        self._run_hook(
            "post_tool",
            {
                "conversationId": conv,
                "stepIdx": 2,
                "cwd": repo_dir,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/"}},
                "exitCode": 0,
            },
        )
        tp_ok = self._write_transcript("Created adapter.py cleanly.")
        res_ok, _ = self._run_hook(
            "stop",
            {"conversationId": conv, "transcriptPath": tp_ok, "workspacePaths": [repo_dir]},
        )
        self.assertEqual(res_ok.get("decision"), "allow")

        # Fabricated file claim still blocks with UNVERIFIED CLAIM
        tp_fake = self._write_transcript("Created non_existent_module.py cleanly.")
        res_fake, _ = self._run_hook(
            "stop",
            {"conversationId": conv, "transcriptPath": tp_fake, "workspacePaths": [repo_dir]},
        )
        self.assertEqual(res_fake.get("decision"), "continue")
        self.assertIn("UNVERIFIED CLAIM", res_fake.get("reason", ""))

    def test_premise_modified_files_capped_at_20(self):
        repo_dir = os.path.join(self.test_dir, "many_repo")
        os.makedirs(repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True)
        with open(os.path.join(repo_dir, "base.py"), "w") as f:
            f.write("x = 1\n")
        subprocess.run(["git", "add", "base.py"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)

        for i in range(25):
            with open(os.path.join(repo_dir, f"doc_{i:02d}.md"), "w") as f:
                f.write("note\n")

        import client.hardtruth_hook as hook_mod

        old_ledger = os.environ.get("HARDTRUTH_LEDGER_PATH")
        old_halt = os.environ.get("HARDTRUTH_HALT_DIR")
        old_key = os.environ.get("HARDTRUTH_API_KEY")
        os.environ["HARDTRUTH_LEDGER_PATH"] = self.ledger_path
        os.environ["HARDTRUTH_HALT_DIR"] = self.halt_dir
        os.environ["HARDTRUTH_API_KEY"] = os.path.join(self.test_dir, "missing-key")
        captured = {}
        try:
            tp = self._write_transcript("I fixed the codebase bug cleanly.")
            orig_call = hook_mod.call_system_one

            def fake_call(endpoint, data=None, timeout=5.0):
                if endpoint in ("v1/verify-claims-batch", "v1/verify-claim"):
                    if endpoint == "v1/verify-claims-batch" and data and data.get("pairs"):
                        captured["premise"] = data["pairs"][0]["premise"]
                        return {"results": [{"hypothesis": p["claim"], "probabilities": {"contradiction": 0.01}} for p in data["pairs"]]}
                    if data:
                        captured["premise"] = data.get("premise", "")
                        return {"probabilities": {"contradiction": 0.01}}
                return None

            hook_mod.call_system_one = fake_call
            try:
                hook_mod.handle_stop({"conversationId": "conv-cap20", "transcriptPath": tp, "workspacePaths": [repo_dir]})
            finally:
                hook_mod.call_system_one = orig_call
        finally:
            if old_ledger is not None:
                os.environ["HARDTRUTH_LEDGER_PATH"] = old_ledger
            else:
                os.environ.pop("HARDTRUTH_LEDGER_PATH", None)
            if old_halt is not None:
                os.environ["HARDTRUTH_HALT_DIR"] = old_halt
            else:
                os.environ.pop("HARDTRUTH_HALT_DIR", None)
            if old_key is not None:
                os.environ["HARDTRUTH_API_KEY"] = old_key
            else:
                os.environ.pop("HARDTRUTH_API_KEY", None)

        self.assertIn("(+5 more)", captured.get("premise", ""))


if __name__ == "__main__":
    unittest.main()
