#!/usr/bin/env python3
"""
HardTruth Verification Daemon (Port 8000)
Exposes:
1. Sub-15ms Non-Autoregressive NLI Verification via DeBERTa-v3 Cross-Encoder:
   - cross-encoder/nli-deberta-v3-small
2. Cryptographically tamper-evident, HMAC-SHA256 hash-chained Daemon Ledger:
   - POST /v1/ledger/record
   - GET /v1/ledger/premise
3. Tier 2 External Deterministic Verification Gate (Outer Loop):
   - POST /v1/verify/handoff
"""

from __future__ import annotations
import os
import sys
import time
import hmac
import logging
import threading
try:
    import psutil
except ImportError:
    psutil = None
from typing import Dict, Any, Optional, List

logger = logging.getLogger("hardtruth.daemon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
from pydantic import BaseModel, Field
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if _CURRENT_DIR not in sys.path:
    sys.path.insert(0, _CURRENT_DIR)

try:
    from ledger import DaemonLedger, validate_api_token
except ImportError:
    try:
        from daemon.ledger import DaemonLedger, validate_api_token
    except ImportError:
        from hardtruth.ledger import DaemonLedger, validate_api_token

try:
    from tier2_runner import run_independent_verification
except ImportError:
    try:
        from daemon.tier2_runner import run_independent_verification
    except ImportError:
        from hardtruth.tier2_runner import run_independent_verification

app = FastAPI(
    title="HardTruth Verification Daemon",
    description="Sub-15ms Natural Language Inference verification, Tamper-Evident Ledger & Tier 2 Gate",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

@app.middleware("http")
async def limit_payload_size(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > 2_000_000:  # 2MB payload limit
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Payload too large: maximum request body size is 2MB to prevent OOM"}
                )
        except ValueError:
            pass
    return await call_next(request)

# ---------------------------------------------------------------------------
# Ledger Engine
# ---------------------------------------------------------------------------

_ledger = DaemonLedger()


# ---------------------------------------------------------------------------
# API Write Authentication (Round 7 Finding A)
# ---------------------------------------------------------------------------
def require_daemon_auth(authorization: Optional[str] = Header(None)):
    """
    Requires the shared HardTruth API token on all state-changing endpoints.
    Prevents any local process from forging ledger/baseline records or from
    triggering external execution / NLI inference (Round 7 Finding A).
    """
    token = None
    if authorization:
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        else:
            token = authorization.strip()
    if not validate_api_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized: missing or invalid HardTruth API token")

# ---------------------------------------------------------------------------
# Model Engine (ModernBERT / Universal NLI Cross-Encoder)
# ---------------------------------------------------------------------------

_nli_direct_lock = threading.Lock()
_nli_direct_model = None
_nli_direct_tok = None
_nli_label_indices = {"contradiction": 0, "entailment": 1, "neutral": 2}
_nli_max_length = 2048
_nli_model_name = os.environ.get("HARDTRUTH_NLI_MODEL", "tasksource/ModernBERT-base-nli")

def _resolve_label_indices(config) -> Dict[str, int]:
    raw = getattr(config, "label2id", None) or {}
    l2i = {str(k).strip().lower(): int(v) for k, v in raw.items()}
    try:
        c_idx = l2i["contradiction"]
        e_idx = l2i["entailment"]
        n_idx = l2i["neutral"]
    except KeyError as exc:
        raise RuntimeError(
            f"NLI model '{_nli_model_name}' has incomplete or missing label2id (got {raw!r}). "
            f"Refusing to guess a label ordering for safety-gating NLI engine. Missing key: {exc}"
        )
    resolved = {"contradiction": c_idx, "entailment": e_idx, "neutral": n_idx}
    if sorted(resolved.values()) != [0, 1, 2]:
        raise RuntimeError(f"label2id does not resolve to a clean permutation of [0, 1, 2]: {resolved}")
    return resolved

def _run_startup_canary(tok, model, device, indices: Dict[str, int], max_len: int):
    """
    Executes a deterministic sanity canary on startup.
    Asserts that contradiction pairs yield high contradiction probability,
    and entailment pairs yield high entailment probability.
    Aborts daemon immediately if logits/indices are inverted.
    """
    import torch
    test_premise = "Pytest exit code 1; 2 tests failed."
    test_contra = "All tests passed successfully."
    test_entail = "Some tests failed with exit code 1."

    inputs = tok(
        [test_premise, test_premise],
        [test_contra, test_entail],
        padding=True,
        truncation=True,
        max_length=max_len,
        return_tensors="pt"
    ).to(device)

    with torch.no_grad():
        logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=-1).tolist()

    c_prob = probs[0][indices["contradiction"]]
    e_prob = probs[1][indices["entailment"]]

    logger.info(f"NLI startup canary: contradiction_p={c_prob:.4f}, entailment_p={e_prob:.4f}")
    if c_prob < 0.50:
        raise RuntimeError(
            f"NLI startup canary FAILED: contradiction prob {c_prob:.4f} < 0.50 for obvious contradiction! "
            f"Aborting daemon to prevent safety gate inversion."
        )
    if e_prob < 0.50:
        raise RuntimeError(
            f"NLI startup canary FAILED: entailment prob {e_prob:.4f} < 0.50 for obvious entailment! "
            f"Aborting daemon to prevent safety gate inversion."
        )

def get_nli_direct():
    global _nli_direct_model, _nli_direct_tok, _nli_label_indices, _nli_max_length
    if _nli_direct_model is None:
        with _nli_direct_lock:
            if _nli_direct_model is None:
                import torch
                from transformers import AutoTokenizer, AutoModelForSequenceClassification
                device = "mps" if torch.backends.mps.is_available() else "cpu"
                _nli_direct_tok = AutoTokenizer.from_pretrained(_nli_model_name)
                _nli_direct_model = AutoModelForSequenceClassification.from_pretrained(
                    _nli_model_name
                ).to(device)
                _nli_direct_model.eval()

                # Strictly resolve label indices
                _nli_label_indices = _resolve_label_indices(_nli_direct_model.config)

                # Dynamically resolve max context length
                max_pos = getattr(_nli_direct_model.config, "max_position_embeddings", 2048)
                _nli_max_length = max(1, min(int(max_pos), 8192))

                # Run startup canary self-test
                _run_startup_canary(_nli_direct_tok, _nli_direct_model, device, _nli_label_indices, _nli_max_length)

                logger.info(
                    f"Initialized NLI model '{_nli_model_name}' on {device}. "
                    f"Max context: {_nli_max_length}, Label indices: {_nli_label_indices}"
                )
    return _nli_direct_tok, _nli_direct_model


def _evaluate_nli_pairs(pairs: List[Tuple[str, str]], threshold: float = 0.70):
    """
    Evaluates a batch of (premise, hypothesis) pairs using the loaded NLI cross-encoder.
    Uses dynamic label mapping and dynamic context length.
    Returns list of dicts: {"hypothesis": ..., "status": ..., "confidence": ..., "probabilities": ...}
    """
    import torch
    if not pairs:
        return []

    tok, model = get_nli_direct()
    device = next(model.parameters()).device

    inputs = tok(
        pairs,
        padding=True,
        truncation=True,
        max_length=_nli_max_length,
        return_tensors="pt"
    ).to(device)

    with torch.no_grad():
        logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=-1).tolist()

    idx = _nli_label_indices
    c_i = idx["contradiction"]
    e_i = idx["entailment"]
    n_i = idx["neutral"]

    results = []
    for (premise, hyp), p in zip(pairs, probs):
        contra_p = p[c_i]
        entail_p = p[e_i]
        neutral_p = p[n_i]

        prob_dict = {
            "contradiction": round(contra_p, 4),
            "entailment": round(entail_p, 4),
            "neutral": round(neutral_p, 4)
        }

        if contra_p >= threshold:
            verdict = "CONTRADICTION"
            conf = contra_p
        elif entail_p >= 0.50:
            verdict = "ENTAILED"
            conf = entail_p
        else:
            verdict = "NEUTRAL"
            conf = neutral_p

        results.append({
            "hypothesis": hyp,
            "status": verdict,
            "confidence": round(conf, 4),
            "probabilities": prob_dict
        })
    return results


