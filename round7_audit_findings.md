# Round 7 Adversarial Audit Findings

Date: 2026-09-19 (session takeover verification)
Baseline: `af1b822` on `feat/ledger-integrity` — 55/55 tests passing.
Live daemon: ephemeral container `hardtruth-daemon` (health OK, ledger chain VALID, 1083 records).

Two vectors were **confirmed live** against the running daemon. Both were probed with
throwaway conversation IDs (`spotcheck-*`, `forge-test-*`); all writes are append-only
and the cryptographic chain remained valid throughout (1080 → 1083 records).

---

## Finding A — Unauthenticated ledger-write API enables premise forgery (HIGH)

**Endpoint:** `POST /v1/ledger/record` — no authentication, no per-writer binding.
The only middleware guard is the 2MB payload size limit; Pydantic `max_length` bounds
limit field size but not who may write.

**The premise synthesis trusts any record whose `tool == "run_command"`, including
client-supplied `observed_exit_code`, `target`, and `cwd`:**

- `daemon/ledger.py::get_premise` only processes records with `tool == "run_command"`.
- A record with `observed_exit_code == 0` increments `verification_commands_executed`
  and resolves every matching prior failure via `can_suite_resolve_failure`
  (same target + same/descendant CWD → resolved).

**Live proof:**

1. `POST /v1/ledger/record` with `tool="run_command"`,
   `target="pytest tests/test_daemon_ledger.py"`, `exit 1`, CONV=`forge-test-*`
   → premise: `UNRESOLVED TEST FAILURES (CRITICAL): FAILED: ... (exit code 1)`,
   `verification_commands_executed: 0`.
2. `POST /v1/ledger/record` with the **same target**, `exit 0`, direct API call
   → premise now: `STATUS: SUCCEEDED (exit 0)`, `unresolved_failures: []`,
   `verification_commands_executed: 1`.

**Consequences for the hook's stop gate (`client/hardtruth_hook.py::handle_stop`):**

- Rule 1 (source modified, no verification) — **defeated**: forged record inflates
  `verification_commands_executed`.
- Rule 2 (unresolved failures forbid termination) — **defeated**: forged success
  clears the genuine failure from the premise.
- Rule 4 (NLI claim adjudication) — **weakened**: the premise fed to DeBERTa-v3 now
  contains the fabricated `SUCCEEDED (exit 0)` sentence, so a false "all tests passed"
  claim is entailed rather than contradicted.
- Tier 2 hard gate is the **only remaining containment** (see Finding B for why it may
  be absent).

**Attack scenario (complete bypass when Tier 2 is disabled):**

1. Agent modifies source (hook records `write_to_file`; unavoidable, and git tracking
   independently confirms it at stop).
2. Agent runs a failing test through `run_command` — the hook records the real `exit 1`.
3. Agent `curl`s the local daemon and POSTs a forged `run_command` success record for
   the same target + same CWD.
4. At stop: Rules 1/2 pass, NLI sees the fabricated success. With
   `HARDTRUTH_SKIP_TIER2=1` (the configuration used by the project's own test
   harness), the gate returns `allow`.

**Remediation candidates (not yet implemented):**

