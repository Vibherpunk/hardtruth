# HardTruth Hybrid Verification Architecture

**Status:** APPROVED (Architectural Specification)
**Date:** 2026-09-20

## 1. Executive Summary & Design Philosophy
To definitively solve the security boundaries of an AI coding agent, HardTruth must adopt a **Hybrid Two-Tier Topology**. Relying solely on in-session lifecycle hooks (Tier 1) leaves the system vulnerable to shell environment manipulation, arbitrary file modification, and exit code spoofing by the agent. 

The new architecture splits verification into:
1. **Tier 1 (Inner Loop / Fast Feedback):** A patched client-side hook providing immediate, low-latency feedback to the agent while actively neutralizing shell bypasses and polling vulnerabilities.
2. **Tier 2 (Outer Loop / Hard Gate):** An external, daemon-executed ephemeral runner that independently verifies the repository state in an isolated, tamper-proof container before allowing completion.

---

## 2. Two-Tier Topology

### 2.1 Tier 1: In-Session Interceptor (Inner Loop)
Tier 1 runs inside the agent's interactive environment. It serves to guide the agent and prevent trivial hallucinations, but it operates under a zero-trust model.

**Addressing the 5 Audit Vulnerabilities:**
1. **Universal File Tracking (Git-Native):** 
   - *Fix:* Remove reliance on `write_to_file` interception. On every `PostToolUse`, the hook executes `git status --porcelain`. Any files marked `M` (modified), `A` (added), or `??` (untracked) that match source extensions will increment `source_files_modified`. This perfectly catches `sed`, `echo`, and arbitrary shell manipulations.
2. **Anchored Regex Spoofing Defense:** 
   - *Fix:* The transcript parser will anchor exactly to the system-generated log prefix: `re.search(r"^Created At: .*? The command exited with code (\d+)", content, flags=re.MULTILINE)`. This makes it impossible for an agent's `stdout` echo to spoof the exit code.
3. **Shell Operator Neutralization:** 
   - *Fix:* The hook will statically parse `CommandLine` for shell operators (`;`, `&&`, `||`, `|`). If a command contains a verification target (e.g., `pytest`) mixed with shell operators (e.g., `pytest ; true`), Tier 1 will flag the command as **TAINTED** and immediately reject it, forcing the agent to run tests plainly. Environment variables (`CI=1 pytest`) will be gracefully stripped during parsing.
4. **Generalized Test Resolution (Language Agnostic):** 
   - *Fix:* The state engine will map specific failure targets to their canonical "Suite Commands". A failing `npm run test:unit` will be resolved if the agent subsequently runs the encompassing `npm test` or `npm run test`. This will be implemented via a configurable hierarchy tree rather than naive string prefixes.

### 2.2 Tier 2: External Deterministic Hard Gate (Outer Loop)
Tier 2 is the ultimate arbiter of truth. It operates completely outside the agent's interactive shell control, ensuring an unadulterated execution environment.

**The Mechanism:**
- **Daemon-Executed Ephemeral Container:** When Tier 1 signals a stop attempt, the HardTruth Daemon (running externally on the host or a secure enclave) mounts the agent's workspace as a read-only volume into an ephemeral Docker/OrbStack container.
- **Immutable Verification:** The Daemon executes the canonical test suite (e.g., `npm test`, `pytest`, `cargo test`) inside this container. Because the agent has no access to this container, it cannot manipulate environment variables, spoof exit codes, or intercept the process.
- **The Result:** The Daemon parses the true, immutable exit code directly from the container process, returning the verdict to Tier 1.

---

## 3. The Handoff Contract

The communication protocol between the two tiers is strictly defined to prevent "fail open" conditions.

### 3.1 Handoff Trigger
1. The agent calls the `Stop` tool.
2. Tier 1 executes `handle_stop`. It verifies that no AST stubs exist, that `source_files_modified` is accounted for by in-session tests, and that DeBERTa-v3 NLI claims entail the premise.
3. **If Tier 1 passes:** The Tier 1 hook issues a blocking synchronous HTTP request to the Daemon:
   `POST /v1/verify/handoff`
   Payload: `{"workspace_path": "/path/to/repo", "conversationId": "..."}`

### 3.2 Feedback Loop & Rejection
1. The Daemon spins up the Tier 2 ephemeral container, executes the tests, and captures the result.
2. **If Tier 2 Fails:** The Daemon returns a `406 Not Acceptable` payload containing the container's raw stdout/stderr and exit code. 
3. **Agent Context Feedback:** Tier 1 receives the failure and blocks the stop attempt, returning a high-priority message to the agent:
   `decision: "continue"`
   `reason: "🚨 TIER 2 HARD GATE FAILED: The external deterministic runner found failing tests in a clean environment. Fix the following issues:\n\n<Container Output>"`