def _warmup_nli_in_background():
    """Eager-load the NLI model at startup so /health reports nli_loaded quickly."""
    try:
        get_nli_direct()
    except Exception as exc:
        logger.error(f"Failed to warmup NLI model: {exc}")


threading.Thread(target=_warmup_nli_in_background, daemon=True, name="nli-warmup").start()

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class VerifyClaimRequest(BaseModel):
    premise: str = Field(..., max_length=65536)
    hypothesis: str = Field(..., max_length=4096)
    threshold: Optional[float] = 0.70

class VerifyClaimResponse(BaseModel):
    status: str  # ENTAILED | CONTRADICTION | NEUTRAL
    confidence: float
    probabilities: Dict[str, float]
    latency_ms: float

class ClaimPremisePair(BaseModel):
    claim: str = Field(..., max_length=1000)
    premise: str = Field(..., max_length=65536)

class VerifyClaimsBatchRequest(BaseModel):
    pairs: List[ClaimPremisePair] = Field(..., max_length=128)
    threshold: Optional[float] = 0.70

class VerifyClaimsBatchResponse(BaseModel):
    results: List[SingleClaimResult]
    latency_ms: float

class VerifyClaimsRequest(BaseModel):
    premise: str = Field(..., max_length=65536)
    hypotheses: List[str] = Field(..., max_length=64)
    threshold: Optional[float] = 0.70

