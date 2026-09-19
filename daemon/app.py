#!/usr/bin/env python3
"""
Harbor System One Neural Daemon (Port 8000)
Exposes sub-50ms Non-Autoregressive System One capabilities via free, local, open models:
- Zero-Shot Entity/Hook Extraction (GLiNER urchade/gliner_medium-v2.1)
- Non-Autoregressive Decisions & Policy Triage (DeBERTa-v3 NLI cross-encoder/nli-deberta-v3-small)
- Semantic Internal Link Mapping (SentenceTransformers all-MiniLM-L6-v2)
- Parallel Transcript Compaction (Salience Scoring)
- Turn-Level Dynamic Skill & Tool Routing
- On-Page SEO Quality & Information Gain Auditing
- Real-Time Batch Lead Intent Scoring
Runs 100% locally on Apple Silicon MPS/CPU with zero paid cloud API dependencies.
Interactive Swagger UI available at http://127.0.0.1:8000/docs
"""

from __future__ import annotations
import os
import sys
import time
import json
import re
import psutil
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# System One SEO & Semantic Link Mapping (Practice 10)
try:
    try:
        from .seo_audit import (
            SEOAuditRequest, SEOAuditResponse, InternalLinkRequest,
            InternalLinkResponse, audit_article_system_one, map_internal_links_distribb
        )
    except Exception:
        from seo_audit import (
            SEOAuditRequest, SEOAuditResponse, InternalLinkRequest,
            InternalLinkResponse, audit_article_system_one, map_internal_links_distribb
        )
except Exception:
    class SEOAuditRequest(BaseModel):
        content: str
        target_keyword: str
        existing_urls: List[str] = Field(default_factory=list)
    class SEOAuditResponse(BaseModel):
        score: float = 0.0
        status: str = "unavailable"
    class InternalLinkRequest(BaseModel):
        articles: List[Dict[str, Any]]
    class InternalLinkResponse(BaseModel):
        links: List[Dict[str, Any]] = Field(default_factory=list)
    def audit_article_system_one(req):
        return SEOAuditResponse()
    def map_internal_links_distribb(req):
        return InternalLinkResponse()

# System One Gojiberry Lead Scoring (Practice 6)
try:
    try:
        from .lead_scorer import (
            BatchLeadScoreRequest, BatchLeadScoreResponse, score_lead_batch
        )
    except Exception:
        from lead_scorer import (
            BatchLeadScoreRequest, BatchLeadScoreResponse, score_lead_batch
        )
except Exception:
    class BatchLeadScoreRequest(BaseModel):
        leads: List[Dict[str, Any]]
    class BatchLeadScoreResponse(BaseModel):
        scored_leads: List[Dict[str, Any]] = Field(default_factory=list)
    def score_lead_batch(req):
        return BatchLeadScoreResponse()

# ---------------------------------------------------------------------------
# FastAPI Initialization
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Harbor System One Neural Daemon",
    description="Sub-50ms sensory perception (GLiNER) & local non-autoregressive policy decisions (DeBERTa-v3 NLI)",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# ---------------------------------------------------------------------------
# Model Engines (GLiNER & DeBERTa-v3 NLI Lazy Warm Load on MPS / CPU)
# ---------------------------------------------------------------------------

_gliner_model = None
_nli_classifier = None

def get_gliner():
    global _gliner_model
    if _gliner_model is None:
        from gliner import GLiNER
        import torch
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _gliner_model = GLiNER.from_pretrained("urchade/gliner_medium-v2.1")
        _gliner_model.to(device)
        _gliner_model.eval()
    return _gliner_model

def get_nli_classifier():
    global _nli_classifier
    if _nli_classifier is None:
        import torch
        from transformers import pipeline
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _nli_classifier = pipeline(
            "zero-shot-classification",
            model="cross-encoder/nli-deberta-v3-small",
            device=device
        )
    return _nli_classifier

_nli_direct_model = None
_nli_direct_tok = None

