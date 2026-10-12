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
from typing import Optional, Dict, Any, Tuple, List, Set, Callable


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


def _git_env() -> Dict[str, str]:
    """Environment for git subprocesses with every GIT_* variable removed, so the caller's
    environment cannot redirect git at a different repository, index or object store."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _repo_root_for_dir(path: str) -> Optional[str]:
    """The git top-level containing directory `path`, or None if it is in no repository."""
    if not path:
        return None
    if os.path.exists(os.path.join(path, ".git")):
        return os.path.abspath(path)
    return git_toplevel_for_file(os.path.join(path, "__hardtruth_probe__"))


def _manifest_patterns_for(repo_root: str, workspace_path: str) -> List[str]:
    """MANIFEST_PATTERNS relative to repo_root, plus the same patterns scoped to
    workspace_path when it is a subdirectory of the repository (a monorepo subproject)."""
    patterns = list(MANIFEST_PATTERNS)
    try:
        rel = os.path.relpath(os.path.realpath(workspace_path), os.path.realpath(repo_root))
    except ValueError:
        rel = "."
    if rel not in (".", "") and not rel.startswith(".."):
        rel = rel.replace(os.sep, "/")
        patterns += [f"{rel}/{m}" for m in MANIFEST_FILES]
        patterns += [f":(glob){rel}/**/conftest.py", f":(glob){rel}/*.mk", f":(glob){rel}/Makefile.*"]
    return patterns


def repo_baseline_cache_path(conv_id: str, repo_root: str) -> str:
    """Per-(session, repository) baseline cache file. Distinct from the session-keyed
    baseline file so recording one repository's baseline never overwrites another's."""
    halt_dir = os.environ.get("HARDTRUTH_HALT_DIR", os.path.expanduser("~/.hardtruth/halts"))
    conv_hash = hashlib.sha256(str(conv_id).encode("utf-8")).hexdigest()[:16]
    repo_hash = hashlib.sha256(os.path.realpath(repo_root).encode("utf-8")).hexdigest()[:16]
    return os.path.join(halt_dir, f"baseline_repo_{conv_hash}_{repo_hash}.json")


def check_manifest_tampering(
    workspace_path: str,
    conv_id: Optional[str] = None,
    baseline_sha: Optional[str] = None
) -> Tuple[bool, List[str]]:
    """
    Detects if build/test manifests have uncommitted modifications OR were modified in commits during this session.
    Prevents an agent from editing Makefile (e.g. test: exit 0) or package.json scripts and committing them to spoof Tier 2.

    The check is anchored at the git top-level containing workspace_path, so a project root
    that is a subdirectory of a repository (a monorepo subproject) is checked too: manifests
    at the repository root, manifests in the subproject, and every conftest.py. When the
    caller already holds a trusted session baseline for the repository it passes
    baseline_sha; otherwise the baseline is looked up (daemon ledger, git ref, cache files).
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return False, []
    repo_root = _repo_root_for_dir(workspace_path)
    if not repo_root:
        return False, []

    ws_abs = os.path.abspath(repo_root)
    git_base = ["git", "-c", f"safe.directory={ws_abs}", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null"]
    env = _git_env()
    patterns = _manifest_patterns_for(repo_root, workspace_path)
    modified = set()

    # 1. Check working tree for uncommitted manifest edits
    try:
        proc = subprocess.run(
            git_base + ["status", "--porcelain", "--"] + patterns,
            cwd=repo_root,
            env=env,
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
    if not baseline_sha and conv_id:
        safe_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(conv_id))[:32]
        conv_hash = hashlib.sha256(str(conv_id).encode("utf-8")).hexdigest()[:16]

        # Check daemon ledger immutable baseline (keyed by the repository, then the project path)
        for key in dict.fromkeys([ws_abs, os.path.abspath(workspace_path)]):
            if baseline_sha:
                break
            try:
                from ledger import _ledger
                baseline_sha = _ledger.get_session_baseline(conv_id, key)
            except Exception:
                try:
                    from daemon.ledger import _ledger
                    baseline_sha = _ledger.get_session_baseline(conv_id, key)
                except Exception:
                    pass

        # Check git ref refs/hardtruth/baseline/<conv_hash>
        if not baseline_sha:
            try:
                proc_ref = subprocess.run(
                    git_base + ["rev-parse", f"refs/hardtruth/baseline/{conv_hash}"],
                    cwd=repo_root,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=1.0,
                    text=True
                )
                if proc_ref.returncode == 0 and proc_ref.stdout.strip():
                    baseline_sha = proc_ref.stdout.strip()
            except Exception:
                pass

        # Check halt dir local fallbacks: per-repository file, then the session file
        if not baseline_sha:
            halt_dir = os.environ.get("HARDTRUTH_HALT_DIR", os.path.expanduser("~/.hardtruth/halts"))
            for baseline_file in (repo_baseline_cache_path(conv_id, repo_root),
                                  os.path.join(halt_dir, f"baseline_{safe_slug}_{conv_hash}.json")):
                if baseline_sha:
                    break
                if os.path.exists(baseline_file):
                    try:
                        with open(baseline_file, "r") as f:
                            baseline_sha = json.load(f).get("baseline_sha")
                    except Exception:
                        pass

    if baseline_sha:
        try:
            chk = subprocess.run(
                git_base + ["cat-file", "-e", f"{baseline_sha}^{{commit}}"],
                cwd=repo_root,
                env=env,
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
                git_base + ["diff", "--name-only", baseline_sha, "HEAD", "--"] + patterns,
                cwd=repo_root,
                env=env,
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
        elif is_actual_manifest_tampering(repo_root, f, baseline_sha):
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


# Mirrors client/hardtruth_hook.py SOURCE_CODE_EXTENSIONS: only edited source files outside
# the workspace pull their own project into Tier 2 (notes, memory files, etc. do not).
_TIER2_SOURCE_EXTENSIONS = {
    ".py", ".ts", ".js", ".tsx", ".jsx", ".rs", ".go", ".c", ".cpp",
    ".cc", ".cxx", ".h", ".hpp", ".java", ".rb", ".sh", ".bash",
    ".zsh", ".cs", ".php", ".swift", ".kt", ".scala", ".lua", ".zig",
    ".mjs", ".cjs"
}


def _path_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _is_within_workspace(f_abs: str, ws_norm: str) -> bool:
    """True if f_abs lies under ws_norm, comparing both lexical and symlink-resolved paths
    (macOS /tmp -> /private/tmp, /var -> /private/var)."""
    return (_path_within(os.path.abspath(f_abs), ws_norm)
            or _path_within(os.path.realpath(f_abs), os.path.realpath(ws_norm)))


def git_toplevel_for_file(fpath: str) -> Optional[str]:
    """
    Returns the top-level directory of the git repository containing fpath
    (`git rev-parse --show-toplevel`), or None if fpath is in no git repository.
    Symlinks are resolved first, so an edit made through a link lands in the repository
    that actually holds the file. Walks up to the nearest existing directory, so an
    edited-then-deleted file still resolves. Every GIT_* variable is stripped from the
    environment so the caller's environment cannot redirect the lookup.
    """
    d = os.path.dirname(os.path.realpath(fpath))
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "rev-parse", "--show-toplevel"],
            cwd=d, env=_git_env(), capture_output=True, text=True, timeout=2.0
        )
    except Exception:
        return None
    top = (proc.stdout or "").strip()
    if proc.returncode != 0 or not top or not os.path.isdir(top):
        return None
    return os.path.realpath(top)


