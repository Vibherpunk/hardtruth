#![allow(clippy::collapsible_if)]
#![allow(clippy::useless_format)]
#![allow(clippy::unnecessary_lazy_evaluations)]
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::io::{self, Read};
use std::process::Command;
use std::time::Duration;

#[derive(Serialize, Deserialize, Debug)]
struct HookPayload {
    #[serde(alias = "toolCall")]
    tool_call: Option<ToolCall>,
    #[serde(alias = "conversationId", alias = "session_id")]
    conversation_id: Option<String>,
    #[serde(alias = "workspace_dir", alias = "cwd")]
    workspace: Option<String>,
}

#[derive(Serialize, Deserialize, Debug)]
struct ToolCall {
    name: Option<String>,
    args: Option<Value>,
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let mode = if args.len() > 1 { args[1].as_str() } else { "stop" };

    let mut raw_in = String::new();
    let _ = io::stdin().read_to_string(&mut raw_in);

    let payload: HookPayload = serde_json::from_str(&raw_in).unwrap_or_else(|_| HookPayload {
        tool_call: None,
        conversation_id: None,
        workspace: None,
    });

    let result = match mode {
        "post_tool" | "PostToolUse" => handle_post_tool(&payload),
        "stop" => handle_stop(&payload),
        _ => halt("Unknown mode"),
    };

    println!("{}", serde_json::to_string(&result).unwrap());
}

fn handle_post_tool(payload: &HookPayload) -> Value {
    if let Some(tool) = &payload.tool_call {
        if let Some(name) = &tool.name {
            if name == "run_command" || name == "Bash" {
                if let Some(args) = &tool.args {
                    if let Some(cmd) = args.get("CommandLine").and_then(|v| v.as_str()) {
                        if cmd.contains("; true") || cmd.contains("|| exit 0") || cmd.contains("&& true") {
                            return halt("TAINTED: Chained shell operators detected. Exit-code masking operators are forbidden.");
                        }
                    }
                }
            }
        }
    }
    
    let cwd = payload.workspace.clone().unwrap_or_else(|| std::env::current_dir().unwrap().to_string_lossy().to_string());
    if let Some(err) = check_ast_stubs(&cwd) {
        return halt(&err);
    }

    json!({})
}

fn handle_stop(payload: &HookPayload) -> Value {
    let cwd = payload.workspace.clone().unwrap_or_else(|| std::env::current_dir().unwrap().to_string_lossy().to_string());
    
    if let Some(err) = check_ast_stubs(&cwd) {
        return halt(&err);
    }
    
    let daemon_url = "http://127.0.0.1:49281/evaluate";
    
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_millis(500)))
        .build()
        .into();
        
    let req: ureq::Agent = agent;
    
    let result = req.post(daemon_url)
        .send_json(json!({
            "premise": "Execution ledger",
            "hypothesis": "Agent completed the task",
            "threshold": 0.70
        }));
        
    match result {
        Ok(mut resp) => {
            if let Ok(body_str) = resp.body_mut().read_to_string() {
                if let Ok(body) = serde_json::from_str::<Value>(&body_str) {
                    if let Some(contradiction) = body.get("contradiction").and_then(|v| v.as_f64()) {
                        if contradiction >= 0.70 {
                            return halt(&format!("🚨 HARDTRUTH ENGINE CONTRADICTION DETECTED (conf: {:.2}): Claim contradicts the execution ledger.", contradiction));
                        }
                    }
                }
            }
            allow()
        },
        Err(_) => {
            // Graceful fail-secure fallback: check if manual tests were run
            if has_manual_verification(payload.conversation_id.as_deref().unwrap_or("")) {
                allow()
            } else {
                halt("🚨 HARDTRUTH DAEMON UNREACHABLE: Verifier at http://127.0.0.1:49281 is offline. You must provide manual verification output before completing.")
            }
        }
    }
}

fn has_manual_verification(conv_id: &str) -> bool {
    let ledger_path = std::path::PathBuf::from(std::env::var("HOME").unwrap_or_default())
        .join(".hardtruth/daemon_ledger.jsonl");
        
    if let Ok(content) = fs::read_to_string(&ledger_path) {
        for line in content.lines().rev() {
            if let Ok(entry) = serde_json::from_str::<Value>(line) {
                if entry.get("conversationId").and_then(|v| v.as_str()) == Some(conv_id) {
                    if let Some(tool) = entry.get("tool").and_then(|v| v.as_str()) {
                        if tool == "run_command" || tool == "Bash" {
                            if let Some(target) = entry.get("target").and_then(|v| v.as_str()) {
                                if target.contains("cargo test") || target.contains("cargo check") || target.contains("pytest") {
                                    if entry.get("status").and_then(|v| v.as_str()) == Some("success") {
                                        return true;
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    
    // If not found, return false (fail-secure)
    false
}

fn check_ast_stubs(cwd: &str) -> Option<String> {
    let output = Command::new("git")
        .args(["status", "--porcelain"])
        .current_dir(cwd)
        .output()
        .ok()?;
        
    let status = String::from_utf8_lossy(&output.stdout);
    let code_extensions = [".rs", ".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".zig", ".ml", ".c", ".cpp"];
    for line in status.lines() {
        if line.len() > 3 {
            let file_path = line[3..].trim();
            let is_code = code_extensions.iter().any(|ext| file_path.ends_with(ext));
            if !is_code {
                continue;
            }
            let full_path = format!("{}/{}", cwd, file_path);
            if let Ok(content) = fs::read_to_string(&full_path) {
                let s_todo = format!("{}odo!()", "t");
                let s_unimpl = format!("{}nimplemented!()", "u");
                let s_todo_comment = format!("// TODO");
                
                let has_todo_macro = file_path.ends_with(".rs") && (content.contains(&s_todo) || content.contains(&s_unimpl));
                let has_todo_comment = content.contains(&s_todo_comment);
                let has_py_pass = file_path.ends_with(".py") && content.lines().any(|l| l.trim() == "pass");

                if has_todo_macro || has_todo_comment || has_py_pass {
                    return Some(format!("🚨 HARDTRUTH STUB DETECTED in {}: No stubs allowed.", file_path));
                }
            }
        }
    }
    
    None
}

fn halt(reason: &str) -> Value {
    json!({
        "decision": "continue",
        "reason": reason
    })
}

fn allow() -> Value {
    json!({
        "decision": "allow"
    })
}
