"""
HardTruth Zero-Dependency Client Library
Can be embedded in any Python agent framework (LangGraph, CrewAI, AutoGen, or custom loops).
Automatically connects to HardTruth Daemon (http://127.0.0.1:8000) for sub-15ms NLI,
and gracefully falls back to deterministic AST & exit-code checking if daemon is offline.
"""

import os
import sys
import json
import ast
import re
import urllib.request
import urllib.error

class HardTruthClient:
    def __init__(self, daemon_url: str = "http://127.0.0.1:8000", ledger_path: str = None):
        self.daemon_url = daemon_url.rstrip("/")
        self.ledger_path = ledger_path or os.path.expanduser("~/.gemini/antigravity-cli/ledger.jsonl")

    def is_daemon_online(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.daemon_url}/v1/health", headers={"User-Agent": "HardTruth-Client"})
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
        """Deterministic AST check: rejects empty stubs (pass, NotImplementedError)."""
        violations = []
        if not os.path.exists(filepath) or not filepath.endswith(".py"):
            return violations
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=filepath)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = node.body
                    is_stub = False
                    if len(body) == 1:
                        if isinstance(body[0], ast.Pass):
                            is_stub = True
                        elif isinstance(body[0], ast.Raise) and isinstance(body[0].exc, ast.Name) and body[0].exc.id == "NotImplementedError":
                            is_stub = True
                    elif len(body) == 2 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                        if isinstance(body[1], ast.Pass):
                            is_stub = True
                    if is_stub:
                        violations.append(f"Function '{node.name}' in {os.path.basename(filepath)} is an empty stub.")
        except Exception:
            pass
        return violations
