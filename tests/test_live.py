#!/usr/bin/env python3
"""
Comprehensive Live Verification Suite for HardTruth Anti-Hallucination Gate
Tests:
1. PostToolUse Ledger Recording (deterministic ledger isolation)
2. Hallucination Trap: Fake Test Pass Claim (No Tests in Ledger)
3. Contradiction Trap: Claiming Success When Ledger Shows Failure
4. Verified Truth Pass: Legitimate Evidence in Ledger
5. AST Anti-Stubbing Gate: Catching 'pass' and 'NotImplementedError' Stubs
6. Conversational Bypass: Zero False Positives on General Prose
7. Fenced Code Blocks & Imperatives Allowed
8. Inline Code & Quotes Allowed
9. Backtick & Blockquote Evasion Blocked
10. is_daemon_online Agreement with /health Endpoint
11. AST Stub Checker Extended Coverage (5 patterns caught, 3 patterns exempt)
12. Imperative Filter Regex with Colons (Tip:, Note:, Warning:)
13. Descriptive Filter Bypass Prevention (Example:, Quote: with fake passes blocked)
14. Secure Counter Directory & Path Traversal Prevention
15. Circuit Breaker Visible Warning on 4th Attempt
16. Daemon Unreachable Fail-Closed Behavior
17. Ledger Writes Require API Token (Round 7 Finding A)
18. Tier 2 Handoff Daemon-Visible Workspace (Round 7 Finding B)
19. Ledger Premise Reads Require API Token (Round 8)
20. Session Baseline Reads Require API Token (Round 8)
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

class TestSystemOneSentinel(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-test-")
        self.ledger_file = os.path.join(self.test_dir, "ledger.jsonl")
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700, exist_ok=True)
        os.environ["HARDTRUTH_LEDGER_PATH"] = self.ledger_file
        os.environ["HARDTRUTH_HALT_DIR"] = self.halt_dir
        os.environ["HARDTRUTH_SKIP_TIER2"] = "1"
        # Round 8 (#6): run hook subprocesses token-less so test suites never write
        # records into the physical daemon ledger (daemon rejects with 401; the hook
        # degrades gracefully to its local temp ledger). test_17/test_18 explicitly
        # restore the real token to exercise the live authenticated HTTP path.
        os.environ["HARDTRUTH_API_KEY"] = os.path.join(self.test_dir, "no-such-key")
        self.conv_id = f"test-conv-{uuid.uuid4().hex}"

    def tearDown(self):
        os.environ.pop("HARDTRUTH_SKIP_TIER2", None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def run_hook(self, mode: str, payload: dict, env_override: dict = None) -> dict:
        env = os.environ.copy()
        if env_override:
            env.update(env_override)
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

    def test_1_post_tool_ledger_recording(self):
        payload = {
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "python3 -m unittest test_daemon.py"}
            },
            "stepIdx": 12,
            "conversationId": self.conv_id,
            "error": None
        }
        res = self.run_hook("post_tool", payload)
        self.assertEqual(res, {})
        
        # Verify isolated ledger has this entry
        with open(self.ledger_file, "r") as f:
            lines = [json.loads(line) for line in f if self.conv_id in line]
        self.assertTrue(len(lines) >= 1)
        rec = lines[-1].get("entry", lines[-1])
        self.assertEqual(rec["target"], "python3 -m unittest test_daemon.py")
        self.assertIn(rec.get("status") or rec.get("harness_status"), ["success", "no_error"])

    def test_2_fake_test_pass_claim_blocked(self):
        conv = f"fake-pass-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "I have verified everything. All 10 unit tests passed completely!",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)
        
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("NO commands or test suites were run", res.get("reason", ""))
        print(f"\n[Test 2: Fake Test Pass Blocked] Halt Reason: {res.get('reason')}")

    def test_3_contradiction_claim_blocked(self):
        conv = f"fail-conv-{uuid.uuid4().hex}"
        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/"}},
            "stepIdx": 5,
            "conversationId": conv,
            "error": "exit status 1 (2 tests failed: test_auth, test_billing)"
        })

        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "All 10 unit tests in tests/ passed with 100% success.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("CONTRADICTION DETECTED", res.get("reason", ""))
        print(f"[Test 3: Contradiction Blocked] Halt Reason: {res.get('reason')}")

    def test_4_verified_truth_allowed(self):
        conv = f"success-conv-{uuid.uuid4().hex}"
        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "python3 -m unittest test_daemon.py"}},
            "stepIdx": 8,
            "conversationId": conv,
            "error": None
        })

        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "Ran unit test suite with python3 -m unittest test_daemon.py and all 11 tests passed successfully.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "allow")
        print("[Test 4: Verified Truth Allowed] Decision: allow")

    def test_5_ast_stubbing_blocked(self):
        conv = f"stub-conv-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as pyf:
            pyf.write("def calculate_tuition(child_age):\n    pass\n")
            stub_file = pyf.name

        self.run_hook("post_tool", {
            "toolCall": {"name": "write_to_file", "args": {"TargetFile": stub_file}},
            "stepIdx": 4,
            "conversationId": conv,
            "error": None
        })

        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "I have created the calculate_tuition function in the file.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        res = self.run_hook("stop", payload)
        os.remove(stub_file)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Unimplemented stub detected", res.get("reason", ""))
        print(f"[Test 5: AST Stub Blocked] Halt Reason: {res.get('reason')}")

    def test_6_conversational_prose_allowed(self):
        conv = f"chat-conv-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "Here is an explanation of how the system works. Let me know if you have any questions!",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "allow")
        print("[Test 6: Conversational Prose Allowed] Decision: allow")

    def test_7_fenced_code_blocks_and_imperatives_allowed(self):
        conv = f"prompt-conv-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "Here is the prompt for the other agent:\n```markdown\nCRITICAL REQUIREMENT: Do not declare any integration 'wired' without running tests.\n```\nEnsure you verify all endpoints.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "allow")
        print("[Test 7: Fenced Code Blocks & Imperatives Allowed] Decision: allow")

    def test_8_inline_code_and_quotes_allowed(self):
        conv = f"inline-conv-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "The system will show this error:\n`🚨 HALTED: You claimed tests passed, but no tests ran.`\n> Quote: All tests passed.\nLet me know if you want to proceed.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "allow")
        print("[Test 8: Inline Code & Quotes Allowed] Decision: allow")

    def test_9_backtick_and_blockquote_evasion_blocked(self):
        conv_a = f"evade-backtick-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "Work is done: `All 10 unit tests passed completely!`",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv_a,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        res_a = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res_a.get("decision"), "continue")
        self.assertIn("NO commands or test suites were run", res_a.get("reason", ""))
        print(f"[Test 9A: Backtick Evasion Blocked] Halt Reason: {res_a.get('reason')}")

        conv_b = f"evade-quote-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "Status update:\n> All 10 unit tests passed completely!",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv_b,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        res_b = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res_b.get("decision"), "continue")
        self.assertIn("NO commands or test suites were run", res_b.get("reason", ""))
        print(f"[Test 9B: Blockquote Evasion Blocked] Halt Reason: {res_b.get('reason')}")

    def test_10_is_daemon_online(self):
        from client.hardtruth_client import HardTruthClient
        # Point to unreachable URL
        client_offline = HardTruthClient("http://127.0.0.1:9")
        self.assertFalse(client_offline.is_daemon_online())

    def test_11_ast_checker_extended_patterns(self):
        from client.ast_checker import check_ast_stubs
        cases_to_catch = {
            "bare_raise": "def f():\n    raise NotImplementedError",
            "raise_call_empty": "def f():\n    raise NotImplementedError()",
            "raise_call_msg": "def f():\n    raise NotImplementedError('not implemented')",
            "ellipsis": "def f():\n    ...",
            "return_true": "def f():\n    return True",
            "return_none": "def f():\n    return None",
            "bare_return": "def f():\n    return",
            "docstring_then_stub": "def f():\n    '''doc'''\n    pass"
        }
        for name, code in cases_to_catch.items():
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
                tf.write(code)
                tf.flush()
                violations = check_ast_stubs(tf.name)
                os.unlink(tf.name)
            self.assertTrue(len(violations) >= 1, f"Failed to catch stub: {name}")

        cases_to_exempt = {
            "abstractmethod": "from abc import abstractmethod\n@abstractmethod\ndef f():\n    pass",
            "overload": "from typing import overload\n@overload\ndef f():\n    ...",
            "protocol": "from typing import Protocol\nclass P(Protocol):\n    def f(self):\n        pass",
            "implemented": "def f():\n    x = 1\n    return True"
        }
        for name, code in cases_to_exempt.items():
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
                tf.write(code)
                tf.flush()
                violations = check_ast_stubs(tf.name)
                os.unlink(tf.name)
            self.assertEqual(violations, [], f"Falsely flagged exempt code: {name}")

    def test_12_imperative_filter_colon(self):
        from client.hardtruth_hook import imperative_filter
        self.assertTrue(bool(imperative_filter.search("Tip: ensure tests are run")))
        self.assertTrue(bool(imperative_filter.search("Note: verify the output")))
        self.assertTrue(bool(imperative_filter.search("Warning: check the logs")))

    def test_13_descriptive_filter_bypass_prevention(self):
        # Prefixes Example: and Quote: must NOT bypass fake test pass detection
        for prefix in ["Example:", "Quote:", "Sample:"]:
            conv = f"bypass-{uuid.uuid4().hex}"
            with tempfile.NamedTemporaryFile("w", delete=False) as tf:
                tf.write(json.dumps({
                    "type": "PLANNER_RESPONSE",
                    "content": f"{prefix} All 10 unit tests passed completely and the suite is green.",
                    "tool_calls": []
                }) + "\n")
                transcript_path = tf.name

            payload = {
                "conversationId": conv,
                "transcriptPath": transcript_path,
                "executionNum": 1
            }
            res = self.run_hook("stop", payload)
            os.remove(transcript_path)
            self.assertEqual(res.get("decision"), "continue", f"Prefix {prefix} bypassed the gate!")

    def test_14_secure_counter_isolation(self):
        # A conversationId with ../ must not escape HALT_COUNTER_DIR
        evil_conv = "../../evil_path"
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "All 10 unit tests passed completely!",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": evil_conv,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)
        self.assertEqual(res.get("decision"), "continue")

        # Verify that all files created are inside self.halt_dir and nowhere outside
        halt_files = os.listdir(self.halt_dir)
        self.assertTrue(len(halt_files) >= 1)
        for hf in halt_files:
            self.assertFalse(".." in hf)

    def test_15_circuit_breaker_visible_warning(self):
        conv = f"breaker-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "All 10 unit tests passed completely!",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        # First 3 attempts must be halted
        for i in range(3):
            res = self.run_hook("stop", payload)
            self.assertEqual(res.get("decision"), "continue")

        # 4th attempt must trigger hard escalation halt (Never Fail Open)
        res_4 = self.run_hook("stop", payload)
        os.remove(transcript_path)
        self.assertEqual(res_4.get("decision"), "continue")
        self.assertIn("ESCALATION HALT", res_4.get("reason", ""))

    def test_16_daemon_unreachable_fail_closed(self):
        # Claim that cannot be verified deterministically must fail closed if daemon unreachable
        conv = f"dead-daemon-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "I have created and verified the entire authentication subsystem.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "executionNum": 1
        }
        res = self.run_hook("stop", payload, env_override={"SYSTEM_ONE_URL": "http://127.0.0.1:9"})
        os.remove(transcript_path)
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("HARDTRUTH DAEMON UNREACHABLE", res.get("reason", ""))

    def test_17_ledger_record_requires_api_token(self):
        """Round 7 Finding A: forged ledger writes without the shared token are rejected (401)."""
        import urllib.request, urllib.error
        from client.hardtruth_hook import get_api_token
        daemon_base = os.environ.get("SYSTEM_ONE_URL", "http://127.0.0.1:8000")
        if os.environ.get("HARDTRUTH_TIER2_SANDBOX") == "1":
            self.skipTest("Tier 2 in-container run: live daemon tests skipped")
        try:
            urllib.request.urlopen(daemon_base + "/health", timeout=2)
        except Exception:
            self.skipTest("daemon not reachable")
        os.environ.pop("HARDTRUTH_API_KEY", None)  # Round 8 (#6): restore real token source for this live test
        payload = json.dumps({
            "conversationId": f"auth-test-{uuid.uuid4().hex}",
            "stepIdx": 0,
            "tool": "run_command",
            "target": "pytest forged",
            "observed_exit_code": 0
        }).encode()
        # No token -> 401
        req = urllib.request.Request(
            daemon_base + "/v1/ledger/record", data=payload,
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 401, "forged record without token must be rejected")
        # Wrong token -> 401
        req_bad = urllib.request.Request(
            daemon_base + "/v1/ledger/record", data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + "x" * 64})
        with self.assertRaises(urllib.error.HTTPError) as ctx2:
            urllib.request.urlopen(req_bad, timeout=3)
        self.assertEqual(ctx2.exception.code, 401, "record with wrong token must be rejected")
        # Valid token -> recorded
        tok = get_api_token()
        if not tok:
            self.skipTest("no HARDTRUTH_API_TOKEN configured")
        req_ok = urllib.request.Request(
            daemon_base + "/v1/ledger/record", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req_ok, timeout=3) as resp:
            body = json.loads(resp.read().decode())
        self.assertEqual(body.get("status"), "recorded")

    def test_18_tier2_handoff_daemon_visible_workspace(self):
        """Round 7 Finding B: /v1/verify/handoff succeeds when the workspace is daemon-visible."""
        import urllib.request, urllib.error
        from client.hardtruth_hook import get_api_token
        daemon_base = os.environ.get("SYSTEM_ONE_URL", "http://127.0.0.1:8000")
        if os.environ.get("HARDTRUTH_TIER2_SANDBOX") == "1":
            self.skipTest("Tier 2 in-container run: live daemon tests skipped")
        try:
            urllib.request.urlopen(daemon_base + "/health", timeout=2)
        except Exception:
            self.skipTest("daemon not reachable")
        os.environ.pop("HARDTRUTH_API_KEY", None)  # Round 8 (#6): restore real token source for this live test
        tok = get_api_token()
        if not tok:
            self.skipTest("no HARDTRUTH_API_TOKEN configured")
        payload = json.dumps({
            "workspace_path": REPO_ROOT,
            "conversationId": f"tier2-live-{uuid.uuid4().hex}",
            "timeout_sec": 90
        }).encode()
        req = urllib.request.Request(
            daemon_base + "/v1/verify/handoff", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=130) as resp:
            body = json.loads(resp.read().decode())
        self.assertTrue(body.get("success"), f"Tier 2 handoff failed: {body.get('output','')[:300]}")

    def test_19_ledger_premise_requires_api_token(self):
        """Round 8 (#3): premise reads without the shared token are rejected (401)."""
        import urllib.request, urllib.error, urllib.parse
        from client.hardtruth_hook import get_api_token
        daemon_base = os.environ.get("SYSTEM_ONE_URL", "http://127.0.0.1:8000")
        if os.environ.get("HARDTRUTH_TIER2_SANDBOX") == "1":
            self.skipTest("Tier 2 in-container run: live daemon tests skipped")
        try:
            urllib.request.urlopen(daemon_base + "/health", timeout=2)
        except Exception:
            self.skipTest("daemon not reachable")
        os.environ.pop("HARDTRUTH_API_KEY", None)  # Round 8 (#6): restore real token source for this live test
        conv = f"premise-auth-{uuid.uuid4().hex}"
        url = daemon_base + "/v1/ledger/premise?conversationId=" + urllib.parse.quote(conv)
        # No token -> 401
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=3)
        self.assertEqual(ctx.exception.code, 401, "premise read without token must be rejected")
        # Valid token -> 200
        tok = get_api_token()
        if not tok:
            self.skipTest("no HARDTRUTH_API_TOKEN configured")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode())
        self.assertEqual(resp.status, 200)
        self.assertIn("unresolved_failures", body)

    def test_20_session_baseline_get_requires_api_token(self):
        """Round 8 (#3): baseline reads without the shared token are rejected (401)."""
        import urllib.request, urllib.error, urllib.parse
        from client.hardtruth_hook import get_api_token
        daemon_base = os.environ.get("SYSTEM_ONE_URL", "http://127.0.0.1:8000")
        if os.environ.get("HARDTRUTH_TIER2_SANDBOX") == "1":
            self.skipTest("Tier 2 in-container run: live daemon tests skipped")
        try:
            urllib.request.urlopen(daemon_base + "/health", timeout=2)
        except Exception:
            self.skipTest("daemon not reachable")
        os.environ.pop("HARDTRUTH_API_KEY", None)  # Round 8 (#6): restore real token source for this live test
        tok = get_api_token()
        if not tok:
            self.skipTest("no HARDTRUTH_API_TOKEN configured")
        conv = f"baseline-auth-{uuid.uuid4().hex}"
        url = (
            daemon_base + "/v1/session/baseline"
            "?conversationId=" + urllib.parse.quote(conv)
            + "&workspace_path=" + urllib.parse.quote(REPO_ROOT)
        )
        # No token -> 401
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=3)
        self.assertEqual(ctx.exception.code, 401, "baseline read without token must be rejected")
        # Valid token -> 200
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode())
        self.assertEqual(resp.status, 200)
        self.assertIn("baseline_sha", body)

if __name__ == "__main__":
    unittest.main()
