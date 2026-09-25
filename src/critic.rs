use std::path::Path;
use serde::{Deserialize, Serialize};
use crate::intent_stack::IntentStack;
use crate::merkle::ReceiptLedger;

/// The Asymmetric Phase Gate types
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub enum GateType {
    /// G1: Plan Gate (evaluates invariants & initial scope)
    G1Plan,
    /// G2: Mutation Gate (evaluates file write permissions against closure)
    G2Mutation,
    /// G3: Circuit Breaker Gate (safety cutoff after failures)
    G3CircuitBreaker,
    /// G4: Pre-Completion Gate (SRS Spec Matrix Join & receipt completeness)
    G4PreCompletion,
}

/// An SRS functional requirement to be joined against execution receipts
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SrsRequirement {
    pub req_id: String,
    pub title: String,
    pub verified_by_claim: String,
}

/// Gate verdict outcome
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GateResult {
    pub gate_type: GateType,
    pub passed: bool,
    pub reason: String,
    pub circuit_breaker_tripped: bool,
    pub unjoined_requirements: Vec<String>,
}

/// Asymmetric Phase Gates and Circuit Breaker
pub struct PhaseGateCritic {
    consecutive_failures: usize,
    circuit_breaker_tripped: bool,
    max_consecutive_failures: usize,
}

impl PhaseGateCritic {
    pub fn new() -> Self {
        Self {
            consecutive_failures: 0,
            circuit_breaker_tripped: false,
            max_consecutive_failures: 2, // Hard specification: G3 trips after 2 failures
        }
    }

    /// Record an execution outcome to update circuit breaker state
    pub fn record_failure(&mut self) -> bool {
        self.consecutive_failures += 1;
        if self.consecutive_failures >= self.max_consecutive_failures {
            self.circuit_breaker_tripped = true;
        }
        self.circuit_breaker_tripped
    }

    /// Record a successful execution, resetting failure count
    pub fn record_success(&mut self) {
        self.consecutive_failures = 0;
    }

    /// Explicitly reset the circuit breaker
    pub fn reset_circuit_breaker(&mut self) {
        self.consecutive_failures = 0;
        self.circuit_breaker_tripped = false;
    }

    /// Check circuit breaker state
    pub fn is_tripped(&self) -> bool {
        self.circuit_breaker_tripped
    }

    /// G1: Evaluate proposed plan against negative invariants, planner model policy, and initial closure
    pub fn evaluate_g1_plan(
        &self,
        intent_stack: &mut IntentStack,
        referenced_ports: &[u16],
        budget_cents: u64,
        planned_files: &[impl AsRef<Path>],
        planner_model: Option<&str>,
    ) -> GateResult {
        if self.circuit_breaker_tripped {
            return GateResult {
                gate_type: GateType::G1Plan,
                passed: false,
                reason: "G3 Circuit Breaker is active: autonomous operations halted".to_string(),
                circuit_breaker_tripped: true,
                unjoined_requirements: Vec::new(),
            };
        }

        // Check planner model invariant
        if let Some(model) = planner_model {
            if IntentStack::is_forbidden_planner_model(model) {
                return GateResult {
                    gate_type: GateType::G1Plan,
                    passed: false,
                    reason: crate::intent_stack::FRONTIER_PLANNING_VIOLATION.to_string(),
                    circuit_breaker_tripped: false,
                    unjoined_requirements: Vec::new(),
                };
            }
        }

        let res = intent_stack.evaluate_plan(referenced_ports, budget_cents, planned_files, 1);
        let passed = res.allowed;
        let reason = if passed {
            for file in planned_files {
                intent_stack.add_to_closure(file);
            }
            "G1 Plan Gate passed: invariants satisfied and initial closure approved".to_string()
        } else {
            format!("G1 Plan Gate rejected: {:?}", res.violations)
        };

        GateResult {
            gate_type: GateType::G1Plan,
            passed,
            reason,
            circuit_breaker_tripped: false,
            unjoined_requirements: Vec::new(),
        }
    }

