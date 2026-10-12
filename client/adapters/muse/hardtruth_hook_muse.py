#!/usr/bin/env python3
"""
HardTruth <-> Muse (~/.local/bin/muse, muse-bin-1.4.0-R4302.1) shared translator.

Not a CLI on its own -- imported by hardtruth_muse_exec.py (live, block-capable) and
hardtruth_muse_report.py (post-hoc, report-only). Does not modify hardtruth_hook.py.

Why HARNESS=goose, and why this file exists at all (see the top-level task report for
the full investigation): as of this build,

  - `muse plugins install/validate/list` all print "plugins are not available in this
    build", even though the binary embeds a full native hooks capability (event names
    PreToolUse/PostToolUse/PostToolUseFailure/Stop/Notification/PermissionRequest/
    SessionFork/UserPromptSubmit/Interrupt, a hookSpecificOutput.decision JSON contract
    that matches Claude Code's hook ABI almost field-for-field, and a documented
    `.muse-plugin/plugin.json` manifest format). That is HardTruth's natural, best
    integration point for Muse -- but it is gated off in the installed binary, so it
    is not usable today. This is confirmed via `muse plugins --help` exiting 2 with
    that exact message, not inferred.

  - `muse serve` (MSP over stdio) is a real, fully-specified JSON-RPC control plane
    (see `muse schema generate-json-schema`), but "the client owns this process's
    stdin and stdout and is its only connection" -- there is no way to attach a second,
    observer-only connection to an interactively-driven `muse` session. Fully driving
    it ourselves would mean reimplementing Muse's interactive TUI, which is out of
    scope for a verification hook and would change how the user works with Muse day
    to day.

  - `muse exec --json` (headless, single-prompt) DOES stream a stable, documented
    wire-event log on stdout in real time (envelope.payload_type / envelope.payload,
    verified live against the echo provider: "run.output.delta" for streamed text,
    "run.terminal.completed"/"failed"/"cancelled" for turn end with the final text).
    Combined with `--session-id` (resume the same session across separate `muse exec`
    invocations), this gives a genuine, verified BLOCK mechanism for headless/scripted
    Muse usage: hardtruth_muse_exec.py.

  - For ordinary interactive `muse` sessions there is currently no verified way to
    intercept or block anything (native hooks disabled; MSP requires being the sole
    driving client). hardtruth_muse_report.py covers that case honestly, as
    REPORT-ONLY: it runs `muse export` after the session has already ended and can
    only tell you whether HardTruth would have objected, never prevent anything.

As with the Pi and Goose adapters, HARDTRUTH_HARNESS is set to "goose", not a new
"muse" value: the only two harness strings hardtruth_hook.py's _halt()/_allow() treat
as an actual block signal are "claude_code"/"claude" and "goose"; anything else falls
through to the non-blocking "antigravity" default ({"decision": "continue", ...}).
"""
import os
import sys
import json
import time
import tempfile
from typing import Optional, Dict, Any

_HOOK_DIR = os.environ.get("HARDTRUTH_HOOK_DIR", os.path.expanduser("~/.hardtruth/lib"))
if os.path.isdir(_HOOK_DIR) and os.path.exists(os.path.join(_HOOK_DIR, "hardtruth_hook.py")):
    sys.path.insert(0, _HOOK_DIR)
else:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    sys.path.insert(0, os.path.join(_REPO_ROOT, "client"))
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("HARDTRUTH_HARNESS", "goose")
os.environ.setdefault(
    "HARDTRUTH_LEDGER_PATH",
    os.path.expanduser("~/.config/muse/hardtruth_ledger.jsonl"),
)
os.environ.setdefault("SYSTEM_ONE_URL", "http://127.0.0.1:49281")

import hardtruth_hook as hh  # noqa: E402

LOG = os.environ.get("HARDTRUTH_MUSE_LOG", "/tmp/hardtruth_muse_hook.log")


def log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