4. **If Tier 2 Passes:** The Daemon returns `200 OK`. Tier 1 allows the completion (`decision: "allow"`).

---

## 4. Concrete Implementation Plan

### 4.1 Target File Modifications (`/Users/ai/dev/hardtruth-fix`)

1. **`client/hardtruth_hook.py`**
   - Refactor `handle_post_tool_use` to replace tool interception with `subprocess.run(["git", "status", "--porcelain"])`.
   - Update `poll_transcript_for_step` with the heavily anchored regex.
   - Implement shell operator detection (`TAINTED` flag logic).
   - Add the Tier 2 handoff API call to `handle_stop`.

2. **`daemon/ledger.py`**
   - Update `get_premise()` failure resolution to support generic suite mappings (e.g., mapping `"npm test"` as a universal resolver for any `"npm run *"` failure).

3. **`daemon/tier2_runner.py` (NEW FILE)**
   - Implement the Python Docker SDK logic to spawn an ephemeral container, mount the read-only workspace volume, execute the detected test runner, and capture the immutable exit code.

4. **`daemon/app.py`**
   - Expose the `POST /v1/verify/handoff` API route, wired to `tier2_runner.py`.

### 4.2 Provisioning (`install.sh`)
Add an `install.sh` script to the repository root that provisions both tiers:
```bash
#!/usr/bin/env bash
# install.sh

echo "Provisioning Tier 2 (Daemon)..."
mkdir -p ~/.hardtruth
# Ensure daemon starts securely (via Docker Compose for the daemon itself, if preferred, or as a systemd/launchd background service)
docker compose -f docker/docker-compose.yml up -d

echo "Provisioning Tier 1 (Client Hook)..."
mkdir -p ~/.gemini/antigravity-cli/hooks
ln -sf $(pwd)/client/hardtruth_hook.py ~/.gemini/antigravity-cli/hooks/post_tool_use.py
ln -sf $(pwd)/client/hardtruth_hook.py ~/.gemini/antigravity-cli/hooks/stop.py
chmod +x ~/.gemini/antigravity-cli/hooks/*.py

echo "Hybrid Architecture Successfully Provisioned."
```

---

## 5. Ledger Security, Session Authenticity & Monotonicity (Round 7–9)

To ensure the Physical Ledger is completely immune to out-of-band manipulation, session hijacking, credential leakage, and out-of-order execution, the following security constraints are enforced at the protocol level:

### 5.1 Daemon API Authentication (Bearer Token)
- All state-changing endpoints (`POST /v1/ledger/record`, `POST /v1/session/baseline`, `POST /v1/verify/handoff`, `POST /v1/verify-claim`) and sensitive read endpoints (`GET /v1/ledger/premise`, `GET /v1/session/baseline`) require a shared bearer token (`HARDTRUTH_API_TOKEN` or `~/.hardtruth/daemon_api.key`, mode `0400`).
- Missing or invalid tokens return `HTTP 401 Unauthorized`. Constant-time comparison (`hmac.compare_digest`) prevents timing side-channel attacks.

### 5.2 Per-Record Session Authenticity
- **Single-Minting Session Secrets (`POST /v1/session/start`):** On session initialization, the daemon registers the `conversationId` and generates an ephemeral 256-bit cryptographic secret.
- **Idempotency Leak Prevention:** The secret is returned in the response payload **only once** upon creation. Subsequent idempotent calls for the same `conversationId` return `status: "already_active"` with `session_secret: null`. This prevents an agent holding the general API token from querying the endpoint to steal an active session's secret over the loopback network.
- **Local Key Storage:** The client hook caches this secret locally at `~/.hardtruth/halts/session_<conv_id>.key` with strict `0400` POSIX permissions, isolated from the Tier 2 container mount.
- **Per-Record Validation:** Every `/v1/ledger/record` and `/v1/ledger/premise` invocation requires an `X-Session-Secret` header matching the active session. Missing or mismatched secrets return `HTTP 403 Forbidden`.

### 5.3 Monotonic Step Sequencing
- The daemon ledger maintains an in-memory monotonic step counter per registered session.
- Any incoming record with `stepIdx < last_step_idx` is rejected with `HTTP 409 Conflict`.
- This strictly prohibits backdating, replayed steps, or retroactive failure masking, while preserving support for parallel tool executions within the same step index (`stepIdx == last_step_idx`).

### 5.4 Containerized Workspace Visibility & Decoupled CI
- `resolve_workspace_path` parses `HARDTRUTH_WORKSPACE_MAP` (longest-prefix match) to translate host paths into container mount paths. Unmapped workspaces fail closed with `status: "unverified_no_workspace"`.
- Ephemeral runners set `HARDTRUTH_TIER2_SANDBOX=1` to cleanly decouple sandbox isolation checks from standard `CI=true` runner environments, ensuring 100% CI coverage across unreachable, host, and live containerized daemons.