def get_nli_direct():
    global _nli_direct_model, _nli_direct_tok
    if _nli_direct_model is None:
        import torch
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _nli_direct_tok = AutoTokenizer.from_pretrained("cross-encoder/nli-deberta-v3-small")
        _nli_direct_model = AutoModelForSequenceClassification.from_pretrained("cross-encoder/nli-deberta-v3-small").to(device)
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

class ExtractRequest(BaseModel):
    text: str
    labels: List[str] = Field(default_factory=lambda: ["software_tool", "cli_command", "code_block", "architecture_pattern"])
    threshold: float = 0.40

class EntitySpan(BaseModel):
    text: str
    label: str
    score: float
    start: int
    end: int

class ExtractResponse(BaseModel):
    spans: List[EntitySpan]
    latency_ms: float
    device: str

class DecideRequest(BaseModel):
    instruction: str
    criteria: str
    options: List[str]
    context: Optional[str] = None

class DecideResponse(BaseModel):
    decision: str
    confidence: float
    probabilities: Dict[str, float]
    source: str
    latency_ms: float

class Turn(BaseModel):
    role: str
    content: str

class SalienceRequest(BaseModel):
    turns: List[Turn]
    threshold: float = 0.40

class ScoredTurn(BaseModel):
    index: int
    role: str
    salience: float
    keep: bool

class SalienceResponse(BaseModel):
    scored_turns: List[ScoredTurn]
    original_turns: int
    retained_turns: int
    reduction_pct: float
    latency_ms: float

class RouteSkillsRequest(BaseModel):
    turn_prompt: str
    room: Optional[str] = None
    available_skills: List[Dict[str, Any]] = Field(default_factory=list)

class RouteSkillsResponse(BaseModel):
    selected_skills: List[str]
    selected_tools: List[str]
    prompt_token_savings_pct: float
    latency_ms: float

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health_check():
    process = psutil.Process(os.getpid())
    rss_mb = process.memory_info().rss / (1024 * 1024)
    return {
        "status": "healthy",
        "service": "harbor-system-one-daemon",
        "substrate": "free_local_open_source",
        "models": {
            "gliner_medium": "urchade/gliner_medium-v2.1",
            "nli_deberta": "cross-encoder/nli-deberta-v3-small",
            "sentence_embedder": "sentence-transformers/all-MiniLM-L6-v2"
        },
        "rss_memory_mb": round(rss_mb, 2),
        "gliner_loaded": _gliner_model is not None,
        "nli_loaded": _nli_classifier is not None,
        "docs_url": "http://127.0.0.1:8000/docs"
    }

@app.post("/v1/extract", response_model=ExtractResponse)
def extract_entities(req: ExtractRequest):
    t0 = time.perf_counter()
    if not req.text.strip():
        return ExtractResponse(spans=[], latency_ms=0.0, device="none")

    model = get_gliner()
    import torch
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    raw_entities = model.predict_entities(req.text, req.labels, threshold=req.threshold)
    
    spans = [
        EntitySpan(
            text=e["text"],
            label=e["label"],
            score=round(float(e["score"]), 4),
            start=e["start"],
            end=e["end"]
        )
        for e in raw_entities
    ]
    latency = (time.perf_counter() - t0) * 1000.0
    return ExtractResponse(spans=spans, latency_ms=round(latency, 2), device=device)

def extract_option_descriptions(options: List[str], criteria: str) -> Dict[str, str]:
    descriptions = {}
    for opt in options:
        pattern = rf'(?:^|[;\n.]\s*|\b){re.escape(opt)}\s*[:\-–]\s*([^;\n.]+)'
        match = re.search(pattern, criteria, re.IGNORECASE)
        if match:
            desc = match.group(1).strip()
            descriptions[opt] = f"{opt.replace('_', ' ').lower()}: {desc}"
        else:
            descriptions[opt] = opt.replace("_", " ").lower()
    return descriptions

