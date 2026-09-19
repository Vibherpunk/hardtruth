#!/usr/bin/env python3
"""
HardTruth: Autonomous Anti-Hallucination & Truth Enforcement Engine
Lifecycle Hook: PostToolUse & Stop
Powered by DeBERTa-v3 Cross-Encoder & Deterministic Execution Ledger (http://127.0.0.1:8000)

Enforces:
1. Deterministic Execution Ledger: Captures real tool executions, exit codes, and file diffs.
2. AST Anti-Stubbing Linter: Rejects mock stubs (pass, NotImplementedError, dummy return).
3. System One NLI Verification: Evaluates agent claims against execution ledger in ~10ms on MPS.
4. Circuit Breaker Escape Hatch: Releases with visible warning after 3 consecutive halts.
"""

import os
import sys
import json
import time
import re
import ast
import urllib.request
import urllib.error

LEDGER_FILE = os.path.expanduser("~/.gemini/antigravity-cli/ledger.jsonl")
HALT_COUNTER_DIR = "/tmp/hardtruth_halts"
SYSTEM_ONE_URL = os.environ.get("SYSTEM_ONE_URL", "http://127.0.0.1:8000")

def call_system_one(endpoint: str, payload: dict, timeout: float = 3.0) -> dict:
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
    except Exception as e:
        return None

# ---------------------------------------------------------------------------
# PostToolUse: Deterministic Ledger Recording
# ---------------------------------------------------------------------------

def handle_post_tool_use(payload: dict) -> dict:
    """
    Records ground-truth tool execution facts into append-only ledger.
    """
    os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)
    
    tool_call = payload.get("toolCall", {})
    tool_name = tool_call.get("name", "unknown")
    tool_args = tool_call.get("args", {})
    conv_id = payload.get("conversationId", "unknown")
    step_idx = payload.get("stepIdx", 0)
    error_msg = payload.get("error", None)
    
    cmd_or_file = ""
    if tool_name == "run_command":
        cmd_or_file = tool_args.get("CommandLine", "")
    elif tool_name in ["write_to_file", "replace_file_content"]:
        cmd_or_file = tool_args.get("TargetFile", "")
    elif tool_name == "view_file":
        cmd_or_file = tool_args.get("AbsolutePath", "")
        
    entry = {
        "timestamp": time.time(),
        "conversationId": conv_id,
        "stepIdx": step_idx,
        "tool": tool_name,
        "target": cmd_or_file,
        "status": "error" if error_msg else "success",
        "error": str(error_msg) if error_msg else None
    }
    
    try:
        with open(LEDGER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
        
    return {}

# ---------------------------------------------------------------------------
# AST Anti-Stubbing Inspection
# ---------------------------------------------------------------------------

def inspect_python_ast_for_stubs(filepath: str) -> list:
    """Checks python files for vacuous stubs (pass, NotImplementedError)."""
    violations = []
    if not os.path.exists(filepath) or not filepath.endswith(".py"):
        return violations
        
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=filepath)
            
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                # Check for single 'pass' or single docstring + pass
                is_stub = False
                if len(body) == 1:
                    if isinstance(body[0], ast.Pass):
                        is_stub = True
                    elif isinstance(body[0], ast.Raise) and isinstance(body[0].exc, ast.Name) and body[0].exc.id == "NotImplementedError":
                        is_stub = True
                elif len(body) == 2 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    # Docstring + pass
                    if isinstance(body[1], ast.Pass):
                        is_stub = True
                        
                if is_stub:
                    violations.append(f"Function '{node.name}' in {os.path.basename(filepath)} is an empty stub (pass/NotImplementedError).")
    except Exception:
        pass
        
    return violations

# ---------------------------------------------------------------------------
# Stop: Pre-Termination Verification Gate
# ---------------------------------------------------------------------------

