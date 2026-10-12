#!/usr/bin/env python3
"""
HardTruth Benchmark Script
Measures real inference latency, throughput, and memory consumption on the current machine.
Outputs hardware details, warm-up statistics, and percentiles (p50, p90, p99).
"""

import os
import sys
import time
import platform
import statistics
import urllib.request
import json
try:
    import psutil
except ImportError:
    psutil = None

def benchmark_http(daemon_url: str = "http://127.0.0.1:8000", iterations: int = 50):
    print("=" * 60)
    print("  HardTruth HTTP Verification Benchmark")
    print("=" * 60)
    print(f"Target URL: {daemon_url}")
    print(f"Platform: {platform.system()} {platform.machine()} ({platform.processor()})")
    
    # Check health
    try:
        req = urllib.request.Request(f"{daemon_url}/health")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            health = json.loads(resp.read().decode("utf-8"))
            print(f"Daemon Service: {health.get('service')}")
            print(f"Daemon RSS RAM: {health.get('rss_memory_mb')} MB")
    except Exception as e:
        print(f"ERROR: Cannot connect to daemon at {daemon_url}: {e}")
        return

    payload = json.dumps({
        "premise": "COMMAND: 'pytest tests/'. STATUS: FAILED (exit status 1). MODIFIED_FILES: app.py.",
        "hypothesis": "All 10 unit tests in the test suite passed."
    }).encode("utf-8")

    # Warm-up (5 iterations)
    print("\nWarming up (5 iterations)...")
    for _ in range(5):
        req = urllib.request.Request(
            f"{daemon_url}/v1/verify-claim",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            resp.read()

    # Benchmark runs
    print(f"Running {iterations} benchmark requests...")
    latencies = []
    for i in range(iterations):
        t0 = time.perf_counter()
        req = urllib.request.Request(
            f"{daemon_url}/v1/verify-claim",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latencies.append((time.perf_counter() - t0) * 1000.0)

    # Compute statistics
    mean_lat = statistics.mean(latencies)
    median_lat = statistics.median(latencies)
    p90_lat = sorted(latencies)[int(0.90 * len(latencies))]
    p99_lat = sorted(latencies)[int(0.99 * len(latencies))]

    print("\n--- Benchmark Results ---")
    print(f"Iterations:  {iterations}")
    print(f"Mean:        {mean_lat:.2f} ms")
    print(f"Median (p50):{median_lat:.2f} ms")
    print(f"p90:         {p90_lat:.2f} ms")
    print(f"p99:         {p99_lat:.2f} ms")
    print(f"Throughput:  {1000.0 / median_lat:.1f} req/sec (sequential)")
    print("=" * 60)

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    benchmark_http(url, n)
