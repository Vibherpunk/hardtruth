#!/usr/bin/env python3
"""
HardTruth REPORT-ONLY post-hoc check for an ordinary interactive `muse` session.

This does NOT and CANNOT block anything: it runs after the session has already ended,
against `muse export`'s durable session log. Use hardtruth_muse_exec.py instead for
anything that should actually be enforced (headless/scripted `muse exec` usage).

Why this exists as a separate, weaker tool (see hardtruth_hook_muse.py's module
docstring for the full investigation): Muse's native plugin/hooks system exists in the
binary (event names matching Claude Code's hook ABI almost exactly) but
`muse plugins --help` reports "plugins are not available in this build" on this
machine, and `muse serve` (MSP) only accepts a single driving client for the whole
session, so there is no way to attach a non-invasive observer to a normal interactive
`muse` TUI session today. This script is the honest fallback for that case.

Usage:
    hardtruth_muse_report.py [--session <id>] [--last] [--strict]

--session / --last: forwarded to `muse export`. Defaults to --last.
--strict: exit 1 if HardTruth would have halted (default: always exit 0; this is a
          report, not a gate).

Caveat: `muse export`'s event schema (top-level keys under events[i].envelope --
payload_type values like "runtime.session.task", "runtime.command_intake.received")
is NOT the same, more stable wire format `muse exec --json` emits
("run.output.delta", "task.lifecycle.*", "run.terminal.completed") -- confirmed by
running both against the same session live. `muse export --help` does not document
its event schema as a stable public contract (unlike `muse exec --json`'s "Emit
machine-readable JSONL events on stdout"), so this script only relies on the two
things that ARE documented and stable across both: `sessions[]` (session_id,
turn_count) and `session_end` (exit_reason). It does not attempt to reconstruct a
tool-by-tool ledger from the export the way hardtruth_muse_exec.py does from the live
--json stream, and it does not feed a final "agent text" into the NLI/claim-contradiction
check -- so this is strictly a Rule 1 / Rule 2 (git-diff-based: source modified without
any recorded verification) check, using ONLY git state, not a claim-verification pass.
"""
import os
import sys
import json
import argparse
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hardtruth_hook_muse as htm  # noqa: E402


def _muse_bin() -> str:
    import shutil

    return os.environ.get("HARDTRUTH_MUSE_BIN") or shutil.which("muse") or "muse"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None)
    ap.add_argument("--last", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        export_path = tf.name

    cmd = [_muse_bin(), "export", "--out", export_path]
    if args.session:
        cmd += ["--session", args.session]
    else:
        cmd += ["--last"]

    try:
        subprocess.run(cmd, check=True)
        with open(export_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    finally:
        try:
            os.unlink(export_path)
        except Exception:
            pass

    sessions = doc.get("sessions") or []
    if not sessions:
        print("[HARDTRUTH REPORT-ONLY] no session found in export; nothing to check.")
        return 0

    sess = sessions[0]
    session_id = sess.get("session_id") or "muse-report"
    cwd = os.getcwd()  # `muse export` doesn't carry workspace_root at the top level

    verdict = htm.run_stop(session_id, cwd, "")

    label = "[HARDTRUTH REPORT-ONLY -- session already ended, this cannot block]"
    if verdict.get("action") == "halt":
        print(f"{label} HardTruth would have halted this turn:\n{verdict.get('message')}")
        return 1 if args.strict else 0
    else:
        print(f"{label} No objections (git-state check only; no claim-verification pass -- see this script's module docstring).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
