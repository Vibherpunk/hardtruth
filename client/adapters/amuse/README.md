# HardTruth <-> Amuse Adapter

Two-layer fail-closed verification and command-safety bridge for **Amuse** (containerized TypeScript agent + host Mac terminal executor):

## 1. Host Terminal Executor Guard (`executor_guard.py` -> `~/amuse-terminal/hardtruth_guard.py`)
Runs inside the host launchd service (`com.amuse.terminal-executor` on `127.0.0.1:8124`, Python 3.9 stdlib-only):
- **Pre-execution gate (`evaluate_command`):** Rejects exit-code masking (`|| true`, `; exit 0`, `set +e`, test runners piped to `cat`/`grep`/`head`/`tail` without `pipefail`), local Port `8000` quarantine violations (`127.0.0.1:8000`, `localhost:8000`, `--port 8000`, `lsof`/`kill` on `8000` while allowing remote hosts like Windmill), and direct tampering with `~/.hardtruth/` keys, ledger, halt files, or `refs/hardtruth/`.
- **Execution receipting (`record_execution`):** Records completed executions to `http://127.0.0.1:49281/v1/ledger/record` and forwards passing test runs to `hardtruth-gate receipt` over the Rust `hardtruthd` UDS.

## 2. Amuse Agent Loop Integration (`hardtruth.ts` -> `apps/server/src/agent/hardtruth.ts`)
Runs inside the Amuse server container (`amuse-amuse-1`), communicating with the host HardTruth v2 daemon over `http://host.docker.internal:49281`:
- **Session lifecycle:** Calls `POST /v1/session/start` per run (`amuse-chat-<chatId>-run-<runId>`) to acquire an ephemeral `X-Session-Secret`, and records every tool call (`terminal_run`, `run_code`, `save_tool`, `files`, etc.) via `POST /v1/ledger/record`.
- **Pre-reply verification (`verifyReply`):**
  1. Checks code written by `save_tool` / `write_file` for language stubs (`NotImplementedError`, `todo!()`, `unimplemented!()`, `throw new Error('not implemented')`, empty `pass` functions).
  2. Deterministically rejects `"all tests passed"` claims when no passing test receipt exists or when the latest test run failed.
  3. Sends remaining completion/test claims to `POST /v1/verify-claims` (`tasksource/ModernBERT-base-nli`) against the failure-biased run premise and triggers a bounded corrective model pass (`action: "correct"`) or audit annotation (`action: "annotate"`) on contradiction, recording `HALT` / `ALLOW` via `POST /v1/gate/verdict`.

## Configuration
Set in Amuse compose / `.env`:
- `AMUSE_HARDTRUTH_URL=http://host.docker.internal:49281`
- `AMUSE_HARDTRUTH_TOKEN=<contents of ~/.hardtruth/daemon_api.key>`
