"""
HardTruth Daemon Ledger Engine
Cryptographically tamper-evident, HMAC-SHA256 hash-chained, daemon-owned execution ledger.
Maintains physical state, verification command tracking, hierarchical suite resolution, and shell-operator defense.
"""

from __future__ import annotations
import os
import re
import json
import time
import hmac
import hashlib
import secrets
from typing import Dict, List, Optional, Tuple, Any

DEFAULT_LEDGER_PATH = os.path.expanduser("~/.hardtruth/daemon_ledger.jsonl")
DEFAULT_KEY_PATH = os.path.expanduser("~/.hardtruth/daemon_hmac.key")

# ---------------------------------------------------------------------------
# Round 7 Finding A: unauthenticated daemon writes allowed any local process to
# forge ledger records (fake tool="run_command" / exit 0) and resolve genuine
# failures in the premise the hook's gate consults. All state-changing API
# endpoints now require a shared bearer token. The token is distributed out of
# band: HARDTRUTH_API_TOKEN env (hook + daemon) or the shared key file
# ~/.hardtruth/daemon_api.key (auto-generated if missing).
# ---------------------------------------------------------------------------
DEFAULT_API_KEY_PATH = os.path.expanduser("~/.hardtruth/daemon_api.key")


def get_daemon_api_token() -> str:
    """Returns the daemon API write token: HARDTRUTH_API_TOKEN env, else key file."""
    env_tok = os.environ.get("HARDTRUTH_API_TOKEN", "").strip()
    if env_tok:
        return env_tok
    path = os.environ.get("HARDTRUTH_API_KEY", DEFAULT_API_KEY_PATH)
    key_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(key_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(key_dir, 0o700)
    except Exception:
        pass
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                tok = f.read().strip()
                if len(tok) >= 32:
                    return tok
        except Exception:
            pass
    tok = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o400)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok)
    try:
        os.chmod(path, 0o400)
    except Exception:
        pass
    return tok


def validate_api_token(token: Optional[str]) -> bool:
    """Constant-time comparison against the daemon API write token."""
    if not token:
        return False
    expected = get_daemon_api_token()
    return hmac.compare_digest(str(token), expected)

# Matches verification commands even with leading env vars like CI=1 or PYTHONPATH=.
VERIFICATION_CMD_PATTERN = re.compile(
    r"(?:^|[\s;\|\&])(?:[A-Z_0-9]+=\S+\s+)*(pytest|python3?\s+-m\s+(unittest|pytest)|npm\s+test|npm\s+run\s+test(?::\w+)?|yarn\s+test|bun\s+test|cargo\s+test|make\s+test|go\s+test|rspec|jest|vitest|tox|ctest|ruff|mypy|flake8|eslint)\b",
    re.IGNORECASE
)

# Chained shell operators, pipes, subshells, newlines, or conditionals that mask exit codes
SHELL_OPERATOR_MASK_PATTERN = re.compile(
    r"(?:[\r\n]|\|\||;|&&|\|(?!=)|&|^\s*if\b|\beval\b|\bexec\b|\(|\))",
    re.IGNORECASE
)

EXPLORATORY_CMD_PATTERN = re.compile(
    r"^(?:[A-Z_0-9]+=\S+\s+)*(cat|ls|grep|find|echo|cd|pwd|curl|head|tail|which|whoami|env|date|uname|git\s+(status|log|diff|branch|show))\b",
    re.IGNORECASE
)

SOURCE_CODE_EXTENSIONS = {
    ".py", ".ts", ".js", ".tsx", ".jsx", ".rs", ".go", ".c", ".cpp",
    ".cc", ".cxx", ".h", ".hpp", ".java", ".rb", ".sh", ".bash",
    ".zsh", ".cs", ".php", ".swift", ".kt", ".scala", ".lua", ".zig",
    ".mjs", ".cjs"
}

DOC_EXTENSIONS = {
    ".md", ".txt", ".rst", ".adoc", ".csv", ".tsv", ".json", ".yaml",
    ".yml", ".toml", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".pdf", ".html", ".css", ".xml", ".lock", ".gitignore"
}


