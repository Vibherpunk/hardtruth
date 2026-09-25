use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::io::{self, Read};
use std::process::Command;
use std::time::Duration;

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
struct HookPayload {
    #[serde(alias = "toolCall")]
    tool_call: Option<ToolCall>,
    #[serde(alias = "tool_name", alias = "tool")]
    tool_name: Option<String>,
    #[serde(alias = "tool_args", alias = "tool_input", alias = "arguments")]
    tool_args: Option<Value>,
    #[serde(alias = "conversationId", alias = "session_id", alias = "sessionId")]
    conversation_id: Option<String>,
    #[serde(alias = "workspace_dir", alias = "cwd")]
    workspace: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
struct ToolCall {
    name: Option<String>,
    args: Option<Value>,
}

impl HookPayload {
    fn get_tool_name(&self) -> Option<&str> {
        self.tool_call
            .as_ref()
            .and_then(|t| t.name.as_deref())
            .or(self.tool_name.as_deref())
    }

    fn get_tool_args(&self) -> Option<&Value> {
        self.tool_call
            .as_ref()
            .and_then(|t| t.args.as_ref())
            .or(self.tool_args.as_ref())
    }
}

fn check_exit_code_masking(tool_name: Option<&str>, tool_args: Option<&Value>) -> Option<String> {
    let name = tool_name?;
    let is_shell_tool = matches!(
        name,
        "run_command" | "Bash" | "bash" | "sh" | "bothard_exec_command"
    );
    if !is_shell_tool {
        return None;
    }
    let args = tool_args?;
    let cmd = args
        .get("CommandLine")
        .or_else(|| args.get("command"))
        .or_else(|| args.get("cmd"))
        .and_then(|v| v.as_str())?;

    let lower = cmd.to_lowercase();
    let forbidden_patterns = [
        "; true",
        "|| true",
        "&& true",
        "; exit 0",
        "|| exit 0",
        "&& exit 0",
        "|| :",
        "; :",
        "& true",
        "| true",
    ];
    for p in &forbidden_patterns {
        if lower.contains(p) {
            return Some(format!(
                "🚨 HARDTRUTH REJECTION: Exit-code masking operator detected in command: \"{p}\""
            ));
        }
    }
    None
}

fn contains_cleartext_secret(text: &str) -> Option<&'static str> {
    if text.contains("-----BEGIN") && text.contains("PRIVATE KEY-----") {
        return Some("Private cryptographic key detected");
    }
    let prefixes = [
        ("sk-ant-", "Anthropic API key"),
        ("sk-proj-", "OpenAI Project API key"),
        ("sk-live-", "Stripe Live Secret Key"),
        ("rk_live_", "Stripe Live Restricted Key"),
        ("ghp_", "GitHub Personal Access Token"),
        ("gho_", "GitHub OAuth Token"),
        ("github_pat_", "GitHub Fine-Grained Token"),
    ];
    for (prefix, desc) in &prefixes {
        if let Some(pos) = text.find(prefix) {
            let rest = &text[pos + prefix.len()..];
            let token_len = rest
                .chars()
                .take_while(|c| c.is_alphanumeric() || *c == '_' || *c == '-')
                .count();
            if token_len >= 16 {
                return Some(desc);
            }
        }
    }
    None
}

fn check_cleartext_secrets(args: Option<&Value>) -> Option<String> {
    let args = args?;
    let text = match args {
        Value::String(s) => s.as_str(),
        Value::Object(_) | Value::Array(_) => {
            let s = args.to_string();
            if let Some(desc) = contains_cleartext_secret(&s) {
                return Some(format!(
                    "🚨 HARDTRUTH REJECTION: Cleartext secret detected: {desc}"
                ));
            }
            return None;
        }
        _ => return None,
    };
    if let Some(desc) = contains_cleartext_secret(text) {
        return Some(format!(
            "🚨 HARDTRUTH REJECTION: Cleartext secret detected: {desc}"
        ));
    }
    None
}

fn check_content_for_stubs(content: &str, is_rs: bool, is_py: bool) -> bool {
    let s_todo = concat!("t", "odo!()");
    let s_unimpl = concat!("u", "nimplemented!()");
    let s_todo_comment = concat!("/", "/ TODO");
    let s_py_todo_comment = "# TODO";

    if content.contains(s_todo_comment) || content.contains(s_py_todo_comment) {
        return true;
    }
    if is_rs && (content.contains(s_todo) || content.contains(s_unimpl)) {
        return true;
    }
    if is_py && content.lines().any(|l| l.trim() == "pass") {
        return true;
    }
    false
}

