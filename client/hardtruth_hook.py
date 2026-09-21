#!/usr/bin/env python3
"""
HardTruth: Autonomous Anti-Hallucination & Physical Truth Enforcement Engine
Lifecycle Hook: PostToolUse & Stop
Powered by DeBERTa-v3 Cross-Encoder, Cryptographically Chained Execution Ledger & Tier 2 Hard Gate

Enforces:
1. Physical Ledger Integrity: HMAC-SHA256 chained, daemon-owned, failure-biased ledger.
2. Universal File Tracking: Runs git status --porcelain to catch all file edits (including sed, echo, patch).
3. Shell Operator Defense: Rejects exit-code masking operators (; true, || exit 0).
4. Real Exit Codes & Non-Synthesis: Anchored transcript parsing defeats stdout spoofing; never synthesizes exit 0.
5. Inverted Language-Independent Gate:
   - AST Anti-Stubbing Linter on modified code -> HALT on stubs.
   - Rule 1: Source code modified without test execution -> HALT.
   - Rule 2: Unresolved test failures remain in ledger -> HALT.
   - Rule 4: Targeted DeBERTa-v3 NLI Claim Adjudication -> HALT if contradiction >= tau*.
6. Tier 2 External Hard Gate: Out-of-band test execution in clean environment before final approval.
7. Hard Escalation Halt: Never fails open after 3 consecutive halts.
"""

from __future__ import annotations
import os
import sys
import json
import time
import re
import hashlib
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional, Dict, Any, List, Tuple, Set

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if _CURRENT_DIR not in sys.path:
    sys.path.insert(0, _CURRENT_DIR)
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

try:
    from ast_checker import check_ast_stubs
except ImportError:
    try:
        from client.ast_checker import check_ast_stubs
    except ImportError:
        from hardtruth.ast_checker import check_ast_stubs

_IMPORT_DEGRADED = None
try:
    from daemon.ledger import DaemonLedger, is_verification_command, is_test_execution_command, is_tainted_shell_command, classify_file
except ImportError:
    try:
        from ledger import DaemonLedger, is_verification_command, is_test_execution_command, is_tainted_shell_command, classify_file
    except ImportError:
        DaemonLedger = None
        is_verification_command = lambda cmd: False
        is_test_execution_command = lambda cmd: False
        is_tainted_shell_command = lambda cmd: False
        classify_file = lambda path, workspace_dir=None: "other"
        _IMPORT_DEGRADED = "daemon.ledger module unavailable"



HARNESS = os.environ.get("HARDTRUTH_HARNESS", "antigravity").lower()

def _halt(reason: str) -> dict:
    if HARNESS in ("claude_code", "claude"):
        return {"decision": "block", "reason": reason}
    if HARNESS == "goose":
        return {"action": "halt", "message": reason}
    return {"decision": "continue", "reason": reason}

def _allow() -> dict:
    if HARNESS == "goose":
        return {"action": "continue"}
    return {"decision": "allow"}

_CC_TOOL_MAP = {
    "Bash": "run_command", "bash": "run_command", "sh": "run_command",
    "Write": "write_to_file", "write": "write_to_file",
    "Edit": "replace_file_content", "edit": "replace_file_content",
    "MultiEdit": "replace_file_content", "NotebookEdit": "replace_file_content",
    "Read": "view_file", "read": "view_file"
}

def normalize_payload(p: dict) -> dict:
    """Normalizes payloads across Antigravity, Claude Code, and Goose into standard HardTruth format."""
    if not isinstance(p, dict):
        return {}
    if "toolCall" in p and "conversationId" in p:
        return p
    out = dict(p)
    out["conversationId"] = p.get("session_id") or p.get("conversationId") or p.get("sessionId") or "unknown"
    out["transcriptPath"] = p.get("transcript_path") or p.get("transcriptPath") or ""
    out["stepIdx"] = int(p.get("stepIdx", p.get("step", p.get("step_index", 0))) or 0)

    tn = p.get("tool_name") or p.get("tool") or (p.get("toolCall", {}).get("name") if isinstance(p.get("toolCall"), dict) else None)
    ti = p.get("tool_input") or p.get("arguments") or (p.get("toolCall", {}).get("args") if isinstance(p.get("toolCall"), dict) else {})
    if tn:
        mapped_name = _CC_TOOL_MAP.get(tn, tn)
        out["toolCall"] = {
            "name": mapped_name,
            "args": {
                "CommandLine": ti.get("command") or ti.get("cmd") or ti.get("CommandLine", ""),
                "TargetFile": ti.get("file_path") or ti.get("path") or ti.get("TargetFile", ""),
                "AbsolutePath": ti.get("file_path") or ti.get("path") or ti.get("AbsolutePath", ""),
                "Cwd": ti.get("cwd") or p.get("cwd", "")
            }
        }
    tr = p.get("tool_response") or p.get("result") or p.get("output") or {}
    if isinstance(tr, dict):
        if tr.get("exitCode") is not None:
            out["exitCode"] = tr["exitCode"]
        elif tr.get("exit_code") is not None:
            out["exitCode"] = tr["exit_code"]
    elif p.get("exit_code") is not None and "exitCode" not in out:
        out["exitCode"] = p["exit_code"]
    return out

CONTRADICTION_THRESHOLD = 0.70
SYSTEM_ONE_URL = os.environ.get("SYSTEM_ONE_URL", "http://127.0.0.1:8000")

SOURCE_CODE_EXTENSIONS = {
    ".py", ".ts", ".js", ".tsx", ".jsx", ".rs", ".go", ".c", ".cpp",
    ".cc", ".cxx", ".h", ".hpp", ".java", ".rb", ".sh", ".bash",
    ".zsh", ".cs", ".php", ".swift", ".kt", ".scala", ".lua", ".zig",
    ".mjs", ".cjs"
}

IGNORED_BUILD_DIRS = {
    ".git", ".pytest_cache", "__pycache__", "node_modules", "target", ".venv", "venv",
    ".tox", ".mypy_cache", "build", "dist", "vendor", ".eggs", ".next", ".nuxt",
    "site-packages", ".gradle", "Pods", ".terraform"
}

def get_changed_lines(workspace_dir: Optional[str], fpath: str, baseline_sha: Optional[str] = None) -> Optional[Set[int]]:
    """
    Returns set of line numbers in fpath modified since baseline commit (or unstaged working tree changes).
    Parses git diff -U0 hunk headers: @@ -l,s +start,count @@
    If file is untracked (newly added by agent), returns all line numbers in file.
    If file is tracked and has no changes, returns set() (0 modified lines).
    """
    if not workspace_dir or not os.path.isdir(os.path.join(workspace_dir, ".git")):
        return None
    rel_path = os.path.relpath(fpath, workspace_dir)
    ws_abs = os.path.abspath(workspace_dir)

    # Check if untracked in git (brand new file created by agent)
    try:
        unmatch_check = subprocess.run(
            ["git", "-c", f"safe.directory={ws_abs}", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
             "ls-files", "--error-unmatch", "--", rel_path],
            cwd=workspace_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2.0
        )
        if unmatch_check.returncode != 0:
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    cnt = sum(1 for _ in f)
                return set(range(1, max(cnt, 1) + 1))
            except Exception:
                return None
    except Exception:
        pass

    diff_args = [
        "git", "-c", f"safe.directory={ws_abs}",
        "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
        "diff", "-U0"
    ]
    if baseline_sha:
        diff_args.append(baseline_sha)
    diff_args.extend(["--", rel_path])
    try:
        proc = subprocess.run(diff_args, cwd=workspace_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0)
        if proc.returncode != 0:
            return None
        changed = set()
        for line in proc.stdout.splitlines():
            if line.startswith("@@"):
                m = re.search(r"\+(\d+)(?:,(\d+))?", line)
                if m:
                    start = int(m.group(1))
                    count = int(m.group(2)) if m.group(2) is not None else 1
                    for ln in range(start, start + max(count, 1)):
                        changed.add(ln)
        return changed
    except Exception:
        return None


def get_api_token() -> Optional[str]:
    """Returns the shared HardTruth API token: HARDTRUTH_API_TOKEN env, then key file."""
    tok = os.environ.get("HARDTRUTH_API_TOKEN", "").strip()
    if tok:
        return tok
    key_file = os.environ.get("HARDTRUTH_API_KEY", os.path.expanduser("~/.hardtruth/daemon_api.key"))
    try:
        with open(key_file, "r", encoding="utf-8") as f:
            tok = f.read().strip()
            if len(tok) >= 32:
                return tok
    except Exception:
        pass
    return None

