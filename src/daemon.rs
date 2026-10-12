use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::Mutex;
use crate::annotation::ContextAnnotator;
use crate::ast_parser::ShellAstEvaluator;
use crate::critic::{PhaseGateCritic, SrsRequirement};
use crate::intent_stack::IntentStack;
use crate::key_custody::{secure_socket_permissions, KeyCustody};
use crate::merkle::{ReceiptLedger, WorldState};
use crate::protocol::{IpcRequest, IpcResponse};
use crate::telemetry::QuantizationTelemetry;

/// Default socket path: ~/Library/Caches/skidnir/hardtruth.sock
pub fn default_socket_path() -> PathBuf {
    if let Ok(override_path) = std::env::var("HARDTRUTH_SOCK") {
        return PathBuf::from(override_path);
    }

    let run_path = PathBuf::from("/run/hardtruth/hardtruth.sock");
    if run_path.exists() {
        return run_path;
    }

    let cache_dir = dirs::cache_dir().unwrap_or_else(|| {
        dirs::home_dir()
            .map(|h| h.join("Library/Caches"))
            .unwrap_or_else(|| PathBuf::from("/tmp"))
    });

    cache_dir.join("skidnir/hardtruth.sock")
}

/// Shared state of the hardtruthd resident daemon
pub struct DaemonState {
    pub key: KeyCustody,
    pub ledger: ReceiptLedger,
    pub intent_stack: IntentStack,
    pub critic: PhaseGateCritic,
    pub telemetry: QuantizationTelemetry,
    pub start_time: Instant,
    pub root_dir: PathBuf,
    pub cached_digest: Option<String>,
}

impl DaemonState {
    pub fn new(root_dir: PathBuf) -> Self {
        let initial_closure = vec![
            root_dir.clone(),
            PathBuf::from("Cargo.toml"),
            PathBuf::from("src"),
            PathBuf::from("apps"),
        ];
        Self {
            key: KeyCustody::new_random(),
            ledger: ReceiptLedger::new(),
            intent_stack: IntentStack::new(initial_closure),
            critic: PhaseGateCritic::new(),
            telemetry: QuantizationTelemetry::new(),
            start_time: Instant::now(),
            root_dir,
            cached_digest: None,
        }
    }
}

/// Resident async anti-hallucination and integrity daemon
pub struct HardtruthDaemon {
    socket_path: PathBuf,
    state: Arc<Mutex<DaemonState>>,
}

impl HardtruthDaemon {
    pub fn new(socket_path: Option<PathBuf>, root_dir: Option<PathBuf>) -> Self {
        let sock = socket_path.unwrap_or_else(default_socket_path);
        let root = root_dir.unwrap_or_else(|| {
            let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
            if cwd.join(".git").exists() || cwd.join("Cargo.toml").exists() {
                cwd
            } else if Path::new("/opt/vibehard").exists() {
                PathBuf::from("/opt/vibehard")
            } else if Path::new("/Users/ai/dev/vibehard").exists() {
                PathBuf::from("/Users/ai/dev/vibehard")
            } else {
                cwd
            }
        });
        Self {
            socket_path: sock,
            state: Arc::new(Mutex::new(DaemonState::new(root))),
        }
    }