def is_verification_command(cmd: str) -> bool:
    cmd_clean = (cmd or "").strip()
    # Fake verification commands like pytest --version, pytest --help, pytest --fixtures, cargo test --help are NOT verification runs
    if re.search(r"(?:^|\s)(?:--help|-h|--version|-V|--collect-only|--co|--fixtures|--markers|--setup-only|--setup-plan|--setup-show|--cache-show)(?:\s|$)", cmd_clean):
        return False
    return bool(VERIFICATION_CMD_PATTERN.search(cmd_clean))


DANGEROUS_ENV_OVERRIDE_PATTERN = re.compile(
    r"(?:^|[\s;\|\&])(?:PATH|PYTHONPATH|PYTHONHOME|LD_PRELOAD|DYLD_INSERT_LIBRARIES|DYLD_LIBRARY_PATH|NODE_OPTIONS|PERL5LIB|RUBYLIB)=",
    re.IGNORECASE
)


def is_tainted_shell_command(cmd: str) -> bool:
    """Detects verification commands chained with masking operators or dangerous env overrides (PATH=, LD_PRELOAD=)."""
    cmd_clean = (cmd or "").strip()
    if VERIFICATION_CMD_PATTERN.search(cmd_clean):
        if SHELL_OPERATOR_MASK_PATTERN.search(cmd_clean):
            return True
        if DANGEROUS_ENV_OVERRIDE_PATTERN.search(cmd_clean):
            return True
    return False


def is_exploratory_command(cmd: str) -> bool:
    cmd_clean = (cmd or "").strip()
    if re.search(r"(?:^|\s)(?:--help|-h|--version|-V|--collect-only|--co|--fixtures|--markers|--setup-only|--setup-plan|--setup-show|--cache-show)(?:\s|$)", cmd_clean):
        return True
    return bool(EXPLORATORY_CMD_PATTERN.search(cmd_clean))


def classify_file(filepath: str, workspace_dir: Optional[str] = None) -> str:
    """Returns 'source', 'doc', or 'other'."""
    basename = os.path.basename(filepath or "")
    if basename in ["Makefile", "Dockerfile", "Containerfile", "build.sh", "deploy.sh"]:
        return "source"

    _, ext = os.path.splitext(filepath or "")
    ext = ext.lower()
    if ext in SOURCE_CODE_EXTENSIONS:
        return "source"
    elif ext in DOC_EXTENSIONS:
        return "doc"

    # Sniff executable bits or shebang for extensionless scripts (e.g. bin/build)
    full_path = filepath
    if workspace_dir and not os.path.isabs(filepath):
        full_path = os.path.join(workspace_dir, filepath)

    if os.path.isfile(full_path):
        try:
            if os.access(full_path, os.X_OK):
                return "source"
            with open(full_path, "rb") as f:
                header = f.read(256)
                if header.startswith(b"#!"):
                    return "source"
        except Exception:
            pass

    return "other"


