#!/usr/bin/env python3
"""
HardTruth <-> Pi (@earendil-works/pi-coding-agent) adapter hook.

Translates events from the Pi extension (client/adapters/pi/hardtruth-extension/index.ts)
into the HardTruth lifecycle-hook ABI (hardtruth_hook.py), the same way
client/adapters's sibling goose-plugin-hardtruth/hardtruth_hook_goose.py bridges Goose.

This file does NOT modify hardtruth_hook.py or its core logic. It is a pure translator:
Pi tool_result event -> handle_post_tool_use(); Pi agent_settled event -> handle_stop().

Usage (invoked by the Pi extension, JSON payload via stdin):
  hardtruth_hook_pi.py tool   # tool_result      -> execution ledger
  hardtruth_hook_pi.py stop   # agent_settled     -> claim verification gate

Harness selection: HARDTRUTH_HARNESS is set to "goose" (not a new "pi" value). Pi has
no dedicated branch in hardtruth_hook.py's _halt()/_allow() helpers, and the only two
harness strings that produce an actual BLOCK signal (as opposed to a non-blocking
"continue with reason") are "claude_code"/"claude" and "goose". The "goose" wire shape
({"action": "halt", "message": ...} / {"action": "continue"}) is the generic one meant
for exactly this kind of external-agent-hook wiring (it is not Claude-Code-transcript
specific), so this adapter reuses it rather than inventing a new harness string that
would silently fall through to the non-blocking "antigravity" default.
"""
import os
import sys
import json
import time
import tempfile

# ---------------------------------------------------------------------------
# Locate and import the installed hook module without touching its source.
# ---------------------------------------------------------------------------
_HOOK_DIR = os.environ.get("HARDTRUTH_HOOK_DIR", os.path.expanduser("~/.hardtruth/lib"))
if os.path.isdir(_HOOK_DIR) and os.path.exists(os.path.join(_HOOK_DIR, "hardtruth_hook.py")):
    sys.path.insert(0, _HOOK_DIR)
else:
    # Dev-checkout fallback: .../hardtruth-fix/client/adapters/pi/hardtruth_hook_pi.py
    # -> repo root is three levels up from this file's directory.
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    sys.path.insert(0, os.path.join(_REPO_ROOT, "client"))
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("HARDTRUTH_HARNESS", "goose")
os.environ.setdefault(
    "HARDTRUTH_LEDGER_PATH",
    os.path.expanduser("~/.pi/agent/hardtruth_ledger.jsonl"),
)
os.environ.setdefault("SYSTEM_ONE_URL", "http://127.0.0.1:49281")

import hardtruth_hook as hh  # noqa: E402

LOG = os.environ.get("HARDTRUTH_PI_LOG", "/tmp/hardtruth_pi_hook.log")


def log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


# Pi's built-in tool names (see docs/extensions.md "tool_call"/"tool_result" events
# and dist/core/tools/*.d.ts) mapped onto HardTruth's generic tool vocabulary.
_PI_TOOL_MAP = {
    "bash": "run_command",
    "write": "write_to_file",
    "edit": "replace_file_content",
    "read": "view_file",
}


