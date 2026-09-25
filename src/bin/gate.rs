use clap::{Parser, Subcommand};
use std::path::PathBuf;
use hardtruth::gate_client::GateClient;
use hardtruth::protocol::{IpcRequest, IpcResponse};

#[derive(Parser, Debug)]
#[command(name = "hardtruth-gate")]
#[command(author = "Adam Matar <adam@skidnir.io>")]
#[command(version = "0.2.0")]
#[command(about = "Thin-client CLI gate communicating over UDS with hardtruthd", long_about = None)]
struct Cli {
    /// Custom path for the Unix Domain Socket
    #[arg(short, long)]
    socket: Option<PathBuf>,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand, Debug)]
enum Commands {
    /// Ping the resident hardtruthd daemon
    Ping,

    /// Query daemon status, uptime, ledger counts, and circuit breaker state
    Status,

    /// Verify a stated claim against the cryptographic receipt ledger and Merkle world digest
    Verify {
        /// The claim to verify (e.g. "all tests pass")
        claim: String,
        /// Path to the repository root (default: current working directory)
        #[arg(short, long)]
        root: Option<PathBuf>,
    },

    /// Record a verified execution receipt in the ledger
    Receipt {
        /// Stated claim
        claim: String,
        /// Command that was executed
        command: String,
        /// Exit code of the command
        exit_code: i32,
        /// Path to the repository root
        #[arg(short, long)]
        root: Option<PathBuf>,
    },

    /// Evaluate a shell command for AST exit masking operators (; true, || exit 0)
    Eval {
        /// Shell command string to analyze
        command: String,
    },

    /// Rewrite a file or prompt in-place with the anti-hallucination annotation ladder
    Annotate {
        /// File to rewrite in-place, or raw text if --text is passed
        target: String,
        /// Treat target argument as raw text rather than a file path
        #[arg(short, long)]
        text: bool,
        /// Path to repository root
        #[arg(short, long)]
        root: Option<PathBuf>,
    },

    /// Run an asymmetric phase gate (G1, G2, G3, G4)
    Gate {
        /// Gate type: G1, G2, G3, or G4
        gate_type: String,
        /// JSON payload describing the gate inputs
        #[arg(short, long)]
        payload: Option<String>,
    },

    /// Antigravity PreToolUse hook integration
    #[command(name = "pre_tool")]
    PreTool,

    /// Antigravity PostToolUse hook integration
    #[command(name = "post_tool")]
    PostTool,

    /// Antigravity Stop hook integration
    #[command(name = "stop")]
    Stop,

    /// Reset G3 Circuit Breaker
    #[command(name = "reset")]
    Reset,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    let client = GateClient::new(cli.socket);

