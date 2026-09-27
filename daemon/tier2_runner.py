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
from typing import Optional, Dict, Any, Tuple, List, Set


_pinned_runners: Dict[Tuple[str, str], str] = {}
_session_baseline_failures: Dict[Tuple[str, str], Set[str]] = {}


def extract_test_failures(output: str) -> Set[str]:
    failures = set()
    if not output:
        return failures
    for line in output.splitlines():
        line_s = line.strip()
        if line_s.startswith("FAILED ") or line_s.startswith("ERROR "):
            parts = line_s.split()
            if len(parts) >= 2:
                failures.add(parts[1].split(" - ")[0])
        elif line_s.startswith("FAIL: ") or line_s.startswith("ERROR: "):
            parts = line_s.split()
            if len(parts) >= 2:
                failures.add(parts[1])
        elif line_s.startswith("FAIL ") or line_s.startswith("✕ "):
            failures.add(line_s)
        elif " ... FAILED" in line_s:
            failures.add(line_s.split()[1] if len(line_s.split()) >= 2 else line_s)
        elif line_s.startswith("--- FAIL:"):
            parts = line_s.split()
            if len(parts) >= 3:
                failures.add(parts[2])
    return failures


MANIFEST_FILES = [
    "Makefile", "GNUmakefile", "package.json", "pyproject.toml", "Cargo.toml", "setup.py", "setup.cfg",
    "pytest.ini", ".pytest.ini", "tox.ini", "noxfile.py", ".mocharc.json", ".mocharc.yml",
    "tsconfig.json", "conftest.py", "tests/conftest.py",
    "jest.config.js", "jest.config.ts", "jest.setup.js", "setupTests.js",
    "vite.config.js", "vite.config.ts", "vitest.config.js", "vitest.config.ts"
]
MANIFEST_PATTERNS = MANIFEST_FILES + [":(glob)**/conftest.py", ":(glob)*.mk", ":(glob)Makefile.*"]


def is_actual_manifest_tampering(workspace_path: str, fname: str, baseline_sha: Optional[str] = None) -> bool:
    """
    Distinguishes legitimate dependency/configuration edits from test-weakening/tampering.
    Dedicated test manifests (conftest.py, pytest.ini, jest.config.*, etc.) are always tampering.
    Dual-use manifests (package.json, pyproject.toml, setup.cfg, Makefile, Cargo.toml)
    are only tampering if test runner commands, test configurations, or test dependencies are altered.
    """
    base = os.path.basename(fname)
    if "conftest" in base or "pytest.ini" in base or "tox.ini" in base or "jest" in base or "vitest" in base or "mocharc" in base or "noxfile" in base:
        return True

    ws_abs = os.path.abspath(workspace_path)
    diff_args = [
        "git", "-c", f"safe.directory={ws_abs}",
        "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "diff"
    ]
    if baseline_sha:
        diff_args.append(baseline_sha)
    diff_args.extend(["--", fname])
    diff_text = ""
    try:
        proc = subprocess.run(diff_args, cwd=workspace_path, capture_output=True, text=True, timeout=2.0)
        diff_text = proc.stdout if proc.returncode == 0 else ""
    except Exception:
        return True

    if not diff_text:
        try:
            proc = subprocess.run([
                "git", "-c", f"safe.directory={ws_abs}",
                "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "diff", "--", fname
            ], cwd=workspace_path, capture_output=True, text=True, timeout=2.0)
            diff_text = proc.stdout if proc.returncode == 0 else ""
        except Exception:
            return True

    if not diff_text:
        return True

    if base in ("package.json", "tsconfig.json"):
        if re.search(r'^\+[^\n]*"(?:test|test:[\w-]+|scripts)"', diff_text, re.MULTILINE):
            return True
        return False
    elif base in ("setup.cfg", "pyproject.toml"):
        if re.search(r'^\+[^\n]*(?:\[tool\.pytest|\[tool:pytest|addopts|testpaths|ignore|norecursedirs)', diff_text, re.MULTILINE | re.IGNORECASE):
            return True
        return False
    elif base in ("Makefile", "GNUmakefile"):
        if re.search(r'(?:^|\n)[ \t\+-]*(?:test|check)\s*:', diff_text) or re.search(r'^\+[^\n]*(?:test|check)\b', diff_text, re.MULTILINE):
            return True
        return False
    elif base == "Cargo.toml":
        if re.search(r'^\+[^\n]*\[\[test\]\]', diff_text, re.MULTILINE):
            return True
        return False

    return True


