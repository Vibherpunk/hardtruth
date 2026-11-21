import { hardtruthApiToken, hardtruthBaseUrl } from "../lib/config";
import { logger } from "../lib/logger";

export interface ToolReceipt {
  stepIdx: number;
  toolName: string;
  argsSummary: string;
  exitCode: number;
  isError: boolean;
  errorMsg?: string;
  outputTail: string;
  isTestCommand: boolean;
  wrotePath?: string;
  stubViolation?: string;
}

export interface HardTruthVerdict {
  action: "allow" | "correct" | "annotate";
  reason?: string;
  note?: string;
  contradictedClaims?: string[];
}

const sessionSecrets = new Map<string, string>();

const TEST_CMD_RE =
  /(?:^|[;&|\s])(?:pytest|py\.test|vitest|jest|cargo\s+test|go\s+test|(?:npm|pnpm|yarn|bun)\s+(?:--\S+\s+)*test|python3?(?:\.\d+)?\s+-m\s+(?:pytest|unittest))\b/i;

const TEST_PASS_CLAIM_RE =
  /\b(?:all\s+(?:\d+\s+)?tests?\s+(?:passed|pass|succeeded|green)|(?:tests?|test\s+suite|unit\s+tests?|integration\s+tests?)\s+(?:all\s+)?(?:passed|succeeded)|pytest\s+(?:passed|succeeded)|vitest\s+(?:passed|succeeded)|cargo\s+test\s+(?:passed|succeeded)|go\s+test\s+(?:passed|succeeded)|0\s+failed|\d+\s+passed\b(?!.*\bfailed))/i;

const COMPLETION_CLAIM_RE =
  /\b(?:verified|confirmed|tested\s+(?:end[- ]to[- ]end|and\s+(?:it\s+)?works|locally)|fixed\s+and\s+tested|saved\s+(?:the\s+)?tool|deployed|implemented\s+and\s+verified|everything\s+(?:works|passed))\b/i;

const IMPERATIVE_OR_HYPOTHETICAL_RE =
  /^\s*(?:please\s+|you\s+can\s+|try\s+|next[,:]?\s+|to\s+verify|run\s+|if\s+|should\s+|once\s+|note:|tip:|warning:)/i;

const CODE_EXT_RE = /\.(?:[cm]?[jt]sx?|py|rs|go)$/i;

export function conversationIdForRun(chatId: number, runId: number): string {
  return `amuse-chat-${chatId}-run-${runId}`;
}

export function isHardTruthEnabled(): boolean {
  return Boolean(hardtruthBaseUrl && hardtruthApiToken);
}

export function isTestCommandString(cmd: string): boolean {
  return TEST_CMD_RE.test(cmd || "");
}

export function stripNonClaimRegions(text: string): string {
  if (!text) return "";
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`\n]+`/g, " ")
    .replace(/^\s*>.*$/gm, " ");
}