    /// G2: Evaluate file mutations against Channel B closure set
    pub fn evaluate_g2_mutation(
        &mut self,
        intent_stack: &IntentStack,
        mutated_files: &[impl AsRef<Path>],
    ) -> GateResult {
        if self.circuit_breaker_tripped {
            return GateResult {
                gate_type: GateType::G2Mutation,
                passed: false,
                reason: "G3 Circuit Breaker is active: file mutations halted".to_string(),
                circuit_breaker_tripped: true,
                unjoined_requirements: Vec::new(),
            };
        }

        // Rule 5 Mechanical Lock: Direct script synthesis in services/, apps/, or scripts/ outside VibeHard
        if let Some(violation_msg) = intent_stack.check_rule_5(mutated_files) {
            let tripped = self.record_failure();
            return GateResult {
                gate_type: GateType::G2Mutation,
                passed: false,
                reason: violation_msg,
                circuit_breaker_tripped: tripped,
                unjoined_requirements: Vec::new(),
            };
        }

        let violations = intent_stack.check_channel_b(mutated_files);
        if violations.is_empty() {
            GateResult {
                gate_type: GateType::G2Mutation,
                passed: true,
                reason: "G2 Mutation Gate passed: all files exist in authorized closure set".to_string(),
                circuit_breaker_tripped: false,
                unjoined_requirements: Vec::new(),
            }
        } else {
            let tripped = self.record_failure();
            GateResult {
                gate_type: GateType::G2Mutation,
                passed: false,
                reason: format!("G2 Mutation Gate rejected: unauthorized paths mutated. Tripped={tripped}"),
                circuit_breaker_tripped: tripped,
                unjoined_requirements: Vec::new(),
            }
        }
    }

    /// G3: Query circuit breaker status
    pub fn evaluate_g3_circuit_breaker(&self) -> GateResult {
        GateResult {
            gate_type: GateType::G3CircuitBreaker,
            passed: !self.circuit_breaker_tripped,
            reason: if self.circuit_breaker_tripped {
                format!(
                    "G3 Circuit Breaker TRIPPED: {} consecutive failures reached",
                    self.consecutive_failures
                )
            } else {
                format!(
                    "G3 Circuit Breaker healthy: {}/{} failures",
                    self.consecutive_failures, self.max_consecutive_failures
                )
            },
            circuit_breaker_tripped: self.circuit_breaker_tripped,
            unjoined_requirements: Vec::new(),
        }
    }

    /// G4 / Phase 5: Relational join of SRS requirements against verified ledger receipts
    pub fn evaluate_g4_spec_matrix_join(
        &self,
        srs_requirements: &[SrsRequirement],
        ledger: &ReceiptLedger,
        current_world_digest: &str,
    ) -> GateResult {
        if self.circuit_breaker_tripped {
            return GateResult {
                gate_type: GateType::G4PreCompletion,
                passed: false,
                reason: "G3 Circuit Breaker is active: completion blocked".to_string(),
                circuit_breaker_tripped: true,
                unjoined_requirements: Vec::new(),
            };
        }

        let mut unjoined = Vec::new();

        for req in srs_requirements {
            let status = ledger.verify_claim(&req.verified_by_claim, current_world_digest);
            match status {
                crate::merkle::ClaimStatus::Verified { .. } => {}
                _ => {
                    unjoined.push(format!(
                        "{} ('{}') unverified against current world digest",
                        req.req_id, req.title
                    ));
                }
            }
        }

        let passed = unjoined.is_empty();
        let reason = if passed {
            format!(
                "G4 Pre-Completion Gate PASSED: All {} SRS requirements verified in ledger join",
                srs_requirements.len()
            )
        } else {
            format!(
                "G4 Pre-Completion Gate REJECTED: {} SRS requirement(s) lack valid current receipts",
                unjoined.len()
            )
        };

        GateResult {
            gate_type: GateType::G4PreCompletion,
            passed,
            reason,
            circuit_breaker_tripped: false,
            unjoined_requirements: unjoined,
        }
    }
}

impl Default for PhaseGateCritic {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::key_custody::KeyCustody;

    #[test]
    fn test_circuit_breaker_trips_after_two_failures() {
        let mut critic = PhaseGateCritic::new();
        assert!(!critic.is_tripped());

        // 1st failure
        critic.record_failure();
        assert!(!critic.is_tripped());

        // 2nd failure -> TRIPPED!
        critic.record_failure();
        assert!(critic.is_tripped());

        let g3 = critic.evaluate_g3_circuit_breaker();
        assert!(!g3.passed);
        assert!(g3.circuit_breaker_tripped);

        // Reset restores healthy state
        critic.reset_circuit_breaker();
        assert!(!critic.is_tripped());
    }

