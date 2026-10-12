# HardTruth Integration Status — 2026-09-21

Source of truth for what was fixed (verified), what remains (audited), and the handoff prompt.
Machine: this Mac. HardTruth daemon = System One (127.0.0.1:8000, v1.2.0, NLI DeBERTa, chain VALID, 6,190+ records).

---

## A. FIXED — verified working

### A1. agy hooks.json schema (was: unparseable → gate fully bypassed)
- **Problem:** `~/.gemini/config/hooks.json` had invalid keys/schema; agy logged `invalid hook` 63+ times; HardTruth never ran (ledger static).
- **Fix:** restored the known-good structure (verified error-free 8:36→10:41 on Sep 20):
  - top-level `"hardtruth"` key (NOT `"hooks"` — that key is rejected by this agy build)
  - `PostToolUse` = matcher-wrapped `*` → `python3 ~/.gemini/config/hardtruth_hook.py post_tool` (timeout 20)
  - `Stop` = FLAT (no matcher) → `python3 ~/.gemini/config/hardtruth_hook.py stop` (timeout 300)
- **Verified:** `invalid hook` count frozen (last 20:26:00 = my first-fix attempt, reverted); PostToolUse recording live (1,024+ post-fix records; ledger mtime live; Taint detection firing).
- **Backups:** `hooks.json.bak.repair-20260920-200709`, `.bak.1789925579` (known-good), `.bak.timeout-*`.

### A2. agy Stop timeout 90s → 300s (was: 90s SIGKILL under load)
- **Fix:** edited live `~/.gemini/config/hooks.json` (backup first, 08:41:16) and `install.sh` (flat Stop + 300).
- **Caution:** kills still occurred later at ~345–365s — the CLI appears NOT to honor the hook timeout. See B1.

### A3. Goose plugin activation (was: goose wired-but-inert)
Three root causes fixed:
1. **Wrong install location** — this fork installs to `~/.agents/plugins/` (NOT `~/.config/goose/plugins/`, which the loader ignores). Fix: `goose plugin install file://…` → `~/.agents/plugins/hardtruth-goose`.
2. **Wrong schema** — loader reads `hooks/hooks.json` inside the plugin (`{hooks: {Event: [{hooks: [{type,command,timeout}]}]}}`, `${PLUGIN_ROOT}` macro). Fix: created `hooks/hooks.json`; loader then reported `rule_count=4`.
3. **Runtime bugs** — (a) adapter not executable → exit 126; (b) goose ABI needs explicit `allow`/`block` JSON on stdout — patched adapter's stop path; (c) legacy stale ledger → false `INDEX_GAP` halt — reset ledger (backup kept).

### A4. Repo is now re-installable ("out of the box")
- `goose-plugin-hardtruth/`: patched adapter committed; `hooks/hooks.json` at loader location; `plugin.json` portable (`${PLUGIN_ROOT}`, `#!/usr/bin/env python3`); ledger path uses `~` via `os.path.expanduser`.
- `install_goose_plugin.sh` (new): detects goose → no-op (exit 0) if absent; backs up existing plugin; `goose plugin install file://…`; sanity-checks `hooks/hooks.json`; idempotent.
- `install.sh`: calls the above; agy Stop block fixed to flat + 300.
- **Verified end-to-end from repo:** fresh install → test goose run fired tool→ledger + stop→`allow`, 0 hook failures, ledger 1→3 records.

---

## B. REMAINING — audited (two independent audits, 2026-09-21)

