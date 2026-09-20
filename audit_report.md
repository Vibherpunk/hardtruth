# HardTruth Security & Architectural Audit Report

**Target:** `feat/ledger-integrity` (Commit: `0083e6b`)
**Date:** 2026-09-19

## Executive Summary
While the shift to a daemon-owned ledger and an inverted language-independent gate significantly improves robustness against LLM hallucinations, the current implementation contains critical bypasses in file modification tracking, command classification, and shell execution boundaries. The system accurately documents its OS-level limitations but fails to address how trivially the agent's available tools (`run_command`) can subvert the ledger's tracking mechanisms.

---

## 1. Bypass Analysis (Rules 1 & 2)

### 1.1 Rule 1 Complete Bypass via CLI Text Manipulation
Rule 1 relies entirely on intercepting `write_to_file` and `replace_file_content` to increment `source_files_modified`. 
- **The Exploit:** If an agent modifies source code using `run_command` (e.g., `sed -i 's/foo/bar/' app.py`, `echo "def foo(): pass" >> main.py`, or `patch`), the hook completely ignores the file modification. `source_files_modified` remains `0`, and the agent can cleanly exit without running any tests, completely bypassing Rule 1.

### 1.2 Rule 1 Bypass via Extension Hardcoding
`classify_file` relies on a strict set of `SOURCE_CODE_EXTENSIONS`.
- **The Exploit:** Executable scripts without extensions (e.g., `build`, `deploy`), `Makefile`, `Dockerfile`, or newer extensions (e.g., `.mjs`, `.cjs`) fall through to `"other"`. Modifying them via legitimate tool calls will not trigger Rule 1.

### 1.3 Rule 2 Bypass via Shell Operators (Multi-command & Backgrounding)
`is_verification_command` relies on the regex `VERIFICATION_CMD_PATTERN`.
- **The Exploit:** An agent can bypass unresolved test failures by running a command like `pytest tests/ ; true` or `pytest tests/ || exit 0`. The regex matches `pytest` as the `base_cmd`, classifying it as a verification command, but the shell returns exit code `0` (due to `true` or `exit 0`). This falsely signals a clean run to the ledger, wiping the previous failures from `unresolved_failures` even if the tests actually failed.
- **Prefix Evasion:** Conversely, running `cd tests && pytest` or `CI=1 pytest` fails the `^` start-of-string anchor in the regex. The test run is entirely ignored by the state engine.

---

## 2. Race Conditions & Polling (`poll_transcript_for_step`)

### 2.1 Regex Spoofing in Transcript Parsing
The polling mechanism parses the transcript for the exit code using:
`re.search(r"The command exited with code (\d+)", content)`
- **The Vulnerability:** `content` contains both the system prefix and the raw stdout. If an agent intentionally or accidentally echoes `"The command exited with code 0"`, the regex may capture the spoofed string instead of the actual harness exit code, artificially clearing failures.

### 2.2 Dropped Exit Codes on High I/O
If the disk buffer flush exceeds the `max_wait_ms` (300ms) polling window, the hook falls back to inspecting `payload.get("error")`. 
- **The Consequence:** For successful commands, `error` is `None`, so the hook assigns `observed_exit_code = None` and `harness_status = "no_error"`. Due to the non-synthesis rule, this prevents resolving previous failures because the explicit `0` is missing. A legitimate, clean test run on a slow machine will fail to resolve the ledger, causing a false positive halt.

---

## 3. State Engine Flaws (`daemon/ledger.py`)

### 3.1 Suite Resolution Blindspots
The failure resolution logic in `get_premise` uses strict string matching to allow a broad test suite to clear specific test failures:
```python
target == f"{base_cmd} tests/" or target == f"{base_cmd} tests"
```
- **The Flaw:** This logic is hardcoded for Python/Rust paradigms (`pytest tests/`). It completely breaks for JS/TS, Go, and C. If an agent fails `npm run test:unit`, and then successfully runs `npm test`, `target` is `"npm test"`. This does not match `"npm tests/"`, so the original failure is **never resolved**. The agent becomes deadlocked until it runs the exact failing string again.

---

## 4. Threat Model Accuracy

### 4.1 Missing Acknowledgement of Environment Variable Overrides
The README admirably avoids security theater by stating the "Same-User OS Boundary" limitation. However, it omits that an agent with shell access doesn't even need to crack the HMAC chain to disable the system.
- **The Reality:** An agent can simply `export HARDTRUTH_LEDGER_PATH=/dev/null` or overwrite `client/hardtruth_hook.py` directly. While the container boundary is mentioned, the threat model should explicitly note that the client-side hook is entirely defenseless against a local agent tampering with its execution environment.

---

## 5. Code Quality & Regressions

