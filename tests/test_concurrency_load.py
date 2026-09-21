#!/usr/bin/env python3
import os
import pytest
pytestmark = pytest.mark.skipif(os.environ.get("HARDTRUTH_TIER2_SANDBOX") == "1", reason="Tier 2 in-container run")
"""
Concurrency & Load Fuzzing Suite for HardTruth Daemon (127.0.0.1:8000) & Client Hook.

Scenarios Tested:
1. Multi-Session Interleaving:
   Simulate 8 concurrent conversation sessions writing post_tool events simultaneously.
   Verify SQLite / flock write locking, sequence numbers, and hash chain integrity (checking B8 out-of-order 409 handling).
2. Stop-Gate Burst Test:
   Dispatch 10 concurrent handle_stop evaluation payloads against the daemon to measure NLI verification latency
   and verify that no requests exceed the 45-second deadline belt.
3. Premise Cache & Secret Isolation:
   Verify that concurrent requests with unique session secrets (X-Session-Secret) do not leak premise data across conversation IDs.
4. Latency Benchmarks:
   Calculate and report p50, p95, p99 latencies, throughput, and error rates.
"""

import os
import sys
import json
import time
import uuid
import tempfile
import urllib.request
import urllib.error
import concurrent.futures
import subprocess
from typing import List, Dict, Any, Tuple
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

HOOK_SCRIPT = os.path.join(REPO_ROOT, "client", "hardtruth_hook.py")
DAEMON_URL = os.environ.get("SYSTEM_ONE_URL", "http://127.0.0.1:8000")
API_KEY_PATH = os.path.expanduser("~/.hardtruth/daemon_api.key")

if os.environ.get("HARDTRUTH_TIER2_SANDBOX") == "1":
    pytest.skip("Tier 2 in-container run: live daemon load tests skipped", allow_module_level=True)