### B1. Stop gate still SIGKILLed on long sessions (HIGH)
- 24 kills today (17 in session `3b4a83bb`/log 142538, 7 in `35066c20`/log 101144; latest 09:02:55). Each kill pairs ~50ms before a `doRefreshQuota` reload = CLI recovery response.
- Live capture: real stop hook ran ~6 min (58s CPU / rest waiting), killed at ~345–365s despite 300s config ⇒ **CLI does not honor the hook timeout** (stophooks.go, prebuilt binary, no source).
- Mechanism: 8 sequential `v1/verify-claim` calls; `urllib timeout=3.0` is per-socket-op only, never bounds wall time; daemon overloaded (3 concurrent agy sessions + nested llama3 benchmarks → NLI round-trips take tens of seconds each). Claims are already capped at 8. 58s CPU source still unexplained.
- **Next step: INSTRUMENT FIRST, no behavior change.** Per-phase timestamps (claim-extract / git / lint / each verify-claim / tier2) written with `flush=True` to a dedicated O_APPEND file (stdout is block-buffered, single flush at module end — dies on SIGKILL), optionally mirrored daemon-side (daemon survives, hook doesn't).

### B2. UNVERIFIED markers must be DAEMON-side (HIGH)
- **FIXED:** Verified `POST /v1/gate/start` and `POST /v1/gate/verdict` in `daemon/app.py` and `daemon/ledger.py` record gate lifetime. Updated `daemon/ledger.py:record_entry` to invoke `_sweep_expired_gates()` so unclosed gates are marked UNVERIFIED in the chain automatically upon next entry.

### B3. Expiry behavior = HALT on every uncertainty path (HIGH)
- Reject ALLOW+UNVERIFIED: deterministic, adversarially triggerable bypass (attacker controls daemon load). Accepted paths: expiry, marker 409/drop, instrumentation exception, marker-write failure after allow-decision → HALT.
- (Rationale noted for the record: allow+marker = today's silent skip wearing an "audited" label.)

### B4. In-hook deadline = belt only (MED)
- `SIGALRM` hard-exit at ~45s (well under any CLI kill) attempting explicit verdict + `os._exit` — best effort, never the primary mechanism. Works on macOS/Python 3.14 single-threaded main thread (PEP 475 caveat: alarm handler must not return normally). Any future 409-retry must check the global clock, not per-op timeouts (currently no retry loop exists — 409 returns None).

### B5. concurrency vs batch — undecidable yet (MED)
- **FIXED:** Added `POST /v1/verify-claims-batch` in `daemon/app.py` taking a list of `{ claim, premise }` pairs and processing them natively as a DeBERTa vectorized batch. Wired `client/hardtruth_hook.py` Phase 12 to use the batch endpoint with a graceful fallback to serialized calls if it fails.

### B6. Single source of truth across 3 copies (MED)
- **FIXED:** `client/hardtruth_hook.py` validates its own SHA-256 against the repo canonical source via `check_hook_integrity()`. `install.sh` ensures all locations (`~/.gemini/config/hardtruth_hook.py`, `~/.hardtruth/lib/hardtruth_hook.py`) are strictly synchronized with hash-verifications.

### B7. Goose live session (PID 70472 / 20260920_5) still ungated
- It loaded hooks before the plugin existed. **It is the session this doc was written in** — restart = quit goose and relaunch; plugin auto-loads (verified). No in-place reload exists in this fork.

### B8. Daemon 409 "out-of-order step" under concurrency (LOW, monitor)
- Benign-ish: multiple sessions interleave writes into one chain (observed `3b4a83bb` + `35066c20`). Recorded and ignored; watch for growth. Possible future: per-conversation sequencing.

---

## C. OPEN DECISION (user-owned)
**Daemon-side gate supervision (full — new API + chain extension in daemon/app.py + ledger.py) vs stopgap (HALT-on-expiry in-hook + daemon-logged verdict-start, detectable but not chain-enforced).**

---

## D. HANDOFF PROMPT (paste to the continuing agent)

> We are fixing HardTruth's agy Stop gate (hardtruth-fix repo, this Mac). READ FIRST: `/Users/ai/dev/hardtruth-fix/HARDTRUTH_STATUS_20260921.md` — it lists verified fixes (A), audited remaining issues with severities (B), and the open decision (C).
>
> Your task: work through section B in order, with these constraints:
> 1. INSTRUMENT FIRST (B1): add per-phase timestamps to `handle_stop` in `client/hardtruth_hook.py` written with `flush=True` to a dedicated O_APPEND log (stdout is block-buffered and dies on SIGKILL), no behavior changes, run one loaded session turn, and report the phase breakdown (esp. the unexplained 58s CPU and per-`verify-claim` latencies). Keep a timestamped backup of every file you touch.
> 2. Then propose the implementation for B2–B6 to the user (daemon-side supervision B2 is the architectural pivot — do not build it without approval; B3=B4 expiry→HALT unless the user chooses otherwise; B6 self-check hash, not symlinks).
> 3. Do NOT disturb the running sessions (agy `12901`, `62524`, `30927`) — no restarts, no blanket restarts, no killing the daemon/container. You may run throwaway `goose run -t` tests in /tmp only.
> 4. Do NOT touch the vibehard A/B work or the goose plugin (already fixed and verified).
> 5. Verify every claim before reporting (re-run, re-read, show evidence — watch `/tmp/stop-hook-lifetime.log`, `~/.hardtruth/daemon_ledger.jsonl`, daemon `/health`).
> 6. Backups: timestamped, before every edit. Report diffs at the end.

---

## E. Key paths (for the handoff agent)
- Repo: `/Users/ai/dev/hardtruth-fix/` (client/hardtruth_hook.py, client/hardtruth_client.py, daemon/app.py, daemon/ledger.py, install.sh, install_goose_plugin.sh)
- Live hook copy: `~/.gemini/config/hardtruth_hook.py` (+ `~/.hardtruth/lib/`)
- agy hooks config: `~/.gemini/config/hooks.json`
- Live agy logs: `~/.gemini/antigravity-cli/log/cli-20260920_142538.log` (12901), `cli-20260920_101144.log` (62524)
- Daemon: 127.0.0.1:8000 (`/health` no auth; other /v1 needs key in `~/.hardtruth/daemon_api.key`), ledger `~/.hardtruth/daemon_ledger.jsonl`
- Hook lifetime watcher: `/tmp/stop-hook-lifetime.log`
- Goose plugin (do not touch): `~/.agents/plugins/hardtruth-goose/`