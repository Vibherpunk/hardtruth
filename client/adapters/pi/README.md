# HardTruth <-> Pi adapter

Wires HardTruth's execution ledger + claim-verification gate into
[`pi`](https://github.com/earendil-works/pi-mono) (`@earendil-works/pi-coding-agent`),
following the same pattern as `goose-plugin-hardtruth/` in this repo.

## Files

- `hardtruth_hook_pi.py` — translator: Pi event JSON (stdin) -> `hardtruth_hook.py`'s
  `handle_post_tool_use` / `handle_stop` -> JSON (stdout). Does not modify
  `client/hardtruth_hook.py`.
- `hardtruth-extension/index.ts` — Pi extension. Subscribes to `tool_result` (records
  every tool call into the ledger) and `agent_settled` (runs the Stop-equivalent claim
  check; on halt, injects a corrective user message via `pi.sendUserMessage` so the
  agent keeps going instead of finishing on an unverified claim).

## Mechanism

BLOCK-capable. `agent_settled` only fires when Pi is about to go idle, so there is no
"reject this Stop" return value the way Claude Code's Stop hook has one — instead the
extension calls `pi.sendUserMessage(reason)`, which Pi's docs guarantee delivers
immediately and triggers a new turn when idle. The agent run does not end on the
rejected claim.

`HARDTRUTH_HARNESS=goose` is used (not a new `"pi"` value) because the only two harness
strings `hardtruth_hook.py`'s `_halt()`/`_allow()` treat as an actual block signal are
`claude_code`/`claude` and `goose`; anything else falls through to the non-blocking
`antigravity` default. The `goose` wire shape (`{"action": "halt", "message": ...}`) is
the generic one meant for this kind of external-agent wiring.

## Install (not performed by this change — coordinator applies after review)

1. Ensure the hook is installed at `~/.hardtruth/lib/hardtruth_hook.py` (its normal
   install location; override with `HARDTRUTH_HOOK_DIR` if different).
2. Copy this adapter into Pi's global extensions directory as a directory-style
   extension:

   ```
   mkdir -p ~/.pi/agent/extensions/hardtruth
   cp client/adapters/pi/hardtruth_hook_pi.py ~/.pi/agent/extensions/hardtruth/hardtruth_hook_pi.py
   mkdir -p ~/.pi/agent/extensions/hardtruth/hardtruth-extension
   cp client/adapters/pi/hardtruth-extension/index.ts ~/.pi/agent/extensions/hardtruth/hardtruth-extension/index.ts
   ```

   (Pi loads `~/.pi/agent/extensions/*/index.ts` automatically — see
   `docs/extensions.md` "Extension Locations" — but this machine's
   `~/.pi/agent/settings.json` also carries an explicit `"extensions"` allow-list, so
   step 3 below is required too.)

3. Back up and edit `~/.pi/agent/settings.json`:

   ```
   cp ~/.pi/agent/settings.json ~/.pi/agent/settings.json.bak-hardtruth-$(date +%Y%m%d-%H%M%S)
   ```

   Add `"+extensions/hardtruth/hardtruth-extension/index.ts"` to the `"extensions"`
   array (alongside the existing `"+extensions/skill-accessor.ts"` /
   `"+extensions/mcp-bridge/index.ts"` entries).

4. Restart `pi` (or run `/reload` inside a session).

## Verification performed

- Read `@earendil-works/pi-coding-agent`'s `docs/extensions.md`, `dist/index.d.ts`, and
  `dist/core/tools/bash.d.ts` to confirm event shapes (`tool_result`, `agent_settled`,
  `message_end`) and that `BashToolDetails` carries no numeric exit code.
- Did not run this extension inside a live `pi` session (would require editing this
  machine's live `~/.pi/agent` config, which is out of scope for this change per the
  hard rules). The Python translator (`hardtruth_hook_pi.py`) can be smoke-tested
  standalone: `echo '{"toolName":"bash","input":{"command":"true"},"isError":false,"sessionId":"t1","cwd":"/tmp"}' | python3 hardtruth_hook_pi.py tool`.

## Limitations

- `pi`'s Bash tool result carries no numeric exit code in its typed details (only
  `truncation`/`fullOutputPath`); the translator falls back to the `isError` boolean
  (0/1), so it cannot distinguish e.g. exit 2 from exit 1. It never fabricates a pass.
- A HardTruth escalation halt (3+ consecutive halts with no ledger state change) will
  keep re-halting with the same reason under `HARNESS=goose` (by design — "never fails
  open"). The extension caps forced re-injection at 5 consecutive halts and then falls
  back to a visible notification instead of looping forever; a human still needs to
  clear the halt counter file HardTruth reports in its reason to fully unstick it.
- Not installed or exercised against this machine's live Pi config (see hard rules).
