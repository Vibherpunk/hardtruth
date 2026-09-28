#!/usr/bin/env python3
"""
Tests for the SessionStart baseline fix (hardtruth-claude-code-fixes task):

Problem: at the first Stop of a Claude Code session in which nothing was edited, Rule 1
("Source code files were modified") could fire on dirt that pre-dated the session, because
the old baseline (get_or_set_session_baseline / get_session_baseline_dirty_files) is only
captured lazily on the FIRST hook call for a conversation -- which, when that first call is
itself the Stop call, happens too late: the snapshot and the check are the same event.

Fix: a `session_start` CLI subcommand (client/hardtruth_hook.py, handle_session_start) that
Claude Code's SessionStart hook invokes before the agent's first tool call. It records, per
session_id, HEAD sha plus a content hash of every currently-dirty/untracked file for cwd's
repo (and any git repos directly under cwd). get_git_modified_source_files then treats a
dirty file as "modified this session" only if it wasn't dirty at baseline, or its content
hash has changed since baseline -- so pre-existing dirt that is never touched is excluded,
while any edit to it (including a raw shell echo/sed/patch) is still caught.

These tests run the real hook script as a subprocess (like test_hybrid_architecture.py),
use only local temp git repos, and touch no real daemon or network (HARDTRUTH_SKIP_TIER2=1,
an unreachable SYSTEM_ONE_URL with sub-second timeouts baked into the hook itself, and a
throwaway HARDTRUTH_API_KEY path so nothing is ever written under ~/.hardtruth).
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


class TestSessionStartBaseline(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-session-start-test-")
        self.ledger_file = os.path.join(self.test_dir, "daemon_ledger.jsonl")
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700, exist_ok=True)
        self.conv_id = f"test-sessionstart-{uuid.uuid4().hex}"
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

    def make_repo(self) -> str:
        repo_dir = os.path.join(self.test_dir, f"repo-{uuid.uuid4().hex[:8]}")
        os.makedirs(repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
        app_file = os.path.join(repo_dir, "app.py")
        with open(app_file, "w") as f:
            f.write("# initial code\n")
        subprocess.run(["git", "add", "app.py"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)
        return repo_dir

    def make_transcript(self, text: str = "Nothing to report.") -> str:
        with tempfile.NamedTemporaryFile("w", delete=False, dir=self.test_dir, suffix=".jsonl") as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": text,
                "tool_calls": []
            }) + "\n")
            return tf.name

    def session_start(self, repo_dir: str, source: str = "startup"):
        return self.run_hook("session_start", {
            "session_id": self.conv_id,
            "cwd": repo_dir,
            "source": source,
            "transcript_path": ""
        }, harness="claude_code")

    def stop(self, repo_dir: str, transcript_path: str):
        return self.run_hook("stop", {
            "conversationId": self.conv_id,
            "transcriptPath": transcript_path,
            "workspacePaths": [repo_dir]
        })

    # ------------------------------------------------------------------
    # 1. Pre-existing dirty file + read-only session -> NOT counted
    # ------------------------------------------------------------------
    def test_pre_existing_dirty_file_read_only_session_not_counted(self):
        repo_dir = self.make_repo()
        app_file = os.path.join(repo_dir, "app.py")
        # Dirt that pre-dates the session (e.g. left over from before Claude Code launched).
        with open(app_file, "a") as f:
            f.write("# pre-existing uncommitted line\n")

        start_res = self.session_start(repo_dir)
        self.assertEqual(start_res, {}, "SessionStart must return non-blocking {} output")

        transcript_path = self.make_transcript()
        res = self.stop(repo_dir, transcript_path)
        os.remove(transcript_path)

        self.assertNotIn(
            "Source code files were modified", res.get("reason", ""),
            f"Pre-existing dirt untouched during the session must not trigger Rule 1: {res}"
        )

    # ------------------------------------------------------------------
    # 2. Pre-existing dirty file edited AGAIN via raw shell after baseline -> counted
    #    (this is the anti-evasion guarantee: baseline-dirty is not a permanent exemption)
    # ------------------------------------------------------------------
    def test_pre_existing_dirty_file_edited_after_baseline_is_counted(self):
        repo_dir = self.make_repo()
        app_file = os.path.join(repo_dir, "app.py")
        with open(app_file, "a") as f:
            f.write("# pre-existing uncommitted line\n")

        self.session_start(repo_dir)

        # Raw shell modification AFTER the baseline was captured -- simulates `echo >> file`.
        with open(app_file, "a") as f:
            f.write("def calculate():\n    return 42\n")

        transcript_path = self.make_transcript("I edited app.py using a shell command.")
        res = self.stop(repo_dir, transcript_path)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Source code files were modified", res.get("reason", ""))

    # ------------------------------------------------------------------
    # 3. New file created after baseline -> counted
    # ------------------------------------------------------------------
    def test_new_file_after_baseline_is_counted(self):
        repo_dir = self.make_repo()
        self.session_start(repo_dir)

        new_file = os.path.join(repo_dir, "new_module.py")
        with open(new_file, "w") as f:
            f.write("def hello():\n    return 'hi'\n")

        transcript_path = self.make_transcript("I created new_module.py.")
        res = self.stop(repo_dir, transcript_path)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Source code files were modified", res.get("reason", ""))

    # ------------------------------------------------------------------
    # 4. No SessionStart baseline (older session / other harness) -> old behavior unchanged
    #    (this documents the pre-fix limitation is deliberately preserved when SessionStart
    #    never ran: the hook must never fail open just because a baseline is missing).
    # ------------------------------------------------------------------
    def test_no_session_start_baseline_keeps_old_behavior(self):
        repo_dir = self.make_repo()
        app_file = os.path.join(repo_dir, "app.py")
        with open(app_file, "a") as f:
            f.write("# pre-existing uncommitted line, no SessionStart ever ran\n")

        # No session_start call at all.
        transcript_path = self.make_transcript()
        res = self.stop(repo_dir, transcript_path)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Source code files were modified", res.get("reason", ""))

    # ------------------------------------------------------------------
    # 5. resume/compact/clear must not overwrite an existing baseline
    # ------------------------------------------------------------------
    def test_resume_does_not_overwrite_baseline(self):
        repo_dir = self.make_repo()
        # Repo is clean at the real session start.
        start_res = self.session_start(repo_dir, source="startup")
        self.assertEqual(start_res, {})

        # Agent edits a tracked file mid-session (non-stub body: a `pass`-bodied function
        # would trip the unrelated AST anti-stub gate before Rule 1 is even reached).
        app_file = os.path.join(repo_dir, "app.py")
        with open(app_file, "a") as f:
            f.write("def new_feature():\n    return 99\n")

        # A resume/compact event fires later in the same session_id. It must NOT re-snapshot
        # the now-dirty tree as the new baseline (which would wrongly forgive the edit above).
        resume_res = self.session_start(repo_dir, source="resume")
        self.assertEqual(resume_res, {})

        transcript_path = self.make_transcript("I added new_feature to app.py.")
        res = self.stop(repo_dir, transcript_path)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn(
            "Source code files were modified", res.get("reason", ""),
            "resume must not have overwritten the startup baseline to forgive the mid-session edit"
        )

    # ------------------------------------------------------------------
    # 6. Echo-evasion guarantee still holds when a SessionStart baseline exists for a
    #    CLEAN repo (no pre-existing dirt at all): a same-session raw shell edit is caught.
    # ------------------------------------------------------------------
    def test_echo_evasion_still_caught_with_session_start_baseline(self):
        repo_dir = self.make_repo()
        self.session_start(repo_dir)

        app_file = os.path.join(repo_dir, "app.py")
        with open(app_file, "a") as f:
            f.write("def calculate():\n    return 42\n")

        transcript_path = self.make_transcript("I finished editing app.py using a shell command.")
        res = self.stop(repo_dir, transcript_path)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Source code files were modified", res.get("reason", ""))
        self.assertIn("NO verification commands", res.get("reason", ""))

    # ------------------------------------------------------------------
    # 7. SessionStart itself never raises / never emits blocking JSON, even for a bogus cwd.
    # ------------------------------------------------------------------
    def test_session_start_never_blocks_on_bad_input(self):
        res = self.run_hook("session_start", {
            "session_id": self.conv_id,
            "cwd": "/path/does/not/exist/at/all",
            "source": "startup"
        }, harness="claude_code")
        self.assertEqual(res, {})


if __name__ == "__main__":
    unittest.main()
