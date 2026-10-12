# HARDTRUTH SPECIFICATION: COMPREHENSIVE SOURCE OF TRUTH (v2.1)
## Autonomous Anti-Hallucination & Anti-Drift Execution Gate for Coding Agents

---

### 1. MISSION & SCOPE (NON-NEGOTIABLE INVARIANTS)

* **Primary Purpose:** Prevent autonomous coding models (`agy`, Claude Code, Goose, local LLMs) from hallucinating progress, fabricating code/test assertions, and drifting over long multi-turn execution loops.
* **Core Invariant:** "Evidence Before Assertions." An agent is physically blocked from completing its session until every action it claims to have taken (files created/modified, commands run, test suites passed) is corroborated by immutable OS- and git-level evidence.
* **Explicit Anti-Scope:** HardTruth explicitly rejects the "Stoin Universal Evidence Contract" and generalized multi-domain enterprise attestation frameworks (accounting, PMS, etc.). HardTruth is strictly a deterministic, language-aware execution and integrity gate for software development.
* **Security Invariant:** HardTruth must not increase the security risk of the host computer. Untrusted agent-written code must never execute within the verifier daemon's security domain where signing keys and physical ledgers reside.

---

### 2. THREAT MODEL & TRUST BOUNDARIES

* **Tier 0 (Confabulation & Drift - Primary Focus):** The model is not actively attacking the host OS, but due to context truncation, needle-in-haystack degradation, or sycophancy, it hallucinates that it wrote code, executed commands, or fixed bugs when it did not.
* **Tier 1 (Goal-Directed Evasion):** The agent operates with shell execution privileges and will attempt shortcuts to satisfy stop conditions (e.g. `git stash`, modifying test assertions to `assert True`, `pytest || true`, renaming test files, editing config switches). HardTruth must resist Tier 1 evasion deterministically.
* **Tier 2 (Root OS Compromise):** An attacker with full root access or hardware control is out of scope. HardTruth assumes standard user-space isolation.
* **Trust Boundary Invariant:** The verifier daemon is authoritative; the agent hook is an untrusted probe. All enforcement policies, timeouts, and kill switches are configured daemon-side. The agent cannot disable Tier 2 or clear halts via local environment variables.

---

### 3. HARNESS ADAPTER CONTRACT

HardTruth integrates across different agent runtime harnesses. Each harness requires an exact adapter mapping:

| Harness | Stop Event | Tool Event | Blocking Verb / Schema | Exit Code Handling | Timeout Budget |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Antigravity (`agy`)** | `Stop` | `PostToolUse` | `{"decision": "continue", "reason": "..."}` | Stdout string regex + ExitCode | Hook: 90s, Tier 2: 60s |
| **Claude Code** | `Stop` | `PostToolUse` | `{"decision": "block", "reason": "..."}` | ExitCode / stderr parsing | Hook: 90s, Tier 2: 60s |
| **Goose** | `on_stop` | `post_tool_execution` | `{"action": "halt", "message": "..."}` | Process exit code | Hook: 90s, Tier 2: 60s |

* **Timeout Invariant:** The installer must ensure `hook_timeout >= tier2_timeout + 25s`. If Tier 2 is allocated 60 seconds, the client harness hook timeout must be set to at least 85–90 seconds.

---

### 4. EVASION REGISTER & HARDENED DEFENSES

* **E1: `git stash` / Dirty Tree Cleanse:** 
  * *Vector:* Agent stashes broken code before stop so `git status` reads clean and Rule 1 vacuously passes.
  * *Defense:* Halt if `git stash list` is non-empty at stop, or if files recorded as modified in the session ledger do not appear in the cumulative baseline diff.
* **E2: Test Runner Substitution / Suppression:**
  * *Vector:* Agent adds `pytest.ini` with `addopts = -k none` or creates dummy test directories to fool detection.
  * *Defense:* The canonical test runner is detected and pinned at **Session Bootstrap (Step 0)** and locked into the ledger; it is never re-detected at stop.
* **E3: Assertion Weakening & Test Dropping (Rule 5):**
  * *Vector:* Agent changes `assert func() == True` to `assert True` or adds `@pytest.mark.skip` / `test.skip`.
  * *Defense:* Compare test AST / assertion counts against baseline git blobs (`git show baseline:path`). Halt if test identifiers disappear, assertion counts decrease, or skip decorators are added without human waiver.
* **E4: Branch & History Rewriting:**
  * *Vector:* Agent switches branches or checks out detached HEAD to hide diffs.
  * *Defense:* Enforce `git merge-base --is-ancestor baseline HEAD`.
* **E5: Shell Masking & Taint Categories:**
  * *Vector:* Agent uses `pytest || true` or `; exit 0` to fake success.
  * *Defense:* Categorize commands into:
    1. *Masking* (`|| true`, `; exit 0`, `|| echo pass`) $\rightarrow$ **TAINTED FAILURE** (permanent until resolved).
    2. *Exit-Preserving* (`cd dir && pytest`, env prefixes `CI=1 pytest`) $\rightarrow$ **VALID VERIFICATION COMMAND**; extract effective directory.
    3. *Exit-Lossy* (unpiped `|` without pipefail, uncaptured subshells) $\rightarrow$ `UNCORROBORATED_EXECUTION` (ignored: neither credit nor failure).
* **E6: Linter Substituted for Tests:**
  * *Vector:* Agent runs `ruff` or `eslint` to satisfy "verification command" requirement.
  * *Defense:* Split patterns into `TEST_EXECUTION` vs. `STATIC_CHECK`. Rule 1 strictly requires at least one passing `TEST_EXECUTION`.
