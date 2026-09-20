#!/usr/bin/env python3
"""
HardTruth: Autonomous Anti-Hallucination & Physical Truth Enforcement Engine
Lifecycle Hook: PostToolUse & Stop
Powered by DeBERTa-v3 Cross-Encoder & Cryptographically Chained Execution Ledger (http://127.0.0.1:8000)

Enforces:
1. Physical Ledger Integrity: HMAC-SHA256 chained, daemon-owned, failure-biased ledger.
2. Real Exit Codes & Non-Synthesis: Extracts real exit codes from transcript; never synthesizes exit 0.
3. Inverted Language-Independent Gate:
   - AST Anti-Stubbing Linter on modified code -> HALT on stubs.
   - Rule 1: Source code modified without test execution -> HALT.
   - Rule 2: Unresolved test failures remain in ledger -> HALT.
   - Rule 4: Targeted DeBERTa-v3 NLI Claim Adjudication -> HALT if contradiction >= tau*.
4. Hard Escalation Halt: Never fails open after 3 consecutive halts.
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
from typing import Optional, Dict, Any, List, Tuple

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

try:
    from daemon.ledger import DaemonLedger, is_verification_command, classify_file
except ImportError:
    try:
        from ledger import DaemonLedger, is_verification_command, classify_file
    except ImportError:
        DaemonLedger = None
        is_verification_command = lambda cmd: False
        classify_file = lambda path: "other"

CONTRADICTION_THRESHOLD = 0.70
SYSTEM_ONE_URL = os.environ.get("SYSTEM_ONE_URL", "http://127.0.0.1:8000")

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


def call_system_one(endpoint: str, payload: dict, timeout: float = 3.0) -> Optional[dict]:
    url = f"{SYSTEM_ONE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def poll_transcript_for_step(transcript_path: str, target_step_idx: int, max_wait_ms: int = 300) -> Tuple[Optional[int], Optional[str]]:
    """
    Polls transcript for up to max_wait_ms (50ms intervals) to extract observed exit code and stdout tail.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None, None

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
                    if target_step_idx is not None and step.get("step_index") == target_step_idx:
                        content = step.get("content", "")
                        exit_m = re.search(r"The command exited with code (\d+)", content)
                        exit_code = int(exit_m.group(1)) if exit_m else None
                        lines = content.splitlines()
                        tail = "\n".join(lines[-15:])[:1000] if lines else None
                        return exit_code, tail

                if matching_lines:
                    last_step = matching_lines[-1]
                    content = last_step.get("content", "")
                    exit_m = re.search(r"The command exited with code (\d+)", content)
                    if exit_m:
                        exit_code = int(exit_m.group(1))
                        lines = content.splitlines()
                        tail = "\n".join(lines[-15:])[:1000] if lines else None
                        return exit_code, tail

        except Exception:
            pass

        if time.time() >= end_time:
            break
        time.sleep(0.05)

    return None, None


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
    Non-synthesis rule: NEVER synthesizes 'exit 0' out of thin air.
    Always records to local ledger file as well as daemon for high-availability & testing.
    """
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

    if tool_name == "run_command":
        cmd_or_file = tool_args.get("CommandLine", "")
        ec, tail = poll_transcript_for_step(transcript_path, step_idx)
        if ec is not None:
            observed_exit_code = ec
            stdout_tail = tail
            harness_status = "no_error" if ec == 0 else f"exit_{ec}"
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
                harness_status = "no_error"

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
        "diff_stat": diff_stat
    }

    # Attempt POST to daemon
    call_system_one("v1/ledger/record", record_payload, timeout=2.0)

    # Always ensure local ledger receives the record
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
                diff_stat=diff_stat
            )
        except Exception:
            pass

    return {}


# ---------------------------------------------------------------------------
# Filters for Agent Prose
# ---------------------------------------------------------------------------

action_triggers = re.compile(
    r"\b(i\s+(have\s+)?(ran|run|executed|tested|verified|fixed|modified|created)|"
    r"ran\s+(unit\s+)?tests?|"
    r"(all|all \d+|\d+)?\s*(unit\s+)?tests?(\s+[\w/]+){0,3}\s+passed|"
    r"tests?\s+(have\s+)?passed|"
    r"tests?\s+are\s+passing|"
    r"test\s+suite\s+passed|"
    r"tests?\s+succeeded|"
    r"successfully\s+(verified|passed|tested)|"
    r"all\s+checks?\s+passed|"
    r"(feature|pipeline|integration)\s+is\s+(now\s+)?(wired|working|active)|"
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
# Stop: Pre-Termination Verification Gate
# ---------------------------------------------------------------------------

def handle_stop(payload: dict) -> dict:
    """
    Evaluates physical ledger state and agent claims.
    Gate Inversion Policy:
    1. Check Circuit Breaker / Escalation Policy (Never Fail Open).
    2. AST Anti-Stubbing Linter -> HALT on stubs.
    3. Rule 1: Source code modified without test execution -> HALT.
    4. Rule 2: Unresolved test failures remain in ledger -> HALT.
    5. Rule 4: Targeted DeBERTa-v3 NLI Claim Adjudication.
    """
    conv_id = payload.get("conversationId", "default")
    transcript_path = payload.get("transcriptPath", "")

    counter_file = get_halt_counter_file(conv_id)
    halt_count = 0
    if os.path.exists(counter_file):
        try:
            with open(counter_file, "r") as f:
                halt_count = json.load(f).get("count", 0)
        except Exception:
            halt_count = 0

    # Escalation Policy: Strike 3+ triggers hard escalation halt (Never fails open)
    if halt_count >= 3:
        if os.environ.get("HARDTRUTH_CIRCUIT_BREAKER_LEGACY_ALLOW") == "1":
            try:
                os.remove(counter_file)
            except Exception:
                pass
            return {
                "decision": "allow",
                "reason": "⚠️ HARDTRUTH CIRCUIT BREAKER RELEASE: Gate released after 3 consecutive halts to prevent infinite loop."
            }
        else:
            return {
                "decision": "continue",
                "reason": "🚨 HARDTRUTH ESCALATION HALT: Gate halt limit reached (3 consecutive halts). Automated release is disabled because HardTruth never fails open. Manual verification or human escalation required."
            }

    # Extract agent's final text from transcript
    agent_text = ""
    if transcript_path and os.path.exists(transcript_path):
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        if d.get("type") == "PLANNER_RESPONSE" and d.get("content") and not d.get("tool_calls"):
                            agent_text = d.get("content")
                    except Exception:
                        continue
        except Exception:
            pass

    if not agent_text:
        return {"decision": "allow"}

    # Fetch Ledger Premise: first check local ledger if isolated path specified, or query daemon
    premise_data = None
    local_ledger = get_local_ledger()
    if os.environ.get("HARDTRUTH_LEDGER_PATH") and local_ledger:
        try:
            premise_data = local_ledger.get_premise(conv_id)
        except Exception:
            pass

    if not premise_data:
        try:
            url = f"{SYSTEM_ONE_URL.rstrip('/')}/v1/ledger/premise?conversationId={urllib.parse.quote(conv_id)}"
            req = urllib.request.Request(url, headers={"User-Agent": "HardTruth-Hook"})
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
            "unresolved_failures": []
        }

    # Check for Ledger Tamper
    if premise_data.get("tampered"):
        record_halt(counter_file, halt_count + 1)
        return {
            "decision": "continue",
            "reason": f"🚨 HARDTRUTH GATE HALTED: Cryptographic ledger tamper detected: {premise_data.get('detail', 'chain mismatch')}. Termination forbidden."
        }

    source_files_modified = premise_data.get("source_files_modified", 0)
    verification_commands_executed = premise_data.get("verification_commands_executed", 0)
    unresolved_failures = premise_data.get("unresolved_failures", [])
    premise_str = premise_data.get("premise", "")
    modified_files = premise_data.get("modified_files", [])
    modified_paths = premise_data.get("modified_file_paths", []) or modified_files

    # -----------------------------------------------------------------------
    # Rule 3: AST Anti-Stubbing Linter on Modified Source Files (Check First)
    # -----------------------------------------------------------------------
    for fpath in modified_paths:
        if fpath.endswith(".py") and os.path.exists(fpath):
            stubs = check_ast_stubs(fpath)
            if stubs:
                record_halt(counter_file, halt_count + 1)
                return {
                    "decision": "continue",
                    "reason": f"🚨 HARDTRUTH ENGINE REJECTED: Unimplemented stub detected. {stubs[0]}"
                }

    # -----------------------------------------------------------------------
    # Rule 1: Source Code Changes Require Test Proof (Language-Independent)
    # -----------------------------------------------------------------------
    if source_files_modified > 0 and verification_commands_executed == 0:
        record_halt(counter_file, halt_count + 1)
        return {
            "decision": "continue",
            "reason": "🚨 HARDTRUTH GATE HALTED: Source code files were modified in this conversation, but NO verification commands or test suites were executed. You must execute tests to verify your changes before stopping."
        }

    # -----------------------------------------------------------------------
    # Rule 2: Unresolved Verification Failures Forbid Termination (Language-Independent)
    # -----------------------------------------------------------------------
    if len(unresolved_failures) > 0:
        record_halt(counter_file, halt_count + 1)
        fails_summary = "; ".join([
            f"'{u.get('command')}' (exit {u.get('observed_exit_code')})"
            for u in unresolved_failures
        ])
        return {
            "decision": "continue",
            "reason": f"🚨 HARDTRUTH GATE HALTED (CONTRADICTION DETECTED): Unresolved test failures exist in the ledger: [{fails_summary}]. Fix the failures and re-run tests before stopping."
        }

    # -----------------------------------------------------------------------
    # Rule 4: Targeted DeBERTa-v3 NLI Claim Adjudication
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

    claims_to_verify = claims_to_verify[:4]

    has_test_pass_claim = any(
        re.search(r"\b((all|all \d+|\d+)?\s*(unit\s+)?tests?(\s+[\w/]+){0,3}\s+passed|tests?\s+(have\s+)?passed|unit\s+tests?\s+passed|tests?\s+succeeded|test\s+suite\s+passed|tests?\s+are\s+passing)\b", c, re.IGNORECASE)
        for c in claims_to_verify
    )
    if has_test_pass_claim and verification_commands_executed == 0:
        record_halt(counter_file, halt_count + 1)
        return {
            "decision": "continue",
            "reason": "🚨 HARDTRUTH ENGINE HALTED: You claimed tests passed, but the execution ledger shows NO commands or test suites were run in this session. Execute the test command with run_command to prove it before completing."
        }

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
                record_halt(counter_file, halt_count + 1)
                return {
                    "decision": "continue",
                    "reason": f"🚨 HARDTRUTH ENGINE CONTRADICTION DETECTED (conf: {contradiction:.2f}):\nClaim: '{claim}' contradicts the execution ledger.\nLedger Evidence: {premise_str}\nFix the failure and provide verified command output before stopping."
                }
        else:
            if verification_commands_executed > 0 and len(unresolved_failures) == 0:
                pass
            else:
                record_halt(counter_file, halt_count + 1)
                return {
                    "decision": "continue",
                    "reason": f"🚨 HARDTRUTH DAEMON UNREACHABLE: Verifier at {SYSTEM_ONE_URL} is offline. Factual claim '{claim}' cannot be verified autonomously. You must provide manual verification output before completing."
                }

    if os.path.exists(counter_file):
        try:
            os.remove(counter_file)
        except Exception:
            pass

    return {"decision": "allow"}


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

    if mode in ["post_tool", "PostToolUse"]:
        out = handle_post_tool_use(payload)
    else:
        out = handle_stop(payload)

    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