    #[test]
    fn test_g4_srs_spec_matrix_join() {
        let key = KeyCustody::new_random();
        let mut ledger = ReceiptLedger::new();
        let critic = PhaseGateCritic::new();

        let reqs = vec![
            SrsRequirement {
                req_id: "FR-1".to_string(),
                title: "Daemon Key Custody".to_string(),
                verified_by_claim: "key custody test passes".to_string(),
            },
            SrsRequirement {
                req_id: "FR-2".to_string(),
                title: "Merkle World State".to_string(),
                verified_by_claim: "merkle test passes".to_string(),
            },
        ];

        let digest = "current_digest_123";

        // Without receipts: G4 fails
        let res1 = critic.evaluate_g4_spec_matrix_join(&reqs, &ledger, digest);
        assert!(!res1.passed);
        assert_eq!(res1.unjoined_requirements.len(), 2);

        // Record 1st receipt
        ledger.record_receipt("key custody test passes", "cargo test test_key", 0, digest, &key);
        let res2 = critic.evaluate_g4_spec_matrix_join(&reqs, &ledger, digest);
        assert!(!res2.passed);
        assert_eq!(res2.unjoined_requirements.len(), 1);

        // Record 2nd receipt -> G4 passes!
        ledger.record_receipt("merkle test passes", "cargo test test_merkle", 0, digest, &key);
        let res3 = critic.evaluate_g4_spec_matrix_join(&reqs, &ledger, digest);
        assert!(res3.passed);
        assert!(res3.unjoined_requirements.is_empty());
    }

    #[test]
    fn test_g2_rejects_unverified_script_creation_rule_5() {
        let mut critic = PhaseGateCritic::new();
        let stack = IntentStack::new(vec!["src/main.rs"]);
        let res = critic.evaluate_g2_mutation(&stack, &["services/payment_worker.py"]);
        assert!(!res.passed);
        assert_eq!(
            res.reason,
            "REJECT: Rule 5 Violation: Direct script synthesis in services/ or apps/ is forbidden. All software synthesis must run through VibeHard (bun src/cli.ts build) or be compiled as a declarative n8n DAG."
        );
        assert_eq!(res.gate_type, GateType::G2Mutation);
    }

    #[test]
    fn test_g2_allows_script_creation_with_vibehard_context() {
        let mut critic = PhaseGateCritic::new();
        let mut stack = IntentStack::new(vec!["services/payment_worker.py"]);
        stack.set_vibehard_context(true);
        let res = critic.evaluate_g2_mutation(&stack, &["services/payment_worker.py"]);
        assert!(res.passed);
        assert_eq!(res.gate_type, GateType::G2Mutation);
    }

    #[test]
    fn test_g1_rejects_flash_planner_model() {
        let critic = PhaseGateCritic::new();
        let mut stack = IntentStack::new(vec!["src/main.rs"]);
        let res = critic.evaluate_g1_plan(
            &mut stack,
            &[],
            0,
            &["src/new_file.rs"],
            Some("gemini-3.8-flash"),
        );
        assert!(!res.passed);
        assert_eq!(res.gate_type, GateType::G1Plan);
        assert_eq!(
            res.reason,
            "REJECT: Frontier Planning Invariant Violation: Flash models and Gemini 3.1 Pro are strictly forbidden from acting as lead architect or planner. Must use a frontier model (Claude Opus 5.5, Kimi Pro, Mimo Pro, DeepSeek Pro)."
        );
    }

    #[test]
    fn test_g1_rejects_gemini_3_1_pro_planner_model() {
        let critic = PhaseGateCritic::new();
        let mut stack = IntentStack::new(vec!["src/main.rs"]);
        let res = critic.evaluate_g1_plan(
            &mut stack,
            &[],
            0,
            &["src/new_file.rs"],
            Some("gemini-3.1-pro"),
        );
        assert!(!res.passed);
        assert_eq!(res.gate_type, GateType::G1Plan);
        assert_eq!(
            res.reason,
            "REJECT: Frontier Planning Invariant Violation: Flash models and Gemini 3.1 Pro are strictly forbidden from acting as lead architect or planner. Must use a frontier model (Claude Opus 5.5, Kimi Pro, Mimo Pro, DeepSeek Pro)."
        );
    }

    #[test]
    fn test_g1_allows_frontier_planner_model_and_commits_closure() {
        let critic = PhaseGateCritic::new();
        let mut stack = IntentStack::new(vec!["src/main.rs"]);
        assert_eq!(stack.closure().len(), 1);

        let res = critic.evaluate_g1_plan(
            &mut stack,
            &[],
            0,
            &["src/new_file.rs"],
            Some("claude-opus-5.5"),
        );
        assert!(res.passed);
        assert_eq!(res.gate_type, GateType::G1Plan);
        assert!(stack.closure().contains(std::path::Path::new("src/new_file.rs")));
    }
}
