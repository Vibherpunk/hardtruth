#!/usr/bin/env python3
"""
HardTruth Verification Daemon (Port 8000)
Exposes sub-15ms Non-Autoregressive NLI Verification via DeBERTa-v3 Cross-Encoder:
- cross-encoder/nli-deberta-v3-small
Evaluates (premise, hypothesis) token pairs directly on Apple Silicon MPS or CPU.
"""

from __future__ import annotations
import os
import sys
import time
import psutil
import threading
from typing import Dict, Any
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(
    title="HardTruth Verification Daemon",
    description="Sub-15ms Natural Language Inference verification for agent execution claims",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

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

class VerifyClaimResponse(BaseModel):
    status: str  # ENTAILED | CONTRADICTION | NEUTRAL
    confidence: float
    probabilities: Dict[str, float]
    latency_ms: float

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
@app.get("/v1/health")
def health_check():
    process = psutil.Process(os.getpid())
    rss_mb = process.memory_info().rss / (1024 * 1024)
    return {
        "status": "healthy",
        "service": "hardtruth-daemon",
        "substrate": "free_local_open_source",
        "models": {
            "nli_deberta": "cross-encoder/nli-deberta-v3-small"
        },
        "rss_memory_mb": round(rss_mb, 2),
        "nli_loaded": _nli_direct_model is not None,
        "nli_direct_loaded": _nli_direct_model is not None,
        "docs_url": "http://127.0.0.1:8000/docs"
    }

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

    if contradiction >= 0.70:
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
