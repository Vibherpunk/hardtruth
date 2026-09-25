use std::time::Duration;
use serde::{Deserialize, Serialize};

/// Modality of a statement or claim
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub enum Modality {
    /// Declarative claim of current or past ground truth
    AssertedFact,
    /// Future intention or directive
    Plan,
    /// Speculative or conditional hypothesis
    Hypothesis,
}

/// Modality classification verdict
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ModalityClassification {
    pub modality: Modality,
    pub confidence: f32,
    pub provider: String,
    pub latency_ms: f32,
}

/// NLI contradiction check verdict
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ContradictionResult {
    pub is_contradiction: bool,
    pub contradiction_score: f32,
    pub confidence: f32,
    pub provider: String,
}

/// Modality and Contradiction Guard
pub struct ModalityGuard {
    gliner_port: u16,
    modernbert_port: u16,
    client: reqwest::Client,
}

impl ModalityGuard {
    pub fn new() -> Self {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_millis(45))
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());

        Self {
            gliner_port: 8150,
            modernbert_port: 49281,
            client,
        }
    }

    /// Sub-50ms modality classification using loopback GLiNER or local deterministic fallback
    pub async fn classify(&self, text: &str) -> ModalityClassification {
        let start = std::time::Instant::now();

        // 1. Attempt local loopback GLiNER on port 8150
        let url = format!("http://127.0.0.1:{}/classify", self.gliner_port);
        let request_body = serde_json::json!({
            "text": text,
            "labels": ["ASSERTED_FACT", "PLAN", "HYPOTHESIS"]
        });

        if let Ok(resp) = self.client.post(&url).json(&request_body).send().await {
            if resp.status().is_success() {
                if let Ok(json) = resp.json::<serde_json::Value>().await {
                    let label = json.get("label").and_then(|v| v.as_str()).unwrap_or("");
                    let conf = json.get("score").and_then(|v| v.as_f64()).unwrap_or(0.95) as f32;
                    let latency = start.elapsed().as_secs_f32() * 1000.0;

                    let modality = match label {
                        "PLAN" => Modality::Plan,
                        "HYPOTHESIS" => Modality::Hypothesis,
                        _ => Modality::AssertedFact,
                    };

                    return ModalityClassification {
                        modality,
                        confidence: conf,
                        provider: "gliner:8150".to_string(),
                        latency_ms: latency,
                    };
                }
            }
        }

        // 2. High-speed deterministic fallback
        let modality = Self::heuristic_modality(text);
        let latency = start.elapsed().as_secs_f32() * 1000.0;

        ModalityClassification {
            modality,
            confidence: 0.92,
            provider: "local-heuristic".to_string(),
            latency_ms: latency,
        }
    }

    /// Fast rule-based linguistic heuristic
    fn heuristic_modality(text: &str) -> Modality {
        let lower = text.trim().to_lowercase();

        // Plan markers
        let plan_markers = [
            "i will", "we will", "let's", "lets", "plan to", "todo", "shall",
            "next step", "going to", "should create", "need to", "must add",
            "we are going to", "intend to", "step 1", "step 2"
        ];
        for marker in plan_markers {
            if lower.contains(marker) {
                return Modality::Plan;
            }
        }

        // Hypothesis markers
        let hypothesis_markers = [
            "might be", "could be", "perhaps", "maybe", "assuming",
            "if we", "possibly", "hypothetically", "suppose"
        ];
        for marker in hypothesis_markers {
            if lower.contains(marker) {
                return Modality::Hypothesis;
            }
        }

        // Default to AssertedFact for assertions
        Modality::AssertedFact
    }

    /// Check NLI contradiction between an unbacked assertion and verified premises
    pub async fn check_contradiction(&self, assertion: &str, premise: &str) -> ContradictionResult {
        let url = format!("http://127.0.0.1:{}/nli", self.modernbert_port);
        let request_body = serde_json::json!({
            "premise": premise,
            "hypothesis": assertion
        });

        if let Ok(resp) = self.client.post(&url).json(&request_body).send().await {
            if resp.status().is_success() {
                if let Ok(json) = resp.json::<serde_json::Value>().await {
                    let score = json.get("contradiction_score").and_then(|v| v.as_f64()).unwrap_or(0.0) as f32;
                    let is_contra = score > 0.65;
                    return ContradictionResult {
                        is_contradiction: is_contra,
                        contradiction_score: score,
                        confidence: 0.95,
                        provider: "modernbert:49281".to_string(),
                    };
                }
            }
        }

        // Fallback heuristic: check direct negation of terms
        let lower_prem = premise.to_lowercase();
        let lower_assert = assertion.to_lowercase();

        let is_contra = ((lower_prem.contains("fail") || lower_prem.contains("exit 1") || lower_prem.contains("exited 1") || lower_prem.contains("error"))
            && (lower_assert.contains("pass") || lower_assert.contains("succeed") || lower_assert.contains("clean")))
            || ((lower_prem.contains("pass") || lower_prem.contains("succeed") || lower_prem.contains("clean"))
            && (lower_assert.contains("fail") || lower_assert.contains("exit 1") || lower_assert.contains("error")));

        ContradictionResult {
            is_contradiction: is_contra,
            contradiction_score: if is_contra { 0.98 } else { 0.05 },
            confidence: 0.90,
            provider: "local-heuristic".to_string(),
        }
    }
}

impl Default for ModalityGuard {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_modality_classification() {
        let guard = ModalityGuard::new();

        let r1 = guard.classify("all tests pass and cargo check exited 0").await;
        assert_eq!(r1.modality, Modality::AssertedFact);

        let r2 = guard.classify("We will next implement the Merkle tree and add tests").await;
        assert_eq!(r2.modality, Modality::Plan);

        let r3 = guard.classify("Perhaps the cache issue is caused by race conditions").await;
        assert_eq!(r3.modality, Modality::Hypothesis);
    }

    #[tokio::test]
    async fn test_contradiction_detection() {
        let guard = ModalityGuard::new();

        let premise = "Test runner exited 1 with 2 compilation failures";
        let assertion = "All tests pass successfully";
        let result = guard.check_contradiction(assertion, premise).await;
        assert!(result.is_contradiction);
    }
}
