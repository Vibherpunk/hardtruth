#!/usr/bin/env python3
"""
Unit and Integration Tests for Reachy Support, Dynamic Target Path Resolution,
Mac-Native Test Execution, and Loop Breaker / Circuit Breaker in HardTruth.
"""

import os
import sys
import json
import uuid
import tempfile
import unittest
import subprocess

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from daemon.tier2_runner import (
    find_project_root_for_file,
    resolve_target_project_dir,
    is_macos_native_project,
    detect_test_runner,
    run_independent_verification
)

HOOK_SCRIPT = os.path.abspath(os.path.join(REPO_ROOT, "client/hardtruth_hook.py"))


class TestReachyCircuitBreaker(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="reachy-test-")
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700, exist_ok=True)
        self.conv_id = f"test-reachy-{uuid.uuid4().hex}"
        self.old_halt_dir = os.environ.get("HARDTRUTH_HALT_DIR")
        os.environ["HARDTRUTH_HALT_DIR"] = self.halt_dir

    def tearDown(self):
        if self.old_halt_dir:
            os.environ["HARDTRUTH_HALT_DIR"] = self.old_halt_dir
        else:
            os.environ.pop("HARDTRUTH_HALT_DIR", None)

    def test_01_find_project_root_for_file(self):
        """Tests that find_project_root_for_file identifies the nearest manifest."""
        reachy_src = "/Users/ai/dev/vibehard/apps/reachyd/src/types.rs"
        if not os.path.isfile(reachy_src):
            self.skipTest("reachyd fixture project not present on this host/container")
        root = find_project_root_for_file(reachy_src)
        self.assertEqual(root, "/Users/ai/dev/vibehard/apps/reachyd")

    def test_02_resolve_target_project_dir_with_modified_files(self):
        """Tests that parent dir /Users/ai/dev resolves to reachyd when modified files are present."""
        reachy_src = "/Users/ai/dev/vibehard/apps/reachyd/src/types.rs"
        if not os.path.isfile(reachy_src):
            self.skipTest("reachyd fixture project not present on this host/container")
        resolved = resolve_target_project_dir("/Users/ai/dev", modified_files=[reachy_src])
        self.assertEqual(resolved, "/Users/ai/dev/vibehard/apps/reachyd")

    def test_03_is_macos_native_project_detection(self):
        """Tests that reachyd is detected as macOS native due to CoreAudio/Cocoa/objc dependencies."""
        reachyd_dir = "/Users/ai/dev/vibehard/apps/reachyd"
        if not os.path.isdir(reachyd_dir):
            self.skipTest("reachyd fixture project not present on this host/container")
        self.assertTrue(is_macos_native_project(reachyd_dir))

        # Non-mac project should be false
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "Cargo.toml"), "w") as f:
                f.write('[package]\nname = "generic"\nversion = "0.1.0"\n')
            self.assertFalse(is_macos_native_project(tmp))

    def test_04_run_independent_verification_reachyd(self):
        """Tests that Tier 2 independent runner resolves reachyd and passes tests cleanly on host."""
        reachyd_dir = "/Users/ai/dev/vibehard/apps/reachyd"
        if not os.path.isdir(reachyd_dir):
            self.skipTest("reachyd fixture project not present on this host/container")
        res = run_independent_verification(reachyd_dir, conv_id=self.conv_id, timeout_sec=30)
        self.assertTrue(res.get("success"), f"Tier 2 failed: {res.get('output')}")
        self.assertEqual(res.get("status"), "verified")
        self.assertEqual(res.get("exit_code"), 0)

    def test_05_circuit_breaker_breaks_claude_infinite_loop(self):
        """Tests that >3 consecutive attempts without state change trigger loop breaker in Claude Code."""
        conv = f"claude-loop-{uuid.uuid4().hex}"
        halt_file = os.path.join(self.halt_dir, f"halt_{conv[:32]}_{uuid.uuid4().hex[:16]}.json")

        # Create a mock transcript
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I tried to finish my response."}
                    ]
                }
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "cwd": "/Users/ai/dev"
        }

        env = os.environ.copy()
        env["HARDTRUTH_HARNESS"] = "claude_code"
        env["HARDTRUTH_HALT_DIR"] = self.halt_dir
        env["HARDTRUTH_SKIP_TIER2"] = "1"

        try:
            # Record a file modification in post_tool so HardTruth halts for unverified modifications
            subprocess.run(
                [sys.executable, HOOK_SCRIPT, "post_tool"],
                input=json.dumps({
                    "conversationId": conv,
                    "stepIdx": 1,
                    "toolCall": {
                        "name": "replace_file_content",
                        "args": {"TargetFile": "/Users/ai/dev/vibehard/apps/reachyd/src/types.rs"}
                    }
                }),
                capture_output=True,
                text=True,
                env=env,
                check=True
            )

            # First 3 attempts without state change should be blocked
            for attempt in range(1, 4):
                proc = subprocess.run(
                    [sys.executable, HOOK_SCRIPT, "stop"],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    env=env
                )
                res = json.loads(proc.stdout)
                self.assertEqual(res.get("decision"), "block", f"Attempt {attempt} was not blocked")

            # 4th attempt (>3 without state change) MUST trip circuit breaker and return allow to break loop
            proc_4 = subprocess.run(
                [sys.executable, HOOK_SCRIPT, "stop"],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env
            )
            res_4 = json.loads(proc_4.stdout)
            self.assertEqual(res_4.get("decision"), "allow", "4th attempt without state change must allow to break loop")
            self.assertIn("CIRCUIT BREAKER", proc_4.stderr, "Circuit breaker notice must be in stderr")

        finally:
            if os.path.exists(transcript_path):
                os.remove(transcript_path)


if __name__ == "__main__":
    unittest.main()
