"""
HardTruth Zero-Dependency Client Library
Can be embedded in any Python agent framework (LangGraph, CrewAI, AutoGen, or custom loops).
Automatically connects to HardTruth Daemon (http://127.0.0.1:8000) for sub-15ms NLI,
and gracefully falls back to deterministic AST & exit-code checking if daemon is offline.
"""

import os
import sys
import json
import urllib.request
import urllib.error

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if _CURRENT_DIR not in sys.path:
    sys.path.insert(0, _CURRENT_DIR)
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

try:
    from ast_checker import check_ast_stubs
except ImportError:
    try:
        from client.ast_checker import check_ast_stubs
    except ImportError:
        from hardtruth.ast_checker import check_ast_stubs

CONTRADICTION_THRESHOLD = 0.70

class HardTruthClient:
    def __init__(self, daemon_url: str = "http://127.0.0.1:8000", ledger_path: str = None):
        self.daemon_url = daemon_url.rstrip("/")
        self.ledger_path = ledger_path or os.environ.get(
            "HARDTRUTH_LEDGER_PATH",
            os.path.expanduser("~/.gemini/antigravity-cli/ledger.jsonl")
        )

    def is_daemon_online(self) -> bool:
        """Pings daemon /health endpoint to check availability."""
        try:
            req = urllib.request.Request(f"{self.daemon_url}/health", headers={"User-Agent": "HardTruth-Client"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def verify_claim(self, premise: str, hypothesis: str) -> dict:
        """Evaluates (Premise, Hypothesis) via DeBERTa-v3 Cross-Encoder in ~10ms."""
        payload = json.dumps({"premise": premise, "hypothesis": hypothesis}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.daemon_url}/v1/verify-claim",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def check_ast_stubs(self, filepath: str) -> list:
        """Deterministic AST check: rejects empty stubs (pass, NotImplementedError, ..., return True/None)."""
        return check_ast_stubs(filepath)
