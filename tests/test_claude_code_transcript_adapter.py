#!/usr/bin/env python3
"""
Unit tests for the Claude Code transcript adapter (Bug #1 in the hardtruth-claude-code-fixes
task): Claude Code transcripts are JSONL where each line has "type": "assistant" | "user" |
... , and tool calls live in message.content[] blocks (tool_use in assistant records,
tool_result in the following user record, matched by tool_use_id). These tests use small
synthetic fixtures under tests/fixtures/claude_code_transcripts/ and require no docker,
network, or live daemon.
"""

import os
import sys
import json
import shutil
import subprocess
import tempfile
import uuid
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

HOOK_SCRIPT = os.path.join(REPO_ROOT, "client", "hardtruth_hook.py")

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "claude_code_transcripts")


def fixture(name: str) -> str:
    return os.path.join(FIXTURE_DIR, name)


from client.hardtruth_hook import (
    parse_claude_code_transcript_commands,
    extract_claude_code_command_resolutions,
    extract_claude_code_edited_files,
    poll_claude_code_transcript_for_command,
)


class TestClaudeCodeTranscriptAdapter(unittest.TestCase):

    def test_simple_pass_resolved_exit_0(self):
        """A plain successful Bash run (no explicit exit-code text, no is_error) resolves to exit 0."""
        records = parse_claude_code_transcript_commands(fixture("simple_pass.jsonl"))
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["command"], "python3 -m pytest /Users/ai/dev/hardtruth-fix/tests")
        self.assertTrue(rec["resolved"])
        self.assertEqual(rec["exit_code"], 0)

    def test_failing_test_is_error_resolved_nonzero(self):
        """is_error=True with no explicit exit code text is treated as a failure (exit 1), never a pass."""
        records = parse_claude_code_transcript_commands(fixture("failing_test.jsonl"))
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertTrue(rec["resolved"])
        self.assertEqual(rec["exit_code"], 1)

    def test_explicit_exit_code_extracted(self):
        """An explicit 'exit code N' / 'exited with exit code N' marker in the result text is used verbatim."""
        records = parse_claude_code_transcript_commands(fixture("explicit_exit_code.jsonl"))
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertTrue(rec["resolved"])
        self.assertEqual(rec["exit_code"], 1)

    def test_background_unresolved_never_a_pass(self):
        """A backgrounded run with no later resolution stays unresolved -- never synthesized into a pass."""
        records = parse_claude_code_transcript_commands(fixture("background_unresolved.jsonl"))
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertFalse(rec["resolved"])
        self.assertIsNone(rec["exit_code"])
        self.assertEqual(rec["background_id"], "bash_9f31a2")

        successes, _ = extract_claude_code_command_resolutions(fixture("background_unresolved.jsonl"))
        self.assertEqual(successes, set())

    def test_background_resolved_by_later_bashoutput(self):
        """A backgrounded run IS resolved once a later BashOutput poll for the same id corroborates completion."""
        records = parse_claude_code_transcript_commands(fixture("background_resolved.jsonl"))
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertTrue(rec["resolved"])
        self.assertEqual(rec["exit_code"], 0)

        successes, step_map = extract_claude_code_command_resolutions(fixture("background_resolved.jsonl"))
        self.assertIn("python3 -m pytest /Users/ai/dev/hardtruth-fix/tests", successes)

    def test_background_still_running_stays_unresolved(self):
        """An explicit 'status: running' poll result must NOT resolve the background command."""
        records = parse_claude_code_transcript_commands(fixture("background_still_running.jsonl"))
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertFalse(rec["resolved"])
        self.assertIsNone(rec["exit_code"])

        successes, _ = extract_claude_code_command_resolutions(fixture("background_still_running.jsonl"))
        self.assertEqual(successes, set())

    def test_extract_edited_files_from_edit_write_notebookedit(self):
        """Edit/Write/NotebookEdit tool_use blocks are picked up by file_path/notebook_path."""
        edited = extract_claude_code_edited_files(fixture("edits_only.jsonl"))
        self.assertEqual(edited, {
            "/Users/ai/dev/hardtruth-fix/client/hardtruth_hook.py",
            "/Users/ai/dev/hardtruth-fix/tests/test_new.py",
            "/Users/ai/dev/hardtruth-fix/analysis.ipynb",
        })

    def test_read_only_transcript_has_no_edits_and_no_commands(self):
        """A read-only transcript yields no edited files and no command records (Bug #3 support)."""
        edited = extract_claude_code_edited_files(fixture("read_only.jsonl"))
        self.assertEqual(edited, set())
        records = parse_claude_code_transcript_commands(fixture("read_only.jsonl"))
        self.assertEqual(records, [])

    def test_tainted_piped_run_then_bare_pass_resolves_via_transcript(self):
        """
        Phase 4 reconciliation input: a TAINTED piped run recorded as a ledger failure, followed
        by a later bare pass of the same suite in the transcript, must surface as a resolvable
        success (command_successes contains the exact bare command the ledger's hierarchy/
        substring matching in handle_stop then uses to clear the earlier failure).
        """
        successes, step_map = extract_claude_code_command_resolutions(
            fixture("tainted_piped_then_bare_pass.jsonl")
        )
        self.assertIn("python3 -m pytest /Users/ai/dev/hardtruth-fix/tests", successes)
        # The tainted piped variant itself must never appear as a success.
        self.assertNotIn("python3 -m pytest /Users/ai/dev/hardtruth-fix/tests 2>&1 | tail -15", successes)

    def test_poll_claude_code_transcript_for_command_matches_by_exact_command(self):
        """poll_claude_code_transcript_for_command correlates by exact command text, not step index."""
        ec, tail, timed_out = poll_claude_code_transcript_for_command(
            fixture("simple_pass.jsonl"),
            "python3 -m pytest /Users/ai/dev/hardtruth-fix/tests",
            max_wait_ms=200
        )
        self.assertEqual(ec, 0)
        self.assertFalse(timed_out)

    def test_poll_claude_code_transcript_for_command_background_is_conservative(self):
        """An in-flight background command polled directly returns (None, ..., timed_out=True) --
        never a synthesized pass -- even though the tool_use/tool_result pair was found."""
        ec, tail, timed_out = poll_claude_code_transcript_for_command(
            fixture("background_unresolved.jsonl"),
            "python3 -m pytest /Users/ai/dev/hardtruth-fix/tests",
            max_wait_ms=200
        )
        self.assertIsNone(ec)
        self.assertTrue(timed_out)

    def test_poll_claude_code_transcript_for_command_missing_transcript(self):
        """A nonexistent transcript path never crashes -- fails closed as unverified."""
        ec, tail, timed_out = poll_claude_code_transcript_for_command(
            "/tmp/does-not-exist-hardtruth-transcript.jsonl",
            "python3 -m pytest tests/",
            max_wait_ms=100
        )
        self.assertIsNone(ec)
        self.assertTrue(timed_out)