### 5.1 Test Suite Blindspots
The tests in `test_inverted_gate.py` and `test_daemon_ledger.py` only cover the happy paths of the hardcoded heuristics:
- They assert `cargo test` and `pytest tests/` work correctly, masking the suite resolution blindspot for `npm test`.
- They assert `grep -q` doesn't deadlock, but fail to test `sed` or `echo` file modifications.
- The tests prove the invariants hold *under the assumed tool constraints*, but fail to test adversarial inputs or shell operators.

---

## Recommendations

1. **Track File Modifications Universally:** Update the `run_command` hook handler to parse file paths if commands like `sed`, `awk`, or redirection (`>`, `>>`) are used, or better yet, rely on a pre/post-run `git status` check instead of tool interception to determine if the working tree has changed.
2. **Robust Exit Code Extraction:** Do not regex the raw `content` field which contains agent stdout. The harness should ideally pass the explicit integer `exitCode` in the `PostToolUse` payload. If polling is required, ensure the regex strictly anchors to the system prefix, out of reach of stdout spoofing.
3. **Generalize Test Resolution:** Instead of `base_cmd tests/`, the resolution engine should allow any passing verification command with the same `base_cmd` to clear prior failures, or maintain a graph of test targets.
4. **Fix Regex Anchoring:** Remove the `^` anchor in `VERIFICATION_CMD_PATTERN` to allow `cd dir && cmd` or env var prefixes (`CI=1 pytest`). Ensure shell operators (`;`, `&&`, `||`) are stripped or parsed correctly to prevent exit code spoofing.

---

## 6. Independent Adversarial Audit (Round 2 — Commit `b6d69a1`)

An independent adversarial audit conducted by Opus subagent `f7b3bca5` on commit `b6d69a1` identified 5 critical vulnerabilities and edge cases in the initial hybrid implementation:

1. **`re.MULTILINE` Regex Spoofing (CRITICAL):**
   - *Vulnerability:* The transcript polling regex was compiled with `re.MULTILINE`, allowing `^` to match the start of *any* line in `stdout`. An agent could echo `\nCreated At: ... \nThe command exited with code 0` in its command output to forge an exit code of 0.
   - *Mitigation:* Replaced `re.MULTILINE` with `\A` (strict string start anchor) matching the exact beginning of the harness log content at index 0. Removed all loose regex fallbacks.
2. **Pipe Operator Masking (CRITICAL):**
   - *Vulnerability:* `SHELL_OPERATOR_MASK_PATTERN` caught `; true` and `|| exit 0` but omitted POSIX pipe operators (`|`). An agent could run `pytest | cat` to mask non-zero exit codes with exit 0.
   - *Mitigation:* Expanded `SHELL_OPERATOR_MASK_PATTERN` to check for pipes (`| cat`, `| tee`, `| head`, `| grep`) and flag any piped verification command as `TAINTED`.
3. **Agent-Controlled Manifest Poisoning (CRITICAL):**
   - *Vulnerability:* Tier 2 relied on reading `Makefile` and `package.json` directly from the workspace. An agent could rewrite `Makefile` with `test: exit 0` or edit `package.json` to spoof test suites.
   - *Mitigation:* Added `check_manifest_tampering` to inspect `git status --porcelain` on build manifests. If `Makefile` or `package.json` was modified during the task, Tier 2 refuses execution with `status: "tampered"`.
4. **Docker Read-Only Mount Cache Crashes (CRITICAL):**
   - *Vulnerability:* Standard runners (`pytest`, `npm`, `cargo`) write cache directories (`.pytest_cache`, `.cache`, `target/`), systematically failing with `Read-only file system` errors under `-v workspace:ro`.
   - *Mitigation:* Added `--tmpfs /tmp` and `--tmpfs /root/.cache`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPYCACHEPREFIX=/tmp/pycache`, `CARGO_TARGET_DIR=/tmp/target`, and `-o cache_dir=/tmp/.pytest_cache` for pytest.
5. **CWD-Blind Test Resolution & Overreach (HIGH):**
   - *Vulnerability:* Commands were tracked without recording working directory, allowing an agent in an empty directory to run `pytest` (exit 0) and resolve prior failures. `pytest tests/` was also incorrectly matching `pytest integration_tests/`.
   - *Mitigation:* Embedded `cwd` in the HMAC-chained ledger entries. Enforced exact CWD matching and strict path encompassment (`tests/` resolves `tests/test_foo.py` but never `integration_tests/`).

---

## 7. Verification Status
All 39 tests passing cleanly in `tests/` across 4 test suites:
- `tests/test_hybrid_architecture.py` (10 tests)
- `tests/test_inverted_gate.py` (6 tests)
- `tests/test_daemon_ledger.py` (7 tests)
- `tests/test_live.py` (16 tests)

