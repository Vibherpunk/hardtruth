#!/usr/bin/env python3
"""HardTruth guard for the Amuse host terminal executor.

Python 3.9 stdlib-only: runs inside /usr/bin/python3 under com.amuse.terminal-executor.
Enforces pre-execution shell safety (exit-code masking, local Port 8000 quarantine,
HardTruth state-tamper prevention) and records command receipts to the HardTruth v2
daemon ledger (127.0.0.1:49281) and Rust hardtruth-gate UDS daemon.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Optional, Tuple

DEFAULT_DAEMON_URL = "http://127.0.0.1:49281"
DEFAULT_API_KEY_PATH = os.path.expanduser("~/.hardtruth/daemon_api.key")
DEFAULT_GATE_BIN = os.path.expanduser("~/.local/bin/hardtruth-gate")

_TEST_RUNNER_RE = re.compile(
    r"(?:^|[;&|\s])(?:pytest|py\.test|vitest|cargo\s+test|go\s+test|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:--\S+\s+)*test|python3?(?:\.\d+)?\s+-m\s+(?:pytest|unittest))\b",
    re.IGNORECASE,
)

_EXIT_MASK_PATTERNS = (
    (re.compile(r"\|\|\s*(?:true|:|exit\s+0)\b"), "|| true / || : / || exit 0"),
    (re.compile(r";\s*(?:true|:|exit\s+0)\s*(?:;|$)"), "; true / ; : / ; exit 0"),
    (re.compile(r"&&\s*(?:true|exit\s+0)\s*(?:;|$)"), "&& true / && exit 0"),
    (re.compile(r"\bset\s+\+e\b"), "set +e"),
)

_TEST_PIPE_MASK_RE = re.compile(r"\|\s*(?:cat|true|grep|sed|head|tail)\b")

_LOCAL_PORT_8000_PATTERNS = (
    re.compile(r"(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1?\]):8000\b", re.IGNORECASE),
    re.compile(r"(?:--port|-p)(?:\s+|=)8000\b", re.IGNORECASE),
    re.compile(r"\blsof\b[^\n;|&]*\b8000\b", re.IGNORECASE),
    re.compile(r"\bkill(?:all)?\b[^\n;|&]*\b8000\b", re.IGNORECASE),
)

_TAMPER_PATTERNS = (
    (re.compile(r"(?:\.hardtruth[/\\]|^|\s)daemon_api\.key\b"), "HardTruth daemon API key access"),
    (re.compile(r"(?:\.hardtruth[/\\]|^|\s)daemon_hmac\.key\b"), "HardTruth HMAC key access"),
    (re.compile(r"(?:\.hardtruth[/\\]|^|\s)daemon_ledger\.jsonl\b"), "HardTruth live ledger access"),
    (re.compile(r"\.hardtruth[/\\]halts\b"), "HardTruth halt directory manipulation"),
    (re.compile(r"refs/hardtruth/"), "HardTruth baseline git ref manipulation"),
    (re.compile(r"\bpurge_records\b"), "HardTruth ledger purge"),
    (re.compile(r"\bHARDTRUTH_TIER2_ALLOW_SUBPROCESS\s*=\s*1\b"), "HardTruth Tier 2 isolation override"),
)


def is_test_command(command: str) -> bool:
    return bool(_TEST_RUNNER_RE.search(command or ""))


def evaluate_command(command: str) -> Tuple[bool, Optional[str]]:
    """Pre-execution gate for shell commands proposed to amuse-terminal."""
    cmd = (command or "").strip()
    if not cmd:
        return False, "HARDTRUTH BLOCKED: empty command."

    norm = cmd.replace("\r\n", " ").replace("\n", " ")

    for pattern, label in _EXIT_MASK_PATTERNS:
        if pattern.search(norm):
            return False, f"HARDTRUTH BLOCKED (exit-code masking): '{label}' is forbidden."

    if is_test_command(norm) and _TEST_PIPE_MASK_RE.search(norm) and "pipefail" not in norm:
        return (
            False,
            "HARDTRUTH BLOCKED (exit-code masking): piping a test runner into cat/true/grep/sed/head/tail without pipefail masks test exit codes.",
        )

    for pattern in _LOCAL_PORT_8000_PATTERNS:
        if pattern.search(norm):
            return (
                False,
                "HARDTRUTH BLOCKED (Port 8000 quarantine): local port 8000 is hardware-quarantined and cannot be bound, probed, or signaled.",
            )

    for pattern, label in _TAMPER_PATTERNS:
        if pattern.search(norm):
            return False, f"HARDTRUTH BLOCKED (integrity guard): {label} is forbidden."

    return True, None


def _load_api_token(api_key_path: Optional[str] = None) -> str:
    path = os.path.expanduser(api_key_path or os.environ.get("HARDTRUTH_API_KEY") or DEFAULT_API_KEY_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    return (os.environ.get("HARDTRUTH_API_TOKEN") or "").strip()


def record_execution(
    conversation_id: str,
    command: str,
    cwd: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    daemon_url: Optional[str] = None,
    api_key_path: Optional[str] = None,
) -> bool:
    """Record a completed terminal execution to the HardTruth v2 daemon ledger."""
    conv = (conversation_id or "amuse-terminal").strip() or "amuse-terminal"
    base = (daemon_url or os.environ.get("HARDTRUTH_URL") or os.environ.get("SYSTEM_ONE_URL") or DEFAULT_DAEMON_URL).rstrip("/")
    token = _load_api_token(api_key_path)
    if not token:
        return False

    combined_tail = (stdout or "") + (("\n" + stderr) if stderr else "")
    err_tail = (stderr or stdout or "")[-500:] if exit_code != 0 else None

    payload = json.dumps(
        {
            "conversationId": conv,
            "stepIdx": 0,
            "tool": "run_command",
            "target": (command or "")[:2000],
            "observed_exit_code": int(exit_code),
            "harness_status": "no_error" if exit_code == 0 else "error",
            "error": err_tail,
            "stdout_tail": combined_tail[-1500:],
            "cwd": cwd,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{base}/v1/ledger/record",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    recorded = False
    try:
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            recorded = 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, OSError, ValueError):
        recorded = False

    if exit_code == 0 and is_test_command(command):
        gate_bin = DEFAULT_GATE_BIN if os.path.exists(DEFAULT_GATE_BIN) else shutil.which("hardtruth-gate")
        if gate_bin:
            try:
                subprocess.run(
                    [gate_bin, "receipt", "tests passed", (command or "")[:500], "0", "--root", cwd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                    check=False,
                )
            except Exception:
                pass

    return recorded