def get_api_token() -> str:
    """Reads API token from environment or key file."""
    tok = os.environ.get("HARDTRUTH_API_TOKEN", "")
    if tok:
        return tok.strip()
    if os.path.exists(API_KEY_PATH):
        try:
            with open(API_KEY_PATH, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def calc_percentiles(latencies: List[float]) -> Dict[str, float]:
    """Calculates min, p50, p95, p99, max, mean from latency samples in ms."""
    if not latencies:
        return {"min": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0, "count": 0}
    sorted_lats = sorted(latencies)
    n = len(sorted_lats)
    p50_idx = min(int(n * 0.50), n - 1)
    p95_idx = min(int(n * 0.95), n - 1)
    p99_idx = min(int(n * 0.99), n - 1)
    return {
        "min": round(sorted_lats[0], 2),
        "p50": round(sorted_lats[p50_idx], 2),
        "p95": round(sorted_lats[p95_idx], 2),
        "p99": round(sorted_lats[p99_idx], 2),
        "max": round(sorted_lats[-1], 2),
        "mean": round(sum(sorted_lats) / n, 2),
        "count": n
    }


def make_daemon_request(
    endpoint: str,
    method: str = "GET",
    payload: Dict[str, Any] = None,
    session_secret: str = None,
    timeout: float = 45.0
) -> Tuple[int, Dict[str, Any], float]:
    """
    Sends request to daemon and returns (status_code, response_data, latency_ms).
    """
    url = f"{DAEMON_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {"Content-Type": "application/json"}
    tok = get_api_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    if session_secret:
        headers["X-Session-Secret"] = session_secret

    data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else {}
            return resp.status, data, latency_ms
    except urllib.error.HTTPError as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        err_body = e.read().decode("utf-8")
        try:
            data = json.loads(err_body)
        except Exception:
            data = {"detail": err_body}
        return e.code, data, latency_ms
    except Exception as ex:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return 599, {"error": str(ex)}, latency_ms


# ===========================================================================
# 1. Multi-Session Interleaving Tests
# ===========================================================================

class TestMultiSessionInterleaving:
    """
    Simulates 8 concurrent conversation sessions writing post_tool events simultaneously.
    Verifies write locking, monotonic sequence numbers per session, and hash chain integrity.
    """

    def test_8_concurrent_sessions_post_tool_interleaving(self):
        """
        8 concurrent sessions each writing 8 steps (64 total concurrent writes).
        Verifies:
        - Atomic write locking (no lost updates or file write corruption)
        - Per-session sequence number tracking
        - 100% hash chain validity on the daemon ledger
        - Low p50/p95/p99 write latency
        """
        num_sessions = 8
        steps_per_session = 8
        sessions = {}

        # Initialize 8 sessions and acquire session secrets
        for i in range(num_sessions):
            conv_id = f"fuzz-sess-{i}-{uuid.uuid4().hex[:8]}"
            code, resp, _ = make_daemon_request(
                "v1/session/start",
                method="POST",
                payload={"conversationId": conv_id, "workspace_path": os.getcwd()}
            )
            assert code == 200, f"Session start failed: {resp}"
            assert resp.get("status") == "created"
            sessions[i] = {
                "conversationId": conv_id,
                "secret": resp.get("session_secret")
            }

        # Build execution tasks interleaved across sessions and steps
        tasks = []
        for step in range(steps_per_session):
            for i in range(num_sessions):
                tool = "run_command" if step % 2 == 0 else "write_to_file"
                target = f"pytest tests/test_sess_{i}_step_{step}.py" if tool == "run_command" else f"src/sess_{i}/mod_{step}.py"
                tasks.append((i, step, tool, target))

        def write_event(task_info):
            sess_idx, step_idx, tool, target = task_info
            sess = sessions[sess_idx]
            payload = {
                "conversationId": sess["conversationId"],
                "stepIdx": step_idx,
                "tool": tool,
                "target": target,
                "observed_exit_code": 0,
                "harness_status": "no_error",
                "cwd": os.getcwd()
            }
            code, resp, lat = make_daemon_request(
                "v1/ledger/record",
                method="POST",
                payload=payload,
                session_secret=sess["secret"]
            )
            return sess_idx, step_idx, code, resp, lat

        # Dispatch all 64 writes concurrently across 8 worker threads
        t_start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_sessions) as pool:
            results = list(pool.map(write_event, tasks))
        wall_time_ms = (time.perf_counter() - t_start) * 1000.0

        latencies = [r[4] for r in results]
        status_codes = [r[2] for r in results]
        errors = [r for r in results if r[2] != 200]

        metrics = calc_percentiles(latencies)
        throughput = len(results) / (wall_time_ms / 1000.0)

        print(f"\n[Multi-Session Interleaving] 64 writes across 8 concurrent sessions:")
        print(f"  Wall time: {wall_time_ms:.1f}ms | Throughput: {throughput:.1f} writes/sec")
        print(f"  p50: {metrics['p50']}ms | p95: {metrics['p95']}ms | p99: {metrics['p99']}ms | max: {metrics['max']}ms")
        print(f"  Errors: {len(errors)} / {len(results)} (Error rate: {len(errors)/len(results)*100:.1f}%)")

        assert len(errors) == 0, f"Unexpected write errors: {errors}"
        assert all(code == 200 for code in status_codes)

        # Verify HMAC Hash Chain Integrity on the Daemon
        health_code, health_data, _ = make_daemon_request("health")
        assert health_code == 200
        ledger_info = health_data.get("ledger", {})
        assert ledger_info.get("chain_valid") is True, f"Ledger chain invalid: {ledger_info}"
        assert ledger_info.get("chain_msg") == "VALID"

    def test_b8_out_of_order_step_409_handling(self):
        """
        Verifies B8 handling:
        1. Within a session, submitting a lower stepIdx after a higher stepIdx returns 409 Conflict.
        2. Across separate concurrent sessions, interleaved steps with different indices DO NOT trigger 409.
        3. After a 409 rejection, the ledger chain remains valid and subsequent valid steps succeed.
        """
        conv_a = f"fuzz-b8-a-{uuid.uuid4().hex[:8]}"
        conv_b = f"fuzz-b8-b-{uuid.uuid4().hex[:8]}"

        # Start sessions
        _, resp_a, _ = make_daemon_request("v1/session/start", method="POST", payload={"conversationId": conv_a})
        _, resp_b, _ = make_daemon_request("v1/session/start", method="POST", payload={"conversationId": conv_b})
        sec_a = resp_a.get("session_secret")
        sec_b = resp_b.get("session_secret")

        # Session A records step 10
        code, resp, _ = make_daemon_request(
            "v1/ledger/record",
            method="POST",
            payload={"conversationId": conv_a, "stepIdx": 10, "tool": "view_file", "target": "file.py"},
            session_secret=sec_a
        )
        assert code == 200

        # Concurrently:
        # Session A attempts step 3 (must be rejected with 409 Out-of-order)
        # Session B attempts step 1 (must SUCCEED with 200 OK despite session A being at step 10)
        def submit_a():
            return make_daemon_request(
                "v1/ledger/record",
                method="POST",
                payload={"conversationId": conv_a, "stepIdx": 3, "tool": "view_file", "target": "file.py"},
                session_secret=sec_a
            )

        def submit_b():
            return make_daemon_request(
                "v1/ledger/record",
                method="POST",
                payload={"conversationId": conv_b, "stepIdx": 1, "tool": "view_file", "target": "file.py"},
                session_secret=sec_b
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(submit_a)
            fut_b = pool.submit(submit_b)
            code_a, resp_a_out, _ = fut_a.result()
            code_b, resp_b_out, _ = fut_b.result()

        # Session A must be rejected with 409
        assert code_a == 409, f"Expected 409 for out-of-order step, got {code_a}: {resp_a_out}"
        assert "Out-of-order step execution" in str(resp_a_out.get("detail", ""))

        # Session B must succeed with 200
        assert code_b == 200, f"Expected 200 for session B, got {code_b}: {resp_b_out}"

        # Subsequent monotonically increasing step for Session A (step 11) must succeed
        code_a2, _, _ = make_daemon_request(
            "v1/ledger/record",
            method="POST",
            payload={"conversationId": conv_a, "stepIdx": 11, "tool": "view_file", "target": "file.py"},
            session_secret=sec_a
        )
        assert code_a2 == 200

        # Ledger hash chain remains valid
        _, health_data, _ = make_daemon_request("health")
        assert health_data.get("ledger", {}).get("chain_valid") is True

    def test_client_hook_post_tool_subprocess_concurrency(self):
        """
        Simulates 8 concurrent client hook post_tool executions invoking the Python script CLI.
        Verifies client hook exit codes and non-blocking operation under concurrent spawn.
        """
        num_workers = 8
        conv_prefix = f"hook-load-{uuid.uuid4().hex[:6]}"

        def call_hook(worker_id):
            conv_id = f"{conv_prefix}-{worker_id}"
            payload = {
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": f"pytest tests/test_worker_{worker_id}.py"}
                },
                "stepIdx": 1,
                "conversationId": conv_id,
                "exitCode": 0,
                "test_mode": True
            }
            t0 = time.perf_counter()
            proc = subprocess.run(
                ["python3", HOOK_SCRIPT, "post_tool"],
                input=json.dumps(payload).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            lat = (time.perf_counter() - t0) * 1000.0
            return worker_id, proc.returncode, proc.stdout.decode().strip(), lat

        t_start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as pool:
            results = list(pool.map(call_hook, range(num_workers)))
        wall_time_ms = (time.perf_counter() - t_start) * 1000.0

        latencies = [r[3] for r in results]
        metrics = calc_percentiles(latencies)

        print(f"\n[Client Hook Subprocess Concurrency] {num_workers} concurrent hook processes:")
        print(f"  Wall time: {wall_time_ms:.1f}ms | p50: {metrics['p50']}ms | p95: {metrics['p95']}ms | p99: {metrics['p99']}ms")

        assert all(r[1] == 0 for r in results), f"Some hook calls failed: {results}"


# ===========================================================================
# 2. Stop-Gate Burst Tests
# ===========================================================================

class TestStopGateBurst:
    """
    Dispatches 10 concurrent handle_stop evaluation payloads against the daemon.
    Measures NLI verification latency and verifies that no requests exceed the 45-second deadline belt.
    """

    def test_10_concurrent_stop_gate_evaluations_within_deadline(self):
        """
        Dispatches 10 concurrent stop evaluations with legitimate test-pass claims.
        Verifies:
        - All 10 requests complete well within the 45-second deadline belt (max < 45s).
        - All 10 requests receive verdict 'allow'.
        - Measures p50, p95, p99 NLI evaluation latency under concurrent burst.
        """
        burst_size = 10
        sessions = []

        # Setup 10 sessions with clean test pass evidence in the ledger
        for i in range(burst_size):
            conv_id = f"burst-allow-{i}-{uuid.uuid4().hex[:8]}"
            # Record passing test command in ledger
            post_payload = {
                "toolCall": {"name": "run_command", "args": {"CommandLine": f"pytest tests/test_module_{i}.py"}},
                "stepIdx": 1,
                "conversationId": conv_id,
                "exitCode": 0,
                "test_mode": True
            }
            res = subprocess.run(
                ["python3", HOOK_SCRIPT, "post_tool"],
                input=json.dumps(post_payload).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
            # Create transcript with completion claim
            tf = tempfile.NamedTemporaryFile("w", delete=False)
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": f"I finished implementing module {i}. I executed pytest tests/test_module_{i}.py and all 20 unit tests passed completely.",
                "tool_calls": []
            }) + "\n")
            tf.flush()
            sessions.append({
                "conversationId": conv_id,
                "transcriptPath": tf.name,
                "workspace_dir": os.getcwd()
            })

        def run_stop(sess):
            payload = {
                "conversationId": sess["conversationId"],
                "transcriptPath": sess["transcriptPath"],
                "workspace_dir": sess["workspace_dir"]
            }
            env = os.environ.copy()
            env["HARDTRUTH_SKIP_TIER2"] = "1"
            t0 = time.perf_counter()
            res = subprocess.run(
                ["python3", HOOK_SCRIPT, "stop"],
                input=json.dumps(payload).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env
            )
            lat_ms = (time.perf_counter() - t0) * 1000.0
            out = {}
            try:
                out = json.loads(res.stdout.decode("utf-8").strip())
            except Exception:
                pass
            return sess["conversationId"], res.returncode, out, lat_ms

        t_start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=burst_size) as pool:
            results = list(pool.map(run_stop, sessions))
        total_wall_ms = (time.perf_counter() - t_start) * 1000.0

        # Clean up temporary transcript files
        for s in sessions:
            try:
                os.remove(s["transcriptPath"])
            except Exception:
                pass

        latencies = [r[3] for r in results]
        metrics = calc_percentiles(latencies)
        decisions = [r[2].get("decision") for r in results]

        print(f"\n[Stop-Gate Burst Test] 10 concurrent stop evaluations:")
        print(f"  Total wall time: {total_wall_ms:.1f}ms (Budget: 45,000ms)")
        print(f"  p50: {metrics['p50']}ms | p95: {metrics['p95']}ms | p99: {metrics['p99']}ms | max: {metrics['max']}ms")
        print(f"  All decisions 'allow': {all(d == 'allow' for d in decisions)}")

        # Strict Assertions
        assert all(r[1] == 0 for r in results), f"Process returned non-zero exit code: {results}"
        assert all(d == "allow" for d in decisions), f"Expected all allow, got: {decisions}"
        assert metrics["max"] < 45000.0, f"Stop evaluation exceeded 45s deadline belt! Max was {metrics['max']}ms"

    def test_10_concurrent_stop_gate_contradiction_detection(self):
        """
        Dispatches 10 concurrent stop evaluations where the ledger records test failure,
        but the agent claims tests passed.
        Verifies:
        - All 10 requests halt with contradiction detection in << 45s.
        - Zero false-allow escapes under concurrency.
        """
        burst_size = 10
        sessions = []

        # Setup 10 sessions with failing test evidence
        for i in range(burst_size):
            conv_id = f"burst-halt-{i}-{uuid.uuid4().hex[:8]}"
            post_payload = {
                "toolCall": {"name": "run_command", "args": {"CommandLine": f"pytest tests/test_core_{i}.py"}},
                "stepIdx": 1,
                "conversationId": conv_id,
                "exitCode": 1,
                "error": "AssertionError: 2 != 5",
                "test_mode": True
            }
            subprocess.run(
                ["python3", HOOK_SCRIPT, "post_tool"],
                input=json.dumps(post_payload).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
            tf = tempfile.NamedTemporaryFile("w", delete=False)
            tf.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": f"Fixed the issue in module {i}. All unit tests passed without any errors.",
                "tool_calls": []
            }) + "\n")
            tf.flush()
            sessions.append({
                "conversationId": conv_id,
                "transcriptPath": tf.name,
                "workspace_dir": os.getcwd()
            })

        def run_stop_halt(sess):
            payload = {
                "conversationId": sess["conversationId"],
                "transcriptPath": sess["transcriptPath"],
                "workspace_dir": sess["workspace_dir"]
            }
            env = os.environ.copy()
            env["HARDTRUTH_SKIP_TIER2"] = "1"
            t0 = time.perf_counter()
            res = subprocess.run(
                ["python3", HOOK_SCRIPT, "stop"],
                input=json.dumps(payload).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env
            )
            lat_ms = (time.perf_counter() - t0) * 1000.0
            out = {}
            try:
                out = json.loads(res.stdout.decode("utf-8").strip())
            except Exception:
                pass
            return sess["conversationId"], res.returncode, out, lat_ms

        t_start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=burst_size) as pool:
            results = list(pool.map(run_stop_halt, sessions))
        total_wall_ms = (time.perf_counter() - t_start) * 1000.0

        for s in sessions:
            try:
                os.remove(s["transcriptPath"])
            except Exception:
                pass

        latencies = [r[3] for r in results]
        metrics = calc_percentiles(latencies)
        decisions = [r[2].get("decision") for r in results]
        reasons = [r[2].get("reason", "") for r in results]

        print(f"\n[Contradiction Burst Test] 10 concurrent contradiction halts:")
        print(f"  Total wall time: {total_wall_ms:.1f}ms (Budget: 45,000ms)")
        print(f"  p50: {metrics['p50']}ms | p95: {metrics['p95']}ms | p99: {metrics['p99']}ms | max: {metrics['max']}ms")
        print(f"  All halted (decision='continue'): {all(d == 'continue' for d in decisions)}")

        assert all(d == "continue" for d in decisions), f"Some requests failed to halt: {decisions}"
        assert all("CONTRADICTION DETECTED" in r for r in reasons), f"Unexpected halt reasons: {reasons}"
        assert metrics["max"] < 45000.0, f"Halt evaluation exceeded 45s deadline belt! Max was {metrics['max']}ms"