* **E7: Synthetic Exit 0 Fallback Elimination:**
  * *Vector:* `handle_post_tool_use()` defaulting to `observed_exit_code = 0` when transcript path is missing.
  * *Defense:* Never synthesize `0`. If not corroborated by transcript or process runner metadata, mark as `UNCORROBORATED`.
* **E8: Transcript Spoofing:**
  * *Vector:* Agent writes fake completion strings directly to transcript files.
  * *Defense:* HardTruth cross-validates transcript entries against OS process execution receipts and daemon ledger records.

---

### 5. EXECUTION PIPELINE SPECIFICATION

When `handle_stop()` triggers, execution follows this strict sequence:

```
              [Step 0: Session Bootstrap (at Init)]
   (Capture baseline_sha, pin test runner, snapshot test ASTs, lock ledger)
                                ↓
                 [Step 1: Check Halt Circuit Breaker]
     (Differentiate agent-remediable halts vs. environment-undetermined)
                                ↓
              [Step 2: Universal Git & Worktree Integrity]
      (Verify merge-base ancestry, empty stash, and cumulative diffs)
                                ↓
            [Step 3: Polyglot Anti-Stubbing Linter (Rule 3)]
        (Scan changed hunks in TS, JS, PY, RS, GO for empty stubs)
                                ↓
           [Step 4: Source vs. Verification Check (Rule 1)]
   (If source changed, at least 1 TEST_EXECUTION command must have run)
                                ↓
           [Step 5: Unresolved Failure Check (Rule 2)]
  (All recorded command failures must have matching clean resolutions)
                                ↓
        [Step 6: Test-Surface Monotonicity Audit (Rule 5)]
  (Verify no deleted assertions, dropped test cases, or new skip markers)
                                ↓
       [Step 7: Deterministic Claim-to-Action Grounding (Rule 4A)]
 (Extract file paths & test commands from unstripped prose; verify in ledger)
                                ↓
          [Step 8: Semantic Contradiction Fallback (Rule 4B)]
         (DeBERTa-v3 fallback for high-level narrative assertions)
                                ↓
             [Step 9: Tier 2 Independent Runner Handoff]
 (Clean containerized execution; regression-based comparison against baseline)
                                ↓
                        [DECISION: ALLOW]
```

#### Step 0: Session Bootstrap
* Upon session start (`v1/session/start` or first tool hook), HardTruth registers:
  1. `baseline_commit_sha` via `git rev-parse HEAD`.
  2. Pinned canonical test runner (e.g. `bun test`, `pytest`, `cargo test`).
  3. Baseline test surface metrics (set of test IDs, assertion counts per test file).
  4. Regression baseline: run Tier 2 once on baseline commit. Record any pre-existing failures (`baseline_failures`).

#### Step 3: Polyglot Anti-Stubbing (Hunk-Scoped)
* Only inspects lines modified by the agent (hunks in `git diff baseline..HEAD`).
* Detects:
  * Python: empty `pass`, `...`, `raise NotImplementedError` in non-exception, non-abstract methods.
  * TypeScript/JavaScript: `throw new Error("Not implemented")`, `// TODO: implement`, empty function blocks.
  * Rust/Go: `todo!()`, `unimplemented!()`, `panic("not implemented")`.

#### Step 7: Rule 4A - Deterministic Claim-to-Action Grounding
* Operates on **unstripped agent prose** (including markdown backticks and tables).
* Extracts referenced file paths. If the prose affirms creating or modifying path `P`:
  * Path `P` must exist in `git diff --name-only baseline..HEAD` with non-trivial hunks.
  * If claimed and missing: **HARD REJECT (Claimed File Modification Not Corroborated)**.
* Extracts test execution assertions (e.g., *"I ran `cargo test` and all checks passed"*):
  * Ledger must contain a matching `TEST_EXECUTION` command with confirmed `exit 0` run *after* the latest file edit.
  * If claimed and missing: **HARD REJECT (Claimed Test Execution Not Corroborated)**.

#### Step 9: Regression-Based Tier 2 Hard Gate
* **Isolation:** Disposable container with `--security-opt no-new-privileges`, `--cap-drop ALL`, memory/cpu limits, `--network none`.
* **Dependencies:** Mount host dependency cache (`node_modules`, `.venv`) as strictly read-only (`:ro`).
* **Evaluation:** Tier 2 passes if:
  $$\text{stop\_failures} \subseteq \text{baseline\_failures}$$
  Any *new* failure not present at baseline halts the gate. Pre-existing failures do not block the agent.

---

### 6. CONCURRENCY & LEDGER INTEGRITY

* **Serialized Ledger Appends:** Ledger writes must acquire an exclusive `flock()` and in-process mutex. `prev_hash` is re-read under the lock before computing the HMAC digest and appending the record. Parallel tool calls can never create broken hash chains.
* **Fail-Closed Principle:** Every pipeline step returns `PASS`, `HALT`, or `UNDETERMINED`. Exceptions or unreachable dependencies return `UNDETERMINED` (halts execution; does not burn the agent's 3-strike escalation budget).

---

### 7. VERIFICATION & ADVERSARIAL EVALUATION HARNESS

* HardTruth v2.1 must pass a dedicated adversarial evaluation suite before deployment:
  1. **Red Corpus (E1–E10 Scenarios):** Automated test runs attempting each evasion vector (stashing, assertion weakening, stubbing, prose lying, shell masking, synthetic exit codes). Required outcome: 100% caught (`HALT`).
  2. **Green Corpus:** $\ge 30$ legitimate completion scenarios (pure refactors, new tests added, `cd dir && test`, pre-existing red tests, doc-only edits). Required outcome: 100% clean `ALLOW` (False Positive Rate $\le 2\%$).
  3. **Host Safety:** Subprocess environment scrub and container permission drops verified against credential exfiltration attempts.