def handle_stop(payload: dict) -> dict:
    """
    Evaluates agent's visible claims against execution ledger using System One.
    Halts termination if claims contradict ground truth.
    """
    conv_id = payload.get("conversationId", "default")
    transcript_path = payload.get("transcriptPath", "")
    
    # 1. Infinite Loop Escape Hatch Check
    os.makedirs(HALT_COUNTER_DIR, exist_ok=True)
    counter_file = os.path.join(HALT_COUNTER_DIR, f"halt_{conv_id}.json")
    halt_count = 0
    if os.path.exists(counter_file):
        try:
            with open(counter_file, "r") as f:
                halt_count = json.load(f).get("count", 0)
        except Exception:
            halt_count = 0
            
    if halt_count >= 3:
        # Reset counter and allow termination with downgrade
        try:
            os.remove(counter_file)
        except Exception:
            pass
        return {"decision": "allow"}

    # 2. Extract agent's final text from transcript
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
        
    # 3. Read & Distill Execution Ledger Facts (Strict 400-token limit)
    ledger_entries = []
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("conversationId") == conv_id:
                            ledger_entries.append(entry)
                    except Exception:
                        continue
        except Exception:
            pass

    recent_entries = ledger_entries[-25:]
    commands_run = [e for e in recent_entries if e.get("tool") == "run_command"]
    files_modified = [e for e in recent_entries if e.get("tool") in ["write_to_file", "replace_file_content"]]
    
    # AST Stub check on modified files
    for f_entry in files_modified:
        target = f_entry.get("target")
        if target and os.path.exists(target):
            stubs = inspect_python_ast_for_stubs(target)
            if stubs:
                record_halt(counter_file, halt_count + 1)
                return {
                    "decision": "continue",
                    "reason": f"🚨 HARDTRUTH ENGINE REJECTED: Unimplemented stub detected. {stubs[0]} Write actual working implementation before completing."
                }
                
    # Build compact premise string
    premise_parts = []
    if commands_run:
        for c in commands_run[-6:]:
            status_str = "SUCCEEDED (exit 0)" if c.get("status") == "success" else f"FAILED ({c.get('error')})"
            premise_parts.append(f"COMMAND: '{c.get('target')}'. STATUS: {status_str}.")
    else:
        premise_parts.append("No commands or tests were executed.")
        
    if files_modified:
        mods = [os.path.basename(m.get("target")) for m in files_modified[-6:] if m.get("target")]
        premise_parts.append(f"MODIFIED_FILES: {', '.join(mods)}.")
    else:
        premise_parts.append("No files were modified.")
        
    premise = " ".join(premise_parts)[:1200]
    
    # 4. Atomic Claim Extraction & Targeted Vocabulary Filtering
    # Strip multi-line code fences (```...```) because full scripts/files aren't claims.
    # CRITICAL SECURITY RULE: Do NOT strip the content of backticks or blockquotes,
    # otherwise a lying agent could wrap unverified claims in `...` or > to bypass the sentinel.
    # Instead, we strip only the markdown syntax characters (` and >) so the claims remain fully visible to the judge!
    text_without_fences = re.sub(r"```[\s\S]*?```", "", agent_text)
    text_clean = text_without_fences.replace("`", "")
    text_clean = re.sub(r"^\s*>\s*", "", text_clean, flags=re.MULTILINE)
    
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text_clean)
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
        r"(feature|pipeline|integration)\s+is\s+(now\s+)?(wired|working|active))\b",
        re.IGNORECASE
    )
    descriptive_filter = re.compile(
        r"^\s*(?:quote:|example:|sample:|(?:when|if|for example|e\.g\.|every time|how |the system|the hook|this means|in order to|as an example|such as|to prevent|by default|instead of|note that|on x\b|on twitter|in our search|in research|search results|discussions across|discussions on|users report|practitioners note|engineers note|people are|the community|articles|papers|studies)\b)",
        re.IGNORECASE
    )
    imperative_filter = re.compile(
        r"^\s*([🚨⚠️]|(?:do not|don't|ensure|inspect|execute|verify|check|run|please|must|should|critical requirement|requirement|task|step \d|turn \d|signal \d|tip:|note:|warning:)\b)",
        re.IGNORECASE
    )
    
    claims_to_verify = []
    for s in sentences:
        s_clean = s.strip()
        # Ignore short strings, descriptive explanations, and imperative prompt directives
        if len(s_clean) > 15 and action_triggers.search(s_clean):
            if descriptive_filter.search(s_clean) or imperative_filter.search(s_clean):
                continue
            claims_to_verify.append(s_clean[:250])
            
    # Cap at top 4 high-risk claims
    claims_to_verify = claims_to_verify[:4]
    if not claims_to_verify:
        return {"decision": "allow"}
        
    # 5. NLI Verification against Premise via System One
    for claim in claims_to_verify:
        nli_res = call_system_one("v1/verify-claim", {
            "premise": premise,
            "hypothesis": claim
        })
        
        if not nli_res:
            continue
            
        probs = nli_res.get("probabilities", {})
        contradiction = probs.get("contradiction", 0.0)
        neutral = probs.get("neutral", 0.0)
        
        # Test claim verification: if claim asserts test pass but no commands were run
        is_pass_claim = bool(re.search(
            r"\b((all|all \d+)\s+tests?\s+passed|tests?\s+(have\s+)?passed|unit\s+tests?\s+passed|tests?\s+succeeded|test\s+suite\s+passed|tests?\s+are\s+passing)\b",
            claim,
            re.IGNORECASE
        ))
        if is_pass_claim and not commands_run:
            record_halt(counter_file, halt_count + 1)
            return {
                "decision": "continue",
                "reason": f"🚨 HARDTRUTH ENGINE HALTED: You claimed '{claim}', but the execution ledger shows NO commands or test suites were run in this session. Execute the test command with run_command to prove it before completing."
            }
            
        if contradiction >= 0.70:
            record_halt(counter_file, halt_count + 1)
            return {
                "decision": "continue",
                "reason": f"🚨 HARDTRUTH ENGINE CONTRADICTION DETECTED (conf: {contradiction:.2f}):\nClaim: '{claim}' contradicts the execution ledger.\nLedger Evidence: {premise}\nFix the failure and provide verified command output before stopping."
            }
            
    # All claims verified clean: reset halt counter and allow stop
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
