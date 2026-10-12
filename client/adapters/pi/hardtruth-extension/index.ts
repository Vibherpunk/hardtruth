/**
 * HardTruth <-> Pi extension.
 *
 * Wires Pi's (@earendil-works/pi-coding-agent) extension event system into the
 * HardTruth anti-hallucination circuit breaker, the same way HardTruth already
 * guards Goose (see goose-plugin-hardtruth/hardtruth_hook_goose.py + hooks/hooks.json
 * in this repo) and Claude Code (client/hardtruth_hook.py's own transcript adapter).
 *
 * This extension does not modify client/hardtruth_hook.py. It only shells out to
 * client/adapters/pi/hardtruth_hook_pi.py, a small translator script that speaks
 * the hook's stable Python ABI (handle_post_tool_use / handle_stop).
 *
 * Wiring:
 *   - `tool_result`   -> hardtruth_hook_pi.py tool  -> records ground-truth exit codes
 *                        into the execution ledger. Does not block (mirrors Goose's
 *                        PostToolUse hook, which is record-only).
 *   - `agent_settled` -> hardtruth_hook_pi.py stop  -> verifies the just-finished turn's
 *                        claims against the ledger. If it returns a halt, Pi has already
 *                        gone idle (this event fires only when Pi will not continue on its
 *                        own), so the only way to keep a false claim from standing as Pi's
 *                        final answer is to inject a new user turn -- there is no
 *                        Claude-Code-style "block this Stop" return value for agent_settled.
 *                        We use `pi.sendUserMessage(reason)`, which (per docs/extensions.md
 *                        "pi.sendUserMessage") sends immediately and always triggers a new
 *                        turn when Pi is idle. This is a genuine block: the agent run does
 *                        not end on the unverified claim, it is forced to keep going with
 *                        the HardTruth rejection reason as its next instruction.
 *
 * Limitation (reported honestly): a hard escalation halt (HardTruth's own circuit breaker,
 * fired after 3 consecutive halts with no ledger state change -- see fail_halt() in
 * hardtruth_hook.py) will keep re-halting with the same reason, since HARDTRUTH_HARNESS=goose
 * "never fails open" the way HARNESS=claude_code does. Left unchecked this could loop
 * pi.sendUserMessage forever. This extension adds its own local safety valve on top
 * (MAX_CONSECUTIVE_INJECTIONS below) that Claude Code's own harness gets from the hook
 * itself; Goose's existing adapter has no such local safety valve today and carries the
 * same theoretical risk, so this is a strict improvement, not a new one.
 */
import { spawn } from "node:child_process";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const HOOK_SCRIPT = path.join(__dirname, "..", "hardtruth_hook_pi.py");
const PYTHON_BIN = process.env.HARDTRUTH_PYTHON || "python3";
const TOOL_TIMEOUT_MS = 30_000; // mirrors goose-plugin-hardtruth/hooks/hooks.json PostToolUse timeout
const STOP_TIMEOUT_MS = 300_000; // mirrors goose-plugin-hardtruth/hooks/hooks.json Stop timeout
const MAX_CONSECUTIVE_INJECTIONS = 5; // local safety valve, see module docstring above

function runHook(mode: "tool" | "stop", payload: unknown, timeoutMs: number): Promise<any> {
  return new Promise((resolve) => {
    let settled = false;
    const child = spawn(PYTHON_BIN, [HOOK_SCRIPT, mode], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try {
        child.kill("SIGKILL");
      } catch {
        /* ignore */
      }
      resolve({ action: "continue", _hardtruthError: `hardtruth_hook_pi.py ${mode} timed out after ${timeoutMs}ms` });
    }, timeoutMs);

    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      // Fail open on infra errors (missing python3, missing hook install) rather than
      // blocking every tool call / turn end because HardTruth isn't installed yet.
      resolve({ action: "continue", _hardtruthError: `spawn failed: ${String(err)}` });
    });
    child.on("close", () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        resolve(JSON.parse(stdout || "{}"));
      } catch {
        resolve({ action: "continue", _hardtruthError: `unparseable hook output: ${stdout} ${stderr}` });
      }
    });

    child.stdin.write(JSON.stringify(payload));
    child.stdin.end();
  });
}

export default function (pi: ExtensionAPI) {
  let stepIdx = 0;
  let lastAssistantText = "";
  let consecutiveInjections = 0;

  pi.on("tool_result", async (event: any, ctx: ExtensionContext) => {
    const evt = {
      toolName: event.toolName,
      input: event.input ?? {},
      isError: !!event.isError,
      // Pi's tool_result doesn't carry a numeric exit code (see
      // hardtruth_hook_pi.py's build_post_tool_payload docstring for why); the
      // translator derives 0/1 from isError.
      exitCode: undefined,
      sessionId: ctx.sessionManager?.getSessionId?.(),
      cwd: ctx.cwd,
      stepIdx: stepIdx++,
    };
    await runHook("tool", evt, TOOL_TIMEOUT_MS);
    // Record-only, matching Goose's PostToolUse hook: never blocks tool execution.
    // (Returning nothing here leaves the tool result untouched.)
  });

  pi.on("message_end", async (event: any) => {
    if (event?.message?.role !== "assistant") return;
    const content = event.message.content;
    if (typeof content === "string") {
      lastAssistantText = content;
    } else if (Array.isArray(content)) {
      const hasTool = content.some((p: any) => p?.type === "tool_use" || p?.type === "tool_call");
      const text = content
        .filter((p: any) => p?.type === "text")
        .map((p: any) => p.text)
        .join("\n");
      if (text && !hasTool) lastAssistantText = text;
    }
  });

  pi.on("agent_settled", async (_event: any, ctx: ExtensionContext) => {
    const evt = {
      sessionId: ctx.sessionManager?.getSessionId?.(),
      cwd: ctx.cwd,
      agentText: lastAssistantText,
    };
    const result = await runHook("stop", evt, STOP_TIMEOUT_MS);

    if (result?.action === "halt") {
      const reason: string = result.message || "HardTruth rejected this turn's claims.";
      if (consecutiveInjections >= MAX_CONSECUTIVE_INJECTIONS) {
        // Local safety valve: stop force-injecting after N consecutive halts and fall
        // back to a visible, non-blocking notice so the user isn't trapped forever if
        // the hook itself is stuck (e.g. daemon down and HardTruth escalation-halting).
        ctx.ui.notify(
          `HardTruth: ${MAX_CONSECUTIVE_INJECTIONS} consecutive halts, no longer auto-injecting. Last reason: ${reason}`,
          "warning",
        );
        consecutiveInjections = 0;
        return;
      }
      consecutiveInjections++;
      lastAssistantText = "";
      // Pi is idle here (agent_settled only fires when Pi will not continue on its
      // own), so sendUserMessage delivers immediately and triggers a new turn -- this
      // is the actual block: the run does not end on the unverified claim.
      pi.sendUserMessage(reason);
    } else {
      consecutiveInjections = 0;
    }
  });
}