export function detectCodeStubs(code: string, filename?: string): string | null {
  if (!code) return null;
  if (filename && !CODE_EXT_RE.test(filename)) return null;

  if (/\braise\s+NotImplementedError\b/.test(code)) {
    return "Python NotImplementedError stub detected";
  }
  if (/\btodo!\s*\(/.test(code)) {
    return "Rust todo!() stub detected";
  }
  if (/\bunimplemented!\s*\(/.test(code)) {
    return "Rust unimplemented!() stub detected";
  }
  if (/throw\s+new\s+Error\s*\(\s*["']not\s+implemented["']\s*\)|throw\s+new\s+NotImplemented/i.test(code)) {
    return "JS/TS NotImplemented stub detected";
  }
  if (/(?:\/\/|#)\s*TODO:\s*implement\b/i.test(code)) {
    return "TODO: implement placeholder detected";
  }
  if (/^[ \t]*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)\s*(?:->[^:\n]+)?:\s*\n([ \t]+)pass\s*(?:#.*)?$/m.test(code)) {
    return "Python empty pass function stub detected";
  }
  return null;
}

export function extractVerifiableClaims(replyText: string): {
  testPassClaims: string[];
  completionClaims: string[];
} {
  const cleaned = stripNonClaimRegions(replyText);
  const rawSentences = cleaned
    .split(/(?<=[.!?])\s+|\n+/)
    .map((s) => s.trim())
    .filter((s) => s.length >= 6 && s.length <= 400);

  const testPassClaims: string[] = [];
  const completionClaims: string[] = [];

  for (const s of rawSentences) {
    if (s.endsWith("?")) continue;
    if (IMPERATIVE_OR_HYPOTHETICAL_RE.test(s)) continue;
    if (/\b(?:should\s+pass|would\s+pass|will\s+pass|if\s+tests?\s+pass)\b/i.test(s)) continue;
    if (TEST_PASS_CLAIM_RE.test(s)) {
      testPassClaims.push(s);
    } else if (COMPLETION_CLAIM_RE.test(s)) {
      completionClaims.push(s);
    }
  }
  return { testPassClaims, completionClaims };
}

export function buildRunPremise(receipts: ToolReceipt[]): string {
  const sections: string[] = [];

  // Track latest outcome per command so a subsequent passing retry resolves an earlier failure,
  // matching HardTruth daemon/ledger.py unresolved_failures semantics.
  const unresolvedFailures = new Map<string, ToolReceipt>();
  for (const r of receipts) {
    if (r.isTestCommand || r.toolName === "terminal_run" || r.toolName === "run_code" || r.toolName === "create") {
      if (r.isError || r.exitCode !== 0) {
        unresolvedFailures.set(r.argsSummary, r);
      } else {
        unresolvedFailures.delete(r.argsSummary);
      }
    }
  }

  // SECTION 1: UNRESOLVED FAILURES (matches daemon/ledger.py)
  if (unresolvedFailures.size > 0) {
    const fails = [...unresolvedFailures.values()].slice(-4).map((f) => {
      const ecStr = `exit code ${f.exitCode}`;
      const tailSource = (f.errorMsg || f.outputTail || "").replace(/\s+/g, " ").trim().slice(0, 300);
      const tail = tailSource ? ` | Output: ${tailSource}` : "";
      return `FAILED: '${f.argsSummary}' (${ecStr}${tail})`;
    });
    sections.push(`UNRESOLVED TEST FAILURES (CRITICAL): ${fails.join("; ")}.`);
  }

  // SECTION 2: RECENT EXECUTIONS (up to 6, matching daemon/ledger.py)
  if (receipts.length > 0) {
    const cmdStrs = receipts.slice(-6).map((c) => {
      const statusDesc =
        !c.isError && c.exitCode === 0
          ? "SUCCEEDED (exit 0)"
          : c.errorMsg
            ? `FAILED (exit ${c.exitCode}: ${c.errorMsg.replace(/\s+/g, " ").trim().slice(0, 150)})`
            : `FAILED (exit ${c.exitCode})`;
      const cleanTail = c.outputTail.replace(/\s+/g, " ").trim().slice(0, 300);
      const tailStr = cleanTail ? ` [Tail: ${cleanTail}]` : "";
      return `COMMAND: '${c.argsSummary}'. STATUS: ${statusDesc}${tailStr}.`;
    });
    sections.push(`RECENT EXECUTIONS: ${cmdStrs.join(" ")}`);
  } else {
    sections.push("RECENT EXECUTIONS: No commands or test suites were executed.");
  }

  // SECTION 3: MODIFIED FILES (matches daemon/ledger.py)
  const writtenPaths = [...new Set(receipts.map((r) => r.wrotePath).filter((p): p is string => Boolean(p)))];
  if (writtenPaths.length > 0) {
    sections.push(`MODIFIED FILES: ${writtenPaths.join(", ")}.`);
  } else {
    sections.push("MODIFIED FILES: No files were modified.");
  }

  // SECTION 4: STUB VIOLATIONS (only emitted when present)
  const stubs = unresolvedStubs(receipts);
  if (stubs.length > 0) {
    sections.push(
      `STUB VIOLATIONS: ${stubs.map((s) => `${s.wrotePath ?? s.toolName}: ${s.stubViolation}`).join("; ")}.`,
    );
  }

  return sections.join(" ").slice(0, 8000);
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

export function buildToolReceipt(
  stepIdx: number,
  toolName: string,
  args: unknown,
  rawResultJson: string,
): ToolReceipt {
  const a = asRecord(args);
  let parsed: Record<string, unknown> = {};
  try {
    parsed = asRecord(JSON.parse(rawResultJson || "{}"));
  } catch {
    parsed = {};
  }

  const errField = typeof parsed.error === "string" && parsed.error ? parsed.error : undefined;
  let exitCode = 0;
  let isError = Boolean(errField);
  let isTestCommand = false;
  let argsSummary = toolName;
  let wrotePath: string | undefined;
  let stubViolation: string | undefined;

  if (toolName === "terminal_run") {
    const cmd = typeof a.command === "string" ? a.command : "";
    argsSummary = cmd.slice(0, 200) || "terminal_run";
    isTestCommand = isTestCommandString(cmd);
    if (typeof parsed.exit_code === "number") {
      exitCode = parsed.exit_code;
      if (exitCode !== 0) isError = true;
    } else if (isError) {
      exitCode = 1;
    }
  } else if (toolName === "run_code" || (toolName === "create" && a.action === "run_code")) {
    const codeArgs = toolName === "create" ? asRecord(a.args) : a;
    argsSummary = `run_code(${String(codeArgs.code ?? "").slice(0, 80)})`;
    if (parsed.ok === false) {
      isError = true;
      exitCode = 1;
    } else if (isError) {
      exitCode = 1;
    }
  } else if (toolName === "save_tool" || (toolName === "create" && a.action === "save_tool")) {
    const saveArgs = toolName === "create" ? asRecord(a.args) : a;
    const slug = typeof saveArgs.name === "string" ? saveArgs.name : "tool";
    const code = typeof saveArgs.code === "string" ? saveArgs.code : "";
    wrotePath = `skills/${slug}/tool.js`;
    argsSummary = `save_tool(${slug})`;
    if (!isError && code) {
      stubViolation = detectCodeStubs(code, wrotePath) ?? undefined;
    }
  } else if (toolName === "write_file" || (toolName === "files" && a.action === "write")) {
    const writeArgs = toolName === "files" ? asRecord(a.args) : a;
    const p = typeof writeArgs.path === "string" ? writeArgs.path : undefined;
    const content = typeof writeArgs.content === "string" ? writeArgs.content : "";
    wrotePath = p;
    argsSummary = `${toolName}(${p ?? ""})`;
    if (!isError && p && content) {
      stubViolation = detectCodeStubs(content, p) ?? undefined;
    }
  } else {
    if (isError) exitCode = 1;
    argsSummary = `${toolName}(${JSON.stringify(a).slice(0, 120)})`;
  }

  const outStr =
    typeof parsed.stdout === "string"
      ? `${parsed.stdout}${typeof parsed.stderr === "string" && parsed.stderr ? `\n${parsed.stderr}` : ""}`
      : rawResultJson;

  return {
    stepIdx,
    toolName,
    argsSummary,
    exitCode,
    isError,
    errorMsg: errField ?? (isError && typeof parsed.stderr === "string" ? parsed.stderr.slice(0, 300) : undefined),
    outputTail: (outStr || "").slice(-1000),
    isTestCommand,
    wrotePath,
    stubViolation,
  };
}

function resolveEndpoint(baseUrl?: string, token?: string): { url: string; tok: string } | null {
  const url = (baseUrl ?? hardtruthBaseUrl ?? "").trim().replace(/\/$/, "");
  const tok = (token ?? hardtruthApiToken ?? "").trim();
  if (!url || !tok) return null;
  return { url, tok };
}

export async function startSession(
  convId: string,
  workspacePath = "/data",
  baseUrl?: string,
  token?: string,
): Promise<string | null> {
  const ep = resolveEndpoint(baseUrl, token);
  if (!ep) return null;
  try {
    const res = await fetch(`${ep.url}/v1/session/start`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${ep.tok}`,
      },
      body: JSON.stringify({ conversationId: convId, workspace_path: workspacePath }),
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { session_secret?: string | null };
    if (data.session_secret) {
      sessionSecrets.set(convId, data.session_secret);
      return data.session_secret;
    }
    return null;
  } catch (err) {
    logger.debug({ err, convId }, "hardtruth startSession skipped");
    return null;
  }
}

export function endSession(convId: string): void {
  sessionSecrets.delete(convId);
}

export async function recordToolExecution(
  convId: string,
  receipt: ToolReceipt,
  baseUrl?: string,
  token?: string,
): Promise<void> {
  const ep = resolveEndpoint(baseUrl, token);
  if (!ep) return;
  const headers: Record<string, string> = {
    "content-type": "application/json",
    authorization: `Bearer ${ep.tok}`,
  };
  const secret = sessionSecrets.get(convId);
  if (secret) headers["x-session-secret"] = secret;

  try {
    await fetch(`${ep.url}/v1/ledger/record`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        conversationId: convId,
        stepIdx: receipt.stepIdx,
        tool: receipt.toolName === "terminal_run" ? "run_command" : receipt.toolName,
        target: receipt.argsSummary.slice(0, 2000),
        observed_exit_code: receipt.exitCode,
        harness_status: receipt.isError ? "error" : "no_error",
        error: receipt.isError ? (receipt.errorMsg ?? `exit ${receipt.exitCode}`).slice(0, 500) : null,
        stdout_tail: receipt.outputTail.slice(-1500),
        cwd: "/data",
      }),
      signal: AbortSignal.timeout(2000),
    });
  } catch (err) {
    logger.debug({ err, convId, tool: receipt.toolName }, "hardtruth recordToolExecution skipped");
  }
}

export async function recordGateVerdict(
  convId: string,
  verdict: "ALLOW" | "HALT",
  reason: string,
  latencyMs: number,
  baseUrl?: string,
  token?: string,
): Promise<void> {
  const ep = resolveEndpoint(baseUrl, token);
  if (!ep) return;
  try {
    await fetch(`${ep.url}/v1/gate/verdict`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${ep.tok}`,
      },
      body: JSON.stringify({
        conversationId: convId,
        verdict,
        reason: reason.slice(0, 1000),
        latency_ms: Math.max(0, Number(latencyMs.toFixed(2))),
      }),
      signal: AbortSignal.timeout(2000),
    });
  } catch (err) {
    logger.debug({ err, convId, verdict }, "hardtruth recordGateVerdict skipped");
  }
}

function unresolvedStubs(receipts: ToolReceipt[]): ToolReceipt[] {
  const latestByPath = new Map<string, ToolReceipt>();
  for (const r of receipts) {
    if (r.wrotePath) latestByPath.set(r.wrotePath, r);
  }
  return [...latestByPath.values()].filter((r) => Boolean(r.stubViolation));
}

export async function verifyReply(opts: {
  convId: string;
  replyText: string;
  receipts: ToolReceipt[];
  alreadyCorrected: boolean;
  baseUrl?: string;
  token?: string;
}): Promise<HardTruthVerdict> {
  const started = Date.now();
  const ep = resolveEndpoint(opts.baseUrl, opts.token);

  // 1. Deterministic stub guard on code written in this run
  const stubs = unresolvedStubs(opts.receipts);
  if (stubs.length > 0) {
    const first = stubs[0]!;
    const reason = `Unimplemented stub detected in ${first.wrotePath ?? first.toolName}: ${first.stubViolation}`;
    if (ep) void recordGateVerdict(opts.convId, "HALT", reason, Date.now() - started, ep.url, ep.tok);
    return {
      action: opts.alreadyCorrected ? "annotate" : "correct",
      reason,
      note: opts.alreadyCorrected
        ? `${reason}.`
        : `HardTruth rejected your written code because ${reason}. Replace the stub with a complete implementation or report what remains unfinished.`,
    };
  }

  const { testPassClaims, completionClaims } = extractVerifiableClaims(opts.replyText);

  // 2 & 3. Deterministic test-pass grounding against session receipts
  if (testPassClaims.length > 0) {
    const testReceipts = opts.receipts.filter((r) => r.isTestCommand);
    const hasPassingTest = testReceipts.some((r) => !r.isError && r.exitCode === 0);
    let lastPassedIdx = -1;
    let lastFailedIdx = -1;
    testReceipts.forEach((r, idx) => {
      if (!r.isError && r.exitCode === 0) lastPassedIdx = idx;
      else lastFailedIdx = idx;
    });

    if (!hasPassingTest) {
      const reason = "Claimed tests passed, but no passing test command was executed in this run.";
      if (ep) void recordGateVerdict(opts.convId, "HALT", reason, Date.now() - started, ep.url, ep.tok);
      return {
        action: opts.alreadyCorrected ? "annotate" : "correct",
        reason,
        contradictedClaims: testPassClaims,
        note: opts.alreadyCorrected
          ? "Claimed tests passed, but no passing test command ran in this session."
          : `HardTruth blocked an unverified test-pass claim ("${testPassClaims[0]}"). No passing test command ran in this run. Either run the test suite now or retract the claim and state accurately what ran.`,
      };
    }

    if (lastFailedIdx > lastPassedIdx) {
      const failed = testReceipts[lastFailedIdx]!;
      const reason = `Claimed tests passed, but the latest test run (${failed.argsSummary}) exited ${failed.exitCode}.`;
      if (ep) void recordGateVerdict(opts.convId, "HALT", reason, Date.now() - started, ep.url, ep.tok);
      return {
        action: opts.alreadyCorrected ? "annotate" : "correct",
        reason,
        contradictedClaims: testPassClaims,
        note: opts.alreadyCorrected
          ? `Claimed tests passed, though '${failed.argsSummary}' exited ${failed.exitCode}.`
          : `HardTruth blocked a contradiction ("${testPassClaims[0]}"). Your latest test run (${failed.argsSummary}) failed with exit code ${failed.exitCode}. Fix and re-run tests or report the failure accurately.`,
      };
    }
  }

  // 4. ModernBERT NLI verification via HardTruth daemon
  const candidates = [...testPassClaims, ...completionClaims].slice(0, 8);
  if (ep && candidates.length > 0) {
    const premise = buildRunPremise(opts.receipts);
    try {
      const res = await fetch(`${ep.url}/v1/verify-claims`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${ep.tok}`,
        },
        body: JSON.stringify({
          premise,
          hypotheses: candidates,
          threshold: 0.72,
        }),
        signal: AbortSignal.timeout(5000),
      });
      if (res.ok) {
        const body = (await res.json()) as {
          results?: Array<{ hypothesis: string; status: string; confidence: number }>;
        };
        const contradicted = (body.results ?? []).filter(
          (r) => r.status === "CONTRADICTION" && Number(r.confidence) >= 0.72,
        );
        if (contradicted.length > 0) {
          const top = contradicted[0]!;
          const reason = `NLI contradiction (${top.confidence.toFixed(2)}): "${top.hypothesis}" vs "${premise}"`;
          void recordGateVerdict(opts.convId, "HALT", reason, Date.now() - started, ep.url, ep.tok);
          return {
            action: opts.alreadyCorrected ? "annotate" : "correct",
            reason,
            contradictedClaims: contradicted.map((c) => c.hypothesis),
            note: opts.alreadyCorrected
              ? `Unverified or contradicted claim detected: "${top.hypothesis}".`
              : `HardTruth NLI detected a contradiction between your reply ("${top.hypothesis}") and the observed execution receipts (${premise}). Revise your reply so every claim matches actual tool outcomes.`,
          };
        }
      }
    } catch (err) {
      logger.debug({ err, convId: opts.convId }, "hardtruth verify-claims unreachable; skipping NLI pass");
    }
  }

  if (ep && (candidates.length > 0 || opts.receipts.length > 0)) {
    void recordGateVerdict(opts.convId, "ALLOW", "Claims grounded against run receipts", Date.now() - started, ep.url, ep.tok);
  }
  return { action: "allow" };
}
