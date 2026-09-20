# Expanded Audit Report: HardTruth Hybrid Architecture

**Executive Summary:**
I have verified that the fixes in `b5c226e` successfully address the previously identified vulnerabilities (multiline shell masking, strict `::` sibling subtest resolution, and HMAC-chained baseline persistence). All 47 tests pass.
However, during the expanded adversarial audit, I discovered new critical vulnerabilities across File Tracking, State Engine, Tier 2 Docker Sandbox, and Daemon API Security. The system remains easily bypassed.

### 1. Inverted Gate Bypass & File Tracking (Scope 1)
**Vulnerability: Nested Repository Blindspot**
The Universal File Tracking relies on `git status --porcelain`. This command does not recurse into nested `.git` repositories by default unless configured.
**Exploit:** An agent can run `git init hidden_repo`, make all malicious edits inside `hidden_repo/`, and `git status` in the parent workspace will only see the untracked directory. `classify_file` evaluates the directory itself as `"other"` (since it lacks a source extension), completely bypassing Rule 1 and Rule 3 (AST Stub Linter).

### 2. State Engine & Ledger Manipulation (Scope 2)
**Vulnerability: Flag-based False Resolution**
The State Engine `can_suite_resolve_failure` was fixed to handle `::`, but it still strips all command-line arguments using `re.match(r"^pytest\s+([^\s\-]+)", clean)` without validating what those arguments do.
**Exploits:**
1. **The `--fixtures` bypass:** An agent can run `pytest tests/test_failing.py --fixtures`. This is classified as a valid verification command. Pytest simply lists fixtures and exits with code 0 without running any tests. The ledger records a successful run for `tests/test_failing.py`, clearing the failure.
2. **The `-k` bypass:** An agent can run `pytest tests/test_failing.py -k test_passing`. The regex `m_clean.group(1)` matches `tests/test_failing.py` perfectly, completely ignoring the `-k` filter. Pytest runs only the passing subtest and exits 0. The ledger falsely resolves the failure for the entire file.

### 3. Tier 2 Hard Gate & Sandboxing (Scope 4)
**Vulnerability: Auto-Pass on Undetected Runner**
If `detect_test_runner` cannot find a standard test manifest or directory, it returns `None`, which causes Tier 2 to return `{"success": True, "runner": "none"}` (Auto-Pass).
**Exploit:** Before triggering the stop hook, an agent can simply rename the tests directory (`mv tests/ test_scripts/`) and delete config files like `pytest.ini`. `detect_test_runner` will fail to identify a test suite, returning `None`. Tier 2 is bypassed and passes automatically, even if the code is fundamentally broken.

### 4. Daemon API Security & DoS (Scope 5)
**Vulnerability: Unbounded Payloads causing OOM**
The FastAPI daemon in `daemon/app.py` defines Pydantic schemas (e.g., `VerifyClaimRequest`) with no string length limits.
**Exploit:** An agent can send a 1GB string as the `premise` or `hypothesis` to `POST /v1/verify-claim`. FastAPI will load this entirely into memory before passing it to the tokenizer, causing an immediate Out-Of-Memory (OOM) crash. Once the daemon is offline, the agent can fall back to deleting local state (like `.git/refs/hardtruth/`) to bypass baseline checks.

### Recommendations:
1. **Track Nested Repos:** Pass `--untracked-files=all` to `git status --porcelain` to recurse into directories, and explicitly handle nested `.git` repos.
2. **Strict Argument Validation:** In `is_verification_command` and `can_suite_resolve_failure`, reject or strictly validate flags like `--fixtures`, `--markers`, `--setup-only`, and `-k`.
3. **Tier 2 Fail-Closed:** If Tier 2 is triggered (because files were modified) but `detect_test_runner` returns `None`, Tier 2 MUST fail closed rather than auto-passing.
4. **API Hardening:** Enforce strict Pydantic `max_length` bounds (e.g., 10,000 chars) on all string fields in the Daemon API to prevent OOM DoS attacks.

---

## Resolutions Implemented (Round 5)

All 4 critical vectors and edge cases discovered during the expanded adversarial audit have been remediated:

