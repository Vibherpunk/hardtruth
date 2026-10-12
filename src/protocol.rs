use serde::{Deserialize, Serialize};
use crate::annotation::AnnotationResult;
use crate::ast_parser::AstReachabilityResult;
use crate::critic::GateResult;
use crate::intent_stack::IntentStackResult;
use crate::merkle::{ClaimStatus, Receipt};
use crate::telemetry::DriftRecord;

/// IPC Request sent from hardtruth-gate to hardtruthd
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum IpcRequest {
    Ping,
    RecordReceipt {
        claim: String,
        command: String,
        exit_code: i32,
        world_root: Option<String>,
    },
    VerifyClaim {
        claim: String,
        world_root: Option<String>,
    },
    RewriteContext {
        text: String,
        world_root: Option<String>,
    },
    EvaluateCommand {
        command: String,
    },
    EvaluateIntent {
        ports: Vec<u16>,
        cost_cents: u64,
        target_files: Vec<String>,
        tokens: u64,
    },
    RunGate {
        gate_type: String,
        payload: serde_json::Value,
    },
    DriftTelemetry {
        model_id: String,
        quant: String,
        logit_margin: f32,
        grammar: Option<String>,
        output: String,
    },
    GetStatus,
    ResetCircuitBreaker,
}

/// IPC Response returned by hardtruthd
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "status")]
pub enum IpcResponse {
    Pong {
        version: String,
        pid: u32,
    },
    ReceiptRecorded(Receipt),
    ClaimVerified(ClaimStatus),
    ContextRewritten(AnnotationResult),
    CommandEvaluated(AstReachabilityResult),
    IntentEvaluated(IntentStackResult),
    GateEvaluated(GateResult),
    TelemetryRecorded(DriftRecord),
    DaemonStatus {
        version: String,
        pid: u32,
        uptime_secs: u64,
        receipts_count: usize,
        circuit_breaker_tripped: bool,
        current_world_digest: String,
        mlock_active: bool,
        forbidden_port_invariant_active: bool,
    },
    CircuitBreakerReset {
        success: bool,
    },
    Error {
        message: String,
    },
}
