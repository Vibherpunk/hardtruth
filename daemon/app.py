#!/usr/bin/env python3
"""
HardTruth Verification Daemon (Port 8000)
Exposes:
1. Sub-15ms Non-Autoregressive NLI Verification via DeBERTa-v3 Cross-Encoder:
   - cross-encoder/nli-deberta-v3-small
2. Cryptographically tamper-evident, HMAC-SHA256 hash-chained Daemon Ledger:
   - POST /v1/ledger/record
   - GET /v1/ledger/premise
"""

from __future__ import annotations
import os
import sys
import time
import psutil
import threading
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if _CURRENT_DIR not in sys.path:
    sys.path.insert(0, _CURRENT_DIR)

try:
    from ledger import DaemonLedger
except ImportError:
    try:
        from daemon.ledger import DaemonLedger
    except ImportError:
        from hardtruth.ledger import DaemonLedger

app = FastAPI(
    title="HardTruth Verification Daemon",
    description="Sub-15ms Natural Language Inference verification & Tamper-Evident Ledger",
    version="1.1.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# ---------------------------------------------------------------------------
# Ledger Engine
# ---------------------------------------------------------------------------

_ledger = DaemonLedger()

# ---------------------------------------------------------------------------
# Model Engine (DeBERTa-v3 NLI Direct Token Pair Evaluation)
# ---------------------------------------------------------------------------

_nli_direct_lock = threading.Lock()
_nli_direct_model = None
_nli_direct_tok = None

def get_nli_direct():
    global _nli_direct_model, _nli_direct_tok
    if _nli_direct_model is None:
        with _nli_direct_lock:
            if _nli_direct_model is None:
                import torch
                from transformers import AutoTokenizer, AutoModelForSequenceClassification
                device = "mps" if torch.backends.mps.is_available() else "cpu"
                _nli_direct_tok = AutoTokenizer.from_pretrained("cross-encoder/nli-deberta-v3-small")
                _nli_direct_model = AutoModelForSequenceClassification.from_pretrained(
                    "cross-encoder/nli-deberta-v3-small"
                ).to(device)
                _nli_direct_model.eval()
    return _nli_direct_tok, _nli_direct_model

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class VerifyClaimRequest(BaseModel):
    premise: str
    hypothesis: str
    threshold: Optional[float] = 0.70

class VerifyClaimResponse(BaseModel):
    status: str  # ENTAILED | CONTRADICTION | NEUTRAL
    confidence: float
    probabilities: Dict[str, float]
    latency_ms: float

class RecordLedgerRequest(BaseModel):
    conversationId: str
    stepIdx: int = 0
    tool: str
    target: str = ""
    observed_exit_code: Optional[int] = None
    harness_status: Optional[str] = None
    error: Optional[str] = None
    stdout_tail: Optional[str] = None
    diff_stat: Optional[str] = None
    timestamp: Optional[float] = None

class UnresolvedFailureItem(BaseModel):
    command: str
    observed_exit_code: Optional[int] = None
    error: Optional[str] = None
    stdout_tail: Optional[str] = None
    stepIdx: Optional[int] = None

class GetPremiseResponse(BaseModel):
    tampered: bool
    conversationId: str
    premise: str
    source_files_modified: int
    doc_files_modified: int
    modified_files: List[str]
    verification_commands_executed: int
    unresolved_failures: List[UnresolvedFailureItem]
    records_count: int
    error: Optional[str] = None
    broken_at_index: Optional[int] = None
    detail: Optional[str] = None

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
@app.get("/v1/health")
def health_check():
    process = psutil.Process(os.getpid())
    rss_mb = process.memory_info().rss / (1024 * 1024)
    valid_chain, records_count, chain_msg = _ledger.verify_chain()
    return {
        "status": "healthy",
        "service": "hardtruth-daemon",
        "version": "1.1.0",
        "substrate": "free_local_open_source",
        "models": {
            "nli_deberta": "cross-encoder/nli-deberta-v3-small"
        },
        "rss_memory_mb": round(rss_mb, 2),
        "nli_loaded": _nli_direct_model is not None,
        "ledger": {
            "path": _ledger.ledger_path,
            "chain_valid": valid_chain,
            "records_count": records_count,
            "chain_msg": chain_msg
        },
        "docs_url": "http://127.0.0.1:8000/docs"
    }

@app.post("/v1/ledger/record")
def record_ledger_entry(req: RecordLedgerRequest):
    """
    Appends an execution event to the HMAC-SHA256 hash-chained ledger.
    """
    res = _ledger.record_entry(
        conversation_id=req.conversationId,
        step_idx=req.stepIdx,
        tool=req.tool,
        target=req.target,
        observed_exit_code=req.observed_exit_code,
        harness_status=req.harness_status,
        error=req.error,
        stdout_tail=req.stdout_tail,
        diff_stat=req.diff_stat,
        timestamp=req.timestamp
    )
    return res

@app.get("/v1/ledger/premise")
def get_ledger_premise(conversationId: str = Query(..., description="Conversation ID to query")):
    """
    Validates HMAC hash-chain integrity, extracts execution evidence, and compiles failure-biased premise.
    """
    premise_data = _ledger.get_premise(conversationId)
    if premise_data.get("tampered"):
        return JSONResponse(status_code=400, content=premise_data)
    return premise_data

@app.post("/v1/verify-claim", response_model=VerifyClaimResponse)
def verify_claim(req: VerifyClaimRequest):
    """
    Evaluates premise vs hypothesis using DeBERTa-v3 cross-encoder directly.
    Computes exact softmax distribution across [contradiction, entailment, neutral].
    """
    t0 = time.perf_counter()
    import torch

    tok, model = get_nli_direct()
    device = next(model.parameters()).device

    inputs = tok(
        req.premise,
        req.hypothesis,
        truncation=True,
        max_length=512,
        return_tensors="pt"
    ).to(device)

    with torch.no_grad():
        logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=-1)[0].tolist()

    # cross-encoder/nli-deberta-v3-small label order: 0: contradiction, 1: entailment, 2: neutral
    contradiction, entailment, neutral = probs[0], probs[1], probs[2]
    prob_dict = {
        "contradiction": round(contradiction, 4),
        "entailment": round(entailment, 4),
        "neutral": round(neutral, 4)
    }

    threshold = req.threshold or 0.70
    if contradiction >= threshold:
        verdict = "CONTRADICTION"
        conf = contradiction
    elif entailment >= 0.50:
        verdict = "ENTAILED"
        conf = entailment
    else:
        verdict = "NEUTRAL"
        conf = neutral

    latency = (time.perf_counter() - t0) * 1000.0

    return VerifyClaimResponse(
        status=verdict,
        confidence=round(conf, 4),
        probabilities=prob_dict,
        latency_ms=round(latency, 2)
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
