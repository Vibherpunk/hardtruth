"""
HardTruth Zero-Dependency Client Library
Connects to HardTruth Daemon (http://127.0.0.1:8000) for sub-15ms NLI and tamper-evident ledger,
and gracefully falls back to deterministic AST & local DaemonLedger if daemon is offline.
"""

import os
import sys
import json
import urllib.request
import urllib.error
from typing import Optional, Dict, Any, List

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

try:
    from daemon.ledger import DaemonLedger
except ImportError:
    try:
        from ledger import DaemonLedger
    except ImportError:
        DaemonLedger = None

CONTRADICTION_THRESHOLD = 0.70


class HardTruthClient:
    def __init__(self, daemon_url: str = os.environ.get("SYSTEM_ONE_URL", "http://127.0.0.1:49281"), ledger_path: str = None):
        self.daemon_url = daemon_url.rstrip("/")
        self.ledger_path = ledger_path or os.environ.get(
            "HARDTRUTH_DAEMON_LEDGER",
            os.path.expanduser("~/.hardtruth/daemon_ledger.jsonl")
        )
        self._local_ledger = DaemonLedger(ledger_path=self.ledger_path) if DaemonLedger else None

    def is_daemon_online(self) -> bool:
        """Pings daemon /health endpoint to check availability."""
        try:
            req = urllib.request.Request(
                f"{self.daemon_url}/health",
                headers={"User-Agent": "HardTruth-Client"}
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def record_ledger_entry(
        self,
        conversation_id: str,
        step_idx: int,
        tool: str,
        target: str = "",
        observed_exit_code: Optional[int] = None,
        harness_status: Optional[str] = None,
        error: Optional[str] = None,
        stdout_tail: Optional[str] = None,
        diff_stat: Optional[str] = None
    ) -> dict:
        """Records an execution event to daemon or local ledger fallback."""
        payload = {
            "conversationId": conversation_id,
            "stepIdx": step_idx,
            "tool": tool,
            "target": target,
            "observed_exit_code": observed_exit_code,
            "harness_status": harness_status,
            "error": error,
            "stdout_tail": stdout_tail,
            "diff_stat": diff_stat
        }
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.daemon_url}/v1/ledger/record",
                data=data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if self._local_ledger:
                return self._local_ledger.record_entry(
                    conversation_id=conversation_id,
                    step_idx=step_idx,
                    tool=tool,
                    target=target,
                    observed_exit_code=observed_exit_code,
                    harness_status=harness_status,
                    error=error,
                    stdout_tail=stdout_tail,
                    diff_stat=diff_stat
                )
            return {"status": "error", "error": "daemon_unreachable"}

    def get_ledger_premise(self, conversation_id: str) -> dict:
        """Fetches verified premise and physical state for conversation."""
        try:
            url = f"{self.daemon_url}/v1/ledger/premise?conversationId={urllib.parse.quote(conversation_id)}"
            req = urllib.request.Request(url, headers={"User-Agent": "HardTruth-Client"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if self._local_ledger:
                return self._local_ledger.get_premise(conversation_id)
            return {
                "tampered": False,
                "conversationId": conversation_id,
                "premise": "Daemon unreachable and no local ledger found.",
                "source_files_modified": 0,
                "doc_files_modified": 0,
                "modified_files": [],
                "verification_commands_executed": 0,
                "unresolved_failures": []
            }

    def verify_claim(self, premise: str, hypothesis: str, threshold: float = CONTRADICTION_THRESHOLD) -> Optional[dict]:
        """Evaluates (Premise, Hypothesis) via DeBERTa-v3 Cross-Encoder in ~10ms."""
        payload = json.dumps({
            "premise": premise,
            "hypothesis": hypothesis,
            "threshold": threshold
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.daemon_url}/v1/verify-claim",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def check_ast_stubs(self, filepath: str) -> list:
        """Deterministic AST check: rejects empty stubs (pass, NotImplementedError, ..., return True/None)."""
        return check_ast_stubs(filepath)