# ===========================================================================
# 3. Premise Cache & Secret Isolation Tests
# ===========================================================================

class TestPremiseCacheAndSecretIsolation:
    """
    Verifies that concurrent requests with unique session secrets (X-Session-Secret)
    do not leak premise data across conversation IDs and correctly reject unauthorized access.
    """

    def test_concurrent_secret_isolation_and_no_data_leakage(self):
        """
        1. Creates 8 sessions with distinct secrets and records distinct events for each.
        2. Concurrently sends 48 queries to /v1/ledger/premise:
           - Legitimate queries (matching secret) -> 200 OK, verify ZERO cross-session leakage.
           - Cross-session adversary queries (mismatched secret) -> 403 Forbidden.
           - Missing secret queries -> 403 Forbidden.
           - Spoofed secret queries -> 403 Forbidden.
        """
        num_sessions = 8
        sessions = {}

        # 1. Setup sessions and write unique marker commands
        for i in range(num_sessions):
            conv_id = f"iso-sess-{i}-{uuid.uuid4().hex[:8]}"
            code, resp, _ = make_daemon_request(
                "v1/session/start",
                method="POST",
                payload={"conversationId": conv_id, "workspace_path": os.getcwd()}
            )
            assert code == 200
            secret = resp.get("session_secret")
            marker = f"UNIQUE_MARKER_SESSION_{i}_{uuid.uuid4().hex[:6]}"
            sessions[i] = {
                "conversationId": conv_id,
                "secret": secret,
                "marker": marker
            }

            # Record event with unique marker
            rec_code, _, _ = make_daemon_request(
                "v1/ledger/record",
                method="POST",
                payload={
                    "conversationId": conv_id,
                    "stepIdx": 1,
                    "tool": "run_command",
                    "target": f"pytest tests/test_{i}.py -k {marker}",
                    "observed_exit_code": 0,
                    "harness_status": "no_error"
                },
                session_secret=secret
            )
            assert rec_code == 200

        # 2. Construct queries: legitimate, cross-session, missing, spoofed
        query_tasks = []
        # Legitimate queries (3 per session = 24 queries)
        for i in range(num_sessions):
            for _ in range(3):
                query_tasks.append({
                    "type": "legit",
                    "sess_idx": i,
                    "secret": sessions[i]["secret"],
                    "expected_status": 200
                })

        # Cross-session queries: using secret from session (i+1)%8 (8 queries)
        for i in range(num_sessions):
            cross_idx = (i + 1) % num_sessions
            query_tasks.append({
                "type": "cross_session",
                "sess_idx": i,
                "secret": sessions[cross_idx]["secret"],
                "expected_status": 403
            })

        # Missing secret queries (8 queries)
        for i in range(num_sessions):
            query_tasks.append({
                "type": "missing_secret",
                "sess_idx": i,
                "secret": None,
                "expected_status": 403
            })

        # Spoofed secret queries (8 queries)
        for i in range(num_sessions):
            query_tasks.append({
                "type": "spoofed_secret",
                "sess_idx": i,
                "secret": f"forged_secret_{uuid.uuid4().hex}",
                "expected_status": 403
            })

        def run_query(task):
            sess = sessions[task["sess_idx"]]
            endpoint = f"v1/ledger/premise?conversationId={sess['conversationId']}"
            code, resp, lat = make_daemon_request(
                endpoint,
                method="GET",
                session_secret=task["secret"]
            )
            return task, code, resp, lat

        t_start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(run_query, query_tasks))
        total_wall_ms = (time.perf_counter() - t_start) * 1000.0

        latencies = [r[3] for r in results]
        metrics = calc_percentiles(latencies)

        legit_results = [r for r in results if r[0]["type"] == "legit"]
        adversary_results = [r for r in results if r[0]["type"] != "legit"]

        # 3. Verify Isolation & Data Integrity
        # All adversary attempts MUST return 403 Forbidden
        adversary_pass = all(r[1] == 403 for r in adversary_results)
        assert adversary_pass, f"Adversary queries succeeded: {[r for r in adversary_results if r[1] != 403]}"

        # All legitimate queries MUST return 200 OK
        assert all(r[1] == 200 for r in legit_results)

        # Check for cross-session premise contamination in legitimate queries
        contamination = False
        for r in legit_results:
            task = r[0]
            data = r[2]
            target_idx = task["sess_idx"]
            target_marker = sessions[target_idx]["marker"]
            premise_text = data.get("premise", "")

            # Must contain own marker
            assert target_marker in premise_text, f"Session {target_idx} missing own marker: {target_marker}"

            # Must NOT contain markers from any other session
            for other_idx in range(num_sessions):
                if other_idx != target_idx:
                    other_marker = sessions[other_idx]["marker"]
                    if other_marker in premise_text:
                        contamination = True
                        print(f"CRITICAL: Premise leak! Session {target_idx} contains {other_marker}")

        assert not contamination, "Cross-session premise contamination detected!"

        print(f"\n[Premise & Secret Isolation] 48 concurrent premise queries:")
        print(f"  Total wall time: {total_wall_ms:.1f}ms | Throughput: {len(results)/(total_wall_ms/1000.0):.1f} req/sec")
        print(f"  p50: {metrics['p50']}ms | p95: {metrics['p95']}ms | p99: {metrics['p99']}ms")
        print(f"  Legitimate queries (24): 100% 200 OK, 0% data leakage")
        print(f"  Adversarial queries (24): 100% 403 Forbidden rejection rate")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
