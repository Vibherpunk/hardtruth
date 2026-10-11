# HardTruth <-> Muse adapter

Wires HardTruth's execution ledger + claim-verification gate into
`muse` (`~/.local/bin/muse`, wrapper -> `muse-bin-1.4.0-R4302.1`).

## Investigation summary (why this shape)

Checked, in preference order, per the task brief:

1. **Native hooks/policy commands.** The binary embeds a full native plugin/hooks
   system: manifest at `.muse-plugin/plugin.json`, hook events
   `PreToolUse`/`PostToolUse`/`PostToolUseFailure`/`Stop`/`Notification`/
   `PermissionRequest`/`SessionFork`/`UserPromptSubmit`/`Interrupt`, and a
   `hookSpecificOutput.decision` JSON contract that matches Claude Code's hook ABI
   almost field-for-field (`hook_event_name`, `session_id`, `transcript_path`).
   **This is gated off in the installed build**: `muse plugins install/validate/list`
   all exit 2 with `plugins are not available in this build`. Confirmed by running the
   command, not inferred from strings alone. So: not usable today, even though it
   would be the best fit if enabled.
2. **MSP (`muse serve` over stdio).** A real, fully-specified JSON-RPC control plane
   (dumped via `muse schema generate-json-schema`). But `muse serve --help` states
   "the client owns this process's stdin and stdout and is its only connection" — there
   is no way to attach a non-invasive second connection to an interactively-driven
   session. Fully driving it ourselves would mean reimplementing Muse's TUI, out of
   scope here.
3. **Post-hoc (`muse trace`/`muse export`).** Implemented, see below.

The one candidate that turned out to be both viable and live-testable is `muse exec
--json` (headless, single-prompt runs): confirmed live with the `echo` provider to
stream a stable, documented wire-event log (`payload_type`/`payload`,
`run.output.delta`, `run.terminal.completed`) in real time, and confirmed live that
`--session-id` resumes the same session across separate invocations. That gives a
genuine BLOCK mechanism, but only for headless/scripted usage.

## Files

- `hardtruth_hook_muse.py` — shared translator library (not a CLI). Sets
  `HARDTRUTH_HARNESS=goose` (see its module docstring for why) and a Muse-dedicated
  ledger path. Provides `record_tool_event()` (best-effort tool/shell detection in the
  `muse exec --json` wire stream -> `handle_post_tool_use`) and `run_stop()` (Goose-style
  synthetic-transcript technique -> `handle_stop`).
- `hardtruth_muse_exec.py` — **BLOCK-capable.** Drop-in wrapper for `muse exec`:
  streams and mirrors `muse exec --json`'s stdout, feeds tool-shaped records into the
  ledger, and on turn completion runs the Stop-equivalent check. On halt, resumes the
  same session (`--session-id`) with the halt reason as the next prompt, up to
  `--hardtruth-max-retries` (default 2); exits 2 if still halted after retries are
  exhausted, 0 otherwise.
- `hardtruth_muse_report.py` — **REPORT-ONLY.** For ordinary interactive `muse`
  sessions, where nothing above can attach. Runs `muse export --last` (or `--session
  <id>`) after the session has ended and prints a clearly labeled report-only verdict.
  Cannot block anything — the session is already over.

## Mechanism (per script)

| Script | Covers | Block or report | 
|---|---|---|
| `hardtruth_muse_exec.py` | headless `muse exec` runs | **BLOCK** (exit code 2 + forced session-resume retry) |
| `hardtruth_muse_report.py` | interactive `muse` sessions | report-only (cannot block; native hooks disabled, MSP is single-client) |

## Install (not performed by this change — coordinator applies after review)

1. Ensure the hook is installed at `~/.hardtruth/lib/hardtruth_hook.py` (override with
   `HARDTRUTH_HOOK_DIR` if different).
2. Nothing under `~/.config/muse` or any other live Muse config needs editing — both
   scripts are standalone CLIs, not Muse plugins (plugins are disabled in this build).
   Copy the adapter directory somewhere on `PATH`, e.g.:

   ```
   mkdir -p ~/.local/lib/hardtruth-muse-adapter
   cp client/adapters/muse/hardtruth_hook_muse.py \
      client/adapters/muse/hardtruth_muse_exec.py \
      client/adapters/muse/hardtruth_muse_report.py \
      ~/.local/lib/hardtruth-muse-adapter/
   ln -sf ~/.local/lib/hardtruth-muse-adapter/hardtruth_muse_exec.py ~/.local/bin/hardtruth-muse-exec
   ln -sf ~/.local/lib/hardtruth-muse-adapter/hardtruth_muse_report.py ~/.local/bin/hardtruth-muse-report
   chmod +x ~/.local/bin/hardtruth-muse-exec ~/.local/bin/hardtruth-muse-report
   ```