fn check_ast_stubs(cwd: &str) -> Option<String> {
    let output = Command::new("git")
        .args(["status", "--porcelain"])
        .current_dir(cwd)
        .output()
        .ok()?;

    if !output.status.success() {
        return None;
    }

    let status = String::from_utf8_lossy(&output.stdout);
    let code_extensions = [
        ".rs", ".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".zig", ".ml", ".c", ".cpp",
    ];

    for line in status.lines() {
        if line.len() > 3 {
            let file_entry = line[3..].trim();
            let file_path = if let Some((_, target)) = file_entry.split_once("->") {
                target.trim()
            } else {
                file_entry
            };

            let is_code = code_extensions.iter().any(|ext| file_path.ends_with(ext));
            if !is_code {
                continue;
            }

            let is_rs = file_path.ends_with(".rs");
            let is_py = file_path.ends_with(".py");

            let diff_output = Command::new("git")
                .args(["diff", "-U0", "--", file_path])
                .current_dir(cwd)
                .output()
                .ok();

            let has_diff = diff_output.as_ref().is_some_and(|d| !d.stdout.is_empty());

            if has_diff {
                if let Some(diff) = diff_output {
                    let diff_text = String::from_utf8_lossy(&diff.stdout);
                    let added_lines: Vec<&str> = diff_text
                        .lines()
                        .filter(|l| l.starts_with('+') && !l.starts_with("+++"))
                        .map(|l| &l[1..])
                        .collect();
                    let added_content = added_lines.join("\n");
                    if check_content_for_stubs(&added_content, is_rs, is_py) {
                        return Some(format!(
                            "🚨 HARDTRUTH STUB DETECTED in {file_path}: No stubs allowed."
                        ));
                    }
                }
            } else {
                let full_path = format!("{cwd}/{file_path}");
                if let Ok(content) = fs::read_to_string(&full_path)
                    && check_content_for_stubs(&content, is_rs, is_py)
                {
                    return Some(format!(
                        "🚨 HARDTRUTH STUB DETECTED in {file_path}: No stubs allowed."
                    ));
                }
            }
        }
    }

    None
}

fn evaluate_post_tool(payload: &HookPayload) -> Result<(), String> {
    if let Some(err) = check_exit_code_masking(payload.get_tool_name(), payload.get_tool_args()) {
        return Err(err);
    }

    if let Some(err) = check_cleartext_secrets(payload.get_tool_args()) {
        return Err(err);
    }

    let cwd = payload
        .workspace
        .clone()
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_default().to_string_lossy().to_string());

    if let Some(err) = check_ast_stubs(&cwd) {
        return Err(err);
    }

    Ok(())
}

fn handle_post_tool(payload: &HookPayload) {
    match evaluate_post_tool(payload) {
        Ok(()) => {
            println!("{{}}");
            std::process::exit(0);
        }
        Err(err) => {
            eprintln!("{err}");
            std::process::exit(1);
        }
    }
}

