#!/usr/bin/env python3
"""
Unit tests for DaemonLedger:
1. Genesis block and hash-chain creation
2. Tamper detection on payload modification or line deletion
3. Verification command vs exploratory command classification
4. Failure-biased resolution: failing pytest pinned through 10 echo commands, resolved by clean pytest
5. Non-synthesis rule: null exit code records harness_status without fake exit 0
6. Source vs doc file classification
"""

import os
import sys
import json
import shutil
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from daemon.ledger import DaemonLedger, is_verification_command, is_exploratory_command, classify_file, get_daemon_api_token, validate_api_token


class TestDaemonLedger(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-ledger-test-")
        self.ledger_path = os.path.join(self.test_dir, "daemon_ledger.jsonl")
        self.key_path = os.path.join(self.test_dir, "daemon_hmac.key")
        self.ledger = DaemonLedger(ledger_path=self.ledger_path, key_path=self.key_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_hash_chain_creation_and_integrity(self):
        # Empty ledger verifies
        valid, count, msg = self.ledger.verify_chain()
        self.assertTrue(valid)
        self.assertEqual(count, 0)

        # Record entries
        rec1 = self.ledger.record_entry("c1", 1, "run_command", "git status", observed_exit_code=0)
        self.assertEqual(rec1["index"], 0)

        rec2 = self.ledger.record_entry("c1", 2, "run_command", "pytest tests/", observed_exit_code=0)
        self.assertEqual(rec2["index"], 1)

        valid, count, msg = self.ledger.verify_chain()
        self.assertTrue(valid)
        self.assertEqual(count, 2)

    def test_tamper_detection(self):
        # Add 3 entries
        for i in range(3):
            self.ledger.record_entry("c1", i, "run_command", f"echo {i}", observed_exit_code=0)

        valid, count, msg = self.ledger.verify_chain()
        self.assertTrue(valid)

        # Read lines and tamper entry 1
        with open(self.ledger_path, "r") as f:
            lines = [json.loads(line) for line in f]

        lines[1]["entry"]["target"] = "tampered_command"

        with open(self.ledger_path, "w") as f:
            for l in lines:
                f.write(json.dumps(l) + "\n")

        valid, count, msg = self.ledger.verify_chain()
        self.assertFalse(valid)
        self.assertEqual(count, 1)

        premise_res = self.ledger.get_premise("c1")
        self.assertTrue(premise_res["tampered"])
        self.assertEqual(premise_res["error"], "LEDGER_TAMPER_DETECTED")

    def test_exploratory_vs_verification_commands(self):
        self.assertTrue(is_verification_command("pytest tests/"))
        self.assertTrue(is_verification_command("cargo test --lib"))
        self.assertTrue(is_verification_command("npm test"))
        self.assertTrue(is_verification_command("python3 -m unittest"))
        self.assertTrue(is_verification_command("ruff check ."))

        self.assertFalse(is_verification_command("grep -q foo bar.py"))
        self.assertFalse(is_verification_command("ls -la"))
        self.assertFalse(is_verification_command("cat README.md"))
        self.assertFalse(is_verification_command("echo hello"))

        self.assertTrue(is_exploratory_command("grep -q foo bar.py"))
        self.assertTrue(is_exploratory_command("ls -la"))
        self.assertTrue(is_exploratory_command("cat file.txt"))
        self.assertTrue(is_exploratory_command("git status"))

    def test_failure_biased_resolution(self):
        # 1. Failing pytest
        self.ledger.record_entry(
            "c_fail", 1, "run_command", "pytest tests/",
            observed_exit_code=1, error="exit status 1", stdout_tail="FAILED test_auth.py"
        )
        premise = self.ledger.get_premise("c_fail")
        self.assertEqual(len(premise["unresolved_failures"]), 1)
        self.assertIn("UNRESOLVED TEST FAILURES", premise["premise"])

        # 2. Run 10 harmless echo/ls commands (sliding window attempt)
        for i in range(10):
            self.ledger.record_entry("c_fail", i + 2, "run_command", f"echo harmless_{i}", observed_exit_code=0)

        premise_after_10 = self.ledger.get_premise("c_fail")
        # Failing pytest MUST STILL BE PINNED in unresolved_failures!
        self.assertEqual(len(premise_after_10["unresolved_failures"]), 1)
        self.assertIn("FAILED: 'pytest tests/'", premise_after_10["premise"])

        # 3. Clean re-run of pytest resolves the failure
        self.ledger.record_entry("c_fail", 15, "run_command", "pytest tests/", observed_exit_code=0)
        premise_resolved = self.ledger.get_premise("c_fail")
        self.assertEqual(len(premise_resolved["unresolved_failures"]), 0)
        self.assertNotIn("UNRESOLVED TEST FAILURES", premise_resolved["premise"])

    def test_exploratory_command_failure_does_not_deadlock(self):
        # grep failing with exit code 1 because no matches found
        self.ledger.record_entry(
            "c_grep", 1, "run_command", "grep -q non_existent file.py",
            observed_exit_code=1, error="exit status 1"
        )
        premise = self.ledger.get_premise("c_grep")
        self.assertEqual(len(premise["unresolved_failures"]), 0)
        self.assertEqual(premise["verification_commands_executed"], 0)

    def test_file_classification(self):
        self.assertEqual(classify_file("app.py"), "source")
        self.assertEqual(classify_file("index.ts"), "source")
        self.assertEqual(classify_file("main.rs"), "source")
        self.assertEqual(classify_file("README.md"), "doc")
        self.assertEqual(classify_file("docs/DESIGN_NOTE.md"), "doc")
        self.assertEqual(classify_file("data.csv"), "doc")

    def test_non_synthesis_rule(self):
        # Command with no exit code corroboration
        self.ledger.record_entry(
            "c_nosynth", 1, "run_command", "python3 some_task.py",
            observed_exit_code=None, harness_status="no_error"
        )
        premise = self.ledger.get_premise("c_nosynth")
        # Must not say "exit 0"
        self.assertNotIn("exit 0", premise["premise"])
        self.assertIn("uncorroborated by transcript", premise["premise"])


class TestRound7ApiToken(unittest.TestCase):
    """Round 7 Finding A: daemon API write-token validation (constant-time)."""

    def test_validate_api_token_env(self):
        os.environ["HARDTRUTH_API_TOKEN"] = "test-token-0123456789abcdef"
        try:
            self.assertTrue(validate_api_token("test-token-0123456789abcdef"))
            self.assertFalse(validate_api_token("forged-token"))
            self.assertFalse(validate_api_token(""))
            self.assertFalse(validate_api_token(None))
        finally:
            os.environ.pop("HARDTRUTH_API_TOKEN", None)

    def test_get_daemon_api_token_min_length(self):
        os.environ.pop("HARDTRUTH_API_TOKEN", None)
        tok = get_daemon_api_token()
        self.assertGreaterEqual(len(tok), 32)
        self.assertTrue(validate_api_token(tok))


class TestRound8PurgeRecords(unittest.TestCase):
    """Round 8 (#6): test-artifact purge rebuilds the ledger chain (chain stays valid)."""

    def test_purge_records_excludes_prefixes_and_keeps_chain_valid(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.jsonl")
            key_path = os.path.join(td, "key")
            ledger = DaemonLedger(ledger_path=ledger_path, key_path=key_path)
            ledger.record_entry(conversation_id="test-conv-abc", step_idx=0, tool="run_command", target="pytest", observed_exit_code=0)
            ledger.record_entry(conversation_id="auth-test-xyz", step_idx=0, tool="run_command", target="pytest", observed_exit_code=0)
            ledger.record_entry(conversation_id="real-conv-1", step_idx=0, tool="run_command", target="pytest", observed_exit_code=0)
            ledger.set_session_baseline("test-conv-abc", os.path.abspath("."), "sha123")

            res = ledger.purge_records(["test-conv-", "auth-test-", "tier2-live-"])
            self.assertEqual(res["purged"], 3, "two records + one baseline for test-conv-abc")
            self.assertEqual(res["kept"], 1)
            valid, count, msg = ledger.verify_chain()
            self.assertTrue(valid, msg)
            self.assertEqual(count, 1)
            premise = ledger.get_premise("real-conv-1")
            self.assertGreaterEqual(premise.get("verification_commands_executed", 0), 1)

    def test_purge_noop_when_no_match(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = DaemonLedger(ledger_path=os.path.join(td, "l.jsonl"), key_path=os.path.join(td, "k"))
            ledger.record_entry(conversation_id="real-conv-1", step_idx=0, tool="run_command", target="pytest", observed_exit_code=0)
            res = ledger.purge_records(["test-conv-"])
            self.assertEqual(res["purged"], 0)
            self.assertEqual(res["kept"], 1)

    def test_purge_refuses_tampered_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "l.jsonl")
            ledger = DaemonLedger(ledger_path=ledger_path, key_path=os.path.join(td, "k"))
            ledger.record_entry(conversation_id="test-conv-abc", step_idx=0, tool="run_command", target="pytest", observed_exit_code=0)
            # Tamper: change the entry's stepIdx so the canonical hash no longer matches.
            with open(ledger_path, "r", encoding="utf-8") as f:
                content = f.read().replace('"stepIdx": 0', '"stepIdx": 99', 1)
            with open(ledger_path, "w", encoding="utf-8") as f:
                f.write(content)
            with self.assertRaises(ValueError):
                ledger.purge_records(["test-conv-"])


if __name__ == "__main__":
    unittest.main()