def get_ledger_file() -> str:
    return os.environ.get(
        "HARDTRUTH_LEDGER_PATH",
        os.environ.get("HARDTRUTH_DAEMON_LEDGER", os.path.expanduser("~/.hardtruth/daemon_ledger.jsonl"))
    )

def get_halt_counter_dir() -> str:
    return os.environ.get(
        "HARDTRUTH_HALT_DIR",
        os.path.expanduser("~/.hardtruth/halts")
    )

def get_local_ledger() -> Optional[DaemonLedger]:
    if DaemonLedger:
        return DaemonLedger(ledger_path=get_ledger_file())
    return None


def get_halt_counter_file(conv_id: str) -> str:
    """Returns secure slugified and hashed counter path inside mode 0o700 dir."""
    halt_dir = get_halt_counter_dir()
    os.makedirs(halt_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(halt_dir, 0o700)
    except Exception:
        pass
    safe_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(conv_id))[:32]
    conv_hash = hashlib.sha256(str(conv_id).encode("utf-8")).hexdigest()[:16]
    return os.path.join(halt_dir, f"halt_{safe_slug}_{conv_hash}.json")


_SESSION_SECRETS: Dict[str, str] = {}


def get_session_secret_file(conv_id: str) -> str:
    """Returns secure slugified and hashed session secret path inside mode 0o700 dir."""
    halt_dir = get_halt_counter_dir()
    os.makedirs(halt_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(halt_dir, 0o700)
    except Exception:
        pass
    safe_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(conv_id))[:32]
    conv_hash = hashlib.sha256(str(conv_id).encode("utf-8")).hexdigest()[:16]
    return os.path.join(halt_dir, f"session_{safe_slug}_{conv_hash}.key")


def get_or_create_session_secret(conv_id: str, workspace_dir: Optional[str] = None) -> Optional[str]:
    """
    Open Item #2: Retrieves or requests an ephemeral session secret from the daemon.
    Caches secret in process memory and mode 0400 file in halt_dir.
    """
    if not conv_id or conv_id == "unknown":
        return None
    if conv_id in _SESSION_SECRETS:
        return _SESSION_SECRETS[conv_id]

    key_path = get_session_secret_file(conv_id)
    if os.path.exists(key_path):
        try:
            with open(key_path, "r", encoding="utf-8") as f:
                sec = f.read().strip()
                if len(sec) >= 32:
                    _SESSION_SECRETS[conv_id] = sec
                    return sec
        except Exception:
            pass

    # Mint session secret on daemon
    resp = call_system_one("v1/session/start", {
        "conversationId": conv_id,
        "workspace_path": workspace_dir or os.getcwd()
    }, timeout=2.0)
    if resp and resp.get("session_secret"):
        sec = resp.get("session_secret")
        _SESSION_SECRETS[conv_id] = sec
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(key_path, flags, 0o400)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(sec)
            try:
                os.chmod(key_path, 0o400)
            except Exception:
                pass
        except Exception:
            pass
        return sec

    return None


def call_system_one(endpoint: str, payload: dict, timeout: float = 3.0, session_secret: Optional[str] = None) -> Optional[dict]:
    url = f"{SYSTEM_ONE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    tok = get_api_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    if session_secret:
        headers["X-Session-Secret"] = session_secret
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = None
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            pass
        if e.code in (400, 406) and isinstance(body, dict):
            return body
        if e.code == 401:
            sys.stderr.write("⚠️ HardTruth: daemon rejected request (401) — HARDTRUTH_API_TOKEN mismatch between hook and daemon.\n")
        elif e.code == 403:
            sys.stderr.write("⚠️ HardTruth: daemon rejected request (403) — X-Session-Secret mismatch or unauthorized session.\n")
        elif e.code == 409:
            sys.stderr.write("⚠️ HardTruth: daemon rejected request (409) — Conflict / out-of-order step.\n")
        return None
    except Exception:
        return None


def get_workspace_dir(payload: dict) -> Optional[str]:
    """Extracts first valid workspace path with robust fallbacks from payload."""
    ws_paths = payload.get("workspacePaths", [])
    if ws_paths:
        for p in ws_paths:
            if isinstance(p, str) and os.path.isdir(p):
                return os.path.abspath(p)
    for k in ["cwd", "workspace", "workspace_dir", "workspaceDir", "projectDir", "project_dir", "root"]:
        v = payload.get(k)
        if isinstance(v, dict):
            candidate = v.get("path") or v.get("uri")
            if isinstance(candidate, str):
                if candidate.startswith("file://"):
                    candidate = candidate[7:]
                if os.path.isdir(candidate):
                    return os.path.abspath(candidate)
        elif isinstance(v, str) and os.path.isdir(v):
            return os.path.abspath(v)
    return None




def is_valid_commit(sha: Optional[str], workspace_dir: str) -> bool:
    if not sha or not workspace_dir or not os.path.isdir(os.path.join(workspace_dir, ".git")):
        return False
    try:
        chk = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=workspace_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0
        )
        return chk.returncode == 0
    except Exception:
        return False


def get_dirty_files(workspace_dir: str) -> Set[str]:
    """Returns set of relative paths of currently modified/untracked files."""
    dirty = set()
    if not workspace_dir or not os.path.isdir(os.path.join(workspace_dir, ".git")):
        return dirty
    ws_abs = os.path.abspath(workspace_dir)
    git_safe_flags = [
        "-c", f"safe.directory={ws_abs}",
        "-c", "core.fsmonitor=",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.quotepath=false"
    ]
    exclude_pathspecs = [
        "--", ".",
        ":(exclude).git",
        ":(exclude)node_modules",
        ":(exclude).venv",
        ":(exclude)venv",
        ":(exclude)target",
        ":(exclude).pytest_cache",
        ":(exclude)__pycache__",
        ":(exclude).tox",
        ":(exclude).mypy_cache",
        ":(exclude)build",
        ":(exclude)dist"
    ]
    try:
        res = subprocess.run(
            ["git"] + git_safe_flags + ["status", "--porcelain", "-uall", "--ignored=matching"] + exclude_pathspecs,
            cwd=workspace_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
            text=True
        )
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                line_clean = line.strip()
                if len(line_clean) < 3:
                    continue
                filepath_rel = line_clean[2:].strip()
                if filepath_rel.startswith('"') and filepath_rel.endswith('"'):
                    filepath_rel = filepath_rel[1:-1]
                if " -> " in filepath_rel:
                    filepath_rel = filepath_rel.split(" -> ")[1].strip()
                    if filepath_rel.startswith('"') and filepath_rel.endswith('"'):
                        filepath_rel = filepath_rel[1:-1]
                dirty.add(filepath_rel)
    except Exception:
        pass
    return dirty


def get_session_baseline_dirty_files(workspace_dir: str, conv_id: str) -> Set[str]:
    """Retrieves the set of dirty files recorded at session baseline."""
    if not workspace_dir or not conv_id:
        return set()
    safe_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(conv_id))[:32]
    conv_hash = hashlib.sha256(str(conv_id).encode("utf-8")).hexdigest()[:16]
    halt_dir = get_halt_counter_dir()
    baseline_file = os.path.join(halt_dir, f"baseline_{safe_slug}_{conv_hash}.json")
    if os.path.exists(baseline_file):
        try:
            with open(baseline_file, "r") as f:
                data = json.load(f)
                if "baseline_dirty_files" in data:
                    return set(data.get("baseline_dirty_files", []))
        except Exception:
            pass
    return set()