def find_project_root_in_commit(repo_root: str, commit_sha: str, fpath: str) -> Optional[str]:
    """
    Like find_project_root_for_file, but decided from the tree of commit_sha (the session
    baseline) rather than the working tree: walks up from fpath's directory to repo_root and
    returns the first directory that, AT THE BASELINE, held a project manifest or a
    tests/ or test/ directory; repo_root itself is the last resort. A project root the agent
    created during the session (a fresh pytest.ini plus a trivial tests/ next to the edited
    file) therefore cannot be chosen as the verification target.
    """
    repo_root = os.path.realpath(repo_root)
    curr = os.path.dirname(os.path.realpath(fpath))
    if not _path_within(curr, repo_root):
        return None
    markers = {os.path.basename(m) for m in MANIFEST_FILES if "/" not in m} | {"tests", "test"}
    while True:
        rel = os.path.relpath(curr, repo_root)
        treeish = f"{commit_sha}:" if rel == "." else f"{commit_sha}:{rel.replace(os.sep, '/')}"
        try:
            proc = subprocess.run(
                ["git", "-c", f"safe.directory={repo_root}", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
                 "ls-tree", "--name-only", treeish],
                cwd=repo_root, env=_git_env(), capture_output=True, text=True, timeout=2.0
            )
            entries = set(proc.stdout.split("\n")) if proc.returncode == 0 else set()
        except Exception:
            entries = set()
        if entries & markers:
            return curr
        if curr == repo_root:
            return repo_root
        parent = os.path.dirname(curr)
        if parent == curr:
            return None
        curr = parent


