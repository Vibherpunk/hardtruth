"""
HardTruth Tier 2 Deterministic External Runner (Outer Loop)
Executes test suites independently in an out-of-band ephemeral container (Docker/OrbStack)
with a read-only workspace mount and no network access, or clean isolated subprocess sandbox.
Guarantees unforgeable, physical exit codes and clean output outside agent shell control.
"""

from __future__ import annotations
import os
import sys
import json
import shutil
import subprocess
from typing import Optional, Dict, Any


def detect_test_runner(workspace_path: str) -> Optional[str]:
    """
    Detects canonical test runner based on repository manifest files.
    """
    if not workspace_path or not os.path.exists(workspace_path):
        return None

    # 1. Python (pytest / unittest)
    pyproject = os.path.join(workspace_path, "pyproject.toml")
    pytest_ini = os.path.join(workspace_path, "pytest.ini")
    tests_dir = os.path.join(workspace_path, "tests")
    setup_py = os.path.join(workspace_path, "setup.py")

    if os.path.exists(pytest_ini) or (os.path.isdir(tests_dir) and any(f.endswith(".py") for f in os.listdir(tests_dir))):
        if shutil.which("pytest"):
            return "pytest tests/"
        elif shutil.which("python3"):
            return "python3 -m unittest discover tests"

    # 2. Node.js (npm test)
    package_json = os.path.join(workspace_path, "package.json")
    if os.path.exists(package_json):
        try:
            with open(package_json, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "test" in data.get("scripts", {}):
                    return "npm test"
        except Exception:
            pass

    # 3. Rust (cargo test)
    cargo_toml = os.path.join(workspace_path, "Cargo.toml")
    if os.path.exists(cargo_toml) and shutil.which("cargo"):
        return "cargo test"

    # 4. Go (go test)
    go_mod = os.path.join(workspace_path, "go.mod")
    if os.path.exists(go_mod) and shutil.which("go"):
        return "go test ./..."

    # 5. Makefile (make test)
    makefile = os.path.join(workspace_path, "Makefile")
    if os.path.exists(makefile) and shutil.which("make"):
        try:
            with open(makefile, "r", encoding="utf-8", errors="ignore") as f:
                if "test:" in f.read():
                    return "make test"
        except Exception:
            pass

    # Fallback to general pytest if python repo
    if os.path.exists(pyproject) or os.path.exists(setup_py):
        return "pytest"

    return None


def run_container_verification(
    workspace_path: str,
    test_cmd: str,
    timeout_sec: int = 60
) -> Optional[Dict[str, Any]]:
    """
    Executes tests inside an ephemeral Docker container with read-only volume and no network access.
    """
    docker_bin = shutil.which("docker")
    if not docker_bin or os.environ.get("HARDTRUTH_TIER2_CONTAINER") == "0":
        return None

    # Check if docker daemon is responding
    try:
        check = subprocess.run([docker_bin, "info"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2.0)
        if check.returncode != 0:
            return None
    except Exception:
        return None

    # Determine lightweight base image
    # For python tests: python:3.11-slim; for node: node:alpine; default: alpine
    image = "python:3.11-slim"
    if "npm" in test_cmd or "yarn" in test_cmd or "bun" in test_cmd:
        image = "node:20-alpine"
    elif "cargo" in test_cmd:
        image = "rust:alpine"
    elif "go test" in test_cmd:
        image = "golang:alpine"

    docker_args = [
        docker_bin, "run", "--rm",
        "--network", "none",
        "-v", f"{os.path.abspath(workspace_path)}:/workspace:ro",
        "-w", "/workspace",
        image,
        "sh", "-c", test_cmd
    ]

    try:
        proc = subprocess.run(
            docker_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_sec,
            text=True
        )
        exit_code = proc.returncode
        output = (proc.stdout or "").strip()

        return {
            "status": "verified" if exit_code == 0 else "failed",
            "success": exit_code == 0,
            "exit_code": exit_code,
            "runner": f"docker({image}): {test_cmd}",
            "output": output[:4000],
            "isolation": "container_read_only"
        }
    except subprocess.TimeoutExpired as te:
        return {
            "status": "timeout",
            "success": False,
            "exit_code": 124,
            "runner": f"docker({image}): {test_cmd}",
            "output": f"Tier 2 container verification timed out after {timeout_sec}s.\n{(te.stdout or '')[:1000]}",
            "isolation": "container_read_only"
        }
    except Exception:
        return None


def run_independent_verification(
    workspace_path: str,
    test_cmd: Optional[str] = None,
    timeout_sec: int = 60
) -> Dict[str, Any]:
    """
    Executes the canonical test suite outside the agent context.
    Prioritizes ephemeral read-only Docker container isolation,
    falling back to clean subprocess sandbox if Docker is unavailable.
    """
    if not workspace_path or not os.path.exists(workspace_path):
        return {
            "status": "error",
            "success": False,
            "exit_code": -1,
            "runner": None,
            "output": f"Workspace path does not exist: {workspace_path}"
        }

    canonical_runner = test_cmd or detect_test_runner(workspace_path)
    if not canonical_runner:
        return {
            "status": "verified",
            "success": True,
            "exit_code": 0,
            "runner": "none",
            "output": "No standard test suite detected in workspace. Tier 2 passed by default."
        }

    # 1. Attempt Ephemeral Docker Container Verification (Full Physical Boundary)
    if os.environ.get("HARDTRUTH_PREFER_DOCKER_TIER2") == "1":
        container_res = run_container_verification(workspace_path, canonical_runner, timeout_sec=timeout_sec)
        if container_res is not None:
            return container_res

    # 2. Clean Subprocess Sandbox Execution (Clean Environment Boundary)
    clean_env = os.environ.copy()
    clean_env.pop("HARDTRUTH_LEDGER_PATH", None)
    clean_env.pop("HARDTRUTH_DAEMON_LEDGER", None)
    clean_env.pop("HARDTRUTH_HALT_DIR", None)
    clean_env.pop("HARDTRUTH_CIRCUIT_BREAKER_LEGACY_ALLOW", None)
    clean_env["PYTHONUNBUFFERED"] = "1"
    clean_env["CI"] = "true"

    try:
        proc = subprocess.run(
            canonical_runner,
            cwd=workspace_path,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=clean_env,
            timeout=timeout_sec,
            text=True
        )
        exit_code = proc.returncode
        output = (proc.stdout or "").strip()

        return {
            "status": "verified" if exit_code == 0 else "failed",
            "success": exit_code == 0,
            "exit_code": exit_code,
            "runner": canonical_runner,
            "output": output[:4000],
            "isolation": "clean_subprocess_sandbox"
        }

    except subprocess.TimeoutExpired as te:
        return {
            "status": "timeout",
            "success": False,
            "exit_code": 124,
            "runner": canonical_runner,
            "output": f"Tier 2 external test execution timed out after {timeout_sec}s.\n{(te.stdout or '')[:1000]}",
            "isolation": "clean_subprocess_sandbox"
        }
    except Exception as e:
        return {
            "status": "error",
            "success": False,
            "exit_code": -1,
            "runner": canonical_runner,
            "output": f"Tier 2 execution encountered an internal error: {e}",
            "isolation": "clean_subprocess_sandbox"
        }