# Best-effort keyword match for a `muse exec --json` wire record that represents a
# tool/shell execution. NOT verified against a live tool call: the echo provider used
# for live smoke-testing (see client/adapters/muse/README.md) never invokes a tool, so
# this list is inferred from the task_kind naming convention observed in other, known
# NON-tool records (e.g. "reminder.agent.skill-reminder", "model.unknown.response",
# "session_name.allocate") rather than from an actual "shell.execute"/"tool.call"
# record. Treat as best-effort: it will under-match before it fabricates a tool call
# that didn't happen.
#
# IMPORTANT (found by live testing, see client/adapters/muse/README.md "Verification"):
# this match is intentionally scoped to task_kind alone, gated on payload_type =
# "task.lifecycle.*" -- matching on payload_type text directly is unsafe. An earlier
# version of this matcher also substring-matched payload_type and tripped on Muse's own
# internal "runtime.command.accepted"/"runtime.command_intake.*" bookkeeping records
# (they contain "command"), which caused handle_post_tool_use() to run -- and, via its
# get_or_set_session_baseline() call -- to fire before any real tool ran, at a point
# when a pre-existing dirty file in the workspace got silently absorbed into the
# session's "pre-existing, not my doing" baseline and never flagged again for the rest
# of the run. Only "task.lifecycle.*" records carry a real per-task task_kind at
# payload.event.task_kind; every other family (runtime.*, session.*) is Muse's own
# internal plumbing and is never treated as a tool call here.
_TOOL_TASK_KIND_KEYWORDS = ("shell", "bash", "tool", "write_file", "edit_file", "read_file")


def _is_tool_record(payload_type: str, task_kind: str) -> bool:
    if not payload_type.startswith("task.lifecycle."):
        return False
    task_kind_l = task_kind.lower()
    return any(k in task_kind_l for k in _TOOL_TASK_KIND_KEYWORDS)


def record_tool_event(envelope: Dict[str, Any], session_id: str, cwd: str, step_idx: int) -> Optional[dict]:
    """
    Inspects one parsed `muse exec --json` envelope record (top-level keys:
    schema_version, id, stream, sequence, recorded_at, record_type, durability,
    causation_id, payload_type, payload_schema_version, payload). If it looks like a
    tool/shell execution record, forwards it to HardTruth's post_tool as a
    harness-agnostic payload (always carrying an explicit "exitCode" so
    handle_post_tool_use never falls through to transcript polling, which Muse's
    stream doesn't produce). Returns the hook's response dict, or None if this
    envelope wasn't tool-shaped. post_tool never blocks, so the caller does not need
    to act on the return value -- it's surfaced for logging only.
    """
    payload = envelope.get("payload") or {}
    payload_type = str(envelope.get("payload_type") or "")
    event = payload.get("event") or {}
    task_kind = str(event.get("task_kind") or payload.get("task_kind") or "")
    if not _is_tool_record(payload_type, task_kind):
        return None

    kind = str(event.get("kind") or payload.get("kind") or "")
    if kind == "failed":
        exit_code = 1
        error = str(event.get("reason") or payload.get("reason") or "task failed")
    elif kind in ("completed", "run_terminal") and str(payload.get("terminal") or "") != "failed":
        exit_code = 0
        error = None
    elif kind in ("proposed", "accepted", "scheduled", "started"):
        # Intermediate lifecycle states carry no outcome yet; nothing to record.
        return None
    else:
        # Unknown shape: record as uncorroborated rather than guessing a pass.
        exit_code = None
        error = f"unrecognized muse task lifecycle kind={kind!r}"

    tool_name = "run_command" if any(k in payload_type.lower() for k in ("shell", "exec", "bash", "command")) else task_kind or "unknown"
    args = {"CommandLine": task_kind or payload_type, "Cwd": cwd}

    ht_payload: Dict[str, Any] = {
        "toolCall": {"name": tool_name, "args": args},
        "conversationId": session_id,
        "stepIdx": step_idx,
        "cwd": cwd,
    }
    if exit_code is not None:
        ht_payload["exitCode"] = exit_code
    if error:
        ht_payload["error"] = error

    out = hh.handle_post_tool_use(ht_payload)
    log(f"tool payload_type={payload_type} task_kind={task_kind} exit={exit_code} conv={session_id}")
    return out


def run_stop(session_id: str, cwd: str, agent_text: str) -> dict:
    """
    Same synthetic-transcript technique as the Goose and Pi adapters: writes agent_text
    into a one-line {"type": "PLANNER_RESPONSE", "content": ...} JSONL file so
    handle_stop's claim-extraction/NLI-contradiction phases run over Muse's real final
    answer, then calls handle_stop. The git-porcelain-based file-tracking and
    test-execution checks run regardless of whether record_tool_event() above
    recognized any tool calls (those checks are premise-data/git driven).
    """
    transcript_path = ""
    if agent_text and agent_text.strip():
        tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, prefix="hardtruth_muse_")
        json.dump({"type": "PLANNER_RESPONSE", "content": agent_text}, tf)
        tf.close()
        transcript_path = tf.name

    payload = {
        "conversationId": session_id,
        "transcriptPath": transcript_path,
        "cwd": cwd,
        "workspace_dir": cwd,
    }
    out = hh.handle_stop(payload)
    log(f"stop conv={session_id} out={out}")
    if transcript_path:
        try:
            os.unlink(transcript_path)
        except Exception:
            pass
    return out
