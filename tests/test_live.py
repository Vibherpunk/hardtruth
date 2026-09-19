#!/usr/bin/env python3
"""
Comprehensive Live Verification Suite for Antigravity System One Sentinel
Tests:
1. PostToolUse Ledger Recording
2. Hallucination Trap: Fake Test Pass Claim (No Tests in Ledger)
3. Contradiction Trap: Claiming Success When Ledger Shows Failure
4. Verified Truth Pass: Legitimate Evidence in Ledger
5. AST Anti-Stubbing Gate: Catching 'pass' and 'NotImplementedError' Stubs
6. Conversational Bypass: Zero False Positives on General Prose
"""

import os
import sys
import json
import subprocess
import tempfile
import unittest

HOOK_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../client/hardtruth_hook.py"))
LEDGER_FILE = os.path.expanduser("~/.gemini/antigravity-cli/ledger.jsonl")

class TestSystemOneSentinel(unittest.TestCase):

    def setUp(self):
        # Create a unique test conversation ID
        self.conv_id = f"test-conv-{os.getpid()}"
        # Ensure ledger dir exists
        os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)
        # Clear halt counter if any
        counter_file = f"/tmp/sentinel_halts/halt_{self.conv_id}.json"
        if os.path.exists(counter_file):
            os.remove(counter_file)

    def run_hook(self, mode: str, payload: dict) -> dict:
        proc = subprocess.run(
            ["python3", HOOK_SCRIPT, mode],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
        
        # Verify ledger has this entry
        with open(LEDGER_FILE, "r") as f:
            lines = [json.loads(line) for line in f if self.conv_id in line]
        self.assertTrue(len(lines) >= 1)
        self.assertEqual(lines[-1]["target"], "python3 -m unittest test_daemon.py")
        self.assertEqual(lines[-1]["status"], "success")

    def test_2_fake_test_pass_claim_blocked(self):
        # Conversation with NO test runs in ledger
        conv = f"fake-pass-{os.getpid()}"
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
        
        # Must halt because no test command was ever run!
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("NO commands or test suites were run", res.get("reason", ""))
        print(f"\n[Test 2: Fake Test Pass Blocked] Halt Reason: {res.get('reason')}")

    def test_3_contradiction_claim_blocked(self):
        # Record a failed test execution in ledger
        conv = f"fail-conv-{os.getpid()}"
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

        # Must halt with contradiction!
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("CONTRADICTION DETECTED", res.get("reason", ""))
        print(f"[Test 3: Contradiction Blocked] Halt Reason: {res.get('reason')}")

    def test_4_verified_truth_allowed(self):
        # Record a successful test execution in ledger
        conv = f"success-conv-{os.getpid()}"
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

        # Must allow termination!
        self.assertEqual(res.get("decision"), "allow")
        print("[Test 4: Verified Truth Allowed] Decision: allow")

    def test_5_ast_stubbing_blocked(self):
        conv = f"stub-conv-{os.getpid()}"
        # Create a stubbed file
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as pyf:
            pyf.write("def calculate_tuition(child_age):\n    pass\n")
            stub_file = pyf.name

        # Record file modification in ledger
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

        # Must halt because of empty stub!
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Unimplemented stub detected", res.get("reason", ""))
        print(f"[Test 5: AST Stub Blocked] Halt Reason: {res.get('reason')}")

    def test_6_conversational_prose_allowed(self):
        conv = f"chat-conv-{os.getpid()}"
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

        # Conversational text without verifiable action claims must pass cleanly
        self.assertEqual(res.get("decision"), "allow")
        print("[Test 6: Conversational Prose Allowed] Decision: allow")

    def test_7_fenced_code_blocks_and_imperatives_allowed(self):
        conv = f"prompt-conv-{os.getpid()}"
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

        # Quoted prompt blocks and imperative instructions must not trigger false alarms
        self.assertEqual(res.get("decision"), "allow")
        print("[Test 7: Fenced Code Blocks & Imperatives Allowed] Decision: allow")

    def test_8_inline_code_and_quotes_allowed(self):
        conv = f"inline-conv-{os.getpid()}"
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

        # Inline code and blockquotes must not trigger false alarms
        self.assertEqual(res.get("decision"), "allow")
        print("[Test 8: Inline Code & Quotes Allowed] Decision: allow")

    def test_9_backtick_and_blockquote_evasion_blocked(self):
        # Case A: Agent tries to hide a fake pass inside backticks
        conv_a = f"evade-backtick-{os.getpid()}"
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

        # MUST HALT: Evasion via backticks is blocked!
        self.assertEqual(res_a.get("decision"), "continue")
        self.assertIn("NO commands or test suites were run", res_a.get("reason", ""))
        print(f"[Test 9A: Backtick Evasion Blocked] Halt Reason: {res_a.get('reason')}")

        # Case B: Agent tries to hide a fake pass inside blockquotes
        conv_b = f"evade-quote-{os.getpid()}"
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

        # MUST HALT: Evasion via blockquote is blocked!
        self.assertEqual(res_b.get("decision"), "continue")
        self.assertIn("NO commands or test suites were run", res_b.get("reason", ""))
        print(f"[Test 9B: Blockquote Evasion Blocked] Halt Reason: {res_b.get('reason')}")

if __name__ == "__main__":
    unittest.main()
