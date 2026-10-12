use serde::{Deserialize, Serialize};

/// Telemetry record capturing local quantization performance & drift
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DriftRecord {
    pub model_id: String,
    pub quantization: String,
    pub logit_margin: f32,
    pub gbnf_grammar: Option<String>,
    pub output_matched_grammar: bool,
    pub drift_detected: bool,
    pub timestamp: i64,
    pub digest: String,
}

/// Local Quantization Drift & Provenance Engine
pub struct QuantizationTelemetry {
    records: Vec<DriftRecord>,
    min_safe_logit_margin: f32,
}

impl QuantizationTelemetry {
    pub fn new() -> Self {
        Self {
            records: Vec::new(),
            min_safe_logit_margin: 0.18, // Margins below 0.18 indicate quantization degradation / perplexity instability
        }
    }

    /// Record decoding telemetry and evaluate quantization drift
    pub fn record(
        &mut self,
        model_id: impl Into<String>,
        quantization: impl Into<String>,
        logit_margin: f32,
        gbnf_grammar: Option<String>,
        output_text: &str,
    ) -> DriftRecord {
        let model_id = model_id.into();
        let quantization = quantization.into();
        let timestamp = chrono::Utc::now().timestamp();

        // 1. Verify GBNF grammar conformity if grammar was supplied
        let output_matched_grammar = match &gbnf_grammar {
            Some(grammar) => Self::validate_grammar(output_text, grammar),
            None => true,
        };

        // 2. Drift detection: low logit margin OR grammar mismatch
        let drift_detected = (logit_margin < self.min_safe_logit_margin) || !output_matched_grammar;

        // 3. Blake3 tamper digest
        let hash_input = format!("{model_id}:{quantization}:{logit_margin}:{output_matched_grammar}:{timestamp}");
        let digest = blake3::hash(hash_input.as_bytes()).to_hex().to_string();

        let record = DriftRecord {
            model_id,
            quantization,
            logit_margin,
            gbnf_grammar,
            output_matched_grammar,
            drift_detected,
            timestamp,
            digest,
        };

        self.records.push(record.clone());
        record
    }

    /// Simple deterministic GBNF grammar validator for common constraints (e.g. JSON, commands)
    pub fn validate_grammar(text: &str, grammar: &str) -> bool {
        let trimmed = text.trim();
        if grammar.contains("root ::= object") || grammar.contains("json") {
            // Must be valid JSON
            serde_json::from_str::<serde_json::Value>(trimmed).is_ok()
        } else if grammar.contains("boolean") {
            trimmed == "true" || trimmed == "false"
        } else if grammar.contains("integer") {
            trimmed.parse::<i64>().is_ok()
        } else {
            // Default check: non-empty and well-formed
            !trimmed.is_empty()
        }
    }

    /// Get all drift telemetry records
    pub fn records(&self) -> &[DriftRecord] {
        &self.records
    }

    /// Count of drift occurrences detected
    pub fn drift_count(&self) -> usize {
        self.records.iter().filter(|r| r.drift_detected).count()
    }
}

impl Default for QuantizationTelemetry {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_quantization_drift_detection() {
        let mut telem = QuantizationTelemetry::new();

        // High margin + valid json grammar -> no drift
        let r1 = telem.record(
            "qwen2.5-coder:7b-q4_k_m",
            "Q4_K_M",
            0.45,
            Some("root ::= object".to_string()),
            r#"{"status": "ok"}"#,
        );
        assert!(!r1.drift_detected);
        assert!(r1.output_matched_grammar);

        // Low margin (< 0.18) -> drift detected
        let r2 = telem.record(
            "qwen2.5-coder:7b-q2_k",
            "Q2_K",
            0.12,
            None,
            "cargo test",
        );
        assert!(r2.drift_detected);

        // Broken JSON grammar -> drift detected
        let r3 = telem.record(
            "qwen2.5-coder:7b-q4_k_m",
            "Q4_K_M",
            0.50,
            Some("root ::= object".to_string()),
            "this is not json",
        );
        assert!(r3.drift_detected);
        assert!(!r3.output_matched_grammar);
    }
}