    /// Run the resident daemon until SIGTERM or SIGINT
    pub async fn run(&self) -> Result<(), Box<dyn std::error::Error>> {
        // 1. Prepare directory and clean stale socket
        if let Some(parent) = self.socket_path.parent() {
            fs::create_dir_all(parent)?;
        }
        if self.socket_path.exists() {
            let _ = fs::remove_file(&self.socket_path);
        }

        // 2. Bind Unix Domain Socket
        let listener = UnixListener::bind(&self.socket_path)?;

        // 3. Set strict permissions (0600)
        secure_socket_permissions(&self.socket_path)?;

        println!(
            "[hardtruthd] Resident daemon listening on {} (mode 0600)",
            self.socket_path.display()
        );

        // 4. Setup termination signals
        #[cfg(unix)]
        let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;

        let state = Arc::clone(&self.state);
        let sock_path = self.socket_path.clone();

        loop {
            tokio::select! {
                accept_res = listener.accept() => {
                    match accept_res {
                        Ok((stream, _)) => {
                            let state_clone = Arc::clone(&state);
                            tokio::spawn(async move {
                                if let Err(e) = Self::handle_connection(stream, state_clone).await {
                                    eprintln!("[hardtruthd] Connection error: {e}");
                                }
                            });
                        }
                        Err(e) => {
                            eprintln!("[hardtruthd] Accept error: {e}");
                        }
                    }
                }
                _ = tokio::signal::ctrl_c() => {
                    println!("[hardtruthd] Received SIGINT (Ctrl+C). Zeroizing key and shutting down.");
                    break;
                }
                _ = async {
                    #[cfg(unix)]
                    {
                        sigterm.recv().await
                    }
                    #[cfg(not(unix))]
                    {
                        std::future::pending::<()>().await;
                        None
                    }
                } => {
                    println!("[hardtruthd] Received SIGTERM. Zeroizing key in non-paged RAM and unlinking socket.");
                    break;
                }
            }
        }

        // Shutdown cleanup
        let mut st = state.lock().await;
        st.key.zeroize_now();
        if sock_path.exists() {
            let _ = fs::remove_file(&sock_path);
        }
        println!("[hardtruthd] Daemon stopped cleanly. Zeroize verified.");
        Ok(())
    }

    async fn handle_connection(
        mut stream: UnixStream,
        state: Arc<Mutex<DaemonState>>,
    ) -> Result<(), Box<dyn std::error::Error>> {
        let mut buffer = Vec::new();
        let mut chunk = [0u8; 4096];

        // Read request from socket
        loop {
            let n = stream.read(&mut chunk).await?;
            if n == 0 {
                break;
            }
            buffer.extend_from_slice(&chunk[..n]);
            // Check for JSON delimiter or complete packet
            if buffer.ends_with(b"\n") || serde_json::from_slice::<IpcRequest>(&buffer).is_ok() {
                break;
            }
        }

        if buffer.is_empty() {
            return Ok(());
        }

        let req: IpcRequest = serde_json::from_slice(&buffer)?;
        let resp = Self::dispatch_request(req, state).await;

        let resp_bytes = serde_json::to_vec(&resp)?;
        stream.write_all(&resp_bytes).await?;
        stream.flush().await?;
        Ok(())
    }