def check_manifest_tampering(workspace_path: str, conv_id: Optional[str] = None) -> Tuple[bool, List[str]]:
    """
    Detects if build/test manifests have uncommitted modifications OR were modified in commits during this session.
    Prevents an agent from editing Makefile (e.g. test: exit 0) or package.json scripts and committing them to spoof Tier 2.
    """
    if not workspace_path or not os.path.exists(os.path.join(workspace_path, ".git")):
        return False, []

    ws_abs = os.path.abspath(workspace_path)
    modified = set()

    # 1. Check working tree for uncommitted manifest edits
    try:
        proc = subprocess.run(
            ["git", "-c", f"safe.directory={ws_abs}", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "status", "--porcelain", "--"] + MANIFEST_PATTERNS,
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
    baseline_sha = None
    if conv_id:
        safe_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(conv_id))[:32]
        conv_hash = hashlib.sha256(str(conv_id).encode("utf-8")).hexdigest()[:16]

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
                    ["git", "-c", f"safe.directory={ws_abs}", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "rev-parse", f"refs/hardtruth/baseline/{conv_hash}"],
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
                chk = subprocess.run(
                    ["git", "-c", f"safe.directory={ws_abs}", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "cat-file", "-e", f"{baseline_sha}^{{commit}}"],
                    cwd=workspace_path,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.0
                )
                if chk.returncode != 0:
                    baseline_sha = None
            except Exception:
                baseline_sha = None

        if baseline_sha:
            try:
                proc_diff = subprocess.run(
                    ["git", "-c", f"safe.directory={ws_abs}", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "diff", "--name-only", baseline_sha, "HEAD", "--"] + MANIFEST_PATTERNS,
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

    # Filter modified manifests to actual tampering vs normal dependency additions
    tampered = []
    for f in modified:
        if f.startswith("<git-"):
            tampered.append(f)
        elif is_actual_manifest_tampering(workspace_path, f, baseline_sha):
            tampered.append(f)

    if tampered:
        return True, sorted(tampered)
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

    # 4. Node.js (npm test, pnpm test, yarn test, bun test) - only if package.json has not been modified
    package_json = os.path.join(workspace_path, "package.json")
    if os.path.exists(package_json) and "package.json" not in tampered:
        try:
            with open(package_json, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "test" in data.get("scripts", {}):
                    if os.path.exists(os.path.join(workspace_path, "pnpm-lock.yaml")) and shutil.which("pnpm"):
                        return "pnpm test"
                    elif os.path.exists(os.path.join(workspace_path, "yarn.lock")) and shutil.which("yarn"):
                        return "yarn test"
                    elif os.path.exists(os.path.join(workspace_path, "bun.lockb")) and shutil.which("bun"):
                        return "bun test"
                    elif shutil.which("npm"):
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


def find_project_root_for_file(fpath: str, boundary: Optional[str] = None) -> Optional[str]:
    """
    Given a file path, walks upward to find the nearest enclosing project root
    containing a project manifest (Cargo.toml, package.json, pyproject.toml, etc.)
    or a tests/ directory. Stops at boundary or user home.
    """
    if not fpath:
        return None
    curr = os.path.dirname(os.path.abspath(fpath))
    stop_dir = os.path.abspath(boundary) if boundary else os.path.expanduser("~")
    while curr and curr != "/" and len(curr) >= len(stop_dir):
        for manifest in MANIFEST_FILES:
            if os.path.isfile(os.path.join(curr, manifest)):
                return curr
        if os.path.isdir(os.path.join(curr, "tests")) or os.path.isdir(os.path.join(curr, "test")):
            return curr
        if os.path.isdir(os.path.join(curr, ".git")):
            return curr
        if curr == stop_dir:
            break
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent
    return None


def resolve_target_project_dir(
    workspace_dir: Optional[str],
    modified_files: Optional[List[str]] = None,
    conv_id: Optional[str] = None
) -> Optional[str]:
    """
    Dynamically resolves the actual project directory containing tests/manifests.
    If workspace_dir has no tests or is a parent directory (like /Users/ai/dev),
    dynamically inspects modified files, git status, or project subfolders to find
    the actual subfolder containing package.json, Cargo.toml, pytest.ini, or tests/.
    """
    if not workspace_dir or not os.path.exists(workspace_dir):
        return workspace_dir
    ws_norm = os.path.abspath(workspace_dir)

    # 1. If workspace_dir itself already has a test runner, keep it
    if detect_test_runner(ws_norm) is not None:
        return ws_norm

    # 2. Check modified files list
    if modified_files:
        for f in modified_files:
            f_abs = f if os.path.isabs(f) else os.path.join(ws_norm, f)
            root = find_project_root_for_file(f_abs, boundary=ws_norm)
            if root and detect_test_runner(root) is not None:
                return root

    # 3. Check session ledger modified files if conv_id provided
    if conv_id:
        try:
            from ledger import _ledger
            premise = _ledger.get_premise(conv_id)
            mod_paths = premise.get("modified_file_paths", []) or premise.get("modified_files", [])
            for p in mod_paths:
                p_abs = p if os.path.isabs(p) else os.path.join(ws_norm, p)
                root = find_project_root_for_file(p_abs, boundary=ws_norm)
                if root and detect_test_runner(root) is not None:
                    return root
        except Exception:
            pass

    # 4. Check git status in workspace or subdirectories
    try:
        proc = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "status", "--porcelain"],
            cwd=ws_norm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0
        )
        if proc.returncode == 0 and proc.stdout.strip():
            for line in proc.stdout.splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    p = os.path.join(ws_norm, parts[1])
                    root = find_project_root_for_file(p, boundary=ws_norm)
                    if root and detect_test_runner(root) is not None:
                        return root
    except Exception:
        pass

    # 5. Search subdirectories (e.g. apps/*, vibehard/apps/*)
    try:
        for entry in sorted(os.listdir(ws_norm)):
            if entry.startswith(".") or entry in ("node_modules", "target", "venv", ".venv"):
                continue
            sub = os.path.join(ws_norm, entry)
            if os.path.isdir(sub):
                if detect_test_runner(sub) is not None:
                    return sub
                for nested in ["apps", "packages", "crates", "services"]:
                    nested_dir = os.path.join(sub, nested)
                    if os.path.isdir(nested_dir):
                        for n_entry in sorted(os.listdir(nested_dir)):
                            n_sub = os.path.join(nested_dir, n_entry)
                            if os.path.isdir(n_sub) and detect_test_runner(n_sub) is not None:
                                return n_sub
                if os.path.isdir(os.path.join(sub, ".git")):
                    try:
                        sub_proc = subprocess.run(
                            ["git", "-c", "core.hooksPath=/dev/null", "status", "--porcelain"],
                            cwd=sub, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0
                        )
                        if sub_proc.returncode == 0 and sub_proc.stdout.strip():
                            for line in sub_proc.stdout.splitlines():
                                parts = line.strip().split(maxsplit=1)
                                if len(parts) == 2:
                                    p = os.path.join(sub, parts[1])
                                    root = find_project_root_for_file(p, boundary=sub)
                                    if root and detect_test_runner(root) is not None:
                                        return root
                    except Exception:
                        pass
    except Exception:
        pass

    return ws_norm


