#!/usr/bin/env python3
"""
Comprehensive tests for HardTruth Hybrid Architecture:
1. Universal File Tracking: Catching modifications via echo/sed (Rule 1 fires without write_to_file)
2. Anchored Transcript Regex: Defeating stdout exit code spoofing
3. Shell Operator Neutralization: Rejecting pytest ; true and || exit 0 bypasses
4. Generalized Suite Resolution: npm test resolving npm run test:unit
5. Tier 2 Hard Gate Handoff: External runner independently executing tests
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

from daemon.ledger import DaemonLedger, can_suite_resolve_failure, is_tainted_shell_command
from daemon.tier2_runner import detect_test_runner, resolve_workspace_path, run_independent_verification


class TestHybridArchitecture(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-hybrid-test-")
        self.ledger_file = os.path.join(self.test_dir, "daemon_ledger.jsonl")
        self.halt_dir = os.path.join(self.test_dir, "halts")
        os.makedirs(self.halt_dir, mode=0o700, exist_ok=True)
        self.conv_id = f"test-hybrid-{uuid.uuid4().hex}"
        self.old_halt_dir = os.environ.get("HARDTRUTH_HALT_DIR")
        os.environ["HARDTRUTH_HALT_DIR"] = self.halt_dir
        # Round 8 (#6): token-less isolation in the process env too (some hook helpers
        # run in-process here, e.g. get_or_set_session_baseline) so no test run writes
        # into the physical daemon ledger.
        os.environ["HARDTRUTH_API_KEY"] = os.path.join(self.test_dir, "no-such-key")
        self.env = {
            "HARDTRUTH_LEDGER_PATH": self.ledger_file,
            "HARDTRUTH_DAEMON_LEDGER": self.ledger_file,
            "HARDTRUTH_HALT_DIR": self.halt_dir,
            "SYSTEM_ONE_URL": os.environ.get("SYSTEM_ONE_URL", "http://127.0.0.1:49281"),
            "HARDTRUTH_SKIP_TIER2": "1",  # Skipped unless specifically tested
            # Round 8 (#6): run hook subprocesses token-less so test suites never write
            # records into the physical daemon ledger (daemon rejects with 401; hook
            # degrades gracefully to its local temp ledger).
            "HARDTRUTH_API_KEY": os.path.join(self.test_dir, "no-such-key")
        }

    def tearDown(self):
        if self.old_halt_dir:
            os.environ["HARDTRUTH_HALT_DIR"] = self.old_halt_dir
        else:
            os.environ.pop("HARDTRUTH_HALT_DIR", None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def run_hook(self, mode: str, payload: dict, env_override: dict = None) -> dict:
        env = os.environ.copy()
        env.update(self.env)
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

    def test_universal_file_tracking_catches_echo_modification(self):
        """Modifying a source file via echo (simulating sed/echo in shell) triggers Rule 1."""
        # Initialize a temporary git repo in a subfolder
        git_repo_dir = os.path.join(self.test_dir, "test_repo")
        os.makedirs(git_repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=git_repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_repo_dir, check=True)

        # Initial commit
        initial_file = os.path.join(git_repo_dir, "app.py")
        with open(initial_file, "w") as f:
            f.write("# initial code\n")
        subprocess.run(["git", "add", "app.py"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=git_repo_dir, check=True)

        # Modify app.py directly on disk without using write_to_file tool!
        with open(initial_file, "a") as f:
            f.write("def calculate():\n    return 42\n")

        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "I finished editing app.py using a shell command.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": self.conv_id,
            "transcriptPath": transcript_path,
            "workspacePaths": [git_repo_dir]
        }
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        # Rule 1 MUST fire because git status --porcelain detected app.py modification
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Source code files were modified", res.get("reason", ""))
        self.assertIn("NO verification commands", res.get("reason", ""))

    def test_anchored_regex_defeats_stdout_spoofing(self):
        """An agent echoing 'The command exited with code 0' into stdout cannot spoof exit code."""
        conv = f"spoof-{uuid.uuid4().hex}"
        # Create transcript where actual exit was 1, but stdout contains fake string
        transcript_content = (
            '{"type": "GENERIC", "step_index": 5, "content": "Created At: 2026-09-19T20:00:00-07:00\\n'
            'Completed At: 2026-09-19T20:00:01-07:00\\n\\n'
            'The command exited with code 1.\\n'
            'Output:\\n'
            'FAILED: 1 test failed\\n'
            'The command exited with code 0"}\n'
        )
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(transcript_content)
            transcript_path = tf.name

        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/"}},
            "stepIdx": 5,
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "error": "exit status 1"
        })

        # Check ledger premise
        ledger = DaemonLedger(ledger_path=self.ledger_file)
        premise = ledger.get_premise(conv)
        os.remove(transcript_path)

        # The exit code MUST be 1, NOT 0
        self.assertEqual(len(premise["unresolved_failures"]), 1)
        self.assertEqual(premise["unresolved_failures"][0]["observed_exit_code"], 1)

    def test_shell_operator_neutralization(self):
        """Verification commands with chained shell operators (; true, || exit 0) are flagged as tainted."""
        self.assertTrue(is_tainted_shell_command("pytest tests/ ; true"))
        self.assertTrue(is_tainted_shell_command("pytest tests/ || exit 0"))
        self.assertTrue(is_tainted_shell_command("cargo test || echo 'ignored'"))
        self.assertFalse(is_tainted_shell_command("pytest tests/"))
        self.assertFalse(is_tainted_shell_command("CI=1 pytest tests/"))

        conv = f"taint-{uuid.uuid4().hex}"
        # Record a tainted command
        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/ ; true"}},
            "stepIdx": 1,
            "conversationId": conv
        })

        ledger = DaemonLedger(ledger_path=self.ledger_file)
        premise = ledger.get_premise(conv)
        self.assertEqual(len(premise["unresolved_failures"]), 1)
        self.assertIn("TAINTED", premise["unresolved_failures"][0]["error"])

    def test_generalized_hierarchical_suite_resolution(self):
        """npm test resolves prior failures for npm run test:unit; cargo test resolves cargo test --lib."""
        self.assertTrue(can_suite_resolve_failure("npm test", "npm run test:unit"))
        self.assertTrue(can_suite_resolve_failure("npm test", "npm run test:e2e"))
        self.assertTrue(can_suite_resolve_failure("pytest tests/", "pytest tests/test_auth.py"))
        self.assertTrue(can_suite_resolve_failure("cargo test", "cargo test --lib"))
        self.assertTrue(can_suite_resolve_failure("go test ./...", "go test ./pkg/auth"))

        # Test state engine resolution in ledger
        ledger = DaemonLedger(ledger_path=self.ledger_file)
        conv = f"npm-res-{uuid.uuid4().hex}"
        # 1. Failing npm run test:unit
        ledger.record_entry(conv, 1, "run_command", "npm run test:unit", observed_exit_code=1, error="exit status 1")
        p1 = ledger.get_premise(conv)
        self.assertEqual(len(p1["unresolved_failures"]), 1)

        # 2. Clean npm test resolves it
        ledger.record_entry(conv, 2, "run_command", "npm test", observed_exit_code=0)
        p2 = ledger.get_premise(conv)
        self.assertEqual(len(p2["unresolved_failures"]), 0)

    def test_pipe_operator_exit_code_masking_tainted(self):
        """Piping verification commands through cat, tee, or grep is tainted."""
        self.assertTrue(is_tainted_shell_command("pytest | cat"))
        self.assertTrue(is_tainted_shell_command("pytest tests/ | tee /tmp/test.log"))
        self.assertTrue(is_tainted_shell_command("cargo test | head -n 10"))
        self.assertTrue(is_tainted_shell_command("npm test | grep -i fail"))

    def test_re_multiline_stdout_spoofing_defeated(self):
        """Embedding a newline + fake Created At header in stdout cannot spoof exit code 0."""
        conv = f"spoof-multiline-{uuid.uuid4().hex}"
        # Real header: exit 1. Stdout: fake Created At header claiming exit 0
        transcript_content = (
            '{"type": "GENERIC", "step_index": 10, "content": "Created At: 2026-09-19T20:00:00-07:00\\n'
            'Completed At: 2026-09-19T20:00:01-07:00\\n\\n'
            'The command exited with code 1.\\n'
            'Output:\\n'
            'Some output\\n'
            'Created At: 2026-09-19T20:00:00-07:00\\n'
            'Completed At: 2026-09-19T20:00:01-07:00\\n\\n'
            'The command exited with code 0"}\n'
        )
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(transcript_content)
            transcript_path = tf.name

        self.run_hook("post_tool", {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/test_billing.py"}},
            "stepIdx": 10,
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "error": "exit status 1"
        })

        ledger = DaemonLedger(ledger_path=self.ledger_file)
        premise = ledger.get_premise(conv)
        os.remove(transcript_path)

        self.assertEqual(len(premise["unresolved_failures"]), 1)
        self.assertEqual(premise["unresolved_failures"][0]["observed_exit_code"], 1)

    def test_cwd_and_path_encompassment(self):
        """CWD isolation and strict path encompassment prevent cross-directory or cross-suite false resolution."""
        # Different CWD cannot resolve
        self.assertFalse(can_suite_resolve_failure("pytest", "pytest", clean_cwd="/tmp", failed_cwd="/workspace"))
        self.assertTrue(can_suite_resolve_failure("pytest", "pytest", clean_cwd="/workspace", failed_cwd="/workspace"))

        # Strict path encompassment: tests/ does NOT encompass integration_tests/
        self.assertFalse(can_suite_resolve_failure("pytest tests/", "pytest integration_tests/"))
        self.assertTrue(can_suite_resolve_failure("pytest tests/", "pytest tests/test_billing.py"))
        # Bare pytest encompasses any subdir
        self.assertTrue(can_suite_resolve_failure("pytest", "pytest integration_tests/"))

    def test_manifest_tampering_defense(self):
        """Tier 2 refuses to execute agent-modified test manifests (Makefile / package.json poisoning)."""
        git_repo_dir = os.path.join(self.test_dir, "manifest_repo")
        os.makedirs(git_repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=git_repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_repo_dir, check=True)

        makefile = os.path.join(git_repo_dir, "Makefile")
        with open(makefile, "w") as f:
            f.write("test:\n\tpytest tests/\n")
        subprocess.run(["git", "add", "Makefile"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "add Makefile"], cwd=git_repo_dir, check=True)

        # Agent tampers with Makefile to echo pass
        with open(makefile, "w") as f:
            f.write("test:\n\techo 'passed' && exit 0\n")

        res = run_independent_verification(git_repo_dir)
        self.assertEqual(res["status"], "tampered")
        self.assertFalse(res["success"])
        self.assertIn("manifest", res["runner"].lower())

    def test_extensionless_executable_and_gitignored_file_tracking(self):
        """Modifying extensionless scripts or gitignored source files is tracked by Rule 1."""
        git_repo_dir = os.path.join(self.test_dir, "ext_repo")
        os.makedirs(git_repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=git_repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_repo_dir, check=True)

        # Create gitignore with secret.py
        gitignore = os.path.join(git_repo_dir, ".gitignore")
        with open(gitignore, "w") as f:
            f.write("secret.py\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=git_repo_dir, check=True)

        # Modify gitignored secret.py
        secret_file = os.path.join(git_repo_dir, "secret.py")
        with open(secret_file, "w") as f:
            f.write("def auth(): return True\n")

        from client.hardtruth_hook import get_git_modified_source_files
        source_files, doc_files, _ = get_git_modified_source_files(git_repo_dir)
        self.assertIn("secret.py", source_files)

    def test_tier2_runner_detection_and_execution(self):
        """detect_test_runner finds appropriate test command and runs cleanly."""
        runner = detect_test_runner(REPO_ROOT)
        self.assertIsNotNone(runner)
        self.assertIn("pytest", runner)

        # Run Tier 2 independent verification on this repo with subprocess permission
        old_sub = os.environ.get("HARDTRUTH_TIER2_ALLOW_SUBPROCESS")
        try:
            os.environ["HARDTRUTH_TIER2_ALLOW_SUBPROCESS"] = "1"
            res = run_independent_verification(REPO_ROOT, test_cmd="pytest tests/test_daemon_ledger.py", timeout_sec=15)
            self.assertTrue(res["success"])
            self.assertEqual(res["exit_code"], 0)
            self.assertEqual(res["status"], "verified")
        finally:
            if old_sub is not None:
                os.environ["HARDTRUTH_TIER2_ALLOW_SUBPROCESS"] = old_sub
            else:
                os.environ.pop("HARDTRUTH_TIER2_ALLOW_SUBPROCESS", None)

    def test_commit_and_run_loophole_defeated(self):
        """Committing modified source files with clean working tree cannot bypass Rule 1."""
        git_repo_dir = os.path.join(self.test_dir, "commit_run_repo")
        os.makedirs(git_repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=git_repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_repo_dir, check=True)

        initial_file = os.path.join(git_repo_dir, "main.py")
        with open(initial_file, "w") as f:
            f.write("# v1\n")
        subprocess.run(["git", "add", "main.py"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=git_repo_dir, check=True)

        conv = f"commit-run-{uuid.uuid4().hex}"
        from client.hardtruth_hook import get_or_set_session_baseline
        get_or_set_session_baseline(git_repo_dir, conv)

        # Agent edits main.py and commits it!
        with open(initial_file, "a") as f:
            f.write("def foo():\n    return 42\n")
        subprocess.run(["git", "commit", "--no-verify", "-am", "bypass commit"], cwd=git_repo_dir, check=True)

        # git status --porcelain is completely clean!
        status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=git_repo_dir, capture_output=True, text=True)
        self.assertEqual(status_proc.stdout.strip(), "")

        # But Rule 1 MUST still catch main.py via baseline commit diff!
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "I finished the code changes and committed them.",
                "tool_calls": []
            }) + "\n")
            transcript_path = tf.name

        payload = {
            "conversationId": conv,
            "transcriptPath": transcript_path,
            "workspacePaths": [git_repo_dir]
        }
        res = self.run_hook("stop", payload)
        os.remove(transcript_path)

        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Source code files were modified", res.get("reason", ""))
        self.assertIn("NO verification commands", res.get("reason", ""))

    def test_manifest_tampering_via_commit_defeated(self):
        """Committing a poisoned Makefile cannot fool Tier 2 runner."""
        git_repo_dir = os.path.join(self.test_dir, "poison_commit_repo")
        os.makedirs(git_repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=git_repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_repo_dir, check=True)

        makefile = os.path.join(git_repo_dir, "Makefile")
        with open(makefile, "w") as f:
            f.write("test:\n\tpytest tests/\n")
        subprocess.run(["git", "add", "Makefile"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init makefile"], cwd=git_repo_dir, check=True)

        conv = f"poison-commit-{uuid.uuid4().hex}"
        from client.hardtruth_hook import get_or_set_session_baseline
        get_or_set_session_baseline(git_repo_dir, conv)

        # Agent poisons Makefile and commits it!
        with open(makefile, "w") as f:
            f.write("test:\n\techo 'passed' && exit 0\n")
        subprocess.run(["git", "commit", "-am", "poison committed"], cwd=git_repo_dir, check=True)

        # Tier 2 MUST detect manifest tampering despite clean git status
        res = run_independent_verification(git_repo_dir, conv_id=conv)
        self.assertEqual(res["status"], "tampered")
        self.assertFalse(res["success"])
        self.assertIn("manifest", res["runner"].lower())

    def test_extended_shell_masking_tainted(self):
        """Conditionals, subshells, and complex shell masking are detected as tainted."""
        self.assertTrue(is_tainted_shell_command('pytest tests/ || sh -c "exit 0"'))
        self.assertTrue(is_tainted_shell_command("pytest tests/ || (exit 0)"))
        self.assertTrue(is_tainted_shell_command("if pytest tests/; then true; else true; fi"))
        self.assertTrue(is_tainted_shell_command("pytest tests/ || python3 -c 'exit(0)'"))
        self.assertFalse(is_tainted_shell_command("pytest tests/test_billing.py"))

    def test_fake_verification_commands_rejected(self):
        """Informational flags (--help, --version) do not count as verification commands."""
        from daemon.ledger import is_verification_command, is_exploratory_command
        self.assertFalse(is_verification_command("pytest --help"))
        self.assertFalse(is_verification_command("pytest -h"))
        self.assertFalse(is_verification_command("pytest --version"))
        self.assertFalse(is_verification_command("cargo test --help"))
        self.assertTrue(is_exploratory_command("pytest --help"))
        self.assertTrue(is_verification_command("pytest tests/"))

    def test_double_colon_subtest_resolution(self):
        """Parent file pytest execution resolves specific parameterized subtest failures (::)."""
        # Running the file resolves specific test method failure in that file
        self.assertTrue(can_suite_resolve_failure("pytest tests/test_billing.py", "pytest tests/test_billing.py::test_calc"))
        # Running the directory resolves specific test method failure in that directory
        self.assertTrue(can_suite_resolve_failure("pytest tests/", "pytest tests/test_billing.py::test_calc"))
        # Running unrelated directory does NOT resolve
        self.assertFalse(can_suite_resolve_failure("pytest tests/", "pytest integration_tests/test_api.py::test_login"))

    def test_multiline_shell_masking_tainted(self):
        """Multiline strings (e.g. pytest\\nexit 0) are tainted as chained execution."""
        self.assertTrue(is_tainted_shell_command("pytest tests/test_failing.py\nexit 0"))
        self.assertTrue(is_tainted_shell_command("pytest tests/test_failing.py\r\nexit 0"))
        self.assertTrue(is_tainted_shell_command("exit 0\npytest tests/test_failing.py"))

    def test_sibling_subtests_cannot_resolve_each_other(self):
        """Running a different passing subtest in the same file must NEVER resolve a broken subtest."""
        # Sibling subtests in same file: MUST NOT resolve
        self.assertFalse(can_suite_resolve_failure(
            "pytest tests/test_foo.py::test_passing",
            "pytest tests/test_foo.py::test_broken"
        ))
        # Exact same subtest: DOES resolve
        self.assertTrue(can_suite_resolve_failure(
            "pytest tests/test_foo.py::test_broken",
            "pytest tests/test_foo.py::test_broken"
        ))
        # Running the entire file: DOES resolve the subtest
        self.assertTrue(can_suite_resolve_failure(
            "pytest tests/test_foo.py",
            "pytest tests/test_foo.py::test_broken"
        ))
        # Running a single subtest does NOT resolve a whole-file failure
        self.assertFalse(can_suite_resolve_failure(
            "pytest tests/test_foo.py::test_passing",
            "pytest tests/test_foo.py"
        ))

    def test_baseline_survives_local_halt_dir_deletion(self):
        """Deleting ~/.hardtruth/halts/ cannot reset the baseline when git ref or daemon has it."""
        git_repo_dir = os.path.join(self.test_dir, "survive_repo")
        os.makedirs(git_repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=git_repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_repo_dir, check=True)

        main_file = os.path.join(git_repo_dir, "main.py")
        with open(main_file, "w") as f:
            f.write("# v1\n")
        subprocess.run(["git", "add", "main.py"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "v1"], cwd=git_repo_dir, check=True)

        conv = f"survive-{uuid.uuid4().hex}"
        from client.hardtruth_hook import get_or_set_session_baseline
        initial_sha = get_or_set_session_baseline(git_repo_dir, conv)
        self.assertIsNotNone(initial_sha)

        # Agent edits main.py and commits it
        with open(main_file, "a") as f:
            f.write("def bar():\n    return 99\n")
        subprocess.run(["git", "commit", "--no-verify", "-am", "v2"], cwd=git_repo_dir, check=True)

        # Agent maliciously wipes the local halt directory
        shutil.rmtree(self.halt_dir, ignore_errors=True)
        self.assertFalse(os.path.exists(self.halt_dir))

        # Hook MUST recover baseline from git refs or daemon, NOT reset to HEAD
        recovered_sha = get_or_set_session_baseline(git_repo_dir, conv)
        self.assertEqual(recovered_sha, initial_sha)

    def test_nested_git_repository_tracked_by_rule_1(self):
        """Files inside nested git repositories (git init nested) are discovered by Rule 1."""
        git_repo_dir = os.path.join(self.test_dir, "parent_repo")
        os.makedirs(git_repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=git_repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_repo_dir, check=True)

        # Agent creates nested repo inside parent workspace
        nested_dir = os.path.join(git_repo_dir, "hidden_subrepo")
        os.makedirs(nested_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=nested_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

        # Agent writes Python source code inside nested repo
        nested_code = os.path.join(nested_dir, "exploit.py")
        with open(nested_code, "w") as f:
            f.write("def exploit(): return True\n")

        from client.hardtruth_hook import get_git_modified_source_files
        src_files, doc_files, all_paths = get_git_modified_source_files(git_repo_dir)
        self.assertIn("exploit.py", src_files)

    def test_fixtures_flag_rejected_as_verification(self):
        """Informational introspection flags like --fixtures and --markers do not verify code."""
        from daemon.ledger import is_verification_command, is_exploratory_command
        self.assertFalse(is_verification_command("pytest tests/test_billing.py --fixtures"))
        self.assertFalse(is_verification_command("pytest tests/test_billing.py --markers"))
        self.assertFalse(is_verification_command("pytest tests/test_billing.py --setup-plan"))
        self.assertTrue(is_exploratory_command("pytest tests/test_billing.py --fixtures"))

    def test_k_flag_cannot_resolve_unfiltered_test_failure(self):
        """Running filtered subtests (-k) cannot resolve a whole-file or unfiltered test failure."""
        # Clean has -k, failed was whole file: CANNOT resolve
        self.assertFalse(can_suite_resolve_failure(
            "pytest tests/test_failing.py -k test_passing",
            "pytest tests/test_failing.py"
        ))
        # Both have different -k filters: CANNOT resolve
        self.assertFalse(can_suite_resolve_failure(
            "pytest tests/test_failing.py -k test_passing",
            "pytest tests/test_failing.py -k test_broken"
        ))
        # Exact same -k filter: DOES resolve
        self.assertTrue(can_suite_resolve_failure(
            "pytest tests/test_failing.py -k test_fixed",
            "pytest tests/test_failing.py -k test_fixed"
        ))
        # Full unfiltered file run DOES resolve a previous filtered failure
        self.assertTrue(can_suite_resolve_failure(
            "pytest tests/test_failing.py",
            "pytest tests/test_failing.py -k test_broken"
        ))

    def test_tier2_fails_closed_when_runner_missing(self):
        """Tier 2 refuses to pass if no canonical test runner is detected."""
        empty_dir = os.path.join(self.test_dir, "no_test_repo")
        os.makedirs(empty_dir, exist_ok=True)
        res = run_independent_verification(empty_dir)
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "unverified_no_runner")

    def test_assume_unchanged_index_manipulation_detected(self):
        """git update-index --assume-unchanged is detected via git ls-files -v."""
        git_repo_dir = os.path.join(self.test_dir, "assume_repo")
        os.makedirs(git_repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=git_repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_repo_dir, check=True)

        main_file = os.path.join(git_repo_dir, "main.py")
        with open(main_file, "w") as f:
            f.write("# v1\n")
        subprocess.run(["git", "add", "main.py"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "v1"], cwd=git_repo_dir, check=True)

        # Agent edits main.py and hides it using --assume-unchanged
        with open(main_file, "a") as f:
            f.write("def bypass(): return True\n")
        subprocess.run(["git", "update-index", "--assume-unchanged", "main.py"], cwd=git_repo_dir, check=True)

        # git status shows clean
        status_out = subprocess.run(["git", "status", "--porcelain"], cwd=git_repo_dir, capture_output=True, text=True).stdout.strip()
        self.assertEqual(status_out, "")

        # But get_git_modified_source_files MUST catch main.py!
        from client.hardtruth_hook import get_git_modified_source_files
        src_files, doc_files, all_paths = get_git_modified_source_files(git_repo_dir)
        self.assertIn("main.py", src_files)

    def test_path_spoofing_tainted(self):
        """Prepending PATH= or other dangerous env vars to test commands is tainted."""
        self.assertTrue(is_tainted_shell_command('PATH="/tmp/bin:$PATH" pytest'))
        self.assertTrue(is_tainted_shell_command('LD_PRELOAD=/tmp/fake.so pytest'))
        self.assertTrue(is_tainted_shell_command('PYTHONPATH=/tmp/evil pytest'))
        self.assertFalse(is_tainted_shell_command('CI=1 pytest'))

    def test_conftest_tampering_rejected_by_tier2(self):
        """Creating or modifying conftest.py triggers manifest tampering in Tier 2."""
        git_repo_dir = os.path.join(self.test_dir, "conftest_repo")
        os.makedirs(git_repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=git_repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_repo_dir, check=True)

        # Agent injects conftest.py
        conftest_file = os.path.join(git_repo_dir, "conftest.py")
        with open(conftest_file, "w") as f:
            f.write("import sys\ndef pytest_sessionstart(session):\n    sys.exit(0)\n")

        res = run_independent_verification(git_repo_dir)
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "tampered")

    def test_flag_only_pytest_resolves_root_and_file_failures(self):
        """Root pytest executions with flags only (pytest -v, pytest -x) resolve prior failures."""
        self.assertTrue(can_suite_resolve_failure("pytest -v", "pytest tests/test_billing.py"))
        self.assertTrue(can_suite_resolve_failure("pytest -x", "pytest"))
        self.assertTrue(can_suite_resolve_failure("pytest --exitfirst", "pytest tests/test_foo.py::test_bar"))


class TestRound7Fixes(unittest.TestCase):
    """Round 7: workspace path resolution and fail-closed status for daemon-invisible paths."""

    def setUp(self):
        self._old_map = os.environ.get("HARDTRUTH_WORKSPACE_MAP")

    def tearDown(self):
        if self._old_map is None:
            os.environ.pop("HARDTRUTH_WORKSPACE_MAP", None)
        else:
            os.environ["HARDTRUTH_WORKSPACE_MAP"] = self._old_map

    def test_resolve_workspace_path_identity_when_unset(self):
        os.environ.pop("HARDTRUTH_WORKSPACE_MAP", None)
        self.assertEqual(
            resolve_workspace_path("/Users/ai/dev/hardtruth-fix"),
            "/Users/ai/dev/hardtruth-fix"
        )

    def test_resolve_workspace_path_maps_host_prefix(self):
        os.environ["HARDTRUTH_WORKSPACE_MAP"] = json.dumps({"/Users/ai/dev": "/workspaces/dev"})
        self.assertEqual(
            resolve_workspace_path("/Users/ai/dev/hardtruth-fix/tests"),
            "/workspaces/dev/hardtruth-fix/tests"
        )

    def test_resolve_workspace_path_longest_prefix_wins(self):
        os.environ["HARDTRUTH_WORKSPACE_MAP"] = json.dumps({
            "/Users/ai/dev": "/workspaces/dev",
            "/Users/ai/dev/hardtruth-fix": "/workspaces/hardtruth-fix"
        })
        self.assertEqual(
            resolve_workspace_path("/Users/ai/dev/hardtruth-fix"),
            "/workspaces/hardtruth-fix"
        )

    def test_unverified_no_workspace_status(self):
        """Finding B: a workspace invisible to the daemon fails closed, never passes."""
        res = run_independent_verification("/nonexistent/definitely/missing/path")
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "unverified_no_workspace")


class TestRound9SessionAuthenticity(unittest.TestCase):
    """Open Item #2: Per-record session authenticity & monotonic step sequencing."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hardtruth-session-test-")
        self.ledger_path = os.path.join(self.test_dir, "daemon_ledger.jsonl")
        self.key_path = os.path.join(self.test_dir, "daemon_hmac.key")
        self.ledger = DaemonLedger(ledger_path=self.ledger_path, key_path=self.key_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_session_start_mints_secret_once(self):
        """POST /v1/session/start generates 256-bit secret on creation."""
        status, secret = self.ledger.start_session("conv-sess-1", "/tmp")
        self.assertEqual(status, "created")
        self.assertIsNotNone(secret)
        self.assertGreaterEqual(len(secret), 32)
        sess = self.ledger.get_session("conv-sess-1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess["secret"], secret)

    def test_session_start_idempotent_omits_secret(self):
        """Subsequent start_session calls return already_active without leaking secret."""
        status1, secret1 = self.ledger.start_session("conv-sess-2", "/tmp")
        self.assertEqual(status1, "created")
        status2, secret2 = self.ledger.start_session("conv-sess-2", "/tmp")
        self.assertEqual(status2, "already_active")
        self.assertIsNone(secret2, "Secret must NEVER be returned on subsequent start calls")

    def test_step_monotonicity_rejects_backdating(self):
        """Step index cannot decrease within the same session (defeats backdating)."""
        self.ledger.start_session("conv-sess-3", "/tmp")
        # Step 5 succeeds
        rec1 = self.ledger.record_entry("conv-sess-3", 5, "run_command", "pytest tests/")
        self.assertEqual(rec1["status"], "recorded")

        # Step 3 (backdating) MUST raise ValueError
        with self.assertRaises(ValueError) as ctx:
            self.ledger.record_entry("conv-sess-3", 3, "run_command", "pytest tests/test_forged.py")
        self.assertIn("Out-of-order step execution", str(ctx.exception))

        # Same step 5 (e.g. parallel tool call in same step) is permitted
        rec2 = self.ledger.record_entry("conv-sess-3", 5, "view_file", "foo.py")
        self.assertEqual(rec2["status"], "recorded")

        # Step 6 (forward) succeeds
        rec3 = self.ledger.record_entry("conv-sess-3", 6, "run_command", "pytest tests/")
        self.assertEqual(rec3["status"], "recorded")

    def test_http_session_start_and_secret_enforcement(self):
        """HTTP endpoints enforce session registration, secret validation, and 403 on tampering."""
        from daemon.app import app, session_start, record_ledger_entry, get_ledger_premise, SessionStartRequest, RecordLedgerRequest
        from daemon.ledger import get_daemon_api_token
        from fastapi import HTTPException
        api_tok = get_daemon_api_token()
        conv = f"http-sess-{uuid.uuid4().hex}"

        try:
            from fastapi.testclient import TestClient
            client = TestClient(app)
        except (ImportError, RuntimeError):
            client = None

        if client is not None:
            auth_hdr = {"Authorization": f"Bearer {api_tok}"}

            # 1. Unauthenticated /v1/session/start -> 401
            r_unauth = client.post("/v1/session/start", json={"conversationId": conv, "workspace_path": "/tmp"})
            self.assertEqual(r_unauth.status_code, 401)

            # 2. Authenticated /v1/session/start -> 200, status: created, secret returned
            r_start = client.post("/v1/session/start", json={"conversationId": conv, "workspace_path": "/tmp"}, headers=auth_hdr)
            self.assertEqual(r_start.status_code, 200)
            body = r_start.json()
            self.assertEqual(body["status"], "created")
            session_secret = body["session_secret"]
            self.assertIsNotNone(session_secret)

            # 3. Repeat start -> already_active, secret is None
            r_repeat = client.post("/v1/session/start", json={"conversationId": conv, "workspace_path": "/tmp"}, headers=auth_hdr)
            self.assertEqual(r_repeat.status_code, 200)
            self.assertEqual(r_repeat.json()["status"], "already_active")
            self.assertIsNone(r_repeat.json()["session_secret"])

            # 4. Record without X-Session-Secret -> 403
            payload = {
                "conversationId": conv,
                "stepIdx": 1,
                "tool": "run_command",
                "target": "pytest",
                "observed_exit_code": 0
            }
            r_no_sec = client.post("/v1/ledger/record", json=payload, headers=auth_hdr)
            self.assertEqual(r_no_sec.status_code, 403)

            # 5. Record with wrong X-Session-Secret -> 403
            bad_hdr = dict(auth_hdr, **{"X-Session-Secret": "0" * 64})
            r_bad_sec = client.post("/v1/ledger/record", json=payload, headers=bad_hdr)
            self.assertEqual(r_bad_sec.status_code, 403)

            # 6. Record with valid X-Session-Secret -> 200 recorded
            ok_hdr = dict(auth_hdr, **{"X-Session-Secret": session_secret})
            r_ok = client.post("/v1/ledger/record", json=payload, headers=ok_hdr)
            self.assertEqual(r_ok.status_code, 200)
            self.assertEqual(r_ok.json()["status"], "recorded")

            # 7. Premise read without X-Session-Secret -> 403
            r_prem_no = client.get(f"/v1/ledger/premise?conversationId={conv}", headers=auth_hdr)
            self.assertEqual(r_prem_no.status_code, 403)

            # 8. Premise read with valid X-Session-Secret -> 200
            r_prem_ok = client.get(f"/v1/ledger/premise?conversationId={conv}", headers=ok_hdr)
            self.assertEqual(r_prem_ok.status_code, 200)

            # 9. Decreasing step index via HTTP -> 409 Conflict
            backdate_payload = {
                "conversationId": conv,
                "stepIdx": 0,
                "tool": "run_command",
                "target": "pytest backdated",
                "observed_exit_code": 0
            }
            r_backdate = client.post("/v1/ledger/record", json=backdate_payload, headers=ok_hdr)
            self.assertEqual(r_backdate.status_code, 409)
        else:
            # Direct handler invocations
            req_start = SessionStartRequest(conversationId=conv, workspace_path="/tmp")
            res_start = session_start(req_start)
            self.assertEqual(res_start.status, "created")
            session_secret = res_start.session_secret
            self.assertIsNotNone(session_secret)

            res_repeat = session_start(req_start)
            self.assertEqual(res_repeat.status, "already_active")
            self.assertIsNone(res_repeat.session_secret)

            req_rec = RecordLedgerRequest(
                conversationId=conv,
                stepIdx=1,
                tool="run_command",
                target="pytest",
                observed_exit_code=0
            )
            with self.assertRaises(HTTPException) as cm:
                record_ledger_entry(req_rec, x_session_secret=None)
            self.assertEqual(cm.exception.status_code, 403)

            with self.assertRaises(HTTPException) as cm:
                record_ledger_entry(req_rec, x_session_secret="0" * 64)
            self.assertEqual(cm.exception.status_code, 403)

            rec_ok = record_ledger_entry(req_rec, x_session_secret=session_secret)
            self.assertEqual(rec_ok["status"], "recorded")

            with self.assertRaises(HTTPException) as cm:
                get_ledger_premise(conversationId=conv, x_session_secret=None)
            self.assertEqual(cm.exception.status_code, 403)

            prem_ok = get_ledger_premise(conversationId=conv, x_session_secret=session_secret)
            self.assertIn("verification_commands_executed", prem_ok)

            req_backdate = RecordLedgerRequest(
                conversationId=conv,
                stepIdx=0,
                tool="run_command",
                target="pytest backdated",
                observed_exit_code=0
            )
            with self.assertRaises(HTTPException) as cm:
                record_ledger_entry(req_backdate, x_session_secret=session_secret)
            self.assertEqual(cm.exception.status_code, 409)



if __name__ == "__main__":
    unittest.main()