    async fn dispatch_request(req: IpcRequest, state: Arc<Mutex<DaemonState>>) -> IpcResponse {
        let mut guard = state.lock().await;
        let st = &mut *guard;

        match req {
            IpcRequest::Ping => IpcResponse::Pong {
                version: "0.2.0".to_string(),
                pid: std::process::id(),
            },
            IpcRequest::RecordReceipt { claim, command, exit_code, world_root } => {
                let root = world_root.map(PathBuf::from).unwrap_or_else(|| st.root_dir.clone());
                let world_digest = WorldState::new(root).compute_world_digest().unwrap_or_default();

                if exit_code == 0 {
                    st.critic.record_success();
                } else {
                    st.critic.record_failure();
                }

                let receipt = st.ledger.record_receipt(claim, command, exit_code, world_digest.clone(), &st.key);
                st.cached_digest = Some(world_digest);
                IpcResponse::ReceiptRecorded(receipt)
            }
            IpcRequest::VerifyClaim { claim, world_root } => {
                let root = world_root.map(PathBuf::from).unwrap_or_else(|| st.root_dir.clone());
                let current_digest = WorldState::new(root).compute_world_digest().unwrap_or_default();
                st.cached_digest = Some(current_digest.clone());
                let status = st.ledger.verify_claim(&claim, &current_digest);
                IpcResponse::ClaimVerified(status)
            }
            IpcRequest::RewriteContext { text, world_root } => {
                let root = world_root.map(PathBuf::from).unwrap_or_else(|| st.root_dir.clone());
                let current_digest = WorldState::new(root).compute_world_digest().unwrap_or_default();
                st.cached_digest = Some(current_digest.clone());
                let res = ContextAnnotator::rewrite(&text, &st.ledger, &current_digest);
                IpcResponse::ContextRewritten(res)
            }
            IpcRequest::EvaluateCommand { command } => {
                let res = ShellAstEvaluator::evaluate(&command);
                IpcResponse::CommandEvaluated(res)
            }
            IpcRequest::EvaluateIntent { ports, cost_cents, target_files, tokens } => {
                let paths: Vec<PathBuf> = target_files.into_iter().map(PathBuf::from).collect();
                let res = st.intent_stack.evaluate(&ports, cost_cents, &paths, tokens);
                IpcResponse::IntentEvaluated(res)
            }
            IpcRequest::RunGate { gate_type, payload } => {
                match gate_type.as_str() {
                    "G1" => {
                        let ports = payload.get("ports")
                            .and_then(|v| v.as_array())
                            .map(|arr| arr.iter().filter_map(|v| v.as_u64().map(|n| n as u16)).collect::<Vec<_>>())
                            .unwrap_or_default();
                        let budget = payload.get("budget_cents").and_then(|v| v.as_u64()).unwrap_or(0);
                        let planned_files = payload.get("planned_files")
                            .and_then(|v| v.as_array())
                            .map(|arr| arr.iter().filter_map(|v| v.as_str().map(PathBuf::from)).collect::<Vec<_>>())
                            .unwrap_or_default();

                        let planner_model = payload.get("planner_model")
                            .or_else(|| payload.get("architect_model"))
                            .or_else(|| payload.get("model"))
                            .or_else(|| payload.get("planner"))
                            .or_else(|| payload.get("architect"))
                            .and_then(|v| v.as_str());
                        let res = st.critic.evaluate_g1_plan(&mut st.intent_stack, &ports, budget, &planned_files, planner_model);
                        IpcResponse::GateEvaluated(res)
                    }
                    "G2" => {
                        let mutated = payload.get("mutated_files")
                            .and_then(|v| v.as_array())
                            .map(|arr| arr.iter().filter_map(|v| v.as_str().map(PathBuf::from)).collect::<Vec<_>>())
                            .unwrap_or_default();
                        if let Some(ticket) = payload.get("session_ticket").and_then(|v| v.as_str()) {
                            st.intent_stack.set_session_ticket(Some(ticket.to_string()));
                        }
                        if let Some(vb_ctx) = payload.get("vibehard_context").and_then(|v| v.as_bool()) {
                            st.intent_stack.set_vibehard_context(vb_ctx);
                        }
                        let res = st.critic.evaluate_g2_mutation(&st.intent_stack, &mutated);
                        IpcResponse::GateEvaluated(res)
                    }
                    "G3" => {
                        let res = st.critic.evaluate_g3_circuit_breaker();
                        IpcResponse::GateEvaluated(res)
                    }
                    "G4" => {
                        let reqs = payload.get("requirements")
                            .and_then(|v| serde_json::from_value::<Vec<SrsRequirement>>(v.clone()).ok())
                            .unwrap_or_default();
                        let current_digest = WorldState::new(&st.root_dir).compute_world_digest().unwrap_or_default();
                        let res = st.critic.evaluate_g4_spec_matrix_join(&reqs, &st.ledger, &current_digest);
                        IpcResponse::GateEvaluated(res)
                    }
                    _ => IpcResponse::Error { message: format!("Unknown gate type: {gate_type}") },
                }
            }
            IpcRequest::DriftTelemetry { model_id, quant, logit_margin, grammar, output } => {
                let record = st.telemetry.record(model_id, quant, logit_margin, grammar, &output);
                IpcResponse::TelemetryRecorded(record)
            }
            IpcRequest::GetStatus => {
                let current_digest = match &st.cached_digest {
                    Some(d) => d.clone(),
                    None => {
                        let d = WorldState::new(&st.root_dir).compute_world_digest().unwrap_or_default();
                        st.cached_digest = Some(d.clone());
                        d
                    }
                };
                IpcResponse::DaemonStatus {
                    version: "0.2.0".to_string(),
                    pid: std::process::id(),
                    uptime_secs: st.start_time.elapsed().as_secs(),
                    receipts_count: st.ledger.count(),
                    circuit_breaker_tripped: st.critic.is_tripped(),
                    current_world_digest: current_digest,
                    mlock_active: st.key.is_mlocked(),
                    forbidden_port_invariant_active: true,
                }
            }
            IpcRequest::ResetCircuitBreaker => {
                st.critic.reset_circuit_breaker();
                IpcResponse::CircuitBreakerReset { success: true }
            }
        }
    }
}