def is_macos_native_project(workspace_path: str) -> bool:
    """
    Detects if a project requires macOS/Darwin native execution environment
    (e.g. Reachy Mini, CoreAudio, Cocoa, objc, Foundation).
    Such projects cannot compile or execute inside Linux Docker containers.
    """
    if not workspace_path or not os.path.exists(workspace_path):
        return False

    # Explicit environment override
    if os.environ.get("HARDTRUTH_NATIVE_MACOS") == "1" or os.environ.get("HARDTRUTH_PLATFORM") == "darwin":
        return True

    ws_lower = workspace_path.lower()
    if "reachy" in ws_lower or "reachyd" in ws_lower:
        return True

    # 1. Cargo.toml inspection
    cargo_toml = os.path.join(workspace_path, "Cargo.toml")
    if os.path.isfile(cargo_toml):
        try:
            with open(cargo_toml, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().lower()
                mac_crates = [
                    "coreaudio", "coreaudio-sys", "cocoa", "objc", "objc2",
                    "core-foundation", "core_foundation", "security-framework",
                    "metal", "io-kit", 'target_os = "macos"', 'target_os="macos"'
                ]
                if any(crate in content for crate in mac_crates):
                    return True
        except Exception:
            pass

    # 2. package.json inspection
    pkg_json = os.path.join(workspace_path, "package.json")
    if os.path.isfile(pkg_json):
        try:
            with open(pkg_json, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().lower()
                if '"darwin"' in content or 'fsevents' in content:
                    return True
        except Exception:
            pass

    # 3. Source files inspection (Swift, Objective-C, CoreAudio imports)
    src_dir = os.path.join(workspace_path, "src")
    check_dir = src_dir if os.path.isdir(src_dir) else workspace_path
    try:
        for root, dirs, files in os.walk(check_dir):
            if any(ignored in root for ignored in ["target", ".git", "node_modules"]):
                continue
            for fname in files:
                if fname.endswith((".swift", ".m", ".mm")):
                    return True
                if fname.endswith((".rs", ".c", ".cpp", ".py")):
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            head = f.read(2048).lower()
                            if ("coreaudio" in head or "nsapplication" in head or
                                "nsworkspace" in head or "<cocoa/cocoa.h>" in head or
                                "import objc" in head):
                                return True
                    except Exception:
                        pass
    except Exception:
        pass

    return False



def find_project_root_for_file(fpath: str, boundary: Optional[str] = None) -> Optional[str]:
    """
    Given a file path, walks upward to find the nearest enclosing project root
    containing a project manifest (Cargo.toml, package.json, pyproject.toml, etc.)
    or a tests/ directory. Stops at boundary or user home.
    """
    if not fpath:
        return None
    curr = os.path.dirname(os.path.abspath(fpath))
    stop_dir = os.path.abspath(boundary) if boundary else os.path.expanduser("~")
    while curr and curr != "/" and len(curr) >= len(stop_dir):
        for manifest in MANIFEST_FILES:
            if os.path.isfile(os.path.join(curr, manifest)):
                return curr
        if os.path.isdir(os.path.join(curr, "tests")) or os.path.isdir(os.path.join(curr, "test")):
            return curr
        if os.path.isdir(os.path.join(curr, ".git")):
            return curr
        if curr == stop_dir:
            break
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent
    return None


def resolve_target_project_dir(
    workspace_dir: Optional[str],
    modified_files: Optional[List[str]] = None,
    conv_id: Optional[str] = None
) -> Optional[str]:
    """
    Dynamically resolves the actual project directory containing tests/manifests.
    If workspace_dir has no tests or is a parent directory (like /Users/ai/dev),
    dynamically inspects modified files, git status, or project subfolders to find
    the actual subfolder containing package.json, Cargo.toml, pytest.ini, or tests/.
    """
    if not workspace_dir or not os.path.exists(workspace_dir):
        return workspace_dir
    ws_norm = os.path.abspath(workspace_dir)

    # 1. If workspace_dir itself already has a test runner, keep it
    if detect_test_runner(ws_norm) is not None:
        return ws_norm

    # 2. Check modified files list
    if modified_files:
        for f in modified_files:
            f_abs = f if os.path.isabs(f) else os.path.join(ws_norm, f)
            root = find_project_root_for_file(f_abs, boundary=ws_norm)
            if root and detect_test_runner(root) is not None:
                return root

    # 3. Check session ledger modified files if conv_id provided
    if conv_id:
        try:
            from ledger import _ledger
            premise = _ledger.get_premise(conv_id)
            mod_paths = premise.get("modified_file_paths", []) or premise.get("modified_files", [])
            for p in mod_paths:
                p_abs = p if os.path.isabs(p) else os.path.join(ws_norm, p)
                root = find_project_root_for_file(p_abs, boundary=ws_norm)
                if root and detect_test_runner(root) is not None:
                    return root
        except Exception:
            pass

    # 4. Check git status in workspace or subdirectories
    try:
        proc = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "status", "--porcelain"],
            cwd=ws_norm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0
        )
        if proc.returncode == 0 and proc.stdout.strip():
            for line in proc.stdout.splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    p = os.path.join(ws_norm, parts[1])
                    root = find_project_root_for_file(p, boundary=ws_norm)
                    if root and detect_test_runner(root) is not None:
                        return root
    except Exception:
        pass

    # 5. Search subdirectories (e.g. apps/*, vibehard/apps/*)
    try:
        for entry in sorted(os.listdir(ws_norm)):
            if entry.startswith(".") or entry in ("node_modules", "target", "venv", ".venv"):
                continue
            sub = os.path.join(ws_norm, entry)
            if os.path.isdir(sub):
                if detect_test_runner(sub) is not None:
                    return sub
                for nested in ["apps", "packages", "crates", "services"]:
                    nested_dir = os.path.join(sub, nested)
                    if os.path.isdir(nested_dir):
                        for n_entry in sorted(os.listdir(nested_dir)):
                            n_sub = os.path.join(nested_dir, n_entry)
                            if os.path.isdir(n_sub) and detect_test_runner(n_sub) is not None:
                                return n_sub
                if os.path.isdir(os.path.join(sub, ".git")):
                    try:
                        sub_proc = subprocess.run(
                            ["git", "-c", "core.hooksPath=/dev/null", "status", "--porcelain"],
                            cwd=sub, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0
                        )
                        if sub_proc.returncode == 0 and sub_proc.stdout.strip():
                            for line in sub_proc.stdout.splitlines():
                                parts = line.strip().split(maxsplit=1)
                                if len(parts) == 2:
                                    p = os.path.join(sub, parts[1])
                                    root = find_project_root_for_file(p, boundary=sub)
                                    if root and detect_test_runner(root) is not None:
                                        return root
                    except Exception:
                        pass
    except Exception:
        pass

    return ws_norm