@app.post("/v1/decide", response_model=DecideResponse)
def evaluate_decision(req: DecideRequest):
    t0 = time.perf_counter()

    # Primary Engine: Free Local DeBERTa-v3 NLI Classifier on Apple Silicon MPS/CPU
    try:
        classifier = get_nli_classifier()
        descriptions = extract_option_descriptions(req.options, req.criteria)
        label_to_opt = {v: k for k, v in descriptions.items()}
        candidate_labels = list(descriptions.values())

        premise = f"{req.instruction}\nContext: {req.context or 'None'}".strip()
        res = classifier(
            premise,
            candidate_labels=candidate_labels,
            hypothesis_template="This case corresponds to {}."
        )

        probs = {}
        for label, score in zip(res["labels"], res["scores"]):
            opt_key = label_to_opt.get(label, req.options[0])
            probs[opt_key] = round(float(score), 4)

        for opt in req.options:
            if opt not in probs:
                probs[opt] = 0.0

        best_opt = max(probs.items(), key=lambda x: x[1])
        latency = (time.perf_counter() - t0) * 1000.0
        return DecideResponse(
            decision=best_opt[0],
            confidence=best_opt[1],
            probabilities=probs,
            source="local_nli_deberta_v3_small",
            latency_ms=round(latency, 2)
        )
    except Exception as err:
        # Fail-closed local deterministic heuristic fallback
        inst_lower = req.instruction.lower()
        crit_lower = req.criteria.lower()
        ctx_lower = (req.context or "").lower()
        combined_query = f"{inst_lower} {ctx_lower}"
        scores = {}
        
        for opt in req.options:
            opt_raw = opt.lower()
            opt_phrase = opt_raw.replace("_", " ")
            opt_words = [w for w in opt_phrase.split() if len(w) > 3]
            score = 0.05

            if opt_raw in combined_query or opt_phrase in combined_query:
                score += 0.70

            for w in opt_words:
                if w in ctx_lower:
                    score += 0.25

            for marker in [opt, opt_raw, opt_phrase]:
                if marker in crit_lower:
                    idx = crit_lower.find(marker)
                    snippet = crit_lower[idx:idx + 120]
                    clues = [c.strip(" ()[],:;.\"") for c in snippet.split() if len(c) > 3]
                    clue_matches = sum(1 for c in clues if c in ctx_lower)
                    score += clue_matches * 0.20

            scores[opt] = max(0.05, score)

        total = sum(scores.values())
        probs = {k: round(v / total, 4) for k, v in scores.items()}
        best = max(probs.items(), key=lambda x: x[1])

        latency = (time.perf_counter() - t0) * 1000.0
        return DecideResponse(
            decision=best[0],
            confidence=best[1],
            probabilities=probs,
            source="local_calibrated_heuristic_fallback",
            latency_ms=round(latency, 2)
        )

@app.post("/v1/verify-claim", response_model=VerifyClaimResponse)
def verify_claim_endpoint(req: VerifyClaimRequest):
    """
    Direct Sequence Pair NLI Verification (Premise -> Hypothesis).
    Evaluates whether an agent's claim is mathematically ENTAILED, CONTRADICTED, or NEUTRAL
    relative to the ground-truth execution ledger in ~10ms on Apple Silicon MPS.
    """
    t0 = time.perf_counter()
    import torch
    tok, model = get_nli_direct()
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    
    inputs = tok(req.premise, req.hypothesis, return_tensors="pt", truncation=True, max_length=512).to(device)
    with torch.no_grad():
        logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=1)[0].cpu().tolist()

    id2label = model.config.id2label
    prob_map = {id2label[i]: round(float(probs[i]), 4) for i in range(len(probs))}
    
    best_label = max(prob_map.items(), key=lambda x: x[1])
    status_map = {
        "contradiction": "CONTRADICTION",
        "entailment": "ENTAILED",
        "neutral": "NEUTRAL"
    }
    status = status_map.get(best_label[0], best_label[0].upper())
    
    latency = (time.perf_counter() - t0) * 1000.0
    return VerifyClaimResponse(
        status=status,
        confidence=best_label[1],
        probabilities=prob_map,
        latency_ms=round(latency, 2)
    )

@app.post("/v1/seo/audit", response_model=SEOAuditResponse)
def evaluate_seo_audit(req: SEOAuditRequest):
    return audit_article_system_one(req)