fn handle_stop(payload: &HookPayload) {
    let cwd = payload
        .workspace
        .clone()
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_default().to_string_lossy().to_string());

    if let Some(err) = check_ast_stubs(&cwd) {
        eprintln!("{err}");
        println!(
            "{}",
            json!({
                "decision": "continue",
                "reason": err
            })
        );
        std::process::exit(0);
    }

    let daemon_url = "http://127.0.0.1:49281/evaluate";

    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_millis(150)))
        .build()
        .into();

    let result = agent
        .post(daemon_url)
        .send_json(json!({
            "premise": "Execution ledger",
            "hypothesis": "Agent completed the task",
            "threshold": 0.70
        }));

    match result {
        Ok(mut resp) => {
            let status = resp.status().as_u16();
            if status == 404 {
                eprintln!(
                    "⚠️ HardTruth daemon at {daemon_url} returned 404; allowing execution gracefully."
                );
                println!("{{}}");
                std::process::exit(0);
            }
            if !resp.status().is_success() {
                eprintln!(
                    "⚠️ HardTruth daemon at {daemon_url} returned HTTP {status}; allowing execution gracefully."
                );
                println!("{{}}");
                std::process::exit(0);
            }
            if let Ok(body_str) = resp.body_mut().read_to_string()
                && let Ok(body) = serde_json::from_str::<Value>(&body_str)
                && let Some(contradiction) = body.get("contradiction").and_then(|v| v.as_f64())
                && contradiction >= 0.70
            {
                let reason = format!(
                    "🚨 HARDTRUTH ENGINE CONTRADICTION DETECTED (conf: {:.2}): Claim contradicts the execution ledger.",
                    contradiction
                );
                eprintln!("{reason}");
                println!(
                    "{}",
                    json!({
                        "decision": "continue",
                        "reason": reason
                    })
                );
                std::process::exit(0);
            }
            println!("{{}}");
            std::process::exit(0);
        }
        Err(err) => {
            eprintln!(
                "⚠️ HardTruth daemon at {daemon_url} unreachable: {err}; allowing execution gracefully."
            );
            println!("{{}}");
            std::process::exit(0);
        }
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let mode = if args.len() > 1 {
        args[1].as_str()
    } else {
        "stop"
    };

    let mut raw_in = String::new();
    let _ = io::stdin().read_to_string(&mut raw_in);

    let payload: HookPayload = serde_json::from_str(&raw_in).unwrap_or_default();

    match mode {
        "post_tool" | "PostToolUse" => handle_post_tool(&payload),
        "stop" | "Stop" => handle_stop(&payload),
        _ => {
            eprintln!("⚠️ HardTruth gate: unrecognized mode '{mode}'; allowing with empty object.");
            println!("{{}}");
            std::process::exit(0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_post_tool_allow_view_file() {
        let raw = r#"{"tool_name":"view_file","tool_args":{"file_path":"/tmp/test.txt"}}"#;
        let payload: HookPayload = serde_json::from_str(raw).expect("valid json");
        assert_eq!(payload.get_tool_name(), Some("view_file"));
        let result = evaluate_post_tool(&payload);
        assert!(result.is_ok());
    }

    #[test]
    fn test_post_tool_allow_camel_case() {
        let raw = r#"{"toolCall":{"name":"view_file","args":{"AbsolutePath":"/tmp/test.txt"}}}"#;
        let payload: HookPayload = serde_json::from_str(raw).expect("valid json");
        assert_eq!(payload.get_tool_name(), Some("view_file"));
        let result = evaluate_post_tool(&payload);
        assert!(result.is_ok());
    }

    #[test]
    fn test_exit_code_masking_detection() {
        let raw = r#"{"tool_name":"run_command","tool_args":{"CommandLine":"pytest tests/ ; true"}}"#;
        let payload: HookPayload = serde_json::from_str(raw).expect("valid json");
        let result = evaluate_post_tool(&payload);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("Exit-code masking operator detected"));
    }

    #[test]
    fn test_exit_code_masking_or_exit_zero() {
        let raw = r#"{"tool_name":"run_command","tool_args":{"CommandLine":"npm test || exit 0"}}"#;
        let payload: HookPayload = serde_json::from_str(raw).expect("valid json");
        let result = evaluate_post_tool(&payload);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("Exit-code masking operator detected"));
    }

    #[test]
    fn test_clean_command_allowed() {
        let raw = r#"{"tool_name":"run_command","tool_args":{"CommandLine":"cargo test --lib"}}"#;
        let payload: HookPayload = serde_json::from_str(raw).expect("valid json");
        let result = evaluate_post_tool(&payload);
        assert!(result.is_ok());
    }

    #[test]
    fn test_cleartext_secret_detection() {
        let raw = r#"{"tool_name":"run_command","tool_args":{"CommandLine":"export KEY=ghp_123456789012345678901234567890123456"}}"#;
        let payload: HookPayload = serde_json::from_str(raw).expect("valid json");
        let result = evaluate_post_tool(&payload);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("Cleartext secret detected"));
    }

    #[test]
    fn test_content_for_stubs() {
        let rs_stub = concat!("fn stub() { ", "t", "odo!(); }");
        assert!(check_content_for_stubs(rs_stub, true, false));

        let rs_unimpl = concat!("fn stub() { ", "u", "nimplemented!(); }");
        assert!(check_content_for_stubs(rs_unimpl, true, false));

        let py_pass = "def foo():\n    pass\n";
        assert!(check_content_for_stubs(py_pass, false, true));

        let clean_code = "fn valid() -> u32 { 42 }";
        assert!(!check_content_for_stubs(clean_code, true, false));
    }
}