class TestClaudeCodePostToolUseIntegration(unittest.TestCase):
    """
    End-to-end: handle_post_tool_use, invoked as a real subprocess with
    HARDTRUTH_HARNESS=claude_code, must never record a backgrounded/unresolved Bash run as
    a pass in the ledger, and must correctly record a genuine pass when the transcript shows
    one -- both via the transcript adapter fallback path (no exitCode in the payload).
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-cc-adapter-test-")
        self.ledger_file = os.path.join(self.test_dir, "ledger.jsonl")
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700, exist_ok=True)
        self.conv_id = f"test-cc-adapter-{uuid.uuid4().hex}"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def run_hook(self, mode: str, payload: dict) -> dict:
        env = os.environ.copy()
        env.update({
            "HARDTRUTH_HARNESS": "claude_code",
            "HARDTRUTH_LEDGER_PATH": self.ledger_file,
            "HARDTRUTH_HALT_DIR": self.halt_dir,
            "HARDTRUTH_SKIP_TIER2": "1",
            "HARDTRUTH_API_KEY": os.path.join(self.test_dir, "no-such-key"),
        })
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

    def _read_ledger_entries(self):
        entries = []
        if not os.path.exists(self.ledger_file):
            return entries
        with open(self.ledger_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                entries.append(rec.get("entry", rec))
        return entries

    def test_background_unresolved_never_recorded_as_pass(self):
        """No exitCode in the PostToolUse payload + a backgrounded transcript match ->
        the ledger entry must be unverified, never a silent pass."""
        cmd = "python3 -m pytest /Users/ai/dev/hardtruth-fix/tests"
        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
            "stepIdx": 1,
            "conversationId": self.conv_id,
            "transcriptPath": fixture("background_unresolved.jsonl"),
        })
        entries = [e for e in self._read_ledger_entries() if e.get("conversationId") == self.conv_id]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertNotEqual(entry.get("observed_exit_code"), 0)
        self.assertEqual(entry.get("harness_status"), "unverified_timeout")

    def test_clean_transcript_pass_recorded_correctly(self):
        """No exitCode in the payload but the transcript shows a clean pass -> recorded as exit 0."""
        cmd = "python3 -m pytest /Users/ai/dev/hardtruth-fix/tests"
        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
            "stepIdx": 1,
            "conversationId": self.conv_id,
            "transcriptPath": fixture("simple_pass.jsonl"),
        })
        entries = [e for e in self._read_ledger_entries() if e.get("conversationId") == self.conv_id]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.get("observed_exit_code"), 0)
        self.assertEqual(entry.get("harness_status"), "no_error")


if __name__ == "__main__":
    unittest.main()
