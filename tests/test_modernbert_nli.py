"""
HardTruth 2.0 ModernBERT NLI Engine Unit & Integration Tests.
Verifies:
1. Dynamic label index resolution across model families (ModernBERT, DeBERTa-v3).
2. Strict error raising on missing, malformed, or invalid label permutations.
3. Startup canary execution and safety abort mechanics.
4. Ledger premise capacity expansion up to 8,000 characters.
5. End-to-end NLI evaluation on canonical contradiction and entailment pairs.
"""

import os
import sys
import unittest
import pytest

# Ensure daemon directory is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "daemon")))

from app import _resolve_label_indices, _run_startup_canary
from ledger import DaemonLedger


class DummyConfig:
    def __init__(self, label2id):
        self.label2id = label2id


class TestLabelResolution(unittest.TestCase):
    def test_modernbert_label_resolution(self):
        # ModernBERT ordering: 0: entailment, 1: neutral, 2: contradiction
        cfg = DummyConfig({
            "entailment": 0,
            "neutral": 1,
            "contradiction": 2
        })
        indices = _resolve_label_indices(cfg)
        self.assertEqual(indices["entailment"], 0)
        self.assertEqual(indices["neutral"], 1)
        self.assertEqual(indices["contradiction"], 2)

    def test_deberta_label_resolution(self):
        # DeBERTa-v3 ordering: 0: contradiction, 1: entailment, 2: neutral
        cfg = DummyConfig({
            "contradiction": 0,
            "entailment": 1,
            "neutral": 2
        })
        indices = _resolve_label_indices(cfg)
        self.assertEqual(indices["contradiction"], 0)
        self.assertEqual(indices["entailment"], 1)
        self.assertEqual(indices["neutral"], 2)

    def test_case_insensitive_label_resolution(self):
        cfg = DummyConfig({
            "CONTRADICTION": "2",
            "  Entailment  ": "0",
            "Neutral": 1
        })
        indices = _resolve_label_indices(cfg)
        self.assertEqual(indices["entailment"], 0)
        self.assertEqual(indices["neutral"], 1)
        self.assertEqual(indices["contradiction"], 2)

    def test_missing_label_raises_runtime_error(self):
        # Generic HuggingFace unmapped labels
        cfg = DummyConfig({
            "LABEL_0": 0,
            "LABEL_1": 1,
            "LABEL_2": 2
        })
        with self.assertRaises(RuntimeError) as ctx:
            _resolve_label_indices(cfg)
        self.assertIn("Refusing to guess a label ordering", str(ctx.exception))

    def test_duplicate_indices_raises_runtime_error(self):
        cfg = DummyConfig({
            "contradiction": 0,
            "entailment": 0,
            "neutral": 1
        })
        with self.assertRaises(RuntimeError) as ctx:
            _resolve_label_indices(cfg)
        self.assertIn("clean permutation", str(ctx.exception))


class TestLedgerPremiseExpansion(unittest.TestCase):
    def test_expanded_premise_and_tails(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as lf, \
             tempfile.NamedTemporaryFile(suffix=".key", delete=False) as kf:
            ledger_path = lf.name
            key_path = kf.name

        ledger = DaemonLedger(ledger_path=ledger_path, key_path=key_path)
        conv_id = "test-conv-expansion"
        
        # Record a test failure with 400 chars of output
        long_output = "AssertionError: Expected 200 OK but received 500 Internal Server Error\n" * 6
        ledger.record_entry(
            conversation_id=conv_id,
            step_idx=1,
            tool="run_command",
            target="pytest tests/test_critical.py",
            observed_exit_code=1,
            stdout_tail=long_output
        )

        # Record multiple recent executions with lengthy tails
        for i in range(2, 6):
            ledger.record_entry(
                conversation_id=conv_id,
                step_idx=i,
                tool="run_command",
                target=f"npm test step {i}",
                observed_exit_code=0,
                stdout_tail=f"PASS test {i}: " + ("x" * 250)
            )

        premise_data = ledger.get_premise(conv_id)
        premise = premise_data["premise"]

        # Verify that premise contains expanded failure trace well past the old 120-char limit
        self.assertIn("AssertionError", premise)
        self.assertGreater(len(premise), 1000)
        self.assertLessEqual(len(premise), 8000)
        # Verify unresolved failures captured
        self.assertEqual(len(premise_data["unresolved_failures"]), 1)


class TestStartupCanary(unittest.TestCase):
    def test_canary_failure_raises(self):
        class InvertedModel:
            def __call__(self, **kwargs):
                class Logits:
                    import torch
                    # Inverted: reports 0 for contradiction and 1 for entailment
                    logits = torch.tensor([[0.0, 10.0, 0.0], [10.0, 0.0, 0.0]])
                return Logits()

        class DummyTok:
            def __call__(self, *args, **kwargs):
                class Inputs(dict):
                    def to(self, device):
                        return self
                return Inputs()

        with self.assertRaises(RuntimeError) as ctx:
            _run_startup_canary(
                tok=DummyTok(),
                model=InvertedModel(),
                device="cpu",
                indices={"contradiction": 0, "entailment": 1, "neutral": 2},
                max_len=512
            )
        self.assertIn("NLI startup canary FAILED", str(ctx.exception))


class TestLiveModernBERTInference(unittest.TestCase):
    def test_live_inference_contradiction_and_entailment(self):
        try:
            from app import _evaluate_nli_pairs, _nli_max_length
        except ImportError:
            self.skipTest("PyTorch/Transformers not available in this test runner")

        # Test contradiction
        res_contra = _evaluate_nli_pairs([
            ("Pytest exit code 1; 2 failed, 10 passed.", "All tests passed successfully.")
        ])
        self.assertEqual(len(res_contra), 1)
        self.assertEqual(res_contra[0]["status"], "CONTRADICTION")
        self.assertGreaterEqual(res_contra[0]["probabilities"]["contradiction"], 0.80)

        # Test entailment
        res_entail = _evaluate_nli_pairs([
            ("All 48 tests passed in 2.3 seconds.", "All tests passed successfully.")
        ])
        self.assertEqual(len(res_entail), 1)
        self.assertEqual(res_entail[0]["status"], "ENTAILED")
        self.assertGreaterEqual(res_entail[0]["probabilities"]["entailment"], 0.80)

    def test_long_sequence_beyond_512_tokens(self):
        try:
            from app import _evaluate_nli_pairs, _nli_max_length
        except ImportError:
            self.skipTest("PyTorch/Transformers not available in this test runner")

        # Create a long premise with > 800 tokens of test log text
        long_log = "PASSED tests/test_core.py::test_case_num_%d in 0.05s\n"
        premise = "UNRESOLVED TEST FAILURES (CRITICAL): FAILED: pytest tests/test_critical.py (exit code 1).\n"
        premise += "".join(long_log % i for i in range(40))
        hypothesis = "All tests passed successfully."

        res = _evaluate_nli_pairs([(premise, hypothesis)])
        self.assertEqual(len(res), 1)
        # Verify that context beyond 512 tokens processes without length or index error
        self.assertIn(res[0]["status"], ["CONTRADICTION", "NEUTRAL"])
        self.assertGreaterEqual(res[0]["probabilities"]["contradiction"], 0.20)


if __name__ == "__main__":
    unittest.main()