def get_or_set_session_baseline(workspace_dir: str, conv_id: str) -> Optional[str]:
    """
    Records and returns the git commit SHA at the start of the session.
    Enforces multi-layer baseline persistence:
    1. Isolated Daemon (tamper-evident, survives local rm -rf ~/.hardtruth/halts)
    2. Git internal ref (refs/hardtruth/baseline/<conv_hash>)
    3. Local cache file (~/.hardtruth/halts/baseline_*.json)
    Defeats the 'commit & run' and baseline reset exploits completely.
    """
    if not workspace_dir or not os.path.isdir(os.path.join(workspace_dir, ".git")):
        return None

    safe_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(conv_id))[:32]
    ws_norm = os.path.abspath(workspace_dir)
    conv_hash = hashlib.sha256(str(conv_id).encode("utf-8")).hexdigest()[:16]
    halt_dir = get_halt_counter_dir()
    os.makedirs(halt_dir, mode=0o700, exist_ok=True)
    baseline_file = os.path.join(halt_dir, f"baseline_{safe_slug}_{conv_hash}.json")

    # 1. Ask isolated daemon (primary source of truth, container-isolated from agent)
    try:
        resp = call_system_one(
            f"v1/session/baseline?conversationId={urllib.parse.quote(str(conv_id))}&workspace_path={urllib.parse.quote(ws_norm)}",
            {},
            timeout=1.0
        )
        if resp and resp.get("baseline_sha"):
            daemon_sha = resp.get("baseline_sha")
            if is_valid_commit(daemon_sha, workspace_dir):
                try:
                    data = {}
                    if os.path.exists(baseline_file):
                        try:
                            with open(baseline_file, "r") as f:
                                data = json.load(f)
                        except Exception:
                            pass
                    data["baseline_sha"] = daemon_sha
                    data["workspace_path"] = ws_norm
                    data.setdefault("created_at", time.time())
                    data.setdefault("baseline_dirty_files", list(get_dirty_files(workspace_dir)))
                    with open(baseline_file, "w") as f:
                        json.dump(data, f)
                except Exception:
                    pass
                return daemon_sha
    except Exception:
        pass

    # 2. Check local git ref (refs/hardtruth/baseline/<conv_hash>)
    try:
        ref_proc = subprocess.run(
            ["git", "rev-parse", f"refs/hardtruth/baseline/{conv_hash}"],
            cwd=workspace_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1.0,
            text=True
        )
        if ref_proc.returncode == 0 and ref_proc.stdout.strip():
            ref_sha = ref_proc.stdout.strip()
            if is_valid_commit(ref_sha, workspace_dir):
                # Register with daemon if online
                call_system_one("v1/session/baseline", {
                    "conversationId": conv_id,
                    "workspace_path": ws_norm,
                    "commit_sha": ref_sha
                }, timeout=1.0)
                try:
                    data = {}
                    if os.path.exists(baseline_file):
                        try:
                            with open(baseline_file, "r") as f:
                                data = json.load(f)
                        except Exception:
                            pass
                    data["baseline_sha"] = ref_sha
                    data["workspace_path"] = ws_norm
                    data.setdefault("created_at", time.time())
                    data.setdefault("baseline_dirty_files", list(get_dirty_files(workspace_dir)))
                    with open(baseline_file, "w") as f:
                        json.dump(data, f)
                except Exception:
                    pass
                return ref_sha
    except Exception:
        pass

    # 3. Check local cache file
    if os.path.exists(baseline_file):
        try:
            with open(baseline_file, "r") as f:
                data = json.load(f)
                cached_sha = data.get("baseline_sha")
                if cached_sha and is_valid_commit(cached_sha, workspace_dir):
                    call_system_one("v1/session/baseline", {
                        "conversationId": conv_id,
                        "workspace_path": ws_norm,
                        "commit_sha": cached_sha
                    }, timeout=1.0)
                    if "baseline_dirty_files" not in data:
                        data["baseline_dirty_files"] = list(get_dirty_files(workspace_dir))
                        try:
                            with open(baseline_file, "w") as f_upd:
                                json.dump(data, f_upd)
                        except Exception:
                            pass
                    return cached_sha
        except Exception:
            pass

    # 4. First initialization for this session: get current HEAD
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2.0,
            text=True
        )
        if res.returncode == 0 and res.stdout.strip():
            sha = res.stdout.strip()

            # Register with daemon (daemon returns existing if already recorded)
            resp = call_system_one("v1/session/baseline", {
                "conversationId": conv_id,
                "workspace_path": ws_norm,
                "commit_sha": sha
            }, timeout=1.0)
            if resp and resp.get("baseline_sha"):
                sha = resp.get("baseline_sha")

            # Store in git ref
            try:
                subprocess.run(
                    ["git", "update-ref", f"refs/hardtruth/baseline/{conv_hash}", sha],
                    cwd=workspace_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=1.0
                )
            except Exception:
                pass

            # Cache locally
            try:
                data = {}
                if os.path.exists(baseline_file):
                    try:
                        with open(baseline_file, "r") as f:
                            data = json.load(f)
                    except Exception:
                        pass
                data["baseline_sha"] = sha
                data["workspace_path"] = ws_norm
                data.setdefault("created_at", time.time())
                data.setdefault("baseline_dirty_files", list(get_dirty_files(workspace_dir)))
                with open(baseline_file, "w") as f:
                    json.dump(data, f)
            except Exception:
                pass

            return sha
    except Exception:
        pass

    return None


def get_git_modified_source_files(workspace_dir: str, conv_id: Optional[str] = None) -> Tuple[Set[str], Set[str], List[str]]:
    """
    Universal File Tracking:
    1. Checks working tree modifications (M, A, ??, R, !!) via git status --porcelain --ignored=matching.
    2. Checks committed modifications made during this session via git diff --name-only <baseline_sha> HEAD.
    Defeats the 'commit & run' evasion loophole completely.
    Returns: (source_files, doc_files, full_file_paths)
    """
    source_files = set()
    doc_files = set()
    all_rel_paths = set()

    if not workspace_dir or not os.path.exists(workspace_dir):
        return source_files, doc_files, []

    ws_abs = os.path.abspath(workspace_dir)
    git_safe_flags = [
        "-c", f"safe.directory={ws_abs}",
        "-c", "core.fsmonitor=",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.quotepath=false"
    ]

    is_git_repo = os.path.exists(os.path.join(workspace_dir, ".git"))

    # 1. Uncommitted, untracked, and ignored source files in working tree
    # Uses --ignored=matching with explicit pathspec exclusions to prevent crawling node_modules/venv
    if is_git_repo:
        baseline_dirty = get_session_baseline_dirty_files(workspace_dir, conv_id) if conv_id else set()
        exclude_pathspecs = [
            "--", ".",
            ":(exclude).git",
            ":(exclude)node_modules",
            ":(exclude).venv",
            ":(exclude)venv",
            ":(exclude)target",
            ":(exclude).pytest_cache",
            ":(exclude)__pycache__",
            ":(exclude).tox",
            ":(exclude).mypy_cache",
            ":(exclude)build",
            ":(exclude)dist"
        ]
        try:
            res = subprocess.run(
                ["git"] + git_safe_flags + ["status", "--porcelain", "-uall", "--ignored=matching"] + exclude_pathspecs,
                cwd=workspace_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5.0,
                text=True
            )
            if res.returncode != 0:
                raise RuntimeError(f"git status failed with exit code {res.returncode}: {res.stderr.strip()}")
            if res.stdout:
                for line in res.stdout.splitlines():
                    line_clean = line.strip()
                    if len(line_clean) < 3:
                        continue
                    filepath_rel = line_clean[2:].strip()
                    if filepath_rel.startswith('"') and filepath_rel.endswith('"'):
                        filepath_rel = filepath_rel[1:-1]
                    if " -> " in filepath_rel:
                        filepath_rel = filepath_rel.split(" -> ")[1].strip()
                        if filepath_rel.startswith('"') and filepath_rel.endswith('"'):
                            filepath_rel = filepath_rel[1:-1]
                    if conv_id and filepath_rel in baseline_dirty:
                        continue
                    all_rel_paths.add(filepath_rel)
        except subprocess.TimeoutExpired:
            raise RuntimeError("git status timed out after 5.0s during workspace verification")

    # 2. Committed files since session baseline
    if is_git_repo and conv_id:
        baseline_sha = get_or_set_session_baseline(workspace_dir, conv_id)
        if baseline_sha:
            try:
                res_diff = subprocess.run(
                    ["git"] + git_safe_flags + ["diff", "--name-only", baseline_sha, "HEAD", "--"],
                    cwd=workspace_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5.0,
                    text=True
                )
                if res_diff.returncode != 0:
                    raise RuntimeError(f"git diff failed with exit code {res_diff.returncode}: {res_diff.stderr.strip()}")
                if res_diff.stdout:
                    for line in res_diff.stdout.splitlines():
                        f = line.strip()
                        if f.startswith('"') and f.endswith('"'):
                            f = f[1:-1]
                        if f:
                            all_rel_paths.add(f)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"git diff against baseline {baseline_sha} timed out after 5.0s")

    # 3. Check for git index manipulation (assume-unchanged or skip-worktree)
    if is_git_repo:
        try:
            res_v = subprocess.run(
                ["git"] + git_safe_flags + ["ls-files", "-v"],
                cwd=workspace_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5.0,
                text=True
            )
            if res_v.returncode != 0:
                raise RuntimeError(f"git ls-files -v failed with exit code {res_v.returncode}: {res_v.stderr.strip()}")
            if res_v.stdout:
                for line in res_v.stdout.splitlines():
                    if len(line) >= 3:
                        tag = line[0]
                        fname = line[2:].strip()
                        if fname.startswith('"') and fname.endswith('"'):
                            fname = fname[1:-1]
                        if tag in ["h", "s", "S"]:
                            try:
                                cur_h = subprocess.run(
                                    ["git"] + git_safe_flags + ["hash-object", "--", fname],
                                    cwd=workspace_dir,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    timeout=2.0,
                                    text=True
                                ).stdout.strip()
                                idx_out = subprocess.run(
                                    ["git"] + git_safe_flags + ["ls-files", "-s", "--", fname],
                                    cwd=workspace_dir,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    timeout=2.0,
                                    text=True
                                ).stdout.split()
                                if len(idx_out) >= 2 and cur_h != idx_out[1]:
                                    all_rel_paths.add(fname)
                            except Exception:
                                all_rel_paths.add(fname)
        except subprocess.TimeoutExpired:
            raise RuntimeError("git ls-files timed out after 5.0s")

    # 4. Fallback if .git is missing (e.g. rm -rf .git)
    if not os.path.exists(os.path.join(workspace_dir, ".git")):
        try:
            for root, dirs, files in os.walk(workspace_dir):
                dirs[:] = [d for d in dirs if d not in IGNORED_BUILD_DIRS]
                for fname in files:
                    rel_p = os.path.relpath(os.path.join(root, fname), workspace_dir)
                    all_rel_paths.add(rel_p)
        except Exception:
            pass

    # 5. Filter out ignored build directories before expanding paths
    filtered_rel_paths = set()
    for filepath_rel in all_rel_paths:
        parts = set(os.path.normpath(filepath_rel).split(os.sep))
        if not (parts & IGNORED_BUILD_DIRS):
            filtered_rel_paths.add(filepath_rel)
    all_rel_paths = filtered_rel_paths

    # Recurse into nested git repositories and untracked directories
    expanded_paths = set()
    for filepath_rel in all_rel_paths:
        full_p = os.path.join(workspace_dir, filepath_rel)
        if os.path.isdir(full_p):
            # Check for nested git repository (git init nested)
            if os.path.isdir(os.path.join(full_p, ".git")):
                try:
                    nested_res = subprocess.run(
                        ["git", "status", "--porcelain", "-uall"],
                        cwd=full_p,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=1.0,
                        text=True
                    )
                    if nested_res.returncode == 0 and nested_res.stdout:
                        for n_line in nested_res.stdout.splitlines():
                            n_clean = n_line.strip()
                            if len(n_clean) >= 3:
                                n_path = n_clean[2:].strip()
                                expanded_paths.add(os.path.join(filepath_rel, n_path))
                except Exception:
                    pass
            # Also recurse into files inside directory
            try:
                for root, dirs, files in os.walk(full_p):
                    dirs[:] = [d for d in dirs if d not in IGNORED_BUILD_DIRS]
                    for fname in files:
                        rel_to_ws = os.path.relpath(os.path.join(root, fname), workspace_dir)
                        expanded_paths.add(rel_to_ws)
            except Exception:
                pass
        else:
            expanded_paths.add(filepath_rel)
    all_rel_paths = expanded_paths

    # Classify all discovered files
    full_paths = []
    for filepath_rel in all_rel_paths:
        parts = set(os.path.normpath(filepath_rel).split(os.sep))
        if parts & IGNORED_BUILD_DIRS:
            continue

        full_path = os.path.join(workspace_dir, filepath_rel)
        full_paths.append(full_path)
        ftype = classify_file(filepath_rel, workspace_dir=workspace_dir)
        base = os.path.basename(filepath_rel)
        if ftype == "source":
            source_files.add(base)
        elif ftype == "doc":
            doc_files.add(base)

    return source_files, doc_files, full_paths