def resolve_tier2_targets(
    workspace_dir: Optional[str],
    modified_files: Optional[List[str]] = None,
    conv_id: Optional[str] = None,
    baseline_lookup: Optional[Callable[[str], Optional[str]]] = None
) -> Dict[str, Any]:
    """
    Resolves EVERY project Tier 2 must verify for this session, deriving targets
    EXCLUSIVELY from files this session is known to have edited: the `modified_files` the
    caller passes (from the session's own transcript / git-diff-against-baseline) and the
    session's own ledger-recorded modified file paths.

    This intentionally does NOT fall back to scanning `git status` across the workspace or
    its subdirectories for "some project with uncommitted changes" -- that picks up
    unrelated sibling repos with pre-existing/leftover dirty state this session never
    touched (e.g. running `npm test` in an unrelated `goose` checkout, or `cargo test` in
    vibehard/apps/reachyd, just because they happened to have local changes sitting under
    the same parent directory as the actual workspace).

    Returns {"targets": [...], "unresolved": [...]}. Each target is
    {"root", "outside", "repo_root", "baseline"}; each unresolved entry is {"file", "reason"}.

    - An edited file inside the workspace resolves to its nearest project root, bounded by
      the workspace (unchanged behaviour; never reported as unresolved).
    - An edited SOURCE file outside the workspace (a session started in one directory that
      edits a repo elsewhere) belongs to its git repository (`git rev-parse --show-toplevel`).
      With `baseline_lookup` (repo_root -> trusted session baseline SHA or None), which the
      Stop hook always passes:
        * no trusted baseline for that repository -> unresolved (callers fail closed);
        * the project root is decided from the BASELINE tree (find_project_root_in_commit),
          so a root fabricated during the session cannot be chosen;
        * a root with no detectable test runner -> unresolved, never silently dropped, so a
          decoy repository cannot mask edits that cannot be verified.
      Without `baseline_lookup` the root is found in the working tree, bounded by the
      top-level, and roots without a runner are skipped.
    - An edited file outside the workspace in NO git repository contributes nothing; a
      session whose only edits are such files still fails closed (no target resolves and
      the workspace has no runner). Non-source files outside the workspace are ignored.
    - If nothing resolves (and nothing is unresolved), the workspace itself is the target
      when it has a runner and the session edited something.

    Callers must verify ALL targets, require every one to pass, and treat any unresolved
    entry as a failure.
    """
    result: Dict[str, Any] = {"targets": [], "unresolved": []}
    if not workspace_dir or not os.path.exists(workspace_dir):
        return result
    ws_norm = os.path.abspath(workspace_dir)

    candidate_files: List[str] = list(modified_files) if modified_files else []

    # Session ledger modified files (this conversation's own recorded edits)
    if conv_id:
        try:
            from ledger import _ledger
            premise = _ledger.get_premise(conv_id)
            mod_paths = premise.get("modified_file_paths", []) or premise.get("modified_files", [])
            candidate_files.extend(mod_paths)
        except Exception:
            pass

    targets: List[Dict[str, Any]] = result["targets"]
    unresolved: List[Dict[str, str]] = result["unresolved"]
    seen_roots: Set[str] = set()
    seen_unresolved: Set[str] = set()

    def _add(root: Optional[str], outside: bool, repo_root: Optional[str], baseline: Optional[str]) -> bool:
        if not root or detect_test_runner(root) is None:
            return False
        if root not in seen_roots:
            seen_roots.add(root)
            targets.append({"root": root, "outside": outside, "repo_root": repo_root, "baseline": baseline})
        return True

    def _unresolved(f: str, reason: str) -> None:
        if f not in seen_unresolved:
            seen_unresolved.add(f)
            unresolved.append({"file": f, "reason": reason})

    baselines: Dict[str, Optional[str]] = {}

    # 1. Each session-edited file -> its nearest project root with a runner.
    for f in candidate_files:
        if not f:
            continue
        f_abs = f if os.path.isabs(f) else os.path.join(ws_norm, f)
        if _is_within_workspace(f_abs, ws_norm):
            _add(find_project_root_for_file(f_abs, boundary=ws_norm), False, None, None)
            continue
        # Outside the workspace: bounded by the file's own git repository, never beyond.
        if os.path.splitext(f_abs)[1].lower() not in _TIER2_SOURCE_EXTENSIONS:
            continue
        toplevel = git_toplevel_for_file(f_abs)
        if not toplevel:
            continue
        f_real = os.path.realpath(f_abs)
        if not _path_within(f_real, toplevel):
            _unresolved(f_abs, f"resolves outside its git repository {toplevel}")
            continue
        if baseline_lookup is None:
            _add(find_project_root_for_file(f_real, boundary=toplevel), True, toplevel, None)
            continue
        if toplevel not in baselines:
            try:
                baselines[toplevel] = baseline_lookup(toplevel)
            except Exception:
                baselines[toplevel] = None
        baseline = baselines[toplevel]
        if not baseline:
            _unresolved(f_abs, f"no trusted session baseline was recorded for its repository {toplevel}")
            continue
        root = find_project_root_in_commit(toplevel, baseline, f_real)
        if not _add(root, True, toplevel, baseline):
            _unresolved(f_abs, f"no test runner detected for its project {root or toplevel}")

    # 2. workspace_dir itself has a runner. Safe to use even without a resolved per-file
    #    root because it's the session's own workspace (never an unrelated sibling
    #    project) -- but only when there is SOME evidence this session touched something.
    if not targets and not unresolved and candidate_files and detect_test_runner(ws_norm) is not None:
        targets.append({"root": ws_norm, "outside": False, "repo_root": None, "baseline": None})

    return result


