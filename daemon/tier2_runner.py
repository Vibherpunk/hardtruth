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
import re
import hashlib
import shlex
import signal
from typing import Optional, Dict, Any, Tuple, List


MANIFEST_FILES = [
    "Makefile", "GNUmakefile", "package.json", "pyproject.toml", "Cargo.toml", "setup.py", "setup.cfg",
    "pytest.ini", ".pytest.ini", "tox.ini", "noxfile.py", ".mocharc.json", ".mocharc.yml",
    "tsconfig.json", "conftest.py", "tests/conftest.py",
    "jest.config.js", "jest.config.ts", "jest.setup.js", "setupTests.js",
    "vite.config.js", "vite.config.ts", "vitest.config.js", "vitest.config.ts"
]
MANIFEST_PATTERNS = MANIFEST_FILES + [":(glob)**/conftest.py", ":(glob)*.mk", ":(glob)Makefile.*"]


def check_manifest_tampering(workspace_path: str, conv_id: Optional[str] = None) -> Tuple[bool, List[str]]:
    """
    Detects if build/test manifests have uncommitted modifications OR were modified in commits during this session.
    Prevents an agent from editing Makefile (e.g. test: exit 0) or package.json scripts and committing them to spoof Tier 2.
    """
    if not workspace_path or not os.path.exists(os.path.join(workspace_path, ".git")):
        return False, []

    modified = set()

    # 1. Check working tree for uncommitted manifest edits
    try:
        proc = subprocess.run(
            ["git", "-c", "safe.directory=*", "status", "--porcelain", "--"] + MANIFEST_PATTERNS,
            cwd=workspace_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2.0,
            text=True
        )
        if proc.returncode == 0 and proc.stdout.strip():
            for line in proc.stdout.splitlines():
                line_s = line.strip()
                if line_s:
                    fname = line_s[2:].strip()
                    if " -> " in fname:
                        fname = fname.split(" -> ")[1].strip()
                    modified.add(fname)
        elif proc.returncode != 0:
            return True, [f"<git-status-error: {(proc.stderr or '').strip()[:200]}>"]
    except Exception as e:
        return True, [f"<git-exception: {str(e)[:200]}>"]

    # 2. Check committed modifications against session baseline commit
    if conv_id:
        safe_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(conv_id))[:32]
        conv_hash = hashlib.sha256(str(conv_id).encode("utf-8")).hexdigest()[:16]
        baseline_sha = None

        # Check daemon ledger immutable baseline
        try:
            from ledger import _ledger
            baseline_sha = _ledger.get_session_baseline(conv_id, workspace_path)
        except Exception:
            try:
                from daemon.ledger import _ledger
                baseline_sha = _ledger.get_session_baseline(conv_id, workspace_path)
            except Exception:
                pass

        # Check git ref refs/hardtruth/baseline/<conv_hash>
        if not baseline_sha:
            try:
                proc_ref = subprocess.run(
                    ["git", "rev-parse", f"refs/hardtruth/baseline/{conv_hash}"],
                    cwd=workspace_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=1.0,
                    text=True
                )
                if proc_ref.returncode == 0 and proc_ref.stdout.strip():
                    baseline_sha = proc_ref.stdout.strip()
            except Exception:
                pass

        # Check halt dir local fallback
        if not baseline_sha:
            halt_dir = os.environ.get("HARDTRUTH_HALT_DIR", os.path.expanduser("~/.hardtruth/halts"))
            baseline_file = os.path.join(halt_dir, f"baseline_{safe_slug}_{conv_hash}.json")
            if os.path.exists(baseline_file):
                try:
                    with open(baseline_file, "r") as f:
                        baseline_sha = json.load(f).get("baseline_sha")
                except Exception:
                    pass

        if baseline_sha:
            try:
                proc_diff = subprocess.run(
                    ["git", "-c", "safe.directory=*", "diff", "--name-only", baseline_sha, "HEAD", "--"] + MANIFEST_PATTERNS,
                    cwd=workspace_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=2.0,
                    text=True
                )
                if proc_diff.returncode == 0 and proc_diff.stdout.strip():
                    for line in proc_diff.stdout.splitlines():
                        fname = line.strip()
                        if fname:
                            modified.add(fname)
                elif proc_diff.returncode != 0:
                    return True, [f"<git-diff-error: {(proc_diff.stderr or '').strip()[:200]}>"]
            except Exception as e:
                return True, [f"<git-diff-exception: {str(e)[:200]}>"]

    if modified:
        return True, sorted(list(modified))
    return False, []