def poll_transcript_for_step(transcript_path: str, target_step_idx: int, max_wait_ms: int = 300) -> Tuple[Optional[int], Optional[str], bool]:
    r"""
    Polls transcript for up to max_wait_ms (50ms intervals) to extract observed exit code and stdout tail.
    Anchored strictly to \A (beginning of whole string) without re.MULTILINE to eliminate stdout spoofing.
    Returns (exit_code, stdout_tail, timed_out).
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None, None, False

    # Anchored strictly to absolute start of string (\A) without MULTILINE
    system_header_regex = re.compile(
        r"\ACreated At: [^\n]+\nCompleted At: [^\n]+\n\nThe command exited with code (\d+)"
    )

    end_time = time.time() + (max_wait_ms / 1000.0)
    while True:
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                matching_lines = []
                for line in f:
                    line_s = line.strip()
                    if not line_s:
                        continue
                    try:
                        d = json.loads(line_s)
                        if d.get("type") == "GENERIC":
                            matching_lines.append(d)
                    except Exception:
                        continue

                for step in reversed(matching_lines):
                    s_idx = step.get("step_index")
                    if target_step_idx is not None and (s_idx == target_step_idx or s_idx == target_step_idx + 1 or s_idx == target_step_idx - 1):
                        content = step.get("content", "")
                        exit_m = system_header_regex.search(content)
                        if exit_m:
                            exit_code = int(exit_m.group(1))
                            lines = content.splitlines()
                            tail = "\n".join(lines[-15:])[:1000] if lines else None
                            return exit_code, tail, False

                if matching_lines:
                    last_step = matching_lines[-1]
                    content = last_step.get("content", "")
                    exit_m = system_header_regex.search(content)
                    if exit_m:
                        exit_code = int(exit_m.group(1))
                        lines = content.splitlines()
                        tail = "\n".join(lines[-15:])[:1000] if lines else None
                        return exit_code, tail, False

        except Exception:
            pass

        if time.time() >= end_time:
            break
        time.sleep(0.05)

    return None, None, True


def get_git_diff_stat(filepath: str) -> Optional[str]:
    """Extracts git diff stat for target file."""
    if not filepath or not os.path.exists(filepath):
        return None
    try:
        file_dir = os.path.dirname(os.path.abspath(filepath))
        res = subprocess.run(
            ["git", "diff", "--stat", "--", filepath],
            cwd=file_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1.0,
            text=True
        )
        stat = res.stdout.strip()
        if stat:
            return stat.splitlines()[-1].strip()
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = sum(1 for _ in f)
        return f"+{lines} lines"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PostToolUse: Ledger Recording
# ---------------------------------------------------------------------------

def handle_post_tool_use(payload: dict) -> dict:
    """
    Records ground-truth tool execution facts with real exit codes and diff stats.
    Neutralizes shell operator bypasses (; true, || exit 0).
    Non-synthesis rule: NEVER synthesizes 'exit 0' out of thin air.
    """
    payload = normalize_payload(payload)
    tool_call = payload.get("toolCall", {})
    tool_name = tool_call.get("name", "unknown")
    tool_args = tool_call.get("args", {})
    conv_id = payload.get("conversationId", "unknown")
    step_idx = payload.get("stepIdx", 0)
    error_msg = payload.get("error", None)
    transcript_path = payload.get("transcriptPath", "")

    cmd_or_file = ""
    observed_exit_code = None
    stdout_tail = None
    diff_stat = None
    harness_status = None
    tool_cwd = (
        tool_args.get("Cwd")
        or payload.get("workspace_dir")
        or payload.get("workspace")
        or payload.get("cwd")
        or get_workspace_dir(payload)
        or os.getcwd()
    )
    if tool_cwd and conv_id != "unknown" and os.path.isdir(tool_cwd):
        get_or_set_session_baseline(tool_cwd, conv_id)

    if tool_name == "run_command":
        cmd_or_file = tool_args.get("CommandLine", "")

        # Shell Operator Defense
        if is_tainted_shell_command(cmd_or_file):
            observed_exit_code = 1
            harness_status = "tainted_shell_operator"
            error_msg = f"TAINTED: Chained shell operators detected: {error_msg or ''}".strip()
        elif payload.get("exitCode") is not None:
            # Direct exitCode provided in hook payload (e.g. Claude Code or explicit harness event)
            observed_exit_code = int(payload["exitCode"])
            harness_status = "no_error" if observed_exit_code == 0 else f"exit_{observed_exit_code}"
            tr = payload.get("tool_response") or payload.get("result") or payload.get("output") or {}
            raw_out = ""
            if isinstance(tr, dict):
                raw_out = tr.get("stdout") or tr.get("output") or tr.get("text") or ""
            elif isinstance(tr, str):
                raw_out = tr
            if raw_out:
                lines = str(raw_out).splitlines()
                stdout_tail = "\n".join(lines[-15:])[:1000] if lines else None
        elif transcript_path and os.path.exists(transcript_path):
            ec, tail, timed_out = poll_transcript_for_step(transcript_path, step_idx)
            if ec is not None:
                observed_exit_code = ec
                stdout_tail = tail
                harness_status = "no_error" if ec == 0 else f"exit_{ec}"
            elif timed_out:
                observed_exit_code = None
                harness_status = "unverified_timeout"
            else:
                if error_msg:
                    m = re.search(r"exit status (\d+)", str(error_msg))
                    if m:
                        observed_exit_code = int(m.group(1))
                        harness_status = f"exit_{observed_exit_code}"
                    else:
                        harness_status = "error"
                else:
                    observed_exit_code = None
                    harness_status = "unverified_timeout"
        else:
            # Fallback when transcript path not attached (e.g. test harnesses / unit tests / direct payloads)
            is_test_harness = (
                payload.get("test_mode") is True
                or os.environ.get("HARDTRUTH_TEST_HARNESS") == "1"
                or bool(os.environ.get("PYTEST_CURRENT_TEST"))
            )
            raw_output = str(payload.get("toolOutput") or payload.get("result") or payload.get("output") or "")
            if error_msg:
                m = re.search(r"exit status (\d+)", str(error_msg))
                if m:
                    observed_exit_code = int(m.group(1))
                    harness_status = f"exit_{observed_exit_code}"
                else:
                    harness_status = "error"
            elif payload.get("exitCode") is not None:
                observed_exit_code = int(payload["exitCode"])
                harness_status = "no_error" if observed_exit_code == 0 else f"exit_{observed_exit_code}"
            else:
                observed_exit_code = None
                harness_status = "uncorroborated_execution"

    elif tool_name in ["write_to_file", "replace_file_content"]:
        cmd_or_file = tool_args.get("TargetFile", "")
        diff_stat = get_git_diff_stat(cmd_or_file)
        harness_status = "no_error" if not error_msg else "error"

    elif tool_name == "view_file":
        cmd_or_file = tool_args.get("AbsolutePath", "")
        harness_status = "no_error" if not error_msg else "error"

    record_payload = {
        "conversationId": conv_id,
        "stepIdx": step_idx,
        "tool": tool_name,
        "target": cmd_or_file,
        "observed_exit_code": observed_exit_code,
        "harness_status": harness_status,
        "error": str(error_msg) if error_msg else None,
        "stdout_tail": stdout_tail,
        "diff_stat": diff_stat,
        "cwd": tool_cwd
    }

    session_secret = get_or_create_session_secret(conv_id, tool_cwd)
    call_system_one("v1/ledger/record", record_payload, timeout=2.0, session_secret=session_secret)

    local_ledger = get_local_ledger()
    if local_ledger:
        try:
            local_ledger.record_entry(
                conversation_id=conv_id,
                step_idx=step_idx,
                tool=tool_name,
                target=cmd_or_file,
                observed_exit_code=observed_exit_code,
                harness_status=harness_status,
                error=str(error_msg) if error_msg else None,
                stdout_tail=stdout_tail,
                diff_stat=diff_stat,
                cwd=tool_cwd
            )
        except Exception:
            pass

    return {}


# ---------------------------------------------------------------------------
# Filters for Agent Prose
# ---------------------------------------------------------------------------

action_triggers = re.compile(
    r"\b((?:i(?:'ve| have)?\s+)?(?:ran|run|executed|tested|verified|fixed|modified|created|implemented|added|wrote|updated|built|refactored|completed|finished|wired up)|"
    r"ran\s+(?:unit\s+)?tests?|"
    r"(?:all|all \d+|\d+)?\s*(?:unit\s+)?tests?(?:\s+[\w/]+){0,3}\s+passed|"
    r"tests?\s+(?:have\s+)?passed|"
    r"tests?\s+are\s+passing|"
    r"test\s+suite\s+passed|"
    r"tests?\s+succeeded|"
    r"successfully\s+(?:verified|passed|tested|built|implemented)|"
    r"all\s+checks?\s+passed|"
    r"(?:feature|pipeline|integration|logic)\s+is\s+(?:now\s+)?(?:wired|working|active|complete|finished)|"
    r"zero\s+failures|10/10\s+green|suite\s+is\s+green|clean\s+test)\b",
    re.IGNORECASE
)

descriptive_filter = re.compile(
    r"^\s*(?:quote:|example:|sample:|(?:when|if|for example|e\.g\.|every time|how |the system|the hook|this means|in order to|as an example|such as|to prevent|by default|instead of|note that|on x\b|on twitter|in our search|in research|search results|discussions across|discussions on|users report|practitioners note|engineers note|people are|the community|articles|papers|studies)\b)",
    re.IGNORECASE
)

imperative_filter = re.compile(
    r"^\s*([🚨⚠️]|(?:do not|don't|ensure|inspect|execute|verify|check|run|please|must|should|critical requirement|requirement|task|step \d|turn \d|signal \d)\b|(?:tip:|note:|warning:))",
    re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Stop: Pre-Termination Verification Gate (Hybrid Two-Tier)
# ---------------------------------------------------------------------------

def handle_stop(payload: dict) -> dict:
    """
    Evaluates physical ledger state, agent claims, and executes Tier 2 Hard Gate.
    """
    payload = normalize_payload(payload)
    conv_id = payload.get("conversationId", "default")
    transcript_path = payload.get("transcriptPath", "")
    workspace_dir = get_workspace_dir(payload)

    counter_file = get_halt_counter_file(conv_id)
    halt_count = 0
    if os.path.exists(counter_file):
        try:
            with open(counter_file, "r") as f:
                halt_count = json.load(f).get("count", 0)
        except Exception:
            halt_count = 0

    def fail_halt(reason: str, remediable: bool = True) -> dict:
        new_count = halt_count + 1 if remediable else halt_count
        if remediable:
            record_halt(counter_file, new_count)
        if new_count >= 3:
            sys.stderr.write(
                f"[HARDTRUTH OPERATOR NOTICE] Escalation halt reached for session {conv_id} ({new_count} consecutive halts).\n"
                f"Counter file: {counter_file}\n"
                f"Operator remediation instructions: Inspect and fix root cause before manual reset: rm {counter_file}\n"
            )
            return _halt(
                f"🚨 HARDTRUTH ESCALATION HALT: Gate halt limit reached ({new_count} consecutive halts). "
                "HardTruth never fails open. Manual verification or human operator escalation required.\n\n"
                f"Root Cause: {reason}"
            )
        return _halt(reason)

    if _IMPORT_DEGRADED:
        return fail_halt(f"🚨 HARDTRUTH INTEGRITY ERROR: {_IMPORT_DEGRADED}. Install layout degraded, cannot verify truth safely. HardTruth fails closed.", remediable=False)

    # Extract agent's final text from transcript (fail closed on unreadable or empty transcript)
    agent_text = ""
    if transcript_path:
        if not os.path.exists(transcript_path):
            return fail_halt(f"🚨 HARDTRUTH REJECTED: Attached transcript path does not exist on disk: {transcript_path}", remediable=False)
        has_agent_response = False
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        # Antigravity schema: type == "PLANNER_RESPONSE"
                        if d.get("type") == "PLANNER_RESPONSE" and d.get("content"):
                            has_agent_response = True
                            if not d.get("tool_calls"):
                                agent_text = d.get("content")
                        # Claude Code schema: type == "assistant" or role == "assistant"
                        elif d.get("type") == "assistant" or d.get("role") == "assistant":
                            msg = d.get("message") or d
                            content = msg.get("content")
                            if isinstance(content, list):
                                text_parts = []
                                has_tool = False
                                for part in content:
                                    if isinstance(part, dict):
                                        if part.get("type") == "text":
                                            text_parts.append(part.get("text", ""))
                                        elif part.get("type") in ("tool_use", "tool_call"):
                                            has_tool = True
                                if text_parts:
                                    has_agent_response = True
                                    if not has_tool:
                                        agent_text = "\n".join(text_parts)
                            elif isinstance(content, str) and content.strip():
                                has_agent_response = True
                                agent_text = content
                    except Exception:
                        continue
        except Exception as e:
            return fail_halt(f"🚨 HARDTRUTH REJECTED: Attached transcript is unreadable: {e}", remediable=False)

        if not has_agent_response:
            return fail_halt("🚨 HARDTRUTH REJECTED: Attached transcript contains no valid agent responses. Emptying or stripping transcripts to bypass gates is forbidden.", remediable=False)

    # Fetch Ledger Premise
    premise_data = None
    session_secret = get_or_create_session_secret(conv_id, workspace_dir)
    local_ledger = get_local_ledger()
    if os.environ.get("HARDTRUTH_LEDGER_PATH") and local_ledger:
        try:
            premise_data = local_ledger.get_premise(conv_id)
        except Exception:
            pass

    if not premise_data:
        try:
            url = f"{SYSTEM_ONE_URL.rstrip('/')}/v1/ledger/premise?conversationId={urllib.parse.quote(conv_id)}"
            premise_headers = {"User-Agent": "HardTruth-Hook"}
            premise_tok = get_api_token()
            if premise_tok:
                premise_headers["Authorization"] = f"Bearer {premise_tok}"
            if session_secret:
                premise_headers["X-Session-Secret"] = session_secret
            req = urllib.request.Request(url, headers=premise_headers)
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                premise_data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass

    if not premise_data and local_ledger:
        try:
            premise_data = local_ledger.get_premise(conv_id)
        except Exception:
            pass

    if not premise_data:
        premise_data = {
            "tampered": False,
            "conversationId": conv_id,
            "premise": "No ledger records found.",
            "source_files_modified": 0,
            "doc_files_modified": 0,
            "modified_files": [],
            "verification_commands_executed": 0,
            "test_commands_executed": 0,
            "unresolved_failures": []
        }

    # Check for Ledger Tamper
    if premise_data.get("tampered"):
        return fail_halt(f"🚨 HARDTRUTH GATE HALTED: Cryptographic ledger tamper detected: {premise_data.get('detail', 'chain mismatch')}. Termination forbidden.", remediable=False)

    # Universal File Tracking (Git Porcelain + Session Baseline Commit Diff)
    try:
        git_src_files, git_doc_files, git_paths = get_git_modified_source_files(workspace_dir, conv_id=conv_id)
    except Exception as e:
        return fail_halt(f"🚨 HARDTRUTH UNDETERMINED: Git command failed or timed out during workspace verification: {e}. HardTruth fails closed.", remediable=False)

    ledger_src_count = premise_data.get("source_files_modified", 0)
    source_files_modified = max(ledger_src_count, len(git_src_files))
    verification_commands_executed = premise_data.get("verification_commands_executed", 0)
    test_commands_executed = premise_data.get("test_commands_executed", 0)
    last_source_mod_step = premise_data.get("last_source_mod_step", -1)
    last_test_step = premise_data.get("last_test_step", -1)
    unresolved_failures = premise_data.get("unresolved_failures", [])

    # Reconcile unverified timeouts against completed transcript
    if transcript_path and os.path.exists(transcript_path) and unresolved_failures:
        try:
            reconciled_fails = []
            additional_verif = 0
            additional_test = 0
            reconciled_test_step = -1

            command_successes = set()
            command_step_map = {}
            task_cmd_map = {}

            with open(transcript_path, "r", encoding="utf-8") as tf:
                pending_cmd = None
                pending_step = -1
                for tline in tf:
                    tline_s = tline.strip()
                    if not tline_s:
                        continue
                    try:
                        td = json.loads(tline_s)
                        ttype = td.get("type")
                        if ttype == "PLANNER_RESPONSE":
                            tcalls = td.get("tool_calls", [])
                            for tc in tcalls:
                                if tc.get("name") == "run_command":
                                    cargs = tc.get("args", {})
                                    raw_c = str(cargs.get("CommandLine", "")).strip()
                                    while (raw_c.startswith('"') and raw_c.endswith('"')) or (raw_c.startswith("'") and raw_c.endswith("'")):
                                        raw_c = raw_c[1:-1].strip()
                                    pending_cmd = raw_c
                                    pending_step = td.get("step_index", -1)
                        elif ttype == "GENERIC":
                            tcontent = td.get("content", "")
                            m_exit = re.search(r"\bThe command exited with code 0\b", tcontent)
                            if m_exit and pending_cmd:
                                command_successes.add(pending_cmd)
                                command_step_map[pending_cmd] = td.get("step_index", pending_step)
                            m_bg = re.search(r"[Tt]ask id:?\s*[\"']?([a-zA-Z0-9_/-]+)", tcontent)
                            if m_bg:
                                t_id_found = m_bg.group(1).strip()
                                cmd_found = pending_cmd
                                m_desc = re.search(r"Task Description:\s*(.*)", tcontent)
                                if m_desc and not cmd_found:
                                    cmd_found = m_desc.group(1).strip()
                                if cmd_found:
                                    task_cmd_map[t_id_found] = (cmd_found, pending_step)
                            pending_cmd = None
                        elif ttype in ("USER_INPUT", "GENERIC", "SYSTEM_MESSAGE"):
                            tcontent = str(td.get("content", ""))
                            m_fin = re.search(r'Task id\s+"?([a-zA-Z0-9_/-]+)"?\s+finished with result:.*?[Tt]he command exited with code (\d+)', tcontent, re.DOTALL)
                            if m_fin:
                                t_id = m_fin.group(1).strip()
                                t_ec = int(m_fin.group(2))
                                if t_ec == 0 and t_id in task_cmd_map:
                                    bg_cmd, bg_step = task_cmd_map[t_id]
                                    command_successes.add(bg_cmd)
                                    command_step_map[bg_cmd] = td.get("step_index", bg_step)
                    except Exception:
                        continue

            for fail in unresolved_failures:
                cmd_target = fail.get("command", "").strip()
                err = fail.get("error")
                status = fail.get("status")

                is_resolved = False
                resolving_step = -1
                for succ in command_successes:
                    if succ == cmd_target:
                        is_resolved = True
                        resolving_step = command_step_map.get(succ, -1)
                        break
                    # Suite encompassment: if succ is pytest tests/ and failed was pytest
                    if cmd_target == "pytest" and succ.startswith("pytest"):
                        is_resolved = True
                        resolving_step = command_step_map.get(succ, -1)
                        break
                    if cmd_target == "pytest tests/" and (succ == "pytest tests/" or succ == "pytest"):
                        is_resolved = True
                        resolving_step = command_step_map.get(succ, -1)
                        break
                    if ("pytest tests" in succ or succ == "pytest") and cmd_target.startswith("pytest"):
                        is_resolved = True
                        resolving_step = command_step_map.get(succ, -1)
                        break
                    if "unittest discover" in succ and "unittest" in cmd_target:
                        is_resolved = True
                        resolving_step = command_step_map.get(succ, -1)
                        break
                    if cmd_target in succ:
                        is_resolved = True
                        resolving_step = command_step_map.get(succ, -1)
                        break

                if is_resolved:
                    additional_verif += 1
                    if is_test_execution_command(cmd_target):
                        additional_test += 1
                        reconciled_test_step = max(reconciled_test_step, resolving_step)
                else:
                    reconciled_fails.append(fail)

            unresolved_failures = reconciled_fails
            verification_commands_executed += additional_verif
            test_commands_executed += additional_test
            if reconciled_test_step > last_test_step:
                last_test_step = reconciled_test_step
        except Exception:
            pass
    premise_str = premise_data.get("premise", "")
    modified_paths = premise_data.get("modified_file_paths", []) or premise_data.get("modified_files", [])
    if git_paths:
        modified_paths = list(set(modified_paths) | set(git_paths))
        all_mod_names = sorted(list(set(os.path.basename(p) for p in modified_paths)))
        if all_mod_names:
            if "MODIFIED FILES: No files were modified." in premise_str:
                premise_str = premise_str.replace(
                    "MODIFIED FILES: No files were modified.",
                    f"MODIFIED FILES: {', '.join(all_mod_names)}."
                )
            elif "MODIFIED FILES:" not in premise_str:
                premise_str += f" MODIFIED FILES: {', '.join(all_mod_names)}."

    # Evasion E4 Check: Git History Lineage (merge-base ancestor check)
    if workspace_dir and os.path.exists(os.path.join(workspace_dir, ".git")):
        ws_abs = os.path.abspath(workspace_dir)
        baseline_sha = get_or_set_session_baseline(workspace_dir, conv_id)
        if baseline_sha:
            try:
                anc_res = subprocess.run(
                    ["git", "-c", f"safe.directory={ws_abs}", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
                     "merge-base", "--is-ancestor", baseline_sha, "HEAD"],
                    cwd=workspace_dir,
                    timeout=5.0
                )
                if anc_res.returncode != 0:
                    return fail_halt(f"🚨 HARDTRUTH GATE HALTED (EVASION E4 DETECTED): Current HEAD is not a descendant of session baseline commit {baseline_sha[:10]}. Branch switching or history rewrites to hide changes are forbidden.")
            except subprocess.TimeoutExpired:
                return fail_halt("🚨 HARDTRUTH UNDETERMINED: Git merge-base lineage check timed out. HardTruth fails closed.", remediable=False)
            except Exception as e:
                return fail_halt(f"🚨 HARDTRUTH UNDETERMINED: Git merge-base lineage check failed: {e}. HardTruth fails closed.", remediable=False)

    # Evasion E1 Check: Git Stash Detection
    if workspace_dir and os.path.exists(os.path.join(workspace_dir, ".git")):
        try:
            stash_res = subprocess.run(
                ["git", "stash", "list", "--format=%gd %ct"],
                cwd=workspace_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=2.0,
                text=True
            )
            if stash_res.returncode == 0 and stash_res.stdout.strip():
                session_start = premise_data.get("created_at", 0)
                active_stash = False
                stash_ref = ""
                for line in stash_res.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        stash_time = int(parts[1])
                        if (session_start > 0 and stash_time >= (session_start - 5)) or (session_start <= 0 and (time.time() - stash_time) < 60):
                            active_stash = True
                            stash_ref = parts[0]
                            break
                    else:
                        continue

                if active_stash:
                    return fail_halt(f"🚨 HARDTRUTH GATE HALTED (EVASION DETECTED): Active git stash detected ('{stash_ref}') created during this session. Stashing changes to bypass dirty-tree verification is forbidden. Restore your stashed changes with 'git stash pop' and execute tests before completing.")
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Rule 3: Polyglot Anti-Stubbing Linter on Modified Source Files
    # -----------------------------------------------------------------------
    baseline_sha = get_or_set_session_baseline(workspace_dir, conv_id) if workspace_dir else None
    for fpath in modified_paths:
        if os.path.exists(fpath):
            mod_lines = get_changed_lines(workspace_dir, fpath, baseline_sha)
            stubs = check_ast_stubs(fpath, modified_lines=mod_lines)
            if stubs:
                return fail_halt(f"🚨 HARDTRUTH ENGINE REJECTED: Unimplemented stub detected. {stubs[0]}")

    # -----------------------------------------------------------------------
    # Rule 1: Source Code Changes Require Test Proof (Language-Independent)
    # -----------------------------------------------------------------------
    if source_files_modified > 0:
        if test_commands_executed == 0:
            return fail_halt("🚨 HARDTRUTH GATE HALTED: Source code files were modified in this conversation, but NO verification commands (tests) were executed. You must execute tests (e.g. pytest, npm test, cargo test, go test) to verify your changes before stopping.")
        if last_source_mod_step > 0 and last_test_step > 0 and last_source_mod_step > last_test_step:
            return fail_halt(f"🚨 HARDTRUTH GATE HALTED: Source code was modified at step {last_source_mod_step} after the last test run at step {last_test_step}. You must re-run your test suite to verify the latest changes before stopping.")

    # -----------------------------------------------------------------------
    # Rule 2: Unresolved Verification Failures Forbid Termination (Language-Independent)
    # -----------------------------------------------------------------------
    if len(unresolved_failures) > 0:
        fails_summary = "; ".join([
            f"'{u.get('command')}' (exit {u.get('observed_exit_code')})"
            for u in unresolved_failures
        ])
        return fail_halt(f"🚨 HARDTRUTH GATE HALTED (CONTRADICTION DETECTED): Unresolved test failures exist in the ledger: [{fails_summary}]. Fix the failures and re-run tests before stopping.")

    # -----------------------------------------------------------------------
    # Rule 5: Test-Surface Monotonicity (Assertion Weakening / Skipping Defense)
    # -----------------------------------------------------------------------
    test_weakening_violations = []
    baseline_sha = None
    if conv_id and workspace_dir and os.path.exists(os.path.join(workspace_dir, ".git")):
        baseline_sha = get_or_set_session_baseline(workspace_dir, conv_id)

    for fpath in modified_paths:
        base_name = os.path.basename(fpath).lower()
        if ("test" in base_name or "spec" in base_name) and os.path.isfile(fpath):
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as tf:
                    tcontent = tf.read()

                changed_lines = get_changed_lines(workspace_dir, fpath, baseline_sha)
                lines = tcontent.splitlines()
                for ln_idx, line_str in enumerate(lines, start=1):
                    if changed_lines is not None and ln_idx not in changed_lines:
                        continue
                    if re.search(r"\bassert\s+(?:True|1\s*==\s*1)\b", line_str):
                        test_weakening_violations.append(f"Tautological assertion ('assert True') found in {base_name}:{ln_idx}")
                    elif re.search(r"\b(?:self\.)?assertTrue\s*\(\s*(?:True|1)\s*\)", line_str):
                        test_weakening_violations.append(f"Tautological assertion ('assertTrue(True)') found in {base_name}:{ln_idx}")
                    elif re.search(r"\b(?:self\.)?assertEqual\s*\(\s*(?:True|1)\s*,\s*(?:True|1)\s*\)", line_str):
                        test_weakening_violations.append(f"Tautological assertion ('assertEqual(True, True)') found in {base_name}:{ln_idx}")
                    elif re.search(r"\b(?:self\.)?assertFalse\s*\(\s*(?:False|0)\s*\)", line_str):
                        test_weakening_violations.append(f"Tautological assertion ('assertFalse(False)') found in {base_name}:{ln_idx}")
                    elif re.search(r"@pytest\.mark\.(?:skip|xfail)\b", line_str):
                        test_weakening_violations.append(f"Skip marker ('@pytest.mark.skip') found in {base_name}:{ln_idx}")
                    elif re.search(r"\b(?:test\.skip|it\.skip|xit\()\b", line_str):
                        test_weakening_violations.append(f"Test skipping marker ('test.skip') found in {base_name}:{ln_idx}")
                    elif re.search(r"\bexpect\s*\(\s*(?:true|1)\s*\)\.to(?:Be|Equal)\s*\(\s*(?:true|1)\s*\)", line_str, re.IGNORECASE):
                        test_weakening_violations.append(f"Tautological assertion ('expect(true).toBe(true)') found in {base_name}:{ln_idx}")
                    elif re.search(r"\bassert(?:ion)?\s*\.\s*(?:equal|strictEqual|deepEqual)\s*\(\s*(?:true|1)\s*,\s*(?:true|1)\s*\)", line_str, re.IGNORECASE):
                        test_weakening_violations.append(f"Tautological assertion ('assert.equal(true, true)') found in {base_name}:{ln_idx}")
                    elif re.search(r"\bassert!\s*\(\s*true\s*\)", line_str):
                        test_weakening_violations.append(f"Tautological assertion ('assert!(true)') found in {base_name}:{ln_idx}")
                    elif re.search(r"\bt\.(?:Skip|SkipNow|Skipf)\s*\(", line_str):
                        test_weakening_violations.append(f"Test skipping call ('t.Skip()') found in {base_name}:{ln_idx}")

                # Assertion count comparison against git baseline blob
                if conv_id and workspace_dir and os.path.exists(os.path.join(workspace_dir, ".git")):
                    if baseline_sha:
                        rel_path = os.path.relpath(fpath, workspace_dir)
                        proc_base = subprocess.run(
                            ["git", "show", f"{baseline_sha}:{rel_path}"],
                            cwd=workspace_dir,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=2.0,
                            text=True
                        )
                        if proc_base.returncode == 0:
                            base_content = proc_base.stdout
                            base_asserts = len(re.findall(r"\b(?:assert|expect|self\.assert)\b", base_content))
                            curr_asserts = len(re.findall(r"\b(?:assert|expect|self\.assert)\b", tcontent))
                            if base_asserts >= 4 and curr_asserts < (base_asserts // 2):
                                test_weakening_violations.append(f"Severe assertion deletion detected in {base_name} ({curr_asserts} assertions remaining vs {base_asserts} in baseline)")
            except Exception:
                pass

    if test_weakening_violations:
        return fail_halt(f"🚨 HARDTRUTH REJECTED (TEST WEAKENING DETECTED): {test_weakening_violations[0]}. Weakening test assertions or skipping tests to pass the gate is forbidden.")

    # -----------------------------------------------------------------------
    # Tier 2: External Deterministic Hard Gate Handoff
    # Only triggered if source files were modified or tests were executed
    # -----------------------------------------------------------------------
    if (source_files_modified > 0 or test_commands_executed > 0) and os.environ.get("HARDTRUTH_SKIP_TIER2") != "1" and HARNESS != "antigravity":
        if not workspace_dir:
            return fail_halt(
                "🚨 HARDTRUTH UNDETERMINED: No workspace path was resolvable, so the Tier 2 "
                "hard gate and git integrity checks could not run. HardTruth never fails open.",
                remediable=False
            )
        tier2_result = call_system_one("v1/verify/handoff", {
            "workspace_path": workspace_dir,
            "conversationId": conv_id,
            "timeout_sec": 50
        }, timeout=55.0)

        if tier2_result is None:
            return fail_halt(
                f"🚨 HARDTRUTH UNDETERMINED: Tier 2 verifier daemon at {SYSTEM_ONE_URL} is unreachable. "
                "HardTruth will not execute untrusted workspace test code inside the host agent process. "
                "Ensure the HardTruth verification daemon is running.",
                remediable=False
            )

        if not tier2_result.get("success"):
            if tier2_result.get("status") != "unverified_no_workspace":
                runner = tier2_result.get("runner", "external runner")
                ec = tier2_result.get("exit_code")
                out_tail = (tier2_result.get("output", "") or "")[:600]
                return fail_halt(f"🚨 TIER 2 HARD GATE FAILED: The external deterministic runner '{runner}' failed in a clean environment (exit {ec}). Fix the following issues before completing:\n\n{out_tail}")

    # If physical gates passed and no agent prose exists, check transcript requirement
    if not agent_text:
        if not transcript_path and (source_files_modified > 0 or verification_commands_executed > 0):
            return fail_halt("🚨 HARDTRUTH UNDETERMINED: Stop requested without transcriptPath when source code modifications or verification activity exist. Missing transcript prevents prose adjudication. HardTruth fails closed.", remediable=False)
        if os.path.exists(counter_file):
            try:
                os.remove(counter_file)
            except Exception:
                pass
        return _allow()

    # -----------------------------------------------------------------------
    # Rule 4A: Deterministic Claim-to-Action Grounding (Unstripped Prose)
    # -----------------------------------------------------------------------
    claimed_file_refs = set(re.findall(r"(?:`|\b)((?:[a-zA-Z0-9_.-]+/)*[a-zA-Z0-9_.-]+\.[a-zA-Z0-9_-]+)(?:`|\b)", agent_text))
    clauses = re.split(r"(?<=[.!?])\s+|\n+|[,;]\s*(?:which|that|whereas|although|because|since)\b", agent_text, flags=re.IGNORECASE)
    for ref in claimed_file_refs:
        norm_ref = os.path.normpath(ref)
        if any(norm_ref.endswith(ext) for ext in SOURCE_CODE_EXTENSIONS):
            pattern = re.compile(
                rf"\b(?:created|wrote|implemented|modified|updated|edited|added|fixed|built|patched)\b[^\n.!?]{{0,120}}?\b{re.escape(ref)}\b|"
                rf"\b{re.escape(ref)}\b[^\n.!?]{{0,120}}?\b(?:is now|was|has been|to add|to implement)\s+(?:created|written|implemented|modified|updated|edited|added|fixed|built|patched)\b|"
                rf"(?:^|\s)[-*]\s*(?:(?:created|wrote|implemented|modified|updated|edited|added|fixed|built|patched)\b[^\n]{{0,120}}?\b{re.escape(ref)}\b|\b{re.escape(ref)}\b[^\n]{{0,120}}?\b(?:created|wrote|implemented|modified|updated|edited|added|fixed|built|patched)\b)",
                re.IGNORECASE
            )
            if any(pattern.search(clause) for clause in clauses):
                is_modified = any(
                    norm_ref == os.path.normpath(m) or os.path.normpath(m).endswith(os.sep + norm_ref)
                    for m in modified_paths
                )
                has_real_content = False
                if is_modified and workspace_dir:
                    full_fpath = os.path.join(workspace_dir, norm_ref) if not os.path.isabs(norm_ref) else norm_ref
                    if os.path.isfile(full_fpath):
                        try:
                            if os.path.getsize(full_fpath) > 0:
                                has_real_content = True
                        except Exception:
                            has_real_content = True
                    else:
                        has_real_content = True
                elif is_modified:
                    has_real_content = True

                if not is_modified or not has_real_content:
                    return fail_halt(f"🚨 HARDTRUTH REJECTED (UNVERIFIED CLAIM): You claimed to have created or modified '{ref}', but git and the ledger show no non-trivial modifications to this file in this session. Write and apply the code to disk before completing.")

    # -----------------------------------------------------------------------
    # Rule 4B: Targeted DeBERTa-v3 NLI Claim Adjudication
    # -----------------------------------------------------------------------
    text_without_fences = re.sub(r"```[\s\S]*?```", "", agent_text)
    text_clean = text_without_fences.replace("`", "")
    text_clean = re.sub(r"^\s*>\s*", "", text_clean, flags=re.MULTILINE)

    sentences = re.split(r"(?<=[.!?])\s+|\n+", text_clean)
    claims_to_verify = []
    for s in sentences:
        s_clean = s.strip()
        if len(s_clean) > 15 and action_triggers.search(s_clean):
            if imperative_filter.search(s_clean):
                continue
            if re.search(r"\b(?:documentation|docs|readme|changelog)\b", s_clean, re.IGNORECASE) and not re.search(r"\b(?:test|tests|passed|suite|feature|pipeline|integration|bug)\b", s_clean, re.IGNORECASE):
                continue
            if descriptive_filter.search(s_clean):
                prefix_match = re.match(r"^\s*(?:quote:|example:|sample:)\s*", s_clean, re.IGNORECASE)
                if prefix_match:
                    remainder = s_clean[prefix_match.end():].strip()
                    is_completion = bool(re.search(
                        r"\b((all\s+\d+|\d+)\s+(unit\s+)?tests?\s+passed|passed\s+completely|suite\s+is\s+green|100%\s+(success|passing)|all\s+checks?\s+passed|(feature|pipeline|integration)\s+is\s+(now\s+)?(wired|working|active)|i\s+(have\s+)?(ran|run|executed|tested|verified|fixed))\b",
                        remainder,
                        re.IGNORECASE
                    ))
                    if not is_completion:
                        continue
                else:
                    continue
            claims_to_verify.append(s_clean[:250])

    # Prioritize completion and test outcome claims first (A15)
    def claim_priority(c: str) -> int:
        if re.search(r"\b(?:test|passed|failing|green|100%|suite|fixed)\b", c, re.IGNORECASE):
            return 0
        return 1
    claims_to_verify.sort(key=claim_priority)
    claims_to_verify = claims_to_verify[:8]

    has_test_pass_claim = any(
        re.search(r"\b((all|all \d+|\d+)?\s*(unit\s+)?tests?(\s+[\w/]+){0,3}\s+passed|tests?\s+(have\s+)?passed|unit\s+tests?\s+passed|tests?\s+succeeded|test\s+suite\s+passed|tests?\s+are\s+passing)\b", c, re.IGNORECASE)
        for c in claims_to_verify
    )
    if has_test_pass_claim:
        if test_commands_executed == 0:
            return fail_halt("🚨 HARDTRUTH ENGINE HALTED: You claimed tests passed, but the execution ledger shows NO commands or test suites were run in this session. Running linters does not count as running tests. Execute the test command with run_command to prove it before completing.")
        if last_source_mod_step > 0 and last_test_step > 0 and last_source_mod_step > last_test_step:
            return fail_halt(f"🚨 HARDTRUTH ENGINE HALTED: You claimed tests passed, but source code was modified at step {last_source_mod_step} after the last test run at step {last_test_step}. Re-run tests to prove they pass on the latest code.")

    for claim in claims_to_verify:
        nli_res = call_system_one("v1/verify-claim", {
            "premise": premise_str,
            "hypothesis": claim,
            "threshold": CONTRADICTION_THRESHOLD
        })

        if nli_res is not None:
            probs = nli_res.get("probabilities", {})
            contradiction = probs.get("contradiction", 0.0)
            if contradiction >= CONTRADICTION_THRESHOLD:
                return fail_halt(f"🚨 HARDTRUTH ENGINE CONTRADICTION DETECTED (conf: {contradiction:.2f}):\nClaim: '{claim}' contradicts the execution ledger.\nLedger Evidence: {premise_str}\nFix the failure and provide verified command output before stopping.")
        else:
            if test_commands_executed > 0 and len(unresolved_failures) == 0:
                pass
            else:
                return fail_halt(f"🚨 HARDTRUTH DAEMON UNREACHABLE: Verifier at {SYSTEM_ONE_URL} is offline. Factual claim '{claim}' cannot be verified autonomously. You must provide manual verification output before completing.", remediable=False)

    # All tiers passed: reset counter file and session key
    if os.path.exists(counter_file):
        try:
            os.remove(counter_file)
        except Exception:
            pass

    sec_file = get_session_secret_file(conv_id)
    if os.path.exists(sec_file):
        try:
            os.remove(sec_file)
        except Exception:
            pass

    return _allow()


def record_halt(counter_file: str, new_count: int):
    try:
        with open(counter_file, "w") as f:
            json.dump({"count": new_count, "timestamp": time.time()}, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "stop"

    try:
        raw_in = sys.stdin.read()
        payload = json.loads(raw_in) if raw_in.strip() else {}
    except Exception:
        payload = {}
    payload = normalize_payload(payload)

    try:
        if mode in ["post_tool", "PostToolUse"]:
            out = handle_post_tool_use(payload)
        else:
            out = handle_stop(payload)
    except Exception as e:
        out = _halt(f"🚨 HARDTRUTH INTERNAL ERROR (FAIL-CLOSED): {e}. Termination forbidden.")

    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
