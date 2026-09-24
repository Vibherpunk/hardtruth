# HardTruth 2.0: ModernBERT Neural NLI Engine Architecture & Upgrade Plan

**Document Version:** 2.0.0-PROPOSAL  
**Author:** Antigravity AI Systems Architect  
**Target Subsystem:** HardTruth Verification Daemon (`hardtruth-daemon`) & Local/VPS Deployments  
**Reference Issue:** DeBERTa-v3 512-Token Context Ceiling & Latency Bottleneck  

---

## 1. Executive Summary

HardTruth protects host agents (Antigravity, Goose, Claude Code) by intercepting turn completion and verifying agent assertions against an immutable, hash-chained execution ledger using Natural Language Inference (NLI).

Currently, HardTruth relies on `cross-encoder/nli-deberta-v3-small` (44M params, released in 2021). While mathematically effective on short text, DeBERTa-v3 exhibits two severe structural limitations:
1. **The 512-Token Context Ceiling:** The cross-encoder concatenates `Premise (Execution Ledger) + Hypothesis (Agent Claim)` into a single sequence capped at 512 tokens. Execution outputs (compiler errors, `pytest` failure logs, git diffs) routinely exceed 1,000 to 4,000 tokens. Truncating at 512 tokens creates a dangerous blind spot where test failures past token 512 are invisible to the NLI model.
2. **Legacy Attention Mechanics:** Lacks FlashAttention-2, Rotary Position Embeddings (RoPE), and unpadding, taking ~68–100ms per verification call.

This proposal upgrades HardTruth's neural inference engine to **ModernBERT** (`tasksource/ModernBERT-base-nli`), expanding the context window to **2,048 tokens** (with architectural support up to 8,192), speeding up inference by 3x (~15–25ms), and implementing dynamic label mapping to ensure backward-compatible safety across any NLI model family.

---

## 2. Model Selection & Architecture Comparison

| Dimension | Current: `nli-deberta-v3-small` | Proposed: `tasksource/ModernBERT-base-nli` | Architectural Impact |
| :--- | :--- | :--- | :--- |
| **Parameters** | 44 Million | 149 Million | 3.3x capacity for complex semantic reasoning |
| **Max Context** | **512 tokens** | **2,048 tokens** (Native up to 8,192) | **4x–16x longer context**; eliminates log truncation |
| **Attention Engine** | Standard $O(N^2)$ quadratic | FlashAttention-2 + Unpadding + RoPE | 3x faster forward pass, zero compute on padding |
| **Inference Latency** | ~68–100 ms (CPU/MPS) | **~15–25 ms** (CPU/MPS) | Sub-25ms turn clearance on client hooks |
| **Memory Footprint** | ~180 MB RAM | ~320 MB RAM | Well within OrbStack & VPS 1.5GB daemon budget |
| **Label Mapping** | `[contradiction, entailment, neutral]` | `[entailment, neutral, contradiction]` | **Requires dynamic label index resolution** |

---

## 3. Critical Failure Modes & Adversarial Edge Cases Addressed

### Edge Case 1: Label Index Permutation (Catastrophic Inversion Risk)
- **The Hazard:** In `cross-encoder/nli-deberta-v3-small`, the classification head outputs logits in the order:
  `0: contradiction`, `1: entailment`, `2: neutral`.
  In `tasksource/ModernBERT-base-nli`, the classification head outputs:
  `0: entailment`, `1: neutral`, `2: contradiction`.
- **The Failure Mode:** Positional unpacking (`probs[0], probs[1], probs[2]`) in `/v1/verify-claim`, `/v1/verify-claims`, and `/v1/verify-claims-batch` would silently treat entailment as contradiction and HALT truthful agents.
- **The Hardening:**
  1. Implement strict dynamic index resolution inspecting `model.config.label2id` at initialization, raising `RuntimeError` if keys are missing or not a permutation of `[0, 1, 2]`.
  2. Implement a startup canary self-test running obvious contradiction & entailment pairs through the model; if the canary fails, the daemon aborts immediately rather than serving inverted verdicts.
  3. Wire dynamic label indices (`idx["contradiction"]`, etc.) into all three live endpoints.

### Edge Case 2: Transformers Dependency Version Incompatibility
- **The Hazard:** ModernBERT was introduced in HuggingFace `transformers` v4.48.0. The current Docker image pins `transformers==4.45.1`. Attempting to load ModernBERT in 4.45.1 throws `KeyError: 'modernbert'` / `ValueError`.
- **The Hardening:** Pin `transformers==4.49.0` in `requirements.txt` and update Dockerfile preloading.

### Edge Case 3: Upstream Ledger Truncation Bottleneck
- **The Hazard:** `ledger.py` previously truncated `premise_str` to `[:1500]` characters (≈300–400 tokens) and sliced `stdout_tail` to `[:120]`. Widening the model's context window to 2,048 tokens does nothing if the upstream premise compiler cuts the context at 400 tokens.
- **The Hardening:** In `daemon/ledger.py`, raise `premise_str` truncation to `[:8000]` characters and expand `stdout_tail` slices to `[:500]` characters for failures and `[:300]` characters for recent executions. In `daemon/app.py`, tokenize with `max_length=_nli_max_length` dynamically derived from `model.config.max_position_embeddings`.

---

## 4. Implementation Specification

### 4.1 `requirements.txt`
```text
fastapi==0.115.0
uvicorn[standard]==0.31.0
pydantic>=2.0.0
psutil==6.0.0
torch>=2.2.0
transformers==4.49.0
httpx>=0.24.0
```

### 4.2 `docker/Dockerfile`
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential git \
    && rm -rf /var/lib/apt/lists/*

# Copy and install python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r /app/requirements.txt

# Preload ModernBERT NLI into Docker image cache for instant container cold starts
ENV HARDTRUTH_NLI_MODEL="tasksource/ModernBERT-base-nli"
RUN python3 -c "from transformers import AutoTokenizer, AutoModelForSequenceClassification; import os; m = os.environ.get('HARDTRUTH_NLI_MODEL', 'tasksource/ModernBERT-base-nli'); AutoTokenizer.from_pretrained(m); AutoModelForSequenceClassification.from_pretrained(m)"

RUN pip install --no-cache-dir pytest httpx

COPY daemon/ /app/

ENV PORT=8000
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 5. Verification & Testing Strategy

1. **Unit & Canary Tests (`tests/test_modernbert_nli.py`):**
   - Dynamic label resolution for ModernBERT, DeBERTa, and malformed configs.
   - Startup canary validation.
   - Sequence length scaling up to 2,048 tokens.
   - Exact softmax probabilities on canonical test assertions.
2. **Regression Tests:** Run entire existing test suite (`python3 -m pytest tests/ -v`).
3. **Live Latency & Memory Profiling:** Verify container RSS is within 1.5GB budget and CPU inference completes cleanly.

---

## 6. Deployment & Restart Protocol

### Do systems need to restart?
- **Daemon Containers (Local OrbStack + VPS Docker):** **YES.**
  Because Python loads PyTorch weights into memory during process startup, upgrading the model and dependencies requires rebuilding the Docker image and recreating the container (`docker compose up -d --build`).
- **Client Agent Hooks (AGY, Goose, Claude Code):** **NO.**
  The `/v1/verify-claim` and `/v1/verify-claims` REST contracts remain 100% byte-for-byte compatible. The hooks communicate over HTTP and require zero code changes or restarts.