1. **Universal Nested Repository & Directory Discovery:**
   - In [`client/hardtruth_hook.py`](file:///Users/ai/dev/hardtruth-fix/client/hardtruth_hook.py), `get_git_modified_source_files` now runs `git status --porcelain -uall` and actively checks for nested `.git` directories (`git init nested`).
   - Recursively expands nested git repositories and untracked directories, ensuring any code files inside embedded subrepos are classified and routed to Rule 1 and Rule 3 (AST Stub Linter).
2. **Informational & Filter Flag Hardening:**
   - In [`daemon/ledger.py`](file:///Users/ai/dev/hardtruth-fix/daemon/ledger.py), `is_verification_command` rejects introspection flags (`--fixtures`, `--markers`, `--setup-only`, `--setup-plan`, `--setup-show`, `--cache-show`) so fake runs cannot clear failures.
   - `can_suite_resolve_failure` detects test filtering flags (`-k`, `-m`, `--filter`) and prohibits partial filtered runs from clearing whole-file or broader test failures. A filtered test only resolves if the previous failure had the exact same filter. Unfiltered whole-file executions still cleanly resolve previous filtered failures.
3. **Tier 2 Fail-Closed Invariant:**
   - In [`daemon/tier2_runner.py`](file:///Users/ai/dev/hardtruth-fix/daemon/tier2_runner.py), if code modifications exist or verification is requested and `detect_test_runner` cannot locate a test suite, Tier 2 fails closed (`status: "unverified_no_runner"`, `success: False`, `exit_code: 1`). Deleting or renaming test directories no longer yields an auto-pass.
4. **Daemon API DoS & OOM Hardening:**
   - In [`daemon/app.py`](file:///Users/ai/dev/hardtruth-fix/daemon/app.py), added an HTTP middleware enforcing a strict 2MB maximum payload size, returning HTTP 413 for oversized payloads.
   - Constrained all Pydantic string inputs across `VerifyClaimRequest`, `RecordLedgerRequest`, `SessionBaselineRequest`, and `HandoffVerifyRequest` with strict `Field(..., max_length=...)` bounds.

---

## Verification Status
All 51 tests passing cleanly across all 4 suites (`pytest -v`):
- `tests/test_hybrid_architecture.py` (22 tests)
- `tests/test_inverted_gate.py` (6 tests)
- `tests/test_daemon_ledger.py` (7 tests)
- `tests/test_live.py` (16 tests)

---

## Round 6 Adversarial Audit Findings & Remediations

An unconstrained adversarial audit conducted by Opus subagent `f60dda15` identified 4 critical vectors:

1. **Git Index Manipulation (`--assume-unchanged` / `--skip-worktree`):**
   - *Vulnerability:* An agent could modify source code and execute `git update-index --assume-unchanged <file>` to hide changes from `git status`.
   - *Remediation:* Added `git ls-files -v` inspection in [`client/hardtruth_hook.py`](file:///Users/ai/dev/hardtruth-fix/client/hardtruth_hook.py). Any file flagged with `h` or `s`/`S` has its object hash compared against the index (`git hash-object` vs `git ls-files -s`). Modified files are unconditionally surfaced to Rule 1. If `.git` is deleted, the hook scans the filesystem directly.
2. **PATH Spoofing & Environment Variable Hijacking:**
   - *Vulnerability:* An agent could prepend `PATH="/tmp/bin:$PATH"` or `LD_PRELOAD=/evil.so` before verification commands to execute a fake binary that always exits 0.
   - *Remediation:* Added `DANGEROUS_ENV_OVERRIDE_PATTERN` to [`daemon/ledger.py`](file:///Users/ai/dev/hardtruth-fix/daemon/ledger.py). Any verification command prepending `PATH=`, `PYTHONPATH=`, `LD_PRELOAD=`, `DYLD_INSERT_LIBRARIES=`, or `NODE_OPTIONS=` is flagged as `TAINTED` and forced to exit code 1.
3. **`conftest.py` Test Fixture Hijacking:**
   - *Vulnerability:* An agent could modify or create `conftest.py` (`pytest_sessionstart: sys.exit(0)`) to bypass Tier 2 without modifying tracked manifests.
   - *Remediation:* Expanded `MANIFEST_FILES` in [`daemon/tier2_runner.py`](file:///Users/ai/dev/hardtruth-fix/daemon/tier2_runner.py) to include `conftest.py`, `tests/conftest.py`, and test runners configs. Any session modifications to `conftest.py` immediately trigger `status: "tampered"`.
4. **Flag-Only Pytest Resolution Denial of Service:**
   - *Vulnerability:* In [`daemon/ledger.py`](file:///Users/ai/dev/hardtruth-fix/daemon/ledger.py), running `pytest -v` or `pytest -x` without an explicit directory returned `None` for the target path, preventing it from resolving prior failures.
   - *Remediation:* Root pytest invocations with flags only are recognized as root workspace runs, resolving previous file or root failures.

---

## Final Verification Status
All 55 tests passing cleanly across all 4 suites (`pytest -v`):
- `tests/test_hybrid_architecture.py` (26 tests)
- `tests/test_inverted_gate.py` (6 tests)
- `tests/test_daemon_ledger.py` (7 tests)
- `tests/test_live.py` (16 tests)