- Sign ledger writes: hook posts records with a shared HMAC key / per-session token
  (e.g. `X-HardTruth-Auth` header derived from the daemon's HMAC key); daemon rejects
  unsigned records. This restores *write authenticity* to match the ledger's read
  integrity.
- Bind records to the tool-call that produced them: the hook should attach a
  non-forgeable reference (e.g. the transcript step's tool_use id) and the daemon
  should reject records referencing steps with no matching hook-generated call.
- Rate-limit/restrict `/v1/ledger/record` to loopback + a per-conversation monotonic
  `stepIdx` that cannot be replayed or out-of-order.

---

## Finding B — Tier 2 hard gate is operationally disconnected in containerized daemon deployments (HIGH ops)

**Live proof:** `POST /v1/verify/handoff` with
`workspace_path="/Users/ai/dev/hardtruth-fix"` returns:

```json
{"status":"error","success":false,"exit_code":-1,"runner":null,
 "output":"Workspace path does not exist: /Users/ai/dev/hardtruth-fix"}
```

The daemon container has **no workspace mounts** (`docker inspect` → `Mounts: []`),
so the host path is invisible inside the daemon's filesystem. `run_independent_verification`
checks `os.path.exists(workspace_path)` in the daemon's own namespace before Docker/subprocess
execution.

**Design intent (documented):** `README.md:107` and `docs/HYBRID_ARCHITECTURE.md:34`
specify that the daemon runs **on the host or a secure enclave** that can see the
workspace, and Tier 2 itself spawns the ephemeral container with `-v workspace:ro`.
The current fixture (daemon inside an unmounted container) does not match that intent.

**Consequences:**

- With Tier 2 enabled, every legitimate stop attempt for a real workspace HALTS on
  `TIER 2 HARD GATE FAILED ... Workspace path does not exist` → permanent false-positive
  lockout (fail-closed, but operationally broken).
- The likely workaround — `HARDTRUTH_SKIP_TIER2=1` — disables the **only** containment
  for Finding A, turning the forge into a complete gate bypass.
- The 55-test suite does not catch this: `test_tier2_runner_detection_and_execution`
  imports `run_independent_verification` **in-process** (host paths exist), and hook
  integration tests set `HARDTRUTH_SKIP_TIER2=1` ("Skipped unless specifically tested").

**Remediation candidates (not yet implemented):**

- Configurable workspace-path mapping for containerized daemons
  (e.g. `HARDTRUTH_WORKSPACE_ROOT_MAP` / a bind mount of the host workspace into the
  daemon container at a known prefix), and/or
- An integration test that exercises `POST /v1/verify/handoff` through the HTTP API
  with a real workspace path as the hook would, asserting `success` rather than `error`.
- Fail with a distinct, actionable status (`unverified_no_runner`-style) instead of a
  generic path error, so misconfiguration is diagnosable.

---

## Non-findings / positives noted

- **Canonical tool-name filtering works:** records with `tool != "run_command"`
  (e.g. `"pytest"`, `"__spotcheck_clean__"`) are ignored by premise synthesis — probe
  confirmed `verification_commands_executed == 0` for such records.
- **Baseline immutability holds:** `POST /v1/session/baseline` returns `existing`
  once set; Round 4/6 protections (nested repos, `--assume-unchanged`, manifest
  tampering, flag-only pytest) all verified by the passing suite and spot review.
- **Ledger read integrity holds:** every premise read re-validates the full HMAC chain;
  chain remained VALID across all probes.

---

## Round 7 RESOLUTION — Fixes Implemented & Verified (2026-09-19)

### Finding A — FIXED: Authenticated ledger/daemon writes (token required)

**Implemented:**
- `daemon/ledger.py`: `get_daemon_api_token()` / `validate_api_token()` — shared token
  from `HARDTRUTH_API_TOKEN` env or `~/.hardtruth/daemon_api.key` (auto-generated,
  mode 0400). Constant-time comparison.
- `daemon/app.py`: `require_daemon_auth` FastAPI dependency on all state-changing
  endpoints (`/v1/ledger/record`, `/v1/session/baseline`, `/v1/verify/handoff`,
  `/v1/verify-claim`). Missing/invalid token → `401`. Read endpoints (health,
  premise GET, baseline GET) stay open.
- `client/hardtruth_hook.py`: hook sends `Authorization: Bearer <token>` on every
  request (env → key file); prints a clear 401 mismatch warning to stderr.

**Live verification (after rebuild):**
- `POST /v1/ledger/record` without token → **HTTP 401** (was: accepted, Round 7 proof).
- With wrong token → **HTTP 401**.
- With valid token → recorded normally.
- The premise-forgery path is now closed to arbitrary local processes; only holders
  of the shared secret (the hook harness) can write. Same-OS-user agents that can
  read `~/.hardtruth/daemon_api.key` remain within the documented "Same-User OS
  Boundary" (README "Physical Limits"): no credential system protects against a
  malicious process with the same user's file permissions — for that, run the agent
  in an isolated sandbox per the documented deployment model.

### Finding B — FIXED: Tier 2 workspace visibility + daemon image completeness

**Implemented:**
- `daemon/tier2_runner.py`: `resolve_workspace_path()` applies `HARDTRUTH_WORKSPACE_MAP`
  (JSON `{host_prefix: container_prefix}`, longest-prefix wins) for containerized
  daemons; workspace invisible to the daemon now fails closed with a distinct
  `status: "unverified_no_workspace"` (was: generic `error`); subprocess-sandbox
  pytest runs get `-p no:cacheprovider` (+ tmpfs cache dir) so read-only mounts work.
- `docker/Dockerfile`: copies the **full daemon package** (was: `app.py` only — a
  rebuild would have produced a broken daemon), adds `pytest` + `git` to the image
  (Tier 2 needs both to execute the workspace's canonical suite and manifest checks).
- `docker/docker-compose.yml`: API token + workspace mount + `HARDTRUTH_WORKSPACE_MAP`
  wiring; `README.md` documents both.
- Fixture: daemon container rebuilt with preserved ledger (named volume), token env,
  and the workspace mounted at the same absolute path (ro).

**Live verification (after rebuild):**
- `POST /v1/verify/handoff` with `workspace_path=/Users/ai/dev/hardtruth-fix`
  → **`status: "verified", success: True, exit_code: 0`** (
  `61 passed, 2 skipped` — the skips are the live-daemon tests under `CI=true`).
  The identical call previously returned `"Workspace path does not exist"`.

### Regression tests (8 new → 63 total, all passing)

- `tests/test_live.py::test_17_ledger_record_requires_api_token` — 401 without/wrong
  token, accepted with valid token.
- `tests/test_live.py::test_18_tier2_handoff_daemon_visible_workspace` — HTTP handoff
  succeeds for the daemon-visible workspace.
- `tests/test_hybrid_architecture.py::TestRound7Fixes` (4) — workspace path
  resolution + fail-closed `unverified_no_workspace`.
- `tests/test_daemon_ledger.py::TestRound7ApiToken` (2) — token validation.

```
$ pytest -v
============================= 63 passed in 11.28s ==============================
```

Daemon health: `chain_valid: True`, `records: 1136`, `nli_loaded: True`,
`tier2_hard_gate: enabled`. Container daemon files md5-identical to HEAD.
Note: the in-container Tier 2 suite records its hook events into the physical
ledger under `test-conv-*` conversation IDs (append-only, isolated, chain intact).