@app.post("/v1/seo/link-map", response_model=InternalLinkResponse)
def evaluate_internal_link_map(req: InternalLinkRequest):
    return map_internal_links_distribb(req)

@app.post("/v1/leads/score", response_model=BatchLeadScoreResponse)
def evaluate_lead_scoring(req: BatchLeadScoreRequest):
    return score_lead_batch(req)

@app.post("/v1/salience", response_model=SalienceResponse)
def evaluate_salience(req: SalienceRequest):
    t0 = time.perf_counter()
    scored = []
    
    # Critical preserved signatures: paths, tokens, error stacks, git hashes
    keep_indicators = ["/", "\\", "error", "fail", "pass", "git", "def ", "class ", "import ", "token", "http"]
    
    for idx, turn in enumerate(req.turns):
        content_lower = turn.content.lower()
        score = 0.30
        
        # System turns or user prompts retain high baseline salience
        if turn.role in ["system", "user"]:
            score += 0.50
        
        # Tool outputs with critical artifacts retain salience
        if any(ind in content_lower for ind in keep_indicators):
            score += 0.40
            
        score = min(score, 1.0)
        keep = score >= req.threshold
        scored.append(ScoredTurn(index=idx, role=turn.role, salience=round(score, 2), keep=keep))

    retained = sum(1 for t in scored if t.keep)
    original = len(req.turns)
    reduction = round((1.0 - (retained / max(original, 1))) * 100.0, 1)
    latency = (time.perf_counter() - t0) * 1000.0
    
    return SalienceResponse(
        scored_turns=scored,
        original_turns=original,
        retained_turns=retained,
        reduction_pct=reduction,
        latency_ms=round(latency, 2)
    )

@app.post("/v1/route-skills", response_model=RouteSkillsResponse)
def route_skills(req: RouteSkillsRequest):
    t0 = time.perf_counter()
    prompt_lower = req.turn_prompt.lower()
    selected_skills = []
    selected_tools = []
    
    # Match skills from available catalog based on intent
    for skill in req.available_skills:
        name = skill.get("name", "")
        desc = skill.get("description", "").lower()
        if name.lower() in prompt_lower or any(word in prompt_lower for word in desc.split()[:5]):
            selected_skills.append(name)
            
    # Default minimum skill route if prompt mentions git, video, or testing
    if "git" in prompt_lower and "using-git-worktrees" not in selected_skills:
        selected_skills.append("using-git-worktrees")
    if "test" in prompt_lower and "verification-before-completion" not in selected_skills:
        selected_skills.append("verification-before-completion")
        
    # Deduplicate and cap to top 2 skills per turn to protect context
    selected_skills = list(dict.fromkeys(selected_skills))[:2]
    
    # Route tools based on intent
    if any(k in prompt_lower for k in ["run", "exec", "test", "bash", "command"]):
        selected_tools.append("run_command")
    if any(k in prompt_lower for k in ["edit", "write", "replace", "code", "file"]):
        selected_tools.extend(["view_file", "replace_file_content", "write_to_file"])
    if any(k in prompt_lower for k in ["search", "grep", "find"]):
        selected_tools.extend(["grep_search", "find_by_name"])
        
    selected_tools = list(set(selected_tools))
    
    # Dynamically compute catalog prompt token reduction percentage
    total_catalog_skills = max(1, len(req.available_skills))
    unpruned_tokens = (total_catalog_skills * 250) + 2000  # full catalog + tool schemas baseline
    pruned_tokens = max(100, (len(selected_skills) * 250) + (len(selected_tools) * 250))
    savings = round(max(0.0, min(95.0, (1.0 - (pruned_tokens / max(1, unpruned_tokens))) * 100.0)), 1)
    latency = (time.perf_counter() - t0) * 1000.0
    
    return RouteSkillsResponse(
        selected_skills=selected_skills,
        selected_tools=selected_tools,
        prompt_token_savings_pct=savings,
        latency_ms=round(latency, 2)
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