class SingleClaimResult(BaseModel):
    hypothesis: str
    status: str  # ENTAILED | CONTRADICTION | NEUTRAL
    confidence: float
    probabilities: Dict[str, float]

class VerifyClaimsResponse(BaseModel):
    results: List[SingleClaimResult]
    latency_ms: float

class GateStartRequest(BaseModel):
    conversationId: str = Field(..., max_length=128)
    transcriptPath: Optional[str] = Field(None, max_length=1024)
    workspace_dir: Optional[str] = Field(None, max_length=1024)

class GateStartResponse(BaseModel):
    conversationId: str
    status: str
    gate_id: str
    opened_at: float

class GateVerdictRequest(BaseModel):
    conversationId: str = Field(..., max_length=128)
    verdict: str = Field(..., max_length=32)
    reason: Optional[str] = Field(None, max_length=16384)
    latency_ms: Optional[float] = None

class GateVerdictResponse(BaseModel):
    conversationId: str
    verdict: str
    recorded: bool
    ledger_index: Optional[int] = None

class RecordLedgerRequest(BaseModel):
    conversationId: str = Field(..., max_length=128)
    stepIdx: int = 0
    tool: str = Field(..., max_length=64)
    target: str = Field("", max_length=4096)
    observed_exit_code: Optional[int] = None
    harness_status: Optional[str] = Field(None, max_length=64)
    error: Optional[str] = Field(None, max_length=16384)
    stdout_tail: Optional[str] = Field(None, max_length=16384)
    diff_stat: Optional[str] = Field(None, max_length=4096)
    timestamp: Optional[float] = None
    cwd: Optional[str] = Field(None, max_length=1024)

class UnresolvedFailureItem(BaseModel):
    command: str = Field(..., max_length=4096)
    observed_exit_code: Optional[int] = None
    error: Optional[str] = Field(None, max_length=16384)
    stdout_tail: Optional[str] = Field(None, max_length=16384)
    stepIdx: Optional[int] = None
    cwd: Optional[str] = Field(None, max_length=1024)

class GetPremiseResponse(BaseModel):
    tampered: bool
    conversationId: str
    premise: str
    source_files_modified: int
    doc_files_modified: int
    modified_files: List[str]
    modified_file_paths: Optional[List[str]] = []
    verification_commands_executed: int
    test_commands_executed: Optional[int] = 0
    last_source_mod_step: Optional[int] = -1
    last_test_step: Optional[int] = -1
    created_at: Optional[float] = 0.0
    unresolved_failures: List[UnresolvedFailureItem]
    records_count: int
    error: Optional[str] = None
    broken_at_index: Optional[int] = None
    detail: Optional[str] = None

class SessionStartRequest(BaseModel):
    conversationId: str = Field(..., max_length=128)
    workspace_path: Optional[str] = Field(None, max_length=1024)

class SessionStartResponse(BaseModel):
    conversationId: str
    session_secret: Optional[str] = None
    status: str

class SessionBaselineRequest(BaseModel):
    conversationId: str = Field(..., max_length=128)
    workspace_path: str = Field(..., max_length=1024)
    commit_sha: Optional[str] = Field(None, max_length=128)

class HandoffVerifyRequest(BaseModel):
    workspace_path: str = Field(..., max_length=1024)
    conversationId: Optional[str] = Field(None, max_length=128)
    test_command: Optional[str] = Field(None, max_length=2048)
    timeout_sec: Optional[int] = Field(60, ge=1, le=300)