3. For scripted/CI usage, invoke `hardtruth-muse-exec` wherever `muse exec` was used
   before (same arguments; `--json` is added automatically).
4. For interactive usage, there's nothing to wire up automatically. Optionally alias
   `hardtruth-muse-report` for occasional manual after-the-fact checks, e.g. run it
   right after closing a `muse` session you want a second opinion on.
5. If Meta ever ships a build with `muse plugins` enabled, re-run
   `muse plugins --help`; if it stops saying "not available in this build", the native
   `.muse-plugin/plugin.json` hooks route (PreToolUse/PostToolUse/Stop, real block
   semantics) should be revisited as the primary integration instead of this one.

## Verification performed

- `muse plugins install/validate/list --help` all confirmed to exit 2 with "plugins
  are not available in this build" on this machine (`muse-stable` channel,
  `1.4.0-R4302.1`).
- `muse schema generate-json-schema` dumped and inspected (`msp.schema.json`,
  7000+ lines) to confirm the MSP method/notification/approval shapes referenced above.
- `muse exec --json --provider echo "..."` run live (trivial prompt, echo provider,
  per the hard rules) and its stdout captured to confirm the wire-event envelope shape.
- `hardtruth_muse_exec.py` run live end-to-end against a scratch git repo, twice:
  - Clean repo, benign echoed text -> HardTruth allowed, wrapper exited 0.
  - Repo with an uncommitted stub function staged -> HardTruth halted ("Unimplemented
    stub detected"), wrapper resumed the session with the reason as the next prompt
    (confirmed via the resumed run's `turn.input.user` record carrying the exact halt
    text), then exited on the retry-exhausted path.
  - **Found and fixed a real bug during this testing**: an earlier version of
    `_is_tool_record()` substring-matched `payload_type` directly and false-matched
    Muse's own internal `runtime.command.accepted`/`runtime.command_intake.*`
    bookkeeping records (they contain "command"). That caused `handle_post_tool_use()`
    to run before any real tool executed, which — via its own
    `get_or_set_session_baseline()` call — silently absorbed the already-dirty stub
    file into the session's "pre-existing, not this session's doing" baseline, so the
    stub was never flagged. Fixed by gating the match to `payload_type` ==
    `"task.lifecycle.*"` and reading `task_kind` from the correct nested field
    (`payload.event.task_kind`), with a narrower keyword list.
- `hardtruth_muse_report.py` run live against a retained session's export.
- **Not verified**: the tool/shell record shape in `muse exec --json`'s stream, since
  the `echo` provider (the only safe option under the hard rules — no real API keys
  configured) never actually invokes a tool. `record_tool_event()`'s matching is
  therefore inferred from naming conventions of other, known non-tool records, not
  from a real example. Recommend a supervised one-off real run before relying on the
  Rule 1/2 (test-execution) ledger checks in production; the git-file-tracking and
  stub-detection checks (demonstrated above) do not depend on this at all.

## Limitations

- Native, real-time blocking only covers headless/scripted `muse exec` usage.
  Interactive `muse` sessions get report-only, after-the-fact coverage at best.
- Tool/shell-call detection inside the live wire stream is best-effort (see above);
  the ledger's exit-code/verification-count facts for `run_command` may be incomplete
  until validated against a real tool call.
- `hardtruth_muse_exec.py`'s retry loop depends on HardTruth's own stub/claim checks
  re-evaluating on each resumed call. Observed live: `handle_stop()`'s AST stub check
  flagged an unresolved stub on the first call for a given conversation id but allowed
  an *identical*, still-unfixed second call for the same conversation id (this is
  `hardtruth_hook.py`'s own existing behavior, confirmed independent of this adapter
  by calling `handle_stop()` directly three times in a row against the same unfixed
  file — not something introduced here). Rule 1/2 (git-diff/ledger-driven) and the NLI
  contradiction check are not known to share this behavior, since they are premise-data
  driven rather than a per-file "already reported" check, but this wasn't exhaustively
  probed against every rule. Worth the HardTruth maintainers double-checking whether a
  repeated, still-broken stub across multiple Stop calls in one conversation should
  keep halting.