def can_suite_resolve_failure(
    clean_cmd: str,
    failed_cmd: str,
    clean_cwd: Optional[str] = None,
    failed_cwd: Optional[str] = None
) -> bool:
    """
    Hierarchical resolution: checks if clean_cmd encompasses failed_cmd.
    Guarantees:
    - Same working directory (CWD-aware)
    - Strict path encompassment (tests/ does NOT resolve integration_tests/)
    - Subtest/parameterized resolution (tests/test_foo.py resolves tests/test_foo.py::test_bar)
    """
    clean = clean_cmd.strip()
    failed = failed_cmd.strip()

    # CWD check: different working directories cannot resolve each other
    if clean_cwd and failed_cwd:
        if os.path.abspath(clean_cwd) != os.path.abspath(failed_cwd):
            return False

    if clean == failed:
        return True

    # Filter flag check: if clean runs with test filters (-k, -m, --filter),
    # it only tests a subset of tests. It can NEVER resolve a whole-file or broader failure!
    # It can only resolve if failed had the exact same filtered command.
    clean_has_filter = bool(re.search(r"(?:^|\s)(?:-k|-m|--filter)\b", clean))
    if clean_has_filter:
        return clean == failed

    # Helper to extract target test file/dir token from pytest command
    def extract_pytest_target(cmd_str: str) -> Optional[str]:
        parts = cmd_str.split()
        if not parts or parts[0] != "pytest":
            return None
        skip_next = False
        for p in parts[1:]:
            if skip_next:
                skip_next = False
                continue
            if p in ["-k", "-m", "-c", "-o", "--override-ini", "--rootdir", "--ignore", "-W"]:
                skip_next = True
                continue
            if p.startswith("-"):
                continue
            return p
        return None

    t_clean = extract_pytest_target(clean)
    t_failed = extract_pytest_target(failed)

    # 1. Python pytest hierarchy
    # A root-level pytest invocation with flags only (e.g. pytest -v, pytest -x, pytest --exitfirst)
    # or bare pytest runs all tests in the workspace root, encompassing any pytest failure.
    clean_parts = clean.split()
    is_root_clean_pytest = (
        bool(clean_parts) and (clean_parts[0] == "pytest" or "unittest" in clean)
        and not clean_has_filter
        and (t_clean is None or t_clean in [".", "./"])
    )
    if is_root_clean_pytest and (failed.startswith("pytest") or "unittest" in failed):
        return True

    if t_clean and t_failed:
        raw_clean_target = t_clean.rstrip("/")
        raw_failed_target = t_failed.rstrip("/")

        # If clean is a specific subtest (contains ::), it can ONLY resolve that exact subtest.
        # It must NEVER resolve a sibling subtest (e.g. test_passing resolving test_broken) or the whole file!
        if "::" in raw_clean_target:
            return raw_clean_target == raw_failed_target

        clean_file = raw_clean_target
        failed_file = raw_failed_target.split("::")[0]

        clean_norm = os.path.normpath(clean_file)
        failed_norm = os.path.normpath(failed_file)

        # 1. Clean ran the entire file that contains failed_cmd (whether failed was whole file or subtest)
        if failed_norm == clean_norm:
            return True
        # 2. Clean ran a parent directory that encompasses failed_cmd
        if failed_norm.startswith(clean_norm + os.sep):
            return True
        return False
    elif t_clean and not t_failed:
        # e.g. clean is 'pytest tests/' but failed was bare 'pytest'
        return False

    # 2. Node npm test hierarchy
    is_clean_npm_suite = clean in ["npm test", "npm run test", "yarn test", "bun test"]
    if is_clean_npm_suite and ("npm" in failed or "yarn" in failed or "bun" in failed):
        return True

    # 3. Rust cargo test hierarchy
    is_clean_cargo_suite = clean in ["cargo test", "cargo test --all"]
    if is_clean_cargo_suite and failed.startswith("cargo test"):
        return True

    # 4. Go test hierarchy
    is_clean_go_suite = clean in ["go test ./...", "go test ."]
    if is_clean_go_suite and failed.startswith("go test"):
        return True

    # 5. Make test hierarchy
    if clean == "make test" and failed.startswith("make test"):
        return True

    return False


