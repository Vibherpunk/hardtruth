pub mod annotation;
pub mod ast_parser;
pub mod critic;
pub mod daemon;
pub mod gate_client;
pub mod intent_stack;
pub mod key_custody;
pub mod merkle;
pub mod modality;
pub mod protocol;
pub mod telemetry;

pub use annotation::{AnnotationResult, ContextAnnotator};
pub use ast_parser::{AstReachabilityResult, ShellAstEvaluator};
pub use critic::{GateResult, GateType, PhaseGateCritic, SrsRequirement};
pub use daemon::{default_socket_path, HardtruthDaemon};
pub use gate_client::GateClient;
pub use intent_stack::{IntentStack, IntentStackResult, IntentViolation};
pub use key_custody::KeyCustody;
pub use merkle::{ClaimStatus, Receipt, ReceiptLedger, WorldState};
pub use modality::{Modality, ModalityClassification, ModalityGuard};
pub use protocol::{IpcRequest, IpcResponse};
pub use telemetry::{DriftRecord, QuantizationTelemetry};

pub const VERSION: &str = "0.2.0";
pub const FORBIDDEN_PORT: u16 = 8000;

#[cfg(test)]
mod integration_tests {
    use super::*;
    use tempfile::tempdir;

    #[tokio::test]
    async fn test_full_pipeline_verification_cycle() {
        let dir = tempdir().unwrap();
        let sock_path = dir.path().join("hardtruth_test.sock");
        let root_dir = dir.path().to_path_buf();

        // 1. Initialize daemon
        let daemon = HardtruthDaemon::new(Some(sock_path.clone()), Some(root_dir.clone()));

        // Run daemon in background task
        let daemon_handle = tokio::spawn(async move {
            let _ = daemon.run().await;
        });

        // Yield to allow daemon to bind
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        // 2. Client connect
        let client = GateClient::new(Some(sock_path));
        let pong = client.ping().await;
        assert!(pong.unwrap());

        // 3. Record a clean receipt
        let rec = client.record_receipt("all tests pass", "cargo test", 0, Some(&root_dir)).await.unwrap();
        match rec {
            IpcResponse::ReceiptRecorded(r) => {
                assert_eq!(r.seq, 1);
                assert_eq!(r.exit_code, 0);
            }
            _ => panic!("Expected ReceiptRecorded"),
        }

        // 4. Verify claim
        let verified = client.verify_claim("all tests pass", Some(&root_dir)).await.unwrap();
        match verified {
            IpcResponse::ClaimVerified(ClaimStatus::Verified { seq, .. }) => {
                assert_eq!(seq, 1);
            }
            _ => panic!("Expected Verified"),
        }

        // 5. Evaluate shell command exit reachability
        let eval = client.evaluate_command("cargo test; true").await.unwrap();
        match eval {
            IpcResponse::CommandEvaluated(res) => {
                assert!(!res.exit_reachable);
                assert!(!res.allowed);
            }
            _ => panic!("Expected CommandEvaluated"),
        }

        // Clean up daemon
        daemon_handle.abort();
    }
}