class HandoffVerifyResponse(BaseModel):
    status: str
    success: bool
    exit_code: int
    runner: Optional[str]
    output: str

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
@app.get("/v1/health")
def health_check():
    rss_mb = 0.0
    if psutil:
        try:
            process = psutil.Process(os.getpid())
            rss_mb = process.memory_info().rss / (1024 * 1024)
        except Exception:
            pass
    valid_chain, records_count, chain_msg = _ledger.verify_chain()
    return {
        "status": "healthy",
        "service": "hardtruth-daemon",
        "version": "2.0.0",
        "substrate": "free_local_open_source",
        "models": {
            "nli_model": _nli_model_name,
            "max_length": _nli_max_length,
            "label_indices": _nli_label_indices
        },
        "rss_memory_mb": round(rss_mb, 2),
        "nli_loaded": _nli_direct_model is not None,
        "ledger": {
            "path": _ledger.ledger_path,
            "chain_valid": valid_chain,
            "records_count": records_count,
            "chain_msg": chain_msg
        },
        "tier2_hard_gate": "enabled",
        "docs_url": "http://127.0.0.1:8000/docs"
    }

@app.post("/v1/session/start", response_model=SessionStartResponse, dependencies=[Depends(require_daemon_auth)])
def session_start(req: SessionStartRequest):
    """
    Open Item #2: Register session and mint ephemeral 256-bit session secret.
    Returns session_secret ONLY on creation. Subsequent calls return status: already_active
    without secret to prevent credential leakage.
    """
    status, secret = _ledger.start_session(req.conversationId, req.workspace_path)
    return SessionStartResponse(
        conversationId=req.conversationId,
        session_secret=secret,
        status=status
    )

@app.post("/v1/ledger/record", dependencies=[Depends(require_daemon_auth)])
def record_ledger_entry(
    req: RecordLedgerRequest,
    x_session_secret: Optional[str] = Header(None, alias="X-Session-Secret")
):
    """
    Appends an execution event to the HMAC-SHA256 hash-chained ledger.
    Validates X-Session-Secret if session is registered.
    Enforces monotonic step sequencing.
    """
    session = _ledger.get_session(req.conversationId)
    if session:
        if not x_session_secret:
            raise HTTPException(status_code=403, detail="Forbidden: missing X-Session-Secret for active session")
        if not hmac.compare_digest(str(x_session_secret), session["secret"]):
            raise HTTPException(status_code=403, detail="Forbidden: invalid X-Session-Secret")

    try:
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
            timestamp=req.timestamp,
            cwd=req.cwd
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

@app.get("/v1/ledger/premise", dependencies=[Depends(require_daemon_auth)])
def get_ledger_premise(
    conversationId: str = Query(..., description="Conversation ID to query"),
    x_session_secret: Optional[str] = Header(None, alias="X-Session-Secret")
):
    """
    Validates HMAC hash-chain integrity, extracts execution evidence, and compiles failure-biased premise.
    Validates X-Session-Secret if session is registered.
    """
    session = _ledger.get_session(conversationId)
    if session:
        if not x_session_secret:
            raise HTTPException(status_code=403, detail="Forbidden: missing X-Session-Secret for active session")
        if not hmac.compare_digest(str(x_session_secret), session["secret"]):
            raise HTTPException(status_code=403, detail="Forbidden: invalid X-Session-Secret")

    premise_data = _ledger.get_premise(conversationId)
    if premise_data.get("tampered"):
        return JSONResponse(status_code=400, content=premise_data)
    return premise_data

@app.post("/v1/session/baseline", dependencies=[Depends(require_daemon_auth)])
def set_session_baseline(req: SessionBaselineRequest):
    """
    Registers the session baseline commit SHA for (conversationId, workspace_path).
    Immutable in daemon memory and physical HMAC-chained ledger.
    """
    baseline = _ledger.get_session_baseline(req.conversationId, req.workspace_path)
    if baseline:
        return {"baseline_sha": baseline, "status": "existing"}
    if req.commit_sha:
        saved = _ledger.set_session_baseline(req.conversationId, req.workspace_path, req.commit_sha)
        return {"baseline_sha": saved, "status": "created"}
    return {"baseline_sha": None, "status": "not_found"}

@app.get("/v1/session/baseline", dependencies=[Depends(require_daemon_auth)])
def get_session_baseline(
    conversationId: str = Query(..., description="Conversation ID"),
    workspace_path: str = Query(..., description="Workspace path")
):
    """
    Retrieves the immutable session baseline commit SHA for (conversationId, workspace_path).
    """
    baseline = _ledger.get_session_baseline(conversationId, workspace_path)
    return {"baseline_sha": baseline}