class DaemonLedger:
    def __init__(self, ledger_path: str = None, key_path: str = None):
        self.ledger_path = ledger_path or os.environ.get("HARDTRUTH_DAEMON_LEDGER", DEFAULT_LEDGER_PATH)
        self.key_path = key_path or os.environ.get("HARDTRUTH_DAEMON_KEY", DEFAULT_KEY_PATH)
        self._key = self._load_or_create_key()
        self._session_baselines: Dict[Tuple[str, str], str] = {}

    def _load_or_create_key(self) -> bytes:
        key_dir = os.path.dirname(os.path.abspath(self.key_path))
        os.makedirs(key_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(key_dir, 0o700)
        except Exception:
            pass

        if os.path.exists(self.key_path):
            with open(self.key_path, "rb") as f:
                key = f.read()
                if len(key) == 32:
                    return key

        key = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        mode = 0o400
        fd = os.open(self.key_path, flags, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        try:
            os.chmod(self.key_path, 0o400)
        except Exception:
            pass
        return key

    def _compute_hash(self, index: int, prev_hash: str, canonical_entry: str) -> str:
        msg = f"{index}:{prev_hash}:{canonical_entry}".encode("utf-8")
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def get_last_record(self) -> Optional[dict]:
        if not os.path.exists(self.ledger_path):
            return None
        last_line = None
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                line_s = line.strip()
                if line_s:
                    last_line = line_s
        if last_line:
            try:
                return json.loads(last_line)
            except Exception:
                return None
        return None

    def record_entry(
        self,
        conversation_id: str,
        step_idx: int,
        tool: str,
        target: str,
        observed_exit_code: Optional[int] = None,
        harness_status: Optional[str] = None,
        error: Optional[str] = None,
        stdout_tail: Optional[str] = None,
        diff_stat: Optional[str] = None,
        timestamp: Optional[float] = None,
        cwd: Optional[str] = None
    ) -> dict:
        """
        Appends an entry to the HMAC-SHA256 hash-chained daemon ledger.
        """
        ledger_dir = os.path.dirname(os.path.abspath(self.ledger_path))
        os.makedirs(ledger_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(ledger_dir, 0o700)
        except Exception:
            pass

        last_record = self.get_last_record()
        if last_record is None:
            index = 0
            prev_hash = "0" * 64
        else:
            index = last_record.get("index", 0) + 1
            prev_hash = last_record.get("hash", "0" * 64)

        # Check for shell operator taint
        is_tainted = False
        if tool == "run_command" and is_tainted_shell_command(target):
            is_tainted = True
            error = f"TAINTED: Chained shell operators detected: {error or ''}".strip()
            harness_status = "tainted_shell_operator"

        entry_data = {
            "conversationId": conversation_id,
            "stepIdx": step_idx,
            "tool": tool,
            "target": target,
            "observed_exit_code": observed_exit_code,
            "harness_status": harness_status or ("no_error" if observed_exit_code == 0 and not is_tainted else "error" if observed_exit_code is not None or is_tainted else None),
            "error": error,
            "stdout_tail": stdout_tail[:1000] if stdout_tail else None,
            "diff_stat": diff_stat[:200] if diff_stat else None,
            "timestamp": timestamp or time.time(),
            "tainted": is_tainted,
            "cwd": cwd
        }

        canonical_entry = json.dumps(entry_data, sort_keys=True, separators=(',', ':'))
        record_hash = self._compute_hash(index, prev_hash, canonical_entry)

        status_compat = "error" if error or is_tainted or (observed_exit_code is not None and observed_exit_code != 0) else "success"

        record = {
            "index": index,
            "prev_hash": prev_hash,
            "entry": entry_data,
            "hash": record_hash,
            # Top-level backwards compatibility fields
            "target": target,
            "status": status_compat,
            "tool": tool,
            "conversationId": conversation_id,
            "stepIdx": step_idx,
            "error": error,
            "cwd": cwd
        }

        file_exists = os.path.exists(self.ledger_path)
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        if not file_exists:
            try:
                os.chmod(self.ledger_path, 0o600)
            except Exception:
                pass

        return {
            "status": "recorded",
            "index": index,
            "hash": record_hash
        }

    def set_session_baseline(self, conversation_id: str, workspace_path: str, baseline_sha: str) -> str:
        """
        Registers the session baseline commit SHA for (conversation_id, workspace_path).
        IMMUTABLE: once registered, subsequent calls return the existing baseline SHA to prevent tampering.
        """
        workspace_norm = os.path.abspath(workspace_path)
        key = (str(conversation_id), workspace_norm)
        existing = self.get_session_baseline(conversation_id, workspace_path)
        if existing:
            return existing

        self._session_baselines[key] = baseline_sha
        # Append to hash-chained ledger for physical persistence & tamper-evidence
        self.record_entry(
            conversation_id=conversation_id,
            step_idx=0,
            tool="__session_baseline__",
            target=workspace_norm,
            diff_stat=baseline_sha
        )
        return baseline_sha

    def get_session_baseline(self, conversation_id: str, workspace_path: str) -> Optional[str]:
        """
        Retrieves the immutable session baseline commit SHA for (conversation_id, workspace_path).
        """
        workspace_norm = os.path.abspath(workspace_path)
        key = (str(conversation_id), workspace_norm)
        if key in self._session_baselines:
            return self._session_baselines[key]

        # Search HMAC-chained ledger for historical record
        if os.path.exists(self.ledger_path):
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    line_s = line.strip()
                    if not line_s:
                        continue
                    try:
                        record = json.loads(line_s)
                        entry = record.get("entry", record)
                        if (
                            entry.get("conversationId") == conversation_id
                            and entry.get("tool") == "__session_baseline__"
                            and entry.get("target") == workspace_norm
                        ):
                            sha = entry.get("diff_stat")
                            if sha:
                                self._session_baselines[key] = sha
                                return sha
                    except Exception:
                        continue
        return None

    def verify_chain(self) -> Tuple[bool, int, str]:
        """Validates the entire HMAC hash chain from 0 to N-1."""
        if not os.path.exists(self.ledger_path):
            return True, 0, "EMPTY_LEDGER"

        expected_prev_hash = "0" * 64
        expected_index = 0

        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f):
                line_s = line.strip()
                if not line_s:
                    continue
                try:
                    record = json.loads(line_s)
                except Exception as e:
                    return False, expected_index, f"JSON_PARSE_ERROR on line {line_no}: {e}"

                idx = record.get("index")
                prev_h = record.get("prev_hash")
                entry = record.get("entry")
                rec_h = record.get("hash")

                if idx != expected_index:
                    return False, expected_index, f"INDEX_GAP: expected {expected_index}, got {idx}"

                if prev_h != expected_prev_hash:
                    return False, expected_index, f"CHAIN_BROKEN at index {idx}: expected prev {expected_prev_hash[:12]}, got {prev_h[:12]}"

                canonical_entry = json.dumps(entry, sort_keys=True, separators=(',', ':'))
                recomputed = self._compute_hash(idx, prev_h, canonical_entry)
                if not hmac.compare_digest(recomputed, rec_h):
                    return False, expected_index, f"HASH_MISMATCH at index {idx}: computed {recomputed[:12]} vs recorded {rec_h[:12]}"

                expected_prev_hash = rec_h
                expected_index += 1

        return True, expected_index, "VALID"

    def get_premise(self, conversation_id: str) -> dict:
        """
        Reads ledger records for conversation_id after validating chain integrity.
        Compiles failure-biased premise, unresolved failures, and source modification stats.
        """
        valid, count, msg = self.verify_chain()
        if not valid:
            return {
                "tampered": True,
                "error": "LEDGER_TAMPER_DETECTED",
                "detail": msg,
                "broken_at_index": count,
                "premise": f"LEDGER CORRUPTION DETECTED: {msg}",
                "source_files_modified": 0,
                "doc_files_modified": 0,
                "modified_files": [],
                "modified_file_paths": [],
                "verification_commands_executed": 0,
                "unresolved_failures": []
            }

        conv_records = []
        if os.path.exists(self.ledger_path):
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    line_s = line.strip()
                    if line_s:
                        try:
                            rec = json.loads(line_s)
                            entry = rec.get("entry", rec)
                            if entry.get("conversationId") == conversation_id:
                                conv_records.append(entry)
                        except Exception:
                            continue

        unresolved_failures: Dict[str, dict] = {}
        verification_commands_count = 0
        modified_source_files = set()
        modified_doc_files = set()
        modified_file_paths = set()
        diff_stats: Dict[str, str] = {}
        all_commands = []

        for e in conv_records:
            tool = e.get("tool")
            target = e.get("target", "")
            exit_code = e.get("observed_exit_code")
            error = e.get("error")
            status = e.get("harness_status")
            tainted = e.get("tainted", False)

            if tool == "run_command":
                all_commands.append(e)
                is_verif = is_verification_command(target)
                if is_verif:
                    failed = tainted or (exit_code is not None and exit_code != 0) or (error is not None) or (status == "unverified_timeout")
                    if failed:
                        unresolved_failures[target] = {
                            "command": target,
                            "observed_exit_code": exit_code,
                            "error": error or ("TAINTED_SHELL_OPERATOR" if tainted else "UNVERIFIED_TIMEOUT" if status == "unverified_timeout" else None),
                            "stdout_tail": e.get("stdout_tail"),
                            "stepIdx": e.get("stepIdx"),
                            "cwd": e.get("cwd")
                        }
                    else:
                        # Only confirmed success with exit_code == 0 or no_error can increment or resolve
                        if status != "unverified_timeout" and (exit_code == 0 or status == "no_error"):
                            verification_commands_count += 1
                            # Hierarchical resolution across test suites with CWD isolation
                            resolved_keys = [
                                k for k in list(unresolved_failures.keys())
                                if can_suite_resolve_failure(
                                    clean_cmd=target,
                                    failed_cmd=k,
                                    clean_cwd=e.get("cwd"),
                                    failed_cwd=unresolved_failures[k].get("cwd")
                                )
                            ]
                            for k in resolved_keys:
                                unresolved_failures.pop(k, None)

            elif tool in ["write_to_file", "replace_file_content"]:
                ftype = classify_file(target)
                base = os.path.basename(target)
                modified_file_paths.add(target)
                if ftype == "source":
                    modified_source_files.add(base)
                elif ftype == "doc":
                    modified_doc_files.add(base)
                if e.get("diff_stat"):
                    diff_stats[base] = e.get("diff_stat")

        premise_sections = []

        # SECTION 1: UNRESOLVED FAILURES (Permanent)
        if unresolved_failures:
            fails = []
            for cmd, info in unresolved_failures.items():
                ec = info.get("observed_exit_code")
                ec_str = f"exit code {ec}" if ec is not None else "failed"
                if info.get("error") and "TAINTED" in info.get("error"):
                    ec_str = "TAINTED_OPERATOR"
                tail = f" | Output: {info.get('stdout_tail')[:120]}" if info.get("stdout_tail") else ""
                fails.append(f"FAILED: '{cmd}' ({ec_str}{tail})")
            premise_sections.append(f"UNRESOLVED TEST FAILURES (CRITICAL): {'; '.join(fails)}.")

        # SECTION 2: RECENT EXECUTIONS (up to 6)
        if all_commands:
            cmd_strs = []
            for c in all_commands[-6:]:
                cmd_name = c.get("target")
                ec = c.get("observed_exit_code")
                st = c.get("harness_status")
                err = c.get("error")
                if c.get("tainted"):
                    status_desc = "TAINTED (shell operator bypass rejected)"
                elif ec == 0:
                    status_desc = "SUCCEEDED (exit 0)"
                elif ec is not None:
                    status_desc = f"FAILED (exit {ec})"
                elif st == "no_error":
                    status_desc = "HARNESS_STATUS: no_error (raw exit code uncorroborated by transcript)"
                else:
                    status_desc = f"ERROR ({err or 'unknown'})"

                tail_str = f" [Tail: {c.get('stdout_tail')[:80]}]" if c.get("stdout_tail") else ""
                cmd_strs.append(f"COMMAND: '{cmd_name}'. STATUS: {status_desc}{tail_str}.")
            premise_sections.append(f"RECENT EXECUTIONS: {' '.join(cmd_strs)}")
        else:
            premise_sections.append("RECENT EXECUTIONS: No commands or test suites were executed.")

        # SECTION 3: MODIFIED FILES & DIFF STATS
        if modified_source_files or modified_doc_files:
            all_mods = list(modified_source_files) + list(modified_doc_files)
            mod_strs = []
            for m in all_mods:
                stat = f" ({diff_stats[m]})" if m in diff_stats else ""
                mod_strs.append(f"{m}{stat}")
            premise_sections.append(f"MODIFIED FILES: {', '.join(mod_strs)}.")
        else:
            premise_sections.append("MODIFIED FILES: No files were modified.")

        premise_str = " ".join(premise_sections)[:1500]

        return {
            "tampered": False,
            "conversationId": conversation_id,
            "premise": premise_str,
            "source_files_modified": len(modified_source_files),
            "doc_files_modified": len(modified_doc_files),
            "modified_files": sorted(list(modified_source_files | modified_doc_files)),
            "modified_file_paths": sorted(list(modified_file_paths)),
            "verification_commands_executed": verification_commands_count,
            "unresolved_failures": list(unresolved_failures.values()),
            "records_count": len(conv_records)
        }


# Default module-level singleton instance
_ledger = DaemonLedger()

