#!/usr/bin/env python3
"""
HardTruth-guarded wrapper around `muse exec` (headless, single-prompt Muse runs).

BLOCK-capable, for scripted/CI/headless usage only (see hardtruth_hook_muse.py's module
docstring for why interactive `muse` sessions are report-only instead, in
hardtruth_muse_report.py).

Usage: identical to `muse exec`, plus an optional --hardtruth-max-retries:

    hardtruth_muse_exec.py [--hardtruth-max-retries N] [muse exec args/prompt...]

Mechanism:
  1. Runs `muse exec --json <args>` as a subprocess (forcing --json if the caller didn't
     pass it), streaming its stdout through unmodified (so anything already parsing
     `muse exec --json` output keeps working) while also parsing each line.
  2. Every wire record whose payload looks like a tool/shell execution is forwarded to
     HardTruth's post_tool ledger in real time (best-effort matching -- see
     hardtruth_hook_muse.py; the echo provider used to verify the wire format live never
     invokes a real tool, so this half is unverified against a live tool call).
  3. On a `run.terminal.*` record (verified live: this record's `payload.text` carries
     the turn's final answer, `payload.terminal` is completed/failed/cancelled), runs
     HardTruth's stop-equivalent claim check.
  4. If HardTruth halts and retries remain, re-invokes `muse exec --json --session-id
     <same session>` with the halt reason as the next prompt -- this resumes the SAME
     Muse session (verified live: `muse export --session <id>` after a `--session-id`
     run showed session continuity), which is the closest headless equivalent to
     "inject a follow-up message and keep going" that `muse exec`'s one-shot process
     model allows.
  5. If still halted after exhausting retries, prints the reason to stderr and exits
     non-zero -- the actual enforcement point for a CI/script caller.

Exit codes: 0 on HardTruth allow (or immediately, matching muse's own exit code, if muse
itself failed before HardTruth gets a verdict); 2 if HardTruth halts after exhausting
retries; muse's own exit code is otherwise preserved.
"""
import os
import sys
import json
import shutil
import subprocess
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hardtruth_hook_muse as htm  # noqa: E402

DEFAULT_MAX_RETRIES = 2


def _muse_bin() -> str:
    return os.environ.get("HARDTRUTH_MUSE_BIN") or shutil.which("muse") or "muse"


def _run_once(extra_args: List[str], cwd: str) -> Tuple[int, Optional[str], Optional[str], str]:
    """
    Runs one `muse exec --json ...` invocation, streaming stdout to our own stdout
    while parsing it. Returns (exit_code, session_id, final_text, terminal_state).
    """
    args = [_muse_bin(), "exec", "--json"] + extra_args
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=None,  # passthrough, so the human/CI log sees muse's own diagnostics live
        text=True,
        bufsize=1,
    )

    session_id: Optional[str] = None
    final_text: Optional[str] = None
    terminal_state = ""
    step_idx = 0

    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        line_s = line.strip()
        if not line_s:
            continue
        try:
            rec = json.loads(line_s)
        except Exception:
            continue

        stream = rec.get("stream") or {}
        if stream.get("kind") == "session" and stream.get("id"):
            session_id = stream["id"]

        payload_type = str(rec.get("payload_type") or "")
        payload = rec.get("payload") or {}

        if payload_type.startswith("run.terminal"):
            terminal_state = str(payload.get("terminal") or "")
            final_text = payload.get("text") or final_text
            continue

        if session_id:
            htm.record_tool_event(rec, session_id, cwd, step_idx)
            step_idx += 1

    proc.wait()
    return proc.returncode, session_id, final_text, terminal_state


def main() -> int:
    argv = sys.argv[1:]
    max_retries = DEFAULT_MAX_RETRIES
    if "--hardtruth-max-retries" in argv:
        i = argv.index("--hardtruth-max-retries")
        try:
            max_retries = int(argv[i + 1])
            del argv[i : i + 2]
        except (IndexError, ValueError):
            sys.stderr.write("hardtruth_muse_exec: --hardtruth-max-retries needs an integer\n")
            return 64

    cwd = os.getcwd()
    prompt_args = list(argv)
    attempt = 0
    exit_code, session_id, final_text, terminal_state = _run_once(prompt_args, cwd)

    if not session_id:
        sys.stderr.write("hardtruth_muse_exec: no session id observed on the wire; skipping HardTruth verification\n")
        return exit_code

    while True:
        verdict = htm.run_stop(session_id, cwd, final_text or "")
        if verdict.get("action") != "halt":
            return exit_code

        reason = verdict.get("message") or "HardTruth rejected this turn's claims."
        if attempt >= max_retries:
            sys.stderr.write(
                f"\n🚨 hardtruth_muse_exec: HardTruth halted after {attempt + 1} attempt(s), "
                f"no retries left. Reason:\n{reason}\n"
            )
            return 2

        attempt += 1
        sys.stderr.write(f"\n⚠️  hardtruth_muse_exec: HardTruth halted (attempt {attempt}/{max_retries}). Resuming session with the reason as the next prompt.\n")
        exit_code, session_id_2, final_text, terminal_state = _run_once(
            ["--session-id", session_id, reason], cwd
        )
        session_id = session_id_2 or session_id


if __name__ == "__main__":
    sys.exit(main())