def detect_test_runner(workspace_path: str, tampered_manifests: Optional[List[str]] = None) -> Optional[str]:
    """
    Detects canonical test runner based on repository manifest files.
    Safeguards against manifest tampering by refusing to execute modified Makefiles or package.json scripts.
    """
    if not workspace_path or not os.path.exists(workspace_path):
        return None

    tampered = set(tampered_manifests or [])

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

    # 2. Rust (cargo test)
    cargo_toml = os.path.join(workspace_path, "Cargo.toml")
    if os.path.exists(cargo_toml) and shutil.which("cargo"):
        return "cargo test"

    # 3. Go (go test)
    go_mod = os.path.join(workspace_path, "go.mod")
    if os.path.exists(go_mod) and shutil.which("go"):
        return "go test ./..."

    # 4. Node.js (npm test) - only if package.json has not been modified
    package_json = os.path.join(workspace_path, "package.json")
    if os.path.exists(package_json) and "package.json" not in tampered:
        try:
            with open(package_json, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "test" in data.get("scripts", {}):
                    return "npm test"
        except Exception:
            pass

    # 5. Makefile (make test) - only if Makefile has not been modified
    makefile = os.path.join(workspace_path, "Makefile")
    if os.path.exists(makefile) and "Makefile" not in tampered and shutil.which("make"):
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


def resolve_workspace_path(workspace_path: Optional[str]) -> Optional[str]:
    """
    Maps a host workspace path to the path visible inside a containerized daemon.
    Uses HARDTRUTH_WORKSPACE_MAP env: a JSON object of {host_prefix: container_prefix}.
    Longest host-prefix match wins. Identity when unset or unmapped.
    """
    if not workspace_path:
        return workspace_path
    raw = os.environ.get("HARDTRUTH_WORKSPACE_MAP", "").strip()
    if not raw:
        return workspace_path
    try:
        mapping = json.loads(raw)
    except Exception:
        return workspace_path
    norm = os.path.abspath(workspace_path)
    best = None
    best_len = -1
    for host_prefix, container_prefix in mapping.items():
        hp = os.path.abspath(str(host_prefix))
        if norm == hp or norm.startswith(hp.rstrip(os.sep) + os.sep):
            if len(hp) > best_len:
                best = (hp, str(container_prefix).rstrip(os.sep))
                best_len = len(hp)
    if best is None:
        return workspace_path
    hp, cp = best
    return cp + norm[len(hp):]


def run_container_verification(
    workspace_path: str,
    test_cmd: str,
    timeout_sec: int = 60
) -> Optional[Dict[str, Any]]:
    """
    Executes tests inside an ephemeral Docker container with read-only volume and no network access.
    Uses tmpfs mounts and environment caches so standard runners (pytest, npm, cargo) don't crash on read-only mount.
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
    image = "python:3.11-slim"
    if "npm" in test_cmd or "yarn" in test_cmd or "bun" in test_cmd:
        image = "node:20-alpine"
    elif "cargo" in test_cmd:
        image = "rust:alpine"
    elif "go test" in test_cmd:
        image = "golang:alpine"

    actual_cmd = test_cmd
    if actual_cmd.startswith("pytest") and "-o cache_dir" not in actual_cmd:
        actual_cmd = f"{actual_cmd} -o cache_dir=/tmp/.pytest_cache -p no:cacheprovider"

    docker_args = [
        docker_bin, "run", "--rm",
        "--network", "none",
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "--pids-limit", "256",
        "--memory", "1024m",
        "-v", f"{os.path.abspath(workspace_path)}:/workspace:ro",
        "-w", "/workspace",
        "--tmpfs", "/tmp:rw,exec,nosuid,size=512m",
        "--tmpfs", "/root/.cache:rw,exec,nosuid,size=512m",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "PYTHONPYCACHEPREFIX=/tmp/pycache",
        "-e", "CARGO_TARGET_DIR=/tmp/target",
        "-e", "npm_config_cache=/tmp/npm-cache",
        "-e", "TMPDIR=/tmp",
        "-e", "HARDTRUTH_TIER2_SANDBOX=1",
        image,
        "sh", "-c", actual_cmd
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
            "runner": f"docker({image}): {actual_cmd}",
            "output": output[:4000],
            "isolation": "container_read_only"
        }
    except subprocess.TimeoutExpired as te:
        return {
            "status": "timeout",
            "success": False,
            "exit_code": 124,
            "runner": f"docker({image}): {actual_cmd}",
            "output": f"Tier 2 container verification timed out after {timeout_sec}s.\n{(te.stdout or '')[:1000]}",
            "isolation": "container_read_only"
        }
    except Exception:
        return None


def run_independent_verification(
    workspace_path: str,
    test_cmd: Optional[str] = None,
    timeout_sec: int = 60,
    conv_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Executes the canonical test suite outside the agent context.
    Prioritizes ephemeral read-only Docker container isolation,
    falling back to clean subprocess sandbox if Docker is unavailable.
    """
    orig_path = workspace_path
    workspace_path = resolve_workspace_path(workspace_path)
    if not workspace_path or not os.path.exists(workspace_path):
        return {
            "status": "unverified_no_workspace",
            "success": False,
            "exit_code": 1,
            "runner": None,
            "output": (f"🚨 TIER 2 HARD GATE FAILED: Workspace path is not visible to the daemon: "
                       f"{orig_path or workspace_path}. For a containerized daemon, mount the workspace "
                       f"and/or configure HARDTRUTH_WORKSPACE_MAP={{'host_prefix':'container_prefix'}}.")
        }

    is_tampered, tampered_files = check_manifest_tampering(workspace_path, conv_id=conv_id)

    # Manifest poisoning defense: reject agent-modified test definitions
    if is_tampered:
        manifest_list = ", ".join(sorted(list(tampered_files)))
        return {
            "status": "tampered",
            "success": False,
            "exit_code": 1,
            "runner": "manifest_integrity_guard",
            "output": f"🚨 TIER 2 HARD GATE REJECTED: Test manifest or configuration file(s) [{manifest_list}] were modified during this session. Tier 2 refuses to execute tests with agent-modified test configurations to prevent manifest poisoning.",
            "isolation": "manifest_tampering_check"
        }

    # Validate test_cmd against command injection if supplied externally
    if test_cmd:
        cmd_clean = test_cmd.strip()
        if re.search(r"[;&|`$><\r\n]", cmd_clean):
            return {
                "status": "rejected_unsafe_command",
                "success": False,
                "exit_code": 1,
                "runner": "command_sanitizer",
                "output": f"🚨 TIER 2 HARD GATE REJECTED: Disallowed shell metacharacters detected in requested test command: {test_cmd}",
                "isolation": "input_validation"
            }
        parts = shlex.split(cmd_clean)
        if not parts:
            return {
                "status": "rejected_empty_command",
                "success": False,
                "exit_code": 1,
                "runner": "command_sanitizer",
                "output": "🚨 TIER 2 HARD GATE REJECTED: Empty test command specified.",
                "isolation": "input_validation"
            }
        base_bin = os.path.basename(parts[0]).lower()
        allowed_bins = {"pytest", "npm", "yarn", "bun", "cargo", "jest", "vitest", "tox", "ctest"}
        if base_bin in ("python", "python3"):
            if len(parts) >= 3 and parts[1] == "-m" and parts[2] in ("unittest", "pytest"):
                pass
            else:
                return {
                    "status": "rejected_unauthorized_runner",
                    "success": False,
                    "exit_code": 1,
                    "runner": "command_sanitizer",
                    "output": f"🚨 TIER 2 HARD GATE REJECTED: Python runner only allows '-m unittest' or '-m pytest', received: {test_cmd}",
                    "isolation": "input_validation"
                }
        elif base_bin == "go":
            if len(parts) >= 2 and parts[1] == "test":
                pass
            else:
                return {
                    "status": "rejected_unauthorized_runner",
                    "success": False,
                    "exit_code": 1,
                    "runner": "command_sanitizer",
                    "output": f"🚨 TIER 2 HARD GATE REJECTED: Go runner only allows 'go test', received: {test_cmd}",
                    "isolation": "input_validation"
                }
        elif base_bin == "make":
            if any(a in ("-f", "--file", "--makefile") for a in parts[1:]):
                return {
                    "status": "rejected_unauthorized_runner",
                    "success": False,
                    "exit_code": 1,
                    "runner": "command_sanitizer",
                    "output": f"🚨 TIER 2 HARD GATE REJECTED: Custom makefile flags are rejected: {test_cmd}",
                    "isolation": "input_validation"
                }
        elif base_bin not in allowed_bins:
            return {
                "status": "rejected_unauthorized_runner",
                "success": False,
                "exit_code": 1,
                "runner": "command_sanitizer",
                "output": f"🚨 TIER 2 HARD GATE REJECTED: Command binary '{parts[0]}' is not an authorized test runner.",
                "isolation": "input_validation"
            }

        # Reject interpreter-escape flags across any runner
        if any(a in ("-c", "--eval", "-e") for a in parts[1:]):
            return {
                "status": "rejected_unauthorized_runner",
                "success": False,
                "exit_code": 1,
                "runner": "command_sanitizer",
                "output": f"🚨 TIER 2 HARD GATE REJECTED: Code execution flag detected in '{test_cmd}'.",
                "isolation": "input_validation"
            }

    canonical_runner = test_cmd or detect_test_runner(workspace_path, tampered_manifests=tampered_files)

    if not canonical_runner:
        return {
            "status": "unverified_no_runner",
            "success": False,
            "exit_code": 1,
            "runner": "none",
            "output": "🚨 TIER 2 HARD GATE FAILED: Code files were modified or verification was requested, but no canonical test suite or runner could be detected in the workspace. HardTruth never fails open: renaming or deleting test suites is not permitted."
        }

    # 1. Attempt Ephemeral Docker Container Verification (Full Physical Boundary)
    if os.environ.get("HARDTRUTH_PREFER_DOCKER_TIER2") == "1":
        container_res = run_container_verification(workspace_path, canonical_runner, timeout_sec=timeout_sec)
        if container_res is not None:
            return container_res

    # 2. Clean Subprocess Sandbox Execution (Clean Environment Boundary)
    # Construct clean_env from an explicit allowlist to prevent leaking daemon HMAC keys, API tokens, or ledger paths
    allowed_env_keys = {
        "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "USER", "SHELL", "PWD",
        "VIRTUAL_ENV", "CONDA_PREFIX", "NODE_PATH", "PYTHONPATH", "CARGO_HOME", "RUSTUP_HOME",
        "GOPATH", "GOROOT", "JAVA_HOME"
    }
    clean_env = {k: v for k, v in os.environ.items() if k in allowed_env_keys}
    # Explicitly ensure NO HARDTRUTH variables or daemon keys leak into test subprocess
    for k in list(clean_env.keys()):
        if k.startswith("HARDTRUTH_"):
            clean_env.pop(k, None)
    clean_env["PYTHONUNBUFFERED"] = "1"
    clean_env["CI"] = "true"
    clean_env["HARDTRUTH_TIER2_SANDBOX"] = "1"

    cmd_args = shlex.split(canonical_runner)
    if cmd_args and cmd_args[0] == "pytest" and "-o" not in cmd_args:
        cmd_args.extend(["-o", "cache_dir=/tmp/.pytest_cache", "-p", "no:cacheprovider"])

    resolved_bin = shutil.which(cmd_args[0])
    if resolved_bin:
        cmd_args[0] = resolved_bin

    try:
        proc = subprocess.Popen(
            cmd_args,
            cwd=workspace_path,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=clean_env,
            text=True,
            start_new_session=True
        )
        stdout, _ = proc.communicate(timeout=timeout_sec)
        exit_code = proc.returncode
        output = (stdout or "").strip()

        return {
            "status": "verified" if exit_code == 0 else "failed",
            "success": exit_code == 0,
            "exit_code": exit_code,
            "runner": canonical_runner,
            "output": output[:4000],
            "isolation": "clean_subprocess_sandbox"
        }

    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        stdout, _ = proc.communicate()
        return {
            "status": "timeout",
            "success": False,
            "exit_code": 124,
            "runner": canonical_runner,
            "output": f"Tier 2 external test execution timed out after {timeout_sec}s.\n{(stdout or '')[:1000]}",
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