@app.post("/v1/verify/handoff", response_model=HandoffVerifyResponse, dependencies=[Depends(require_daemon_auth)])
def verify_handoff(req: HandoffVerifyRequest):
    """
    Tier 2 External Deterministic Verification Gate.
    Executes the canonical test suite in a clean, out-of-band runner outside the agent's shell.
    """
    result = run_independent_verification(
        workspace_path=req.workspace_path,
        test_cmd=req.test_command,
        timeout_sec=req.timeout_sec or 60,
        conv_id=req.conversationId
    )
    if not result.get("success"):
        return JSONResponse(status_code=406, content=result)
    return result

@app.post("/v1/verify-claim", response_model=VerifyClaimResponse, dependencies=[Depends(require_daemon_auth)])
def verify_claim(req: VerifyClaimRequest):
    """
    Evaluates premise vs hypothesis using ModernBERT cross-encoder directly.
    Computes exact softmax distribution across [contradiction, entailment, neutral].
    """
    t0 = time.perf_counter()
    eval_res = _evaluate_nli_pairs([(req.premise, req.hypothesis)], threshold=req.threshold or 0.70)
    latency = (time.perf_counter() - t0) * 1000.0
    item = eval_res[0]
    return VerifyClaimResponse(
        status=item["status"],
        confidence=item["confidence"],
        probabilities=item["probabilities"],
        latency_ms=round(latency, 2)
    )

@app.post("/v1/verify-claims", response_model=VerifyClaimsResponse, dependencies=[Depends(require_daemon_auth)])
def verify_claims(req: VerifyClaimsRequest):
    """
    Batched NLI verification across multiple claims against a common premise in a single forward pass.
    Evaluates premise vs list of hypotheses using ModernBERT cross-encoder directly.
    """
    t0 = time.perf_counter()
    if not req.hypotheses:
        return VerifyClaimsResponse(results=[], latency_ms=0.0)

    pairs = [(req.premise, hyp) for hyp in req.hypotheses]
    eval_res = _evaluate_nli_pairs(pairs, threshold=req.threshold or 0.70)
    latency = (time.perf_counter() - t0) * 1000.0

    results = [
        SingleClaimResult(
            hypothesis=item["hypothesis"],
            status=item["status"],
            confidence=item["confidence"],
            probabilities=item["probabilities"]
        )
        for item in eval_res
    ]
    return VerifyClaimsResponse(
        results=results,
        latency_ms=round(latency, 2)
    )

@app.post("/v1/verify-claims-batch", response_model=VerifyClaimsBatchResponse, dependencies=[Depends(require_daemon_auth)])
def verify_claims_batch(req: VerifyClaimsBatchRequest):
    """
    Batched NLI verification across multiple (premise, hypothesis) pairs in a single forward pass (B5).
    """
    t0 = time.perf_counter()
    if not req.pairs:
        return VerifyClaimsBatchResponse(results=[], latency_ms=0.0)

    pairs = [(pair.premise, pair.claim) for pair in req.pairs]
    eval_res = _evaluate_nli_pairs(pairs, threshold=req.threshold or 0.70)
    latency = (time.perf_counter() - t0) * 1000.0

    results = [
        SingleClaimResult(
            hypothesis=item["hypothesis"],
            status=item["status"],
            confidence=item["confidence"],
            probabilities=item["probabilities"]
        )
        for item in eval_res
    ]
    return VerifyClaimsBatchResponse(
        results=results,
        latency_ms=round(latency, 2)
    )

@app.post("/v1/gate/start", response_model=GateStartResponse, dependencies=[Depends(require_daemon_auth)])
def gate_start(req: GateStartRequest):
    """
    B2: Opens a gate supervision session. Daemon tracks gate lifetime and sweeps expired gates.
    """
    res = _ledger.start_gate(req.conversationId, req.transcriptPath, req.workspace_dir)
    return GateStartResponse(**res)

@app.post("/v1/gate/verdict", response_model=GateVerdictResponse, dependencies=[Depends(require_daemon_auth)])
def gate_verdict(req: GateVerdictRequest):
    """
    B2/B3: Records terminal gate verdict (ALLOW, HALT, TIMEOUT_HALT) and appends to ledger.
    """
    res = _ledger.record_gate_verdict(
        conversation_id=req.conversationId,
        verdict=req.verdict,
        reason=req.reason,
        latency_ms=req.latency_ms
    )
    return GateVerdictResponse(**res)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