    match cli.command {
        Commands::Ping => {
            match client.ping().await {
                Ok(true) => {
                    println!("hardtruthd: PONG (resident daemon healthy)");
                }
                Ok(false) => {
                    eprintln!("hardtruthd: unexpected response");
                    std::process::exit(1);
                }
                Err(e) => {
                    eprintln!("hardtruth-gate error: {e}");
                    std::process::exit(1);
                }
            }
        }
        Commands::Status => {
            match client.get_status().await {
                Ok(IpcResponse::DaemonStatus {
                    version,
                    pid,
                    uptime_secs,
                    receipts_count,
                    circuit_breaker_tripped,
                    current_world_digest,
                    mlock_active,
                    forbidden_port_invariant_active,
                }) => {
                    println!("hardtruthd v{version} (PID {pid})");
                    println!("  Uptime:                {uptime_secs}s");
                    println!("  Ledger receipts:       {receipts_count}");
                    println!("  Circuit breaker:       {}", if circuit_breaker_tripped { "TRIPPED" } else { "HEALTHY" });
                    println!("  Current world digest:  {current_world_digest}");
                    println!("  RAM key custody:       {}", if mlock_active { "mlock() non-paged" } else { "heap" });
                    println!("  Forbidden Port 8000:   {}", if forbidden_port_invariant_active { "ENFORCED" } else { "INACTIVE" });
                }
                Ok(other) => {
                    println!("{other:?}");
                }
                Err(e) => {
                    eprintln!("hardtruth-gate status error: {e}");
                    std::process::exit(1);
                }
            }
        }
        Commands::Verify { claim, root } => {
            match client.verify_claim(&claim, root).await {
                Ok(IpcResponse::ClaimVerified(status)) => match status {
                    hardtruth::merkle::ClaimStatus::Verified { seq, receipt } => {
                        println!("VERIFIED (Receipt #{seq})");
                        println!("  Claim:        {}", receipt.claim);
                        println!("  Command:      {}", receipt.command);
                        println!("  Exit code:    {}", receipt.exit_code);
                        println!("  World digest: {}", receipt.world_digest);
                        println!("  Signature:    {}", receipt.signature);
                    }
                    hardtruth::merkle::ClaimStatus::Stale { seq, receipt_digest, current_digest } => {
                        eprintln!("STALE (Receipt #{seq})");
                        eprintln!("  Receipt digest: {receipt_digest}");
                        eprintln!("  Current digest: {current_digest}");
                        eprintln!("  Notice: World state diverged since last verified test run.");
                        std::process::exit(2);
                    }
                    hardtruth::merkle::ClaimStatus::Unverified { last_seq, reason } => {
                        eprintln!("UNVERIFIED");
                        eprintln!("  Reason:   {reason}");
                        if let Some(seq) = last_seq {
                            eprintln!("  Last seq: {seq}");
                        }
                        std::process::exit(1);
                    }
                },
                Ok(other) => {
                    println!("{other:?}");
                }
                Err(e) => {
                    eprintln!("hardtruth-gate verify error: {e}");
                    std::process::exit(1);
                }
            }
        }
        Commands::Receipt { claim, command, exit_code, root } => {
            match client.record_receipt(&claim, &command, exit_code, root).await {
                Ok(IpcResponse::ReceiptRecorded(r)) => {
                    println!("Receipt recorded successfully (Seq #{})", r.seq);
                    println!("  Signature: {}", r.signature);
                }
                Ok(other) => println!("{other:?}"),
                Err(e) => {
                    eprintln!("hardtruth-gate receipt error: {e}");
                    std::process::exit(1);
                }
            }
        }
        Commands::Eval { command } => {
            match client.evaluate_command(&command).await {
                Ok(IpcResponse::CommandEvaluated(res)) => {
                    if res.exit_reachable && res.allowed {
                        println!("ALLOW: Exit code reachable (no masking operators)");
                    } else {
                        eprintln!("REJECT: Invariant violation or exit code masked: {}", res.masked_operators.join(", "));
                        eprintln!("Diagnostics: {}", res.diagnostics);
                        std::process::exit(1);
                    }
                }
                Ok(other) => println!("{other:?}"),
                Err(e) => {
                    eprintln!("hardtruth-gate eval error: {e}");
                    std::process::exit(1);
                }
            }
        }
        Commands::Annotate { target, text, root } => {
            let content = if text {
                target.clone()
            } else {
                std::fs::read_to_string(&target)?
            };

            match client.rewrite_context(&content, root).await {
                Ok(IpcResponse::ContextRewritten(res)) => {
                    if text {
                        println!("{}", res.annotated_text);
                    } else {
                        std::fs::write(&target, &res.annotated_text)?;
                        println!("Annotated {} (modifications: {}, unverified: {}, stale: {}, verified: {})",
                            target, res.modifications_count, res.unverified_count, res.stale_count, res.verified_count);
                    }
                }
                Ok(other) => println!("{other:?}"),
                Err(e) => {
                    eprintln!("hardtruth-gate annotate error: {e}");
                    std::process::exit(1);
                }
            }
        }
        Commands::Gate { gate_type, payload } => {
            let val = match payload {
                Some(p) => serde_json::from_str(&p)?,
                None => serde_json::json!({}),
            };
            let req = IpcRequest::RunGate { gate_type, payload: val };
            match client.send_request(&req).await {
                Ok(IpcResponse::GateEvaluated(res)) => {
                    if res.passed {
                        println!("GATE PASS: {:?}", res.gate_type);
                        println!("Reason: {}", res.reason);
                    } else {
                        eprintln!("GATE FAIL: {:?}", res.gate_type);
                        eprintln!("Reason: {}", res.reason);
                        if res.circuit_breaker_tripped {
                            eprintln!("WARNING: G3 Circuit Breaker has been tripped!");
                        }
                        if !res.unjoined_requirements.is_empty() {
                            eprintln!("Unjoined SRS requirements:\n  • {}", res.unjoined_requirements.join("\n  • "));
                        }
                        std::process::exit(1);
                    }
                }
                Ok(other) => println!("{other:?}"),
                Err(e) => {
                    eprintln!("hardtruth-gate gate error: {e}");
                    std::process::exit(1);
                }
            }
        }
        Commands::PreTool => {
            use std::io::BufRead;
            let mut input = String::new();
            let _ = std::io::stdin().lock().read_line(&mut input);

            if !input.trim().is_empty() {
                if let Ok(val) = serde_json::from_str::<serde_json::Value>(&input) {
                    let tool_call = val.get("toolCall");
                    let tool_name = tool_call
                        .and_then(|t| t.get("name"))
                        .and_then(|n| n.as_str())
                        .unwrap_or_default();
                    let tool_args = tool_call.and_then(|t| t.get("args"));

                    // 1. Physical G2 Mutation Gate check before file mutations
                    if matches!(
                        tool_name,
                        "write_to_file" | "replace_file_content" | "Write" | "write" | "Edit" | "edit"
                    ) {
                        let target_file = tool_args
                            .and_then(|a| {
                                a.get("TargetFile")
                                    .or_else(|| a.get("target_file"))
                                    .or_else(|| a.get("file_path"))
                            })
                            .and_then(|f| f.as_str());

                        if let Some(target) = target_file {
                            let req = IpcRequest::RunGate {
                                gate_type: "G2".to_string(),
                                payload: serde_json::json!({
                                    "mutated_files": [target]
                                }),
                            };
                            if let Ok(IpcResponse::GateEvaluated(res)) = client.send_request(&req).await {
                                if !res.passed {
                                    eprintln!("hardtruth-gate pre_tool: {}", res.reason);
                                    println!("{}", serde_json::json!({
                                        "decision": "deny",
                                        "reason": res.reason
                                    }));
                                    std::process::exit(1);
                                }
                            }
                        }
                    }

                    // 2. Shell Command evaluation
                    if let Some(cmd_str) = tool_args.and_then(|a| a.get("CommandLine")).and_then(|c| c.as_str()) {
                        if cmd_str.contains("hardtruth-gate") {
                            println!("{}", serde_json::json!({ "decision": "allow" }));
                            return Ok(());
                        }

                        if let Ok(IpcResponse::CommandEvaluated(res)) = client.evaluate_command(cmd_str).await {
                            if !res.exit_reachable || !res.allowed {
                                eprintln!("hardtruth-gate pre_tool: REJECT invariant or masking operator {}", res.masked_operators.join(", "));
                                println!("{}", serde_json::json!({
                                    "decision": "deny",
                                    "reason": format!("Invariant violation or exit code masked: {}", res.masked_operators.join(", "))
                                }));
                                std::process::exit(1);
                            }
                        }
                    }
                }
            }

            println!("{}", serde_json::json!({ "decision": "allow" }));
        }
        Commands::PostTool => {
            use std::io::BufRead;
            let mut input = String::new();
            let _ = std::io::stdin().lock().read_line(&mut input);

            if !input.trim().is_empty() {
                if let Ok(val) = serde_json::from_str::<serde_json::Value>(&input) {
                    let tool_call = val.get("toolCall");
                    let tool_name = tool_call
                        .and_then(|t| t.get("name"))
                        .and_then(|n| n.as_str())
                        .unwrap_or_default();
                    let tool_args = tool_call.and_then(|t| t.get("args"));

                    // 1. Check file mutations against G2
                    if matches!(
                        tool_name,
                        "write_to_file" | "replace_file_content" | "Write" | "write" | "Edit" | "edit"
                    ) {
                        let target_file = tool_args
                            .and_then(|a| {
                                a.get("TargetFile")
                                    .or_else(|| a.get("target_file"))
                                    .or_else(|| a.get("file_path"))
                            })
                            .and_then(|f| f.as_str());

                        if let Some(target) = target_file {
                            let req = IpcRequest::RunGate {
                                gate_type: "G2".to_string(),
                                payload: serde_json::json!({
                                    "mutated_files": [target]
                                }),
                            };
                            if let Ok(IpcResponse::GateEvaluated(res)) = client.send_request(&req).await {
                                if !res.passed {
                                    eprintln!("hardtruth-gate post_tool: {}", res.reason);
                                    println!("{}", serde_json::json!({
                                        "decision": "block",
                                        "reason": res.reason
                                    }));
                                    std::process::exit(1);
                                }
                            }
                        }
                    }

                    // 2. Check shell command
                    let cmd = tool_args
                        .and_then(|a| a.get("CommandLine"))
                        .and_then(|c| c.as_str());

                    if let Some(cmd_str) = cmd {
                        if cmd_str.contains("hardtruth-gate") {
                            return Ok(());
                        }

                        if let Ok(IpcResponse::CommandEvaluated(res)) = client.evaluate_command(cmd_str).await {
                            if !res.exit_reachable || !res.allowed {
                                eprintln!("hardtruth-gate post_tool: REJECT invariant or masking operator {}", res.masked_operators.join(", "));
                                println!("{}", serde_json::json!({
                                    "decision": "block",
                                    "reason": format!("Invariant violation or exit code masked: {}", res.masked_operators.join(", "))
                                }));
                                std::process::exit(1);
                            }
                        }
                    }
                }
            }

            // Protojson compatibility: empty stdout on allow
        }
        Commands::Stop => {
            if let Ok(IpcResponse::DaemonStatus { circuit_breaker_tripped, .. }) = client.get_status().await {
                if circuit_breaker_tripped {
                    eprintln!("hardtruth-gate stop: G3 Circuit Breaker is TRIPPED");
                    std::process::exit(1);
                }
            }
            // Protojson compatibility: empty stdout on allow
        }
        Commands::Reset => {
            let req = IpcRequest::ResetCircuitBreaker;
            match client.send_request(&req).await {
                Ok(IpcResponse::CircuitBreakerReset { success }) => {
                    if success {
                        println!("G3 Circuit Breaker reset to HEALTHY");
                    } else {
                        eprintln!("Failed to reset circuit breaker");
                        std::process::exit(1);
                    }
                }
                Ok(other) => println!("{other:?}"),
                Err(e) => {
                    eprintln!("hardtruth-gate reset error: {e}");
                    std::process::exit(1);
                }
            }
        }
    }

    Ok(())
}
