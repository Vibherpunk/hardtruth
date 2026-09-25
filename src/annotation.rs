use regex::{Captures, Regex};
use serde::{Deserialize, Serialize};
use crate::merkle::{ClaimStatus, ReceiptLedger};

/// Summary of an in-place context rewrite
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AnnotationResult {
    pub annotated_text: String,
    pub modifications_count: usize,
    pub unverified_count: usize,
    pub stale_count: usize,
    pub verified_count: usize,
}

/// In-place context rewriter implementing the Anti-Hallucination Annotation Ladder
pub struct ContextAnnotator;

impl ContextAnnotator {
    /// Rewrite text in-place by verifying embedded claims against the cryptographic ledger
    pub fn rewrite(
        text: &str,
        ledger: &ReceiptLedger,
        current_world_digest: &str,
    ) -> AnnotationResult {
        // Regex targeting assertion claims about test/build status
        let claim_regex = Regex::new(
            r"(?i)\b(all\s+tests?\s+(?:pass|passed|are\s+passing)|tests?\s+suite\s+(?:pass|passed)|cargo\s+test\s+(?:pass|passed)|npm\s+test\s+(?:pass|passed)|build\s+succeeded\s+and\s+all\s+tests?\s+pass|100%\s+tests?\s+pass(?:ing)?)\b"
        ).unwrap();

        let mut modifications_count = 0;
        let mut unverified_count = 0;
        let mut stale_count = 0;
        let mut verified_count = 0;

        let annotated_text = claim_regex.replace_all(text, |caps: &Captures| {
            let matched_claim = caps.get(0).unwrap().as_str();
            let status = ledger.verify_claim(matched_claim, current_world_digest);

            modifications_count += 1;

            match status {
                ClaimStatus::Verified { seq, .. } => {
                    verified_count += 1;
                    format!("{matched_claim} ⟨HT-VERIFIED: receipt seq {seq}⟩")
                }
                ClaimStatus::Stale { seq, .. } => {
                    stale_count += 1;
                    format!("⟨HT-STALE: world digest diverged at seq {seq}⟩")
                }
                ClaimStatus::Unverified { last_seq, .. } => {
                    unverified_count += 1;
                    let seq_str = last_seq
                        .map(|s| s.to_string())
                        .unwrap_or_else(|| "0".to_string());
                    format!("⟨HT-UNVERIFIED: no test receipt since seq {seq_str}⟩")
                }
            }
        }).to_string();

        AnnotationResult {
            annotated_text,
            modifications_count,
            unverified_count,
            stale_count,
            verified_count,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::key_custody::KeyCustody;

    #[test]
    fn test_in_place_annotation_unverified() {
        let ledger = ReceiptLedger::new();
        let prompt = "I completed the fix and all tests pass cleanly in release mode.";
        let res = ContextAnnotator::rewrite(prompt, &ledger, "digest_current");

        assert_eq!(res.unverified_count, 1);
        assert!(res.annotated_text.contains("⟨HT-UNVERIFIED: no test receipt since seq 0⟩"));
    }

    #[test]
    fn test_in_place_annotation_stale() {
        let key = KeyCustody::new_random();
        let mut ledger = ReceiptLedger::new();
        ledger.record_receipt("all tests pass", "cargo test", 0, "digest_v1", &key);

        let prompt = "We verified that all tests pass.";
        // World digest changed to v2
        let res = ContextAnnotator::rewrite(prompt, &ledger, "digest_v2");

        assert_eq!(res.stale_count, 1);
        assert!(res.annotated_text.contains("⟨HT-STALE: world digest diverged at seq 1⟩"));
    }

    #[test]
    fn test_in_place_annotation_verified() {
        let key = KeyCustody::new_random();
        let mut ledger = ReceiptLedger::new();
        ledger.record_receipt("all tests pass", "cargo test", 0, "digest_v1", &key);

        let prompt = "Confirmed: all tests pass.";
        // World digest matches receipt
        let res = ContextAnnotator::rewrite(prompt, &ledger, "digest_v1");

        assert_eq!(res.verified_count, 1);
        assert!(res.annotated_text.contains("⟨HT-VERIFIED: receipt seq 1⟩"));
    }
}