def resolve_target_project_dirs(
    workspace_dir: Optional[str],
    modified_files: Optional[List[str]] = None,
    conv_id: Optional[str] = None
) -> List[str]:
    """Roots from resolve_tier2_targets without a baseline lookup (no unresolved reporting).
    The Stop hook uses resolve_tier2_targets directly."""
    return [t["root"] for t in resolve_tier2_targets(workspace_dir, modified_files, conv_id)["targets"]]


def resolve_target_project_dir(
    workspace_dir: Optional[str],
    modified_files: Optional[List[str]] = None,
    conv_id: Optional[str] = None
) -> Optional[str]:
    """
    Single-target form of resolve_target_project_dirs: the first resolved root, or None
    when no file this session actually edited can be attributed to a project with a
    detectable test runner. Callers that gate on the result must use
    resolve_target_project_dirs and verify every root.
    """
    roots = resolve_target_project_dirs(workspace_dir, modified_files, conv_id)
    return roots[0] if roots else None


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



def find_cargo_workspace_root(crate_dir: str) -> str:
    """
    Bug #6: a crate inside a cargo workspace can inherit shared settings from the
    workspace's own Cargo.toml (e.g. `edition.workspace = true`), which `cargo` cannot
    resolve without the workspace root also being present on disk -- mounting only the
    crate subdir makes cargo fail with "failed to find a workspace root" or similar.

    Walks upward from crate_dir looking for the nearest ancestor Cargo.toml. If that
    manifest declares a `[workspace]` table, its directory is the workspace root (kept
    walking up in case of a further-nested workspace is unusual, so this returns as soon
    as one is found). Stops and returns crate_dir unchanged if an ancestor Cargo.toml is
    found WITHOUT a `[workspace]` table (an unrelated project boundary), or if none is
    found before the user's home directory.
    """
    try:
        curr = os.path.abspath(crate_dir)
        stop_dir = os.path.expanduser("~")
        search = curr
        while True:
            parent = os.path.dirname(search)
            if not parent or parent == search or len(parent) < len(stop_dir):
                break
            cargo_toml = os.path.join(parent, "Cargo.toml")
            if os.path.isfile(cargo_toml):
                try:
                    with open(cargo_toml, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception:
                    content = ""
                if re.search(r"(?m)^\s*\[workspace\]", content):
                    return parent
                break  # ancestor manifest with no [workspace]: unrelated project boundary
            search = parent
        return curr
    except Exception:
        return crate_dir


GO_IMAGE_FALLBACK = "golang:alpine"
TEST_DB_ENV_MARKERS = ("TEST_DATABASE_URL",)
_SOURCE_FILE_RE = re.compile(r"\.(go|py|[cm]?[jt]sx?|rs|rb|java|kt|ex|exs)$")
_SCAN_SKIP_DIRS = {".git", "node_modules", "vendor", "target", "dist", "build", "__pycache__", ".venv", "venv"}


def go_container_image(workspace_path: str) -> str:
    """golang:<major.minor>-alpine matching go.mod's toolchain/go directive, so the
    sealed container compiles with the toolchain the module declares (GOTOOLCHAIN=local
    forbids an in-container toolchain download). Falls back to golang:alpine."""
    try:
        with open(os.path.join(workspace_path, "go.mod"), "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return GO_IMAGE_FALLBACK
    m = re.search(r"^toolchain\s+go(\d+)\.(\d+)", text, re.M) or re.search(r"^go\s+(\d+)\.(\d+)", text, re.M)
    return f"golang:{m.group(1)}.{m.group(2)}-alpine" if m else GO_IMAGE_FALLBACK


def host_go_modcache() -> Optional[str]:
    """The host's Go module cache (GOMODCACHE, `go env GOMODCACHE`, $GOPATH/pkg/mod,
    ~/go/pkg/mod), or None. Mounted read-only so a --network none container can build
    modules the host already has."""
    candidates: List[str] = []
    if os.environ.get("GOMODCACHE"):
        candidates.append(os.environ["GOMODCACHE"])
    go_bin = shutil.which("go")
    if go_bin:
        try:
            out = subprocess.run([go_bin, "env", "GOMODCACHE"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, timeout=10).stdout.strip()
            if out:
                candidates.append(out)
        except Exception:
            pass
    if os.environ.get("GOPATH"):
        candidates.append(os.path.join(os.environ["GOPATH"].split(os.pathsep)[0], "pkg", "mod"))
    candidates.append(os.path.expanduser("~/go/pkg/mod"))
    for c in candidates:
        if c and os.path.isdir(c):
            return os.path.abspath(c)
    return None


def prefetch_go_modules(workspace_path: str, timeout_sec: int = 30) -> None:
    """Best-effort `go mod download` on the host so the module cache holds everything
    go.mod/go.sum pin. Downloads checksummed module sources only -- it builds and runs no
    repository code. Disable with HARDTRUTH_TIER2_GO_PREFETCH=0."""
    if os.environ.get("HARDTRUTH_TIER2_GO_PREFETCH") == "0":
        return
    go_bin = shutil.which("go")
    if not go_bin or not os.path.isfile(os.path.join(workspace_path, "go.mod")):
        return
    env = dict(os.environ)
    env.pop("GOFLAGS", None)
    try:
        subprocess.run([go_bin, "mod", "download"], cwd=workspace_path, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_sec)
    except Exception:
        pass


def needs_test_database(workspace_path: str, max_files: int = 5000) -> bool:
    """True when the project's source reads TEST_DATABASE_URL -- in test files or in a
    shared test helper package (e.g. internal/testdb), so all source files are scanned.
    The container then gets a throwaway Postgres on a private, internet-less network
    instead of --network none (a false positive costs only an unused database).
    Disable with HARDTRUTH_TIER2_DB=0."""
    if os.environ.get("HARDTRUTH_TIER2_DB") == "0":
        return False
    seen = 0
    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = [d for d in dirs if d not in _SCAN_SKIP_DIRS]
        for name in files:
            if not _SOURCE_FILE_RE.search(name):
                continue
            seen += 1
            if seen > max_files:
                return False
            try:
                with open(os.path.join(root, name), "r", encoding="utf-8", errors="replace") as f:
                    body = f.read(512 * 1024)
            except OSError:
                continue
            if any(marker in body for marker in TEST_DB_ENV_MARKERS):
                return True
    return False


def ensure_docker_image(docker_bin: str, image: str, timeout_sec: int = 60) -> None:
    """Pull an image up front so pull time never counts against the test timeout."""
    try:
        present = subprocess.run([docker_bin, "image", "inspect", image], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=15).returncode == 0
        if not present:
            subprocess.run([docker_bin, "pull", "-q", image], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout_sec)
    except Exception:
        pass


def start_test_database(docker_bin: str, ready_timeout_sec: int = 30) -> Optional[Dict[str, str]]:
    """Starts a throwaway Postgres (tmpfs data, no published ports) on a fresh
    `docker network create --internal` network: reachable only from the test container,
    with no route to the internet. Returns {network, container, url} or None."""
    image = os.environ.get("HARDTRUTH_TIER2_DB_IMAGE", "postgres:16-alpine")
    ensure_docker_image(docker_bin, image)
    tag = f"hardtruth-t2-{os.getpid()}-{hashlib.sha256(os.urandom(16)).hexdigest()[:10]}"
    network, container = tag, f"{tag}-db"
    try:
        if subprocess.run([docker_bin, "network", "create", "--internal", network],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode != 0:
            return None
        run = subprocess.run([
            docker_bin, "run", "-d", "--rm", "--name", container,
            "--network", network, "--network-alias", "db",
            "--tmpfs", "/var/lib/postgresql/data:rw,size=1g",
            "-e", "POSTGRES_USER=hardtruth", "-e", "POSTGRES_PASSWORD=hardtruth", "-e", "POSTGRES_DB=hardtruth",
            image
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if run.returncode != 0:
            stop_test_database(docker_bin, {"network": network, "container": container})
            return None
        # TCP readiness: the image's init-time temporary server listens on the unix
        # socket only, so a TCP probe succeeds only once the real server is up.
        import time
        deadline = time.time() + ready_timeout_sec
        while time.time() < deadline:
            probe = subprocess.run([docker_bin, "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "hardtruth"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if probe.returncode == 0:
                return {"network": network, "container": container,
                        "url": "postgres://hardtruth:hardtruth@db:5432/hardtruth?sslmode=disable"}
            time.sleep(0.5)
        stop_test_database(docker_bin, {"network": network, "container": container})
        return None
    except Exception:
        stop_test_database(docker_bin, {"network": network, "container": container})
        return None


def stop_test_database(docker_bin: str, db: Optional[Dict[str, str]]) -> None:
    if not db:
        return
    for args in (["rm", "-f", db.get("container", "")], ["network", "rm", db.get("network", "")]):
        try:
            subprocess.run([docker_bin, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except Exception:
            pass


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
        image = go_container_image(workspace_path)
    is_go = "go test" in test_cmd

    actual_cmd = test_cmd
    if actual_cmd.startswith("pytest") and "-o cache_dir" not in actual_cmd:
        actual_cmd = f"{actual_cmd} -o cache_dir=/tmp/.pytest_cache -p no:cacheprovider"

    # Bug #6: for a cargo crate inside a larger workspace, mount the WORKSPACE ROOT
    # (read-only) and set the container's working directory to the crate subdir within
    # it, instead of mounting only the crate dir (which breaks workspace-inherited config).
    mount_root = os.path.abspath(workspace_path)
    container_workdir = "/workspace"
    if "cargo" in test_cmd:
        ws_root = os.path.abspath(find_cargo_workspace_root(workspace_path))
        if ws_root != mount_root:
            rel = os.path.relpath(mount_root, ws_root)
            mount_root = ws_root
            container_workdir = f"/workspace/{rel}"

    # Go: the container has no network, so it builds from the host's module cache
    # (read-only) with the proxy off. The build cache stays in tmpfs -- a writable cache
    # shared across runs could be poisoned by the code under test.
    go_args: List[str] = []
    if is_go:
        prefetch_go_modules(mount_root)
        modcache = host_go_modcache()
        if modcache:
            go_args += ["-v", f"{modcache}:/go/pkg/mod:ro", "-e", "GOMODCACHE=/go/pkg/mod"]
        go_args += ["-e", "GOPROXY=off", "-e", "GOSUMDB=off", "-e", "GOTOOLCHAIN=local",
                    "-e", "GOCACHE=/tmp/go-build", "-e", "CGO_ENABLED=0"]
        try:
            go_floor = int(os.environ.get("HARDTRUTH_TIER2_GO_TIMEOUT_SEC", "90"))
        except (TypeError, ValueError):
            go_floor = 90
        timeout_sec = max(int(timeout_sec or 0), go_floor)

    # Tests that read TEST_DATABASE_URL get a throwaway Postgres on a private internal
    # network (no internet route) instead of --network none; it is torn down after.
    test_db = None
    network = "none"
    db_args: List[str] = []
    if needs_test_database(mount_root):
        test_db = start_test_database(docker_bin)
        if test_db:
            network = test_db["network"]
            db_args = ["-e", f"TEST_DATABASE_URL={test_db['url']}"]

    ensure_docker_image(docker_bin, image)

    docker_args = [
        docker_bin, "run", "--rm",
        "--network", network,
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "--pids-limit", "256",
        "--memory", "3072m" if is_go else "1024m",
        "-v", f"{mount_root}:/workspace:ro",
        "-w", container_workdir,
        "--tmpfs", f"/tmp:rw,exec,nosuid,size={'2g' if is_go else '512m'}",
        "--tmpfs", "/root/.cache:rw,exec,nosuid,size=512m",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "PYTHONPYCACHEPREFIX=/tmp/pycache",
        "-e", "CARGO_TARGET_DIR=/tmp/target",
        "-e", "npm_config_cache=/tmp/npm-cache",
        "-e", "TMPDIR=/tmp",
        "-e", "HARDTRUTH_TIER2_SANDBOX=1",
        *go_args,
        *db_args,
        image,
        "sh", "-c", actual_cmd
    ]

    try:
        return _run_container(docker_args, image, actual_cmd, timeout_sec)
    finally:
        stop_test_database(docker_bin, test_db)


def _run_container(docker_args: List[str], image: str, actual_cmd: str, timeout_sec: int) -> Optional[Dict[str, Any]]:
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


# ---------------------------------------------------------------------------
# Approved policy: "automatic scoped host verification" for macOS-native projects.
#
# Host (non-container) Tier 2 execution is permitted WITHOUT an explicit
# HARDTRUTH_TIER2_ALLOW_SUBPROCESS=1 opt-in only when ALL of:
#   1. host is Darwin AND is_macos_native_project(target) is true (a Linux container
#      cannot build or run macOS-native code, so there is no safer isolated option).
#   2. the resolved target project dir is inside an allowed root (default ~/dev,
#      overridable via HARDTRUTH_TIER2_NATIVE_HOST_ROOTS, os.pathsep-separated).
#   3. the target was resolved from files THIS session actually edited -- never a
#      guessed/unrelated project. Callers must explicitly assert this (session_evidence);
#      it defaults to False, so any caller that doesn't plumb it through gets the
#      pre-existing, stricter behavior.
# Everything else is unchanged: HARDTRUTH_TIER2_ALLOW_SUBPROCESS still defaults to "0",
# and non-native projects still require a container or the explicit env var.
# ---------------------------------------------------------------------------

DEFAULT_NATIVE_HOST_ROOT = "~/dev"


def get_native_host_allowed_roots() -> List[str]:
    """
    Resolved (expanduser + realpath) allowed roots for scoped native-host execution.
    Default: ~/dev. Overridable via HARDTRUTH_TIER2_NATIVE_HOST_ROOTS (os.pathsep-separated).
    """
    raw = os.environ.get("HARDTRUTH_TIER2_NATIVE_HOST_ROOTS")
    candidates = [p for p in raw.split(os.pathsep) if p.strip()] if raw else [DEFAULT_NATIVE_HOST_ROOT]
    roots = []
    for c in candidates:
        try:
            roots.append(os.path.realpath(os.path.expanduser(c.strip())))
        except Exception:
            continue
    return roots


def is_within_native_host_allowed_roots(target_dir: str, allowed_roots: Optional[List[str]] = None) -> bool:
    """
    True iff target_dir is contained within one of the allowed native-host roots.
    Uses realpath on BOTH sides (defeats symlink escapes) and a real path-component
    prefix check -- '/Users/ai/dev-evil' must NOT match an allowed root of
    '/Users/ai/dev' just because it shares a string prefix.
    """
    if not target_dir:
        return False
    try:
        real_target = os.path.realpath(os.path.abspath(target_dir))
    except Exception:
        return False
    roots = allowed_roots if allowed_roots is not None else get_native_host_allowed_roots()
    for root in roots:
        if not root:
            continue
        if real_target == root or real_target.startswith(root + os.sep):
            return True
    return False


def scoped_native_host_permitted(
    workspace_path: str,
    is_mac_native: bool,
    is_host_darwin: bool,
    session_evidence: bool
) -> bool:
    """Combines all three gating conditions for the scoped-native-host policy (see module
    docstring above). Returns False unless every condition explicitly holds."""
    if not (is_host_darwin and is_mac_native):
        return False
    if not session_evidence:
        return False
    return is_within_native_host_allowed_roots(workspace_path)


def get_native_host_timeout_sec(default_timeout_sec: int) -> int:
    """Timeout bound for the scoped-native-host path (a cold cargo/native build can take
    much longer than the normal Tier 2 bound). Default 240s, overridable via
    HARDTRUTH_TIER2_NATIVE_TIMEOUT_SEC. Never shrinks a caller-specified larger timeout."""
    try:
        native_default = int(os.environ.get("HARDTRUTH_TIER2_NATIVE_TIMEOUT_SEC", "240"))
    except (TypeError, ValueError):
        native_default = 240
    return max(int(default_timeout_sec or 0), native_default)


def apply_native_host_env(env: Dict[str, str]) -> Dict[str, str]:
    """
    Mutates (and returns) env in place for the scoped-native-host path: reuses the user's
    own toolchain/target cache rather than a possibly-different `cargo`/`rustc` earlier on
    PATH (e.g. a Homebrew install vs rustup's ~/.cargo/bin), which would otherwise
    invalidate target/ fingerprints and force a full rebuild on every run. Prepends
    ~/.cargo/bin to PATH if it exists (and isn't already present) and defaults
    CARGO_HOME/RUSTUP_HOME to ~/.cargo and ~/.rustup only if not already set -- never
    overrides an explicit value, and touches nothing else in the allowlisted env.
    """
    cargo_bin = os.path.expanduser("~/.cargo/bin")
    if os.path.isdir(cargo_bin):
        existing_path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        if cargo_bin not in existing_path_parts:
            env["PATH"] = os.pathsep.join([cargo_bin] + existing_path_parts)
    env.setdefault("CARGO_HOME", os.path.expanduser("~/.cargo"))
    env.setdefault("RUSTUP_HOME", os.path.expanduser("~/.rustup"))
    return env


def _cargo_can_run_offline(workspace_path: str, cargo_home: str) -> bool:
    """True if a Cargo.lock is present (for workspace_path or its enclosing cargo
    workspace) and the local cargo registry cache exists, so `--offline` is safe to add
    rather than attempting a network fetch."""
    try:
        candidates = [workspace_path, find_cargo_workspace_root(workspace_path)]
        lock_present = any(os.path.isfile(os.path.join(c, "Cargo.lock")) for c in candidates)
        registry_present = os.path.isdir(os.path.join(cargo_home, "registry"))
        return bool(lock_present and registry_present)
    except Exception:
        return False


def run_independent_verification(
    workspace_path: str,
    test_cmd: Optional[str] = None,
    timeout_sec: int = 60,
    conv_id: Optional[str] = None,
    session_evidence: bool = False
) -> Dict[str, Any]:
    """
    Executes the canonical test suite outside the agent context.
    Prioritizes ephemeral read-only Docker container isolation,
    falling back to clean subprocess sandbox if Docker is unavailable and subprocess execution is authorized.

    session_evidence: must be explicitly set True by the caller only when workspace_path
    was resolved from files THIS session actually edited (e.g. resolve_target_project_dir
    returned it from modified_files / the session's own ledger) -- it gates the scoped
    native-host policy (see scoped_native_host_permitted) and defaults to False, so a
    caller that doesn't plumb it through gets the pre-existing, stricter behavior.
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
    # Bug #6: macOS-native projects (Cocoa/objc/CoreAudio, e.g. reachyd) must NOT auto-bypass
    # this gate just because the host happens to be Darwin -- that silently skipped the
    # operator's own HARDTRUTH_TIER2_ALLOW_SUBPROCESS decision. The default itself is
    # unchanged (still "0" unless CI/pytest); a macOS-native project simply cannot be
    # verified in a container at all, so it falls through to this SAME gate like any other
    # host-subprocess path, with a message that says exactly why -- UNLESS the approved
    # scoped-native-host policy applies (Darwin + native + inside an allowed root + this
    # session's own edit evidence), in which case it is permitted without the explicit env var.
    allow_subprocess = os.environ.get("HARDTRUTH_TIER2_ALLOW_SUBPROCESS", "1" if (os.environ.get("CI") or os.environ.get("PYTEST_CURRENT_TEST")) else "0")

    scoped_native_host = False
    if allow_subprocess != "1":
        scoped_native_host = scoped_native_host_permitted(
            workspace_path=workspace_path,
            is_mac_native=is_mac_native,
            is_host_darwin=is_host_darwin,
            session_evidence=session_evidence
        )

    if allow_subprocess != "1" and not scoped_native_host:
        if is_mac_native and is_host_darwin:
            return {
                "status": "unverified_no_isolation",
                "success": False,
                "exit_code": 1,
                "runner": "isolation_guard",
                "output": (
                    "🚨 TIER 2 HARD GATE REJECTED: macOS-native project detected (Cocoa/objc/CoreAudio "
                    "or an equivalent macOS-only dependency) -- container verification not possible "
                    "(a Linux container cannot build or run macOS-native code). Host verification "
                    "requires HARDTRUTH_TIER2_ALLOW_SUBPROCESS=1 (operator decision), UNLESS the target "
                    f"is inside an allowed native-host root ({', '.join(get_native_host_allowed_roots()) or DEFAULT_NATIVE_HOST_ROOT}, "
                    "configurable via HARDTRUTH_TIER2_NATIVE_HOST_ROOTS) and was resolved from files this "
                    "session actually edited. HardTruth never executes repository code unsandboxed on the "
                    "host without either explicit operator approval or that scoped exception."
                ),
                "isolation": "isolation_guard"
            }
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

    isolation_label = "scoped_native_host" if scoped_native_host else "clean_subprocess_sandbox"
    effective_timeout_sec = get_native_host_timeout_sec(timeout_sec) if scoped_native_host else timeout_sec

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

    if scoped_native_host:
        apply_native_host_env(clean_env)

    cmd_args = shlex.split(canonical_runner)
    if cmd_args and cmd_args[0] == "pytest" and "-o" not in cmd_args:
        cmd_args.extend(["-o", "cache_dir=/tmp/.pytest_cache", "-p", "no:cacheprovider"])

    if scoped_native_host and cmd_args and os.path.basename(cmd_args[0]) == "cargo" and "--offline" not in cmd_args:
        if _cargo_can_run_offline(workspace_path, clean_env.get("CARGO_HOME", os.path.expanduser("~/.cargo"))):
            insert_at = 2 if len(cmd_args) >= 2 else len(cmd_args)
            cmd_args.insert(insert_at, "--offline")

    resolved_bin = shutil.which(cmd_args[0], path=clean_env.get("PATH"))
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
        stdout, _ = proc.communicate(timeout=effective_timeout_sec)
        exit_code = proc.returncode
        output = (stdout or "").strip()

        sub_res = {
            "status": "verified" if exit_code == 0 else "failed",
            "success": exit_code == 0,
            "exit_code": exit_code,
            "runner": canonical_runner,
            "output": output[:4000],
            "isolation": isolation_label
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
            "output": f"Tier 2 external test execution timed out after {effective_timeout_sec}s.\n{(stdout or '')[:1000]}",
            "isolation": isolation_label
        }
    except Exception as e:
        return {
            "status": "error",
            "success": False,
            "exit_code": -1,
            "runner": canonical_runner,
            "output": f"Tier 2 execution encountered an internal error: {e}",
            "isolation": isolation_label
        }
