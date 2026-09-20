# Audit Report: HardTruth Hybrid Architecture

**Executive Summary:**
The HardTruth architecture relies on regex patterns, hierarchical path string matching, and session-state baselines that can all be trivially bypassed. I have successfully identified exploits for Rule 1, Rule 2, the State Engine, and the Tier 2 Manifest check.

### 1. Shell Operator Defense Bypass (Rule 2 / Tier 1 Evasion)
**Vulnerability:** The `SHELL_OPERATOR_MASK_PATTERN` in `daemon/ledger.py` explicitly looks for horizontal chaining operators like `||`, `&&`, `;`, `|`, `if`, etc. It completely ignores newline characters (`\n`).
**Exploit:** An agent can run a failing test and mask the exit code by passing a multi-line string to `run_command`'s `CommandLine`:
```bash
pytest tests/test_failing.py
exit 0
```
Bash will execute the test, which fails, and then execute `exit 0`. The overall tool call returns exit code 0. `is_tainted_shell_command` returns `False` because there are no matching masking operators. The ledger records a successful test run, bypassing Rule 2.

### 2. State Engine & Suite Resolution Evasion (False Resolution)
**Vulnerability:** `can_suite_resolve_failure` strips test function selectors using `.split("::")[0]` before comparing paths.
**Exploit:** If an agent breaks `test_A` in `tests/test_foo.py::test_A` and receives a failure, it can clear that failure from `unresolved_failures` by simply running a completely different passing test in the same file:
`run_command(CommandLine="pytest tests/test_foo.py::test_passing")`
The resolution engine strips `::test_passing` and `::test_A`, compares `tests/test_foo.py` == `tests/test_foo.py`, evaluates to `True`, and clears the failure of the broken test.

### 3. Universal File Tracking & Manifest Defense Bypass (Baseline Reset)
**Vulnerability:** `get_git_modified_source_files` and `check_manifest_tampering` rely on `baseline_sha` stored in `~/.hardtruth/halts/baseline_<hash>.json`. `get_or_set_session_baseline` will recreate this file at `HEAD` if it doesn't exist. Furthermore, `run_command` operations (like `sed` or `echo`) do not trigger the `write_to_file` hooks that increment `ledger_src_count`.
**Exploit:** An agent can completely bypass Rule 1 (Code modification without tests) and Tier 2 (Manifest poisoning) using the following sequence:
1. Use `run_command` to modify source files and/or manifests (`Makefile`) via `sed` or `echo`. (This is not tracked by the `write_to_file` hook).
2. Commit the changes: `git commit -am "bypass"`.
3. Delete the session baseline: `rm -rf ~/.hardtruth/halts/`.
4. Trigger the stop hook. The hook recreates the baseline at the *new* `HEAD`. `git status` and `git diff` both return empty. `source_files_modified` evaluates to 0. Rule 1 is bypassed, and Tier 2 is skipped entirely (or runs the poisoned `Makefile` if it's invoked).

### 4. Polling Boundary (`\A` Anchoring & Timeout)
**Finding:** The `\A` string-start anchoring successfully prevents internal stdout spoofing because the system wraps the tool output, ensuring the true exit code is evaluated.
**Vulnerability (UX / Denial of Service):** If a test suite runs asynchronously and takes longer than 300ms, `poll_transcript_for_step` times out and records an `unverified_timeout` as a failure. While this fails closed (secure), it creates a state where valid, long-running asynchronous test suites get stuck as failures unless run synchronously (with high `WaitMsBeforeAsync`).

### 5. Test Suite Rigor Assessment
**Finding:** The 44 tests only prove the *happy path* of the invariants.
- `test_shell_operator_neutralization` tests `; true` and `|| exit 0` but fails to test multiline `\n exit 0`.
- `test_double_colon_subtest_resolution` tests parent file resolving child test, but fails to test sibling subtest resolving another sibling subtest.
- `test_commit_and_run_loophole_defeated` tests committing files, but fails to test an agent tampering with the mutable `.hardtruth/halts/` state directory.

### Recommendations
1. **Fix Shell Defense:** Update `SHELL_OPERATOR_MASK_PATTERN` to match newlines `\n` to prevent multiline execution.
2. **Fix Suite Resolution:** In `can_suite_resolve_failure`, if both the clean and failed commands contain `::`, they must match exactly.
3. **Protect the Baseline:** Store the baseline SHA in the daemon ledger itself, which is cryptographically hash-chained and cannot be silently deleted by the agent, rather than relying on a mutable JSON file in `~/.hardtruth/`.
4. **Harden File Tracking:** Do not rely solely on the `write_to_file` hook. Track file hashes pre- and post-tool execution for all shell commands, or force the agent to run in a filesystem sandbox that intercepts all write syscalls.

---

## Resolutions Implemented (Round 4)

All vulnerabilities and edge cases identified in this audit were patched and validated:

1. **Multiline Shell Masking Neutralized:**
   - Updated `SHELL_OPERATOR_MASK_PATTERN` in `daemon/ledger.py` to include `[\r\n]`.
   - Any verification command containing multiline strings (e.g., `pytest tests/test_failing.py\nexit 0`) is detected as tainted, forced to exit code 1, and flagged with `harness_status: "tainted_shell_operator"`.
2. **Sibling Subtest Evasion Eliminated:**
   - Modified `can_suite_resolve_failure` in `daemon/ledger.py` to check if `clean` contains `::`.
   - If `clean` targets a subtest, it strictly requires `raw_clean_target == raw_failed_target`. Running sibling subtests (`pytest tests/test_foo.py::test_pass`) no longer resolves failures in other subtests (`pytest tests/test_foo.py::test_broken`).
   - Running the entire file (`pytest tests/test_foo.py`) or directory (`pytest tests/`) still correctly resolves any subtests within them.
3. **Daemon-Owned Immutable Baseline Defense:**
   - Added `/v1/session/baseline` endpoints to `daemon/app.py` and `DaemonLedger` in `daemon/ledger.py`.
   - The session baseline is stored in the Docker container's memory and appended as a physical `__session_baseline__` entry in the HMAC-SHA256 hash-chained ledger.
   - Once set for `(conversationId, workspace)`, the daemon enforces strict immutability: any subsequent attempts to overwrite the baseline return the existing baseline.
   - `client/hardtruth_hook.py` and `daemon/tier2_runner.py` query the isolated daemon first, then `refs/hardtruth/baseline/<hash>`, before falling back to local files. Malicious `rm -rf ~/.hardtruth/halts/` fails completely.

---

## Verification Status
All 47 tests passing cleanly in `tests/` across 4 test suites:
- `tests/test_hybrid_architecture.py` (18 tests)
- `tests/test_inverted_gate.py` (6 tests)
- `tests/test_daemon_ledger.py` (7 tests)
- `tests/test_live.py` (16 tests)

