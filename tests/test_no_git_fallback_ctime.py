#!/usr/bin/env python3
"""
Tests for the no-.git fallback ctime fix (fix/no-git-fallback-ctime):

When the workspace is NOT a git repo at Stop time, get_git_modified_source_files
walks the tree instead of using git status. The fix distinguishes three cases via
the SessionStart record (_session_start_workspace_state):
  1. never was a repo  -> count only files with ctime after session start
     (2s margin; ctime can't be faked with touch -t, unlike mtime)
  2. was a repo, .git deleted mid-session -> count EVERYTHING (fail safe,
     anti-evasion: deleting .git to hide edits must not hide anything)
  3. no session-start record, or unreadable/corrupt record -> count EVERYTHING
     (fail safe: falls back to the old behavior exactly)

These tests run the real hook script as a subprocess (like
test_session_start_baseline.py), use only local temp dirs, and touch no real
daemon or network (HARDTRUTH_SKIP_TIER2=1, an unreachable SYSTEM_ONE_URL with
sub-second timeouts baked into the hook itself, and a throwaway
HARDTRUTH_API_KEY path so nothing is ever written under ~/.hardtruth).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

HOOK_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../client/hardtruth_hook.py"))
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class TestNoGitFallbackCtime(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-nogit-test-")
        self.ledger_file = os.path.join(self.test_dir, "daemon_ledger.jsonl")
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700, exist_ok=True)
        self.conv_id = f"test-nogit-{uuid.uuid4().hex}"
        self.env = {
            "HARDTRUTH_LEDGER_PATH": self.ledger_file,
            "HARDTRUTH_DAEMON_LEDGER": self.ledger_file,
            "HARDTRUTH_HALT_DIR": self.halt_dir,
            # Unreachable on purpose -- every daemon call in the hook uses a short
            # (<=3s, mostly 1s) timeout and degrades gracefully, so this stays fast.
            "SYSTEM_ONE_URL": "http://127.0.0.1:1",
            "HARDTRUTH_SKIP_TIER2": "1",
            "HARDTRUTH_API_KEY": os.path.join(self.test_dir, "no-such-key"),
        }

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def run_hook(self, mode: str, payload: dict, harness: str = "antigravity") -> dict:
        env = os.environ.copy()
        env.update(self.env)
        env["HARDTRUTH_HARNESS"] = harness
        proc = subprocess.run(
            ["python3", HOOK_SCRIPT, mode],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=30,
            check=True
        )
        out_str = proc.stdout.decode("utf-8").strip()
        return json.loads(out_str) if out_str else {}

    def make_plain_dir(self) -> str:
        """A workspace that was never a git repo."""
        ws = os.path.join(self.test_dir, f"ws-{uuid.uuid4().hex[:8]}")
        os.makedirs(ws, exist_ok=True)
        return ws

    def make_repo(self) -> str:
        ws = os.path.join(self.test_dir, f"repo-{uuid.uuid4().hex[:8]}")
        os.makedirs(ws, exist_ok=True)
        subprocess.run(["git", "init"], cwd=ws, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=ws, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=ws, check=True)
        with open(os.path.join(ws, "app.py"), "w") as f:
            f.write("# initial code\n")
        subprocess.run(["git", "add", "app.py"], cwd=ws, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=ws, check=True)
        return ws

    def make_transcript(self, text: str = "Nothing to report.") -> str:
        with tempfile.NamedTemporaryFile("w", delete=False, dir=self.test_dir, suffix=".jsonl") as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": text,
                "tool_calls": []
            }) + "\n")
            return tf.name

    def session_start(self, ws: str, source: str = "startup"):
        return self.run_hook("session_start", {
            "session_id": self.conv_id,
            "cwd": ws,
            "source": source,
            "transcript_path": ""
        }, harness="claude_code")

    def stop(self, ws: str, transcript_path: str):
        return self.run_hook("stop", {
            "conversationId": self.conv_id,
            "transcriptPath": transcript_path,
            "workspacePaths": [ws]
        })

    def baseline_file_for(self, conv_id: str) -> str:
        """Replicates get_session_start_baseline_file() path computation."""
        safe_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(conv_id))[:32]
        conv_hash = hashlib.sha256(str(conv_id).encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.halt_dir, f"baseline_{safe_slug}_{conv_hash}.json")

    def assert_rule1_fires(self, res: dict, msg: str):
        self.assertIn("Source code files were modified", res.get("reason", ""), msg)

    def assert_rule1_silent(self, res: dict, msg: str):
        self.assertNotIn("Source code files were modified", res.get("reason", ""), msg)

    # ------------------------------------------------------------------
    # 1. never-repo + file predates session start (ctime) -> NOT counted
    # ------------------------------------------------------------------
    def test_never_repo_pre_session_file_not_counted(self):
        ws = self.make_plain_dir()
        with open(os.path.join(ws, "old.py"), "w") as f:
            f.write("# pre-existing, untouched this session\n")
        # ctime must predate session start by more than the 2s margin.
        time.sleep(3)

        start_res = self.session_start(ws)
        self.assertEqual(start_res, {}, "SessionStart must return non-blocking {} output")

        tp = self.make_transcript()
        res = self.stop(ws, tp)
        os.remove(tp)
        self.assert_rule1_silent(
            res,
            f"File predating the session in a never-repo workspace must not trigger Rule 1: {res}",
        )

    # ------------------------------------------------------------------
    # 2. never-repo + file created after session start -> counted
    # ------------------------------------------------------------------
    def test_never_repo_post_session_file_counted(self):
        ws = self.make_plain_dir()
        self.session_start(ws)

        with open(os.path.join(ws, "new.py"), "w") as f:
            f.write("def calculate():\n    return 42\n")

        tp = self.make_transcript("I created new.py using a shell command.")
        res = self.stop(ws, tp)
        os.remove(tp)
        self.assert_rule1_fires(
            res,
            f"File created after session start in a never-repo workspace must trigger Rule 1: {res}",
        )

    # ------------------------------------------------------------------
    # 3. no session-start record at all -> counts everything (fail safe)
    # ------------------------------------------------------------------
    def test_no_record_counts_everything(self):
        ws = self.make_plain_dir()
        with open(os.path.join(ws, "old.py"), "w") as f:
            f.write("# pre-existing, no session_start ever ran\n")

        # Deliberately NO session_start call: no record exists.
        tp = self.make_transcript()
        res = self.stop(ws, tp)
        os.remove(tp)
        self.assert_rule1_fires(
            res,
            f"With no session-start record the fallback must count everything (fail safe): {res}",
        )

    # ------------------------------------------------------------------
    # 4. was a repo at session start, .git deleted mid-session -> counts
    #    everything (fail safe; deleting .git must not hide edits)
    # ------------------------------------------------------------------
    def test_deleted_git_repo_counts_everything(self):
        ws = self.make_repo()
        self.session_start(ws)

        # Evasion attempt: delete .git mid-session to hide edits.
        shutil.rmtree(os.path.join(ws, ".git"))

        tp = self.make_transcript()
        res = self.stop(ws, tp)
        os.remove(tp)
        self.assert_rule1_fires(
            res,
            f"Deleted .git must not hide files -- fallback must count everything: {res}",
        )

    # ------------------------------------------------------------------
    # 5. corrupt/unreadable session-start record -> counts everything
    # ------------------------------------------------------------------
    def test_corrupt_baseline_counts_everything(self):
        ws = self.make_plain_dir()
        with open(os.path.join(ws, "old.py"), "w") as f:
            f.write("# pre-existing\n")
        self.session_start(ws)

        bf = self.baseline_file_for(self.conv_id)
        self.assertTrue(os.path.exists(bf), "session_start should have written a baseline file")
        with open(bf, "w") as f:
            f.write("{this is not valid json")

        tp = self.make_transcript()
        res = self.stop(ws, tp)
        os.remove(tp)
        self.assert_rule1_fires(
            res,
            f"Corrupt baseline record must fall back to counting everything (fail safe): {res}",
        )


if __name__ == "__main__":
    unittest.main()
