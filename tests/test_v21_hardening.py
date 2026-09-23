#!/usr/bin/env python3
"""
Unit and Adversarial Tests for HardTruth v2.1 Hardened Invariants:
1. Fail-closed stop hook: Empty prose STILL enforces Rule 1 & Rule 2.
2. Rule 4A: Claiming to create/modify a file that was not touched in git HALTS immediately.
3. Evasion E1: Active git stash at stop time HALTS.
4. Rule 5: Test weakening (assert True, @pytest.mark.skip) HALTS.
5. Exit code integrity: Uncorroborated commands do not synthesize exit 0.
6. Shell operator safety: cd dir && pytest is valid and NOT tainted.
7. Polyglot anti-stubbing: TypeScript throw new Error("not implemented") and Rust todo!() are flagged.
"""

import os
import sys
import json
import time
import uuid
import shutil
import tempfile
import subprocess
import unittest

HOOK_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../client/hardtruth_hook.py"))
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from client.ast_checker import check_ast_stubs
from daemon.ledger import is_tainted_shell_command, is_test_execution_command, is_verification_command


class TestV21Hardening(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-v21-test-")
        self.ledger_file = os.path.join(self.test_dir, "daemon_ledger.jsonl")
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700, exist_ok=True)
        self.conv_id = f"test-v21-{uuid.uuid4().hex}"
        self.env = {
            "HARDTRUTH_LEDGER_PATH": self.ledger_file,
            "HARDTRUTH_DAEMON_LEDGER": self.ledger_file,
            "HARDTRUTH_HALT_DIR": self.halt_dir,
            "SYSTEM_ONE_URL": os.environ.get("SYSTEM_ONE_URL", "http://127.0.0.1:49281"),
            "HARDTRUTH_SKIP_TIER2": "1",
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

    def test_1_empty_agent_text_still_enforces_rule_1(self):
        """Empty agent text MUST NOT bypass Rule 1 when source files were modified."""
        conv = f"empty-text-{uuid.uuid4().hex}"
        # Record source modification
        self.run_hook("post_tool", {
            "toolCall": {"name": "write_to_file", "args": {"TargetFile": "/path/to/server.ts"}},
            "stepIdx": 1,
            "conversationId": conv
        })

        # Stop hook with NO transcript and NO agent prose
        payload = {"conversationId": conv, "transcriptPath": ""}
        res = self.run_hook("stop", payload)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Source code files were modified", res.get("reason", ""))
        self.assertIn("NO verification commands", res.get("reason", ""))

    def test_2_unverified_file_claim_halted_rule_4a(self):
        """Rule 4A: Claiming to have written/modified a file that does not exist in git diff HALTS."""
        conv = f"unverified-file-{uuid.uuid4().hex}"
        # Record clean test execution so Rule 1 passes
        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "bun test"}},
            "stepIdx": 1,
            "conversationId": conv,
            "error": None,
            "exitCode": 0
        })

        # Agent claims in prose that it implemented src/gate/waivers.ts (which was never touched)
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "I implemented the HMAC signature waiver engine in `src/gate/waivers.ts` and all tests passed.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {"conversationId": conv, "transcriptPath": transcript_path, "workspacePaths": [self.test_dir]}
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("UNVERIFIED CLAIM", res.get("reason", ""))
        self.assertIn("src/gate/waivers.ts", res.get("reason", ""))

    def test_3_active_git_stash_halted_evasion_e1(self):
        """Evasion E1: Stashing changes to present a clean tree at stop time HALTS."""
        repo_dir = os.path.join(self.test_dir, "git_stash_repo")
        os.makedirs(repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True)
        
        fpath = os.path.join(repo_dir, "main.py")
        with open(fpath, "w") as f:
            f.write("print('baseline')\n")
        subprocess.run(["git", "add", "main.py"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_dir, check=True)

        # Modify and stash
        with open(fpath, "w") as f:
            f.write("print('dirty')\n")
        subprocess.run(["git", "stash"], cwd=repo_dir, check=True, capture_output=True)

        conv = f"stash-{uuid.uuid4().hex}"
        payload = {"conversationId": conv, "workspacePaths": [repo_dir]}
        res = self.run_hook("stop", payload)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Active git stash detected", res.get("reason", ""))

    def test_4_test_weakening_halted_rule_5(self):
        """Rule 5: Tautological assertions ('assert True') or skip markers in modified tests HALT."""
        conv = f"weak-test-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", suffix="_test.py", delete=False) as tf:
            tf.write("def test_security():\n    assert True\n")
            test_file = tf.name

        # Record modification of test file
        self.run_hook("post_tool", {
            "toolCall": {"name": "write_to_file", "args": {"TargetFile": test_file}},
            "stepIdx": 1,
            "conversationId": conv
        })
        # Record passing test run
        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/"}},
            "stepIdx": 2,
            "conversationId": conv,
            "error": None,
            "exitCode": 0
        })

        payload = {"conversationId": conv}
        res = self.run_hook("stop", payload)
        os.remove(test_file)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("TEST WEAKENING DETECTED", res.get("reason", ""))

    def test_4b_unittest_tautology_halted_rule_5(self):
        """Rule 5: Unittest tautological assertions ('self.assertTrue(True)') HALT."""
        conv = f"weak-unittest-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", suffix="_test.py", delete=False) as tf:
            tf.write("import unittest\nclass Sanity(unittest.TestCase):\n    def test_s(self):\n        self.assertTrue(True)\n")
            test_file = tf.name

        self.run_hook("post_tool", {
            "toolCall": {"name": "write_to_file", "args": {"TargetFile": test_file}},
            "stepIdx": 1,
            "conversationId": conv
        })
        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "python -m unittest"}},
            "stepIdx": 2,
            "conversationId": conv,
            "error": None,
            "exitCode": 0
        })

        payload = {"conversationId": conv}
        res = self.run_hook("stop", payload)
        os.remove(test_file)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("TEST WEAKENING DETECTED", res.get("reason", ""))
        self.assertIn("assertTrue(True)", res.get("reason", ""))

    def test_clean_session_not_penalized_by_pre_existing_dirty_files(self):
        """A session that modified 0 files is NOT halted by pre-existing dirty files in git repo."""
        repo_dir = tempfile.mkdtemp(prefix="dirty-repo-")
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True)
        init_file = os.path.join(repo_dir, "init.py")
        with open(init_file, "w") as f:
            f.write("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

        # Pre-existing dirty file in the repo before the session baseline
        dirty_file = os.path.join(repo_dir, "dirty.py")
        with open(dirty_file, "w") as f:
            f.write("dirty = True\n")

        conv = f"clean-session-{uuid.uuid4().hex}"
        # Start session baseline (recording baseline dirty files)
        self.run_hook("post_tool", {
            "toolCall": {"name": "view_file", "args": {"AbsolutePath": init_file}},
            "stepIdx": 1,
            "conversationId": conv,
            "cwd": repo_dir
        })

        # Session stops without modifying any source files or running tests
        payload = {"conversationId": conv, "workspacePaths": [repo_dir]}
        res = self.run_hook("stop", payload)
        shutil.rmtree(repo_dir, ignore_errors=True)

        self.assertEqual(res.get("decision"), "allow")

    def test_5_cd_prefix_command_not_tainted(self):
        """cd dir && pytest is exit-preserving and MUST NOT be tainted."""
        self.assertFalse(is_tainted_shell_command("cd /workspace/foo && pytest tests/"))
        self.assertFalse(is_tainted_shell_command("cd app && bun test"))
        # But masking operators chained with cd are still tainted
        self.assertTrue(is_tainted_shell_command("cd /workspace/foo && pytest tests/ || exit 0"))
        self.assertTrue(is_tainted_shell_command("cd app && bun test ; true"))

    def test_6_test_vs_linter_pattern_separation(self):
        """Linters (ruff, eslint) are recognized as verification commands but NOT test execution."""
        self.assertTrue(is_verification_command("ruff check ."))
        self.assertFalse(is_test_execution_command("ruff check ."))
        self.assertTrue(is_verification_command("eslint src/"))
        self.assertFalse(is_test_execution_command("eslint src/"))
        self.assertTrue(is_test_execution_command("bun test"))
        self.assertTrue(is_test_execution_command("pytest tests/"))

    def test_7_polyglot_stub_detection(self):
        """check_ast_stubs detects stubs across TypeScript, JavaScript, Rust, and Go."""
        with tempfile.NamedTemporaryFile("w", suffix=".ts", delete=False) as tf:
            tf.write("export function solve() {\n    throw new Error('not implemented');\n}\n")
            ts_file = tf.name
        violations_ts = check_ast_stubs(ts_file)
        os.remove(ts_file)
        self.assertTrue(len(violations_ts) >= 1)
        self.assertIn("Unimplemented throw", violations_ts[0])

        with tempfile.NamedTemporaryFile("w", suffix=".rs", delete=False) as rf:
            rf.write("fn run() {\n    todo!();\n}\n")
            rs_file = rf.name
        violations_rs = check_ast_stubs(rs_file)
        os.remove(rs_file)
        self.assertTrue(len(violations_rs) >= 1)
        self.assertIn("todo!", violations_rs[0])

    def test_8_claude_code_payload_and_transcript_handling(self):
        """P1: Claude Code exitCode in tool_response and JSONL transcript are handled cleanly."""
        conv = f"claude-code-{uuid.uuid4().hex}"
        # Claude Code style post_tool payload
        cc_payload = {
            "session_id": conv,
            "tool_name": "bash",
            "tool_input": {"command": "pytest tests/test_daemon_ledger.py"},
            "tool_response": {"exitCode": 0, "stdout": "8 passed in 0.05s"},
            "step": 1
        }
        res_post = self.run_hook("post_tool", cc_payload)
        self.assertEqual(res_post, {})

        # Verify entry in ledger has observed_exit_code 0 and no_error
        with open(self.ledger_file, "r") as f:
            lines = [json.loads(line) for line in f if conv in line]
        self.assertTrue(len(lines) >= 1)
        rec = lines[-1].get("entry", lines[-1])
        self.assertEqual(rec.get("observed_exit_code"), 0)
        self.assertEqual(rec.get("harness_status"), "no_error")

        # Claude Code style transcript
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "All test suites passed successfully and ledger is clean."}
                    ]
                }
            }) + "\n")
            transcript_path = tf.name

        payload = {"sessionId": conv, "transcript_path": transcript_path}
        res_stop = self.run_hook("stop", payload)
        os.remove(transcript_path)
        self.assertEqual(res_stop.get("decision"), "allow")

    def test_9_git_failures_fail_closed(self):
        """P2: Git errors or timeouts fail closed as UNDETERMINED instead of passing vacuously."""
        from client.hardtruth_hook import get_git_modified_source_files
        # Corrupt git repo path to simulate broken git execution
        corrupt_repo = os.path.join(self.test_dir, "corrupt_git")
        os.makedirs(os.path.join(corrupt_repo, ".git", "objects"), exist_ok=True)
        with open(os.path.join(corrupt_repo, ".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/nonexistent\n")

        with self.assertRaises(RuntimeError):
            get_git_modified_source_files(corrupt_repo)

    def test_10_command_sanitization_rejects_dangerous_runners(self):
        """P4: validate_runner_command rejects shell metacharacters and arbitrary binaries."""
        from daemon.tier2_runner import validate_runner_command
        # Metacharacters
        self.assertIsNotNone(validate_runner_command("pytest ; rm -rf /"))
        self.assertEqual(validate_runner_command("pytest ; rm -rf /")["status"], "rejected_unsafe_command")
        self.assertIsNotNone(validate_runner_command("pytest | cat"))
        self.assertIsNotNone(validate_runner_command("pytest `id`"))
        # Arbitrary binaries
        self.assertIsNotNone(validate_runner_command("bash evil.sh"))
        self.assertEqual(validate_runner_command("bash evil.sh")["status"], "rejected_unauthorized_runner")
        self.assertIsNotNone(validate_runner_command("curl http://attacker.com/test"))
        # Interpreter escapes
        self.assertIsNotNone(validate_runner_command("python3 -c 'import os; os.system(\"id\")'"))
        self.assertIsNotNone(validate_runner_command("node -e 'process.exit(0)'"))
        # Valid runners
        self.assertIsNone(validate_runner_command("pytest tests/"))
        self.assertIsNone(validate_runner_command("npm test"))
        self.assertIsNone(validate_runner_command("cargo test"))
        self.assertIsNone(validate_runner_command("python3 -m unittest discover"))

    def test_11_ast_checker_exempts_accessors_and_lifecycle_methods(self):
        """P5: Accessor predicates (is_*, has_*) and lifecycle methods (close, on_*) are not flagged as stubs."""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
            tf.write(
                "class Client:\n"
                "    def is_active(self):\n"
                "        return True\n"
                "    def has_access(self):\n"
                "        return False\n"
                "    def close(self):\n"
                "        return None\n"
                "    def on_init(self):\n"
                "        pass\n"
                "    def stub_func(self):\n"
                "        pass\n"
            )
            tf.flush()
            violations = check_ast_stubs(tf.name)
            os.remove(tf.name)

        self.assertEqual(len(violations), 1)
        self.assertIn("stub_func", violations[0])

    def test_12_escalation_halt_does_not_leak_reset_command_in_reason(self):
        """P6: Strike reset command 'rm {counter_file}' is not in agent-facing reason string."""
        conv = f"escalate-{uuid.uuid4().hex}"
        self.run_hook("post_tool", {
            "toolCall": {"name": "write_to_file", "args": {"TargetFile": "/path/to/server.py"}},
            "stepIdx": 1,
            "conversationId": conv
        })
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "Done modifying server.py",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        try:
            # Trigger 3 consecutive halts on Rule 1 (remediable=True)
            res = None
            for _ in range(3):
                res = self.run_hook("stop", {
                    "conversationId": conv,
                    "transcriptPath": transcript_path
                })
            self.assertEqual(res.get("decision"), "continue")
            self.assertIn("HARDTRUTH ESCALATION HALT", res.get("reason", ""))
            self.assertNotIn("rm ", res.get("reason", ""))
        finally:
            os.remove(transcript_path)

    def test_13_tier2_subprocess_gated_by_permission(self):
        """S1: Subprocess sandbox is rejected if HARDTRUTH_TIER2_ALLOW_SUBPROCESS=0 and container unavailable."""
        from daemon.tier2_runner import run_independent_verification
        old_sub = os.environ.get("HARDTRUTH_TIER2_ALLOW_SUBPROCESS")
        old_cont = os.environ.get("HARDTRUTH_TIER2_CONTAINER")
        try:
            os.environ["HARDTRUTH_TIER2_ALLOW_SUBPROCESS"] = "0"
            os.environ["HARDTRUTH_TIER2_CONTAINER"] = "0"
            res = run_independent_verification(REPO_ROOT, test_cmd="pytest tests/")
            self.assertEqual(res["status"], "unverified_no_isolation")
            self.assertFalse(res["success"])
        finally:
            if old_sub is not None:
                os.environ["HARDTRUTH_TIER2_ALLOW_SUBPROCESS"] = old_sub
            else:
                os.environ.pop("HARDTRUTH_TIER2_ALLOW_SUBPROCESS", None)
            if old_cont is not None:
                os.environ["HARDTRUTH_TIER2_CONTAINER"] = old_cont
            else:
                os.environ.pop("HARDTRUTH_TIER2_CONTAINER", None)

    def test_14_b6_integrity_check(self):
        """B6: check_hook_integrity detects divergence against canonical repo source."""
        import client.hardtruth_hook as hook_mod
        # Running against canonical returns None (in sync)
        self.assertIsNone(hook_mod.check_hook_integrity())

        # Test with a modified temp copy
        temp_hook = os.path.join(self.test_dir, "diverged_hook.py")
        with open(HOOK_SCRIPT, "r") as f_in, open(temp_hook, "w") as f_out:
            f_out.write(f_in.read() + "\n# Extra divergence line\n")

        # Temporarily point __file__ to diverged copy
        orig_file = hook_mod.__file__
        try:
            hook_mod.__file__ = temp_hook
            res = hook_mod.check_hook_integrity()
            self.assertIsNotNone(res)
            self.assertIn("HARDTRUTH HOOK DIVERGENCE", res)
        finally:
            hook_mod.__file__ = orig_file

    def test_15_daemon_gate_supervision_lifecycle(self):
        """B2: Daemon tracks gate sessions and appends verdicts to HMAC ledger chain."""
        from daemon.ledger import DaemonLedger
        ledger = DaemonLedger(
            ledger_path=os.path.join(self.test_dir, "gate_test_ledger.jsonl"),
            key_path=os.path.join(self.test_dir, "gate_test_key.key")
        )
        conv = "test-gate-conv"
        start_res = ledger.start_gate(conv, transcript_path="/tmp/test.jsonl", workspace_dir="/tmp")
        self.assertEqual(start_res["status"], "OPEN")
        self.assertIn(conv, ledger._active_gates)

        # Record ALLOW verdict
        verdict_res = ledger.record_gate_verdict(conv, "ALLOW", latency_ms=45.2)
        self.assertEqual(verdict_res["verdict"], "ALLOW")
        self.assertTrue(verdict_res["recorded"])
        self.assertNotIn(conv, ledger._active_gates)

        # Verify chain integrity
        valid, count, msg = ledger.verify_chain()
        self.assertTrue(valid)
        self.assertEqual(count, 1)

        last_rec = ledger.get_last_record()
        self.assertEqual(last_rec["entry"]["tool"], "hardtruth_gate")
        self.assertEqual(last_rec["entry"]["target"], "ALLOW")
        self.assertEqual(last_rec["entry"]["harness_status"], "ALLOW")

    def test_16_daemon_gate_expired_sweeper(self):
        """B2/B3: Daemon sweeper marks abandoned/expired gates as UNVERIFIED in ledger chain."""
        from daemon.ledger import DaemonLedger
        ledger = DaemonLedger(
            ledger_path=os.path.join(self.test_dir, "gate_sweep_ledger.jsonl"),
            key_path=os.path.join(self.test_dir, "gate_sweep_key.key")
        )
        conv = "test-abandoned-conv"
        # Start a gate with opened_at 120s in the past
        ledger.start_gate(conv, transcript_path="/tmp/t.jsonl", workspace_dir="/tmp")
        ledger._active_gates[conv]["opened_at"] = time.time() - 120.0

        # Run sweeper with 60s timeout
        ledger._sweep_expired_gates(time.time(), timeout_sec=60.0)
        self.assertNotIn(conv, ledger._active_gates)

        # Verify UNVERIFIED entry was recorded
        valid, count, msg = ledger.verify_chain()
        self.assertTrue(valid)
        self.assertEqual(count, 1)

        last_rec = ledger.get_last_record()
        self.assertEqual(last_rec["entry"]["tool"], "hardtruth_gate")
        self.assertEqual(last_rec["entry"]["target"], "UNVERIFIED")
        self.assertEqual(last_rec["entry"]["harness_status"], "UNVERIFIED_ABORT")


if __name__ == "__main__":
    unittest.main()