def build_post_tool_payload(evt: dict) -> dict:
    """
    Builds a harness-agnostic HardTruth post_tool payload from a Pi `tool_result`
    event forwarded by the extension as JSON: {toolName, input, isError, exitCode?,
    sessionId, cwd, stepIdx}.

    Always supplies a numeric top-level "exitCode". This is deliberate: handle_post_tool_use
    checks payload["exitCode"] BEFORE it ever looks at transcriptPath, so supplying it
    directly sidesteps the Claude-Code/Antigravity transcript-polling fallbacks entirely
    (those expect transcript shapes Pi doesn't produce). Pi's BashToolDetails (see
    dist/core/tools/bash.d.ts in @earendil-works/pi-coding-agent) does not expose a numeric
    exit code -- only `truncation`/`fullOutputPath` -- so when the event carries no explicit
    exitCode we fall back to the `isError` boolean tool_result always carries: 0 on success,
    1 on failure. This can't distinguish exit 2 from exit 1, but per HardTruth's non-synthesis
    rule it never turns a failure into a fabricated pass.
    """
    tool_name = evt.get("toolName") or "unknown"
    mapped = _PI_TOOL_MAP.get(tool_name, tool_name)
    args_in = evt.get("input") if isinstance(evt.get("input"), dict) else {}
    is_error = bool(evt.get("isError"))

    exit_code = evt.get("exitCode")
    if exit_code is None:
        exit_code = 1 if is_error else 0

    if mapped == "run_command":
        args2 = {"CommandLine": args_in.get("command", ""), "Cwd": evt.get("cwd") or ""}
    elif mapped in ("write_to_file", "replace_file_content"):
        target = args_in.get("path") or args_in.get("file_path") or ""
        args2 = {"TargetFile": target, "Cwd": evt.get("cwd") or ""}
    elif mapped == "view_file":
        args2 = {"AbsolutePath": args_in.get("path") or "", "Cwd": evt.get("cwd") or ""}
    else:
        # Custom / unmapped tool (e.g. an extension-registered tool). Record it as-is
        # so it still lands in the ledger, even though HardTruth's own rules only
        # reason specially about run_command/write_to_file/replace_file_content/view_file.
        args2 = dict(args_in)
        args2["Cwd"] = evt.get("cwd") or ""

    return {
        "toolCall": {"name": mapped, "args": args2},
        "conversationId": str(evt.get("sessionId") or evt.get("conversationId") or "pi"),
        "stepIdx": int(evt.get("stepIdx") or 0),
        "exitCode": int(exit_code),
        "error": None if not is_error else "tool_result reported isError=true",
        "cwd": evt.get("cwd") or os.getcwd(),
    }


def cmd_tool(evt: dict) -> None:
    payload = build_post_tool_payload(evt)
    out = hh.handle_post_tool_use(payload)
    log(f"tool name={payload['toolCall']['name']} exit={payload['exitCode']} conv={payload['conversationId']}")
    sys.stdout.write(json.dumps(out))


def cmd_stop(evt: dict) -> None:
    """
    evt: {sessionId, cwd, agentText} where agentText is the final assistant text of the
    turn that just settled (accumulated by the extension from message_end/agent_end events).

    Mirrors the Goose adapter's technique of writing a one-line synthetic Antigravity-shaped
    transcript ({"type": "PLANNER_RESPONSE", "content": ...}) so handle_stop's Phase 1
    (agent claim extraction) and NLI contradiction check run over Pi's real final text,
    instead of skipping straight to the (still-active) git/ledger-only checks.
    """
    conv = str(evt.get("sessionId") or evt.get("conversationId") or "pi")
    workspace = evt.get("cwd") or os.getcwd()
    agent_text = evt.get("agentText") or ""

    transcript_path = ""
    if agent_text.strip():
        tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, prefix="hardtruth_pi_")
        json.dump({"type": "PLANNER_RESPONSE", "content": agent_text}, tf)
        tf.close()
        transcript_path = tf.name

    payload = {
        "conversationId": conv,
        "transcriptPath": transcript_path,
        "cwd": workspace,
        "workspace_dir": workspace,
    }
    out = hh.handle_stop(payload)
    log(f"stop conv={conv} decision={out}")
    sys.stdout.write(json.dumps(out))

    if transcript_path:
        try:
            os.unlink(transcript_path)
        except Exception:
            pass


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "tool"
    try:
        evt = json.loads(sys.stdin.read() or "{}")
    except Exception:
        evt = {}
    if not isinstance(evt, dict):
        evt = {}

    if mode == "tool":
        cmd_tool(evt)
    elif mode == "stop":
        cmd_stop(evt)
    else:
        sys.stdout.write(json.dumps({"action": "continue"}))


if __name__ == "__main__":
    main()
