#!/usr/bin/env python3
"""
Focused, network-free unit tests for Rule 4B's daemon-unreachable fail-closed behavior
(companion to tests/test_live.py::test_16_daemon_unreachable_fail_closed, which exercises
the same rule but via a real dead-port TCP connection attempt).

These tests mock client.hardtruth_hook.call_system_one directly -- no socket I/O happens
at all, so they run without a live daemon and without touching the network.

Rule: when the daemon (hosting the NLI cross-encoder) is unreachable, a claim that reached
the per-claim verification loop (i.e. a sentence matching action_triggers -- the agent
asserting it DID something) must fail closed UNLESS the session's own ledger already shows
deterministic evidence that genuine verification happened (test_commands_executed > 0 and
no unresolved failures). A completion/verification claim with ZERO corroborating ledger
activity of any kind must never be silently allowed through just because the daemon that
would have caught it happens to be down.
"""

import os
import sys
import json
import shutil
import tempfile
import uuid
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import client.hardtruth_hook as hardtruth_hook


class TestFailClosedClaimVerificationUnit(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-failclosed-unit-")
        self.ledger_file = os.path.join(self.test_dir, "ledger.jsonl")
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700, exist_ok=True)
        self._env_patch = {
            "HARDTRUTH_LEDGER_PATH": self.ledger_file,
            "HARDTRUTH_HALT_DIR": self.halt_dir,
            "HARDTRUTH_SKIP_TIER2": "1",
            "HARDTRUTH_API_KEY": os.path.join(self.test_dir, "no-such-key"),
        }
        self._old_env = {k: os.environ.get(k) for k in self._env_patch}
        os.environ.update(self._env_patch)

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _write_transcript(self, content):
        tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir=self.test_dir)
        tf.write(json.dumps({
            "type": "PLANNER_RESPONSE",
            "content": content,
            "tool_calls": []
        }) + "\n")
        tf.close()
        return tf.name

    def test_hallucinated_claim_fails_closed_when_daemon_unreachable(self):
        """A completion/verification claim with NO corresponding ledger activity at all
        (no source modified, no test commands executed) must halt when the daemon that
        would adjudicate it is unreachable -- never silently allow."""
        conv = f"unit-fail-closed-{uuid.uuid4().hex}"
        transcript_path = self._write_transcript(
            "I have created and verified the entire authentication subsystem."
        )
        payload = {"conversationId": conv, "transcriptPath": transcript_path, "executionNum": 1}

        with mock.patch.object(hardtruth_hook, "call_system_one", return_value=None):
            res = hardtruth_hook.handle_stop(payload)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("HARDTRUTH DAEMON UNREACHABLE", res.get("reason", ""))

    def test_corroborated_claim_still_allowed_when_daemon_unreachable(self):
        """Legitimate case: the session's own ledger already shows the claimed test run
        genuinely happened (matching command, exit 0, no unresolved failures) -- a daemon
        outage must NOT block this. This is the case the fix must not regress."""
        conv = f"unit-legit-{uuid.uuid4().hex}"

        with mock.patch.object(hardtruth_hook, "call_system_one", return_value=None):
            hardtruth_hook.handle_post_tool_use({
                "toolCall": {"name": "run_command", "args": {"CommandLine": "python3 -m unittest test_daemon.py"}},
                "stepIdx": 8,
                "conversationId": conv,
                "error": None,
                "exitCode": 0
            })

            transcript_path = self._write_transcript(
                "Ran unit test suite with python3 -m unittest test_daemon.py and all 11 tests passed successfully."
            )
            payload = {"conversationId": conv, "transcriptPath": transcript_path, "executionNum": 1}
            res = hardtruth_hook.handle_stop(payload)

        self.assertEqual(res.get("decision"), "allow")

    def test_hallucinated_claim_fails_closed_for_claude_code_harness_too(self):
        """The fail-closed rule is harness-agnostic: Claude Code's _halt() shape ('block')
        must also fire, not just Antigravity's ('continue')."""
        conv = f"unit-fail-closed-cc-{uuid.uuid4().hex}"
        transcript_path = self._write_transcript(
            "I have created and verified the entire authentication subsystem."
        )
        payload = {"conversationId": conv, "transcriptPath": transcript_path, "executionNum": 1}

        old_harness = hardtruth_hook.HARNESS
        hardtruth_hook.HARNESS = "claude_code"
        try:
            with mock.patch.object(hardtruth_hook, "call_system_one", return_value=None):
                res = hardtruth_hook.handle_stop(payload)
        finally:
            hardtruth_hook.HARNESS = old_harness

        self.assertEqual(res.get("decision"), "block")
        self.assertIn("HARDTRUTH DAEMON UNREACHABLE", res.get("reason", ""))

    def test_quiet_session_with_no_action_claims_not_blocked_by_dead_daemon(self):
        """Structural guarantee: a session with no action-claiming sentences never
        populates claims_to_verify and never reaches the per-claim loop at all, so a dead
        daemon must not block it."""
        conv = f"unit-quiet-{uuid.uuid4().hex}"
        transcript_path = self._write_transcript(
            "Let me know if you'd like me to look into anything else."
        )
        payload = {"conversationId": conv, "transcriptPath": transcript_path, "executionNum": 1}

        with mock.patch.object(hardtruth_hook, "call_system_one", return_value=None):
            res = hardtruth_hook.handle_stop(payload)

        self.assertEqual(res.get("decision"), "allow")


if __name__ == "__main__":
    unittest.main()