def is_macos_native_project(workspace_path: str) -> bool:
    """
    Detects if a project requires macOS/Darwin native execution environment
    (e.g. Reachy Mini, CoreAudio, Cocoa, objc, Foundation).
    Such projects cannot compile or execute inside Linux Docker containers.
    """
    if not workspace_path or not os.path.exists(workspace_path):
        return False

    # Explicit environment override
    if os.environ.get("HARDTRUTH_NATIVE_MACOS") == "1" or os.environ.get("HARDTRUTH_PLATFORM") == "darwin":
        return True

    ws_lower = workspace_path.lower()
    if "reachy" in ws_lower or "reachyd" in ws_lower:
        return True

    # 1. Cargo.toml inspection
    cargo_toml = os.path.join(workspace_path, "Cargo.toml")
    if os.path.isfile(cargo_toml):
        try:
            with open(cargo_toml, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().lower()
                mac_crates = [
                    "coreaudio", "coreaudio-sys", "cocoa", "objc", "objc2",
                    "core-foundation", "core_foundation", "security-framework",
                    "metal", "io-kit", 'target_os = "macos"', 'target_os="macos"'
                ]
                if any(crate in content for crate in mac_crates):
                    return True
        except Exception:
            pass

    # 2. package.json inspection
    pkg_json = os.path.join(workspace_path, "package.json")
    if os.path.isfile(pkg_json):
        try:
            with open(pkg_json, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().lower()
                if '"darwin"' in content or 'fsevents' in content:
                    return True
        except Exception:
            pass

    # 3. Source files inspection (Swift, Objective-C, CoreAudio imports)
    src_dir = os.path.join(workspace_path, "src")
    check_dir = src_dir if os.path.isdir(src_dir) else workspace_path
    try:
        for root, dirs, files in os.walk(check_dir):
            if any(ignored in root for ignored in ["target", ".git", "node_modules"]):
                continue
            for fname in files:
                if fname.endswith((".swift", ".m", ".mm")):
                    return True
                if fname.endswith((".rs", ".c", ".cpp", ".py")):
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            head = f.read(2048).lower()
                            if ("coreaudio" in head or "nsapplication" in head or
                                "nsworkspace" in head or "<cocoa/cocoa.h>" in head or
                                "import objc" in head):
                                return True
                    except Exception:
                        pass
    except Exception:
        pass

    return False



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


def validate_runner_command(cmd: str) -> Optional[Dict[str, Any]]:
    """
    Validates runner command against command injection, shell metacharacters,
    disallowed binaries, and interpreter code-execution flags (-c, --eval, -e).
    Returns None if valid, or a rejection dict if invalid.
    """
    cmd_clean = (cmd or "").strip()
    if not cmd_clean:
        return {
            "status": "rejected_empty_command",
            "success": False,
            "exit_code": 1,
            "runner": "command_sanitizer",
            "output": "🚨 TIER 2 HARD GATE REJECTED: Empty test command specified.",
            "isolation": "input_validation"
        }
    if re.search(r"[;&|`$><\r\n]", cmd_clean):
        return {
            "status": "rejected_unsafe_command",
            "success": False,
            "exit_code": 1,
            "runner": "command_sanitizer",
            "output": f"🚨 TIER 2 HARD GATE REJECTED: Disallowed shell metacharacters detected in test runner command: {cmd}",
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
                "output": f"🚨 TIER 2 HARD GATE REJECTED: Python runner only allows '-m unittest' or '-m pytest', received: {cmd}",
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
                "output": f"🚨 TIER 2 HARD GATE REJECTED: Go runner only allows 'go test', received: {cmd}",
                "isolation": "input_validation"
            }
    elif base_bin == "make":
        if any(a in ("-f", "--file", "--makefile") for a in parts[1:]):
            return {
                "status": "rejected_unauthorized_runner",
                "success": False,
                "exit_code": 1,
                "runner": "command_sanitizer",
                "output": f"🚨 TIER 2 HARD GATE REJECTED: Custom makefile flags are rejected: {cmd}",
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
            "output": f"🚨 TIER 2 HARD GATE REJECTED: Code execution flag detected in '{cmd}'.",
            "isolation": "input_validation"
        }
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
    falling back to clean subprocess sandbox if Docker is unavailable and subprocess execution is authorized.
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

    session_key = (str(conv_id), os.path.abspath(workspace_path)) if conv_id else None
    pinned_runner = None
    baseline_failures: Set[str] = set()

    # Authoritative memory/daemon state only (P3 fix: never trust agent-writable halt files)
    if session_key:
        pinned_runner = _pinned_runners.get(session_key)
        if session_key in _session_baseline_failures:
            baseline_failures = _session_baseline_failures[session_key]

    if test_cmd:
        canonical_runner = test_cmd
    elif pinned_runner:
        canonical_runner = pinned_runner
    else:
        canonical_runner = detect_test_runner(workspace_path, tampered_manifests=tampered_files)
        if not canonical_runner:
            # Dynamic Target Path Resolution: check if workspace_path is a parent directory
            resolved_sub = resolve_target_project_dir(workspace_path, conv_id=conv_id)
            if resolved_sub and resolved_sub != workspace_path and os.path.isdir(resolved_sub):
                workspace_path = resolved_sub
                canonical_runner = detect_test_runner(workspace_path, tampered_manifests=tampered_files)
        if canonical_runner and session_key:
            _pinned_runners[session_key] = canonical_runner

    if not canonical_runner:
        return {
            "status": "unverified_no_runner",
            "success": False,
            "exit_code": 1,
            "runner": "none",
            "output": "🚨 TIER 2 HARD GATE FAILED: Code files were modified or verification was requested, but no canonical test suite or runner could be detected in the workspace. HardTruth never fails open: renaming or deleting test suites is not permitted."
        }

    # P4 Fix: Unconditionally validate canonical_runner before ANY execution
    validation_err = validate_runner_command(canonical_runner)
    if validation_err:
        return validation_err

    is_mac_native = is_macos_native_project(workspace_path)
    is_host_darwin = sys.platform == "darwin"

    # 1. Attempt Ephemeral Docker Container Verification (First-priority physical isolation)
    # Skip container if project requires macOS/Darwin native APIs and host is Darwin
    container_res = None
    if os.environ.get("HARDTRUTH_TIER2_CONTAINER") != "0" and not (is_mac_native and is_host_darwin):
        container_res = run_container_verification(workspace_path, canonical_runner, timeout_sec=timeout_sec)
        if container_res is not None and container_res.get("exit_code") != 127:
            # Check if container failed due to Linux platform incompatibility
            out_lower = (container_res.get("output", "") or "").lower()
            linux_platform_failure = any(term in out_lower for term in [
                "can't find crate for `coreaudio_sys`",
                "can't find crate for `objc`",
                "can't find crate for `cocoa`",
                "could not find system library",
                "framework not found",
                "target_os", "unsupported platform", "apple"
            ])
            if not container_res.get("success") and linux_platform_failure and is_host_darwin:
                container_res = None
            else:
                if not container_res.get("success") and container_res.get("exit_code") not in (0, None):
                    curr_failures = extract_test_failures(container_res.get("output", ""))
                    if curr_failures and baseline_failures and curr_failures.issubset(baseline_failures):
                        container_res["success"] = True
                        container_res["status"] = "verified_regression_free"
                        container_res["output"] = f"Tier 2 verified (no new regressions: {len(curr_failures)} pre-existing failures matched baseline set).\n" + container_res.get("output", "")
                return container_res

    # 2. Host Subprocess Execution Gate (S1 fix: host execution must be authorized)
    allow_subprocess = os.environ.get("HARDTRUTH_TIER2_ALLOW_SUBPROCESS", "1" if (os.environ.get("CI") or os.environ.get("PYTEST_CURRENT_TEST")) else "0")
    if is_mac_native and is_host_darwin:
        allow_subprocess = "1"

    if allow_subprocess != "1":
        return {
            "status": "unverified_no_isolation",
            "success": False,
            "exit_code": 1,
            "runner": "isolation_guard",
            "output": (
                "🚨 TIER 2 HARD GATE REJECTED: Ephemeral container isolation is unavailable and host subprocess execution is not explicitly authorized. "
                "HardTruth never executes repository code unsandboxed on the host without operator approval. "
                "To permit clean subprocess sandbox execution on the host in trusted environments, set HARDTRUTH_TIER2_ALLOW_SUBPROCESS=1."
            ),
            "isolation": "isolation_guard"
        }

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

        sub_res = {
            "status": "verified" if exit_code == 0 else "failed",
            "success": exit_code == 0,
            "exit_code": exit_code,
            "runner": canonical_runner,
            "output": output[:4000],
            "isolation": "clean_subprocess_sandbox"
        }
        if not sub_res["success"] and sub_res["exit_code"] not in (0, None):
            curr_failures = extract_test_failures(sub_res.get("output", ""))
            if curr_failures and baseline_failures and curr_failures.issubset(baseline_failures):
                sub_res["success"] = True
                sub_res["status"] = "verified_regression_free"
                sub_res["output"] = f"Tier 2 verified (no new regressions: {len(curr_failures)} pre-existing failures matched baseline set).\n" + sub_res.get("output", "")

        return sub_res

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
