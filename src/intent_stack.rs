use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::Instant;
use regex::Regex;
use serde::{Deserialize, Serialize};

pub const FRONTIER_PLANNING_VIOLATION: &str = "REJECT: Frontier Planning Invariant Violation: Flash models and Gemini 3.1 Pro are strictly forbidden from acting as lead architect or planner. Must use a frontier model (Claude Opus 5.5, Kimi Pro, Mimo Pro, DeepSeek Pro).";

/// Violation result for a channel check
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum IntentViolation {
    ChannelA { rule: String, detail: String },
    ChannelB { unapproved_path: String, allowed_closure_size: usize },
    ChannelC { tokens_requested: u64, tokens_available: u64 },
    Rule5 { unapproved_path: String, detail: String },
    PlannerModel { model: String, detail: String },
}

/// Evaluation outcome from the 3-Channel Intent Stack
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IntentStackResult {
    pub allowed: bool,
    pub violations: Vec<IntentViolation>,
    pub tokens_remaining: u64,
    pub closure_size: usize,
}

/// 3-Channel Intent Stack
pub struct IntentStack {
    // Channel A: Negative Invariants
    forbidden_ports: HashSet<u16>,
    spend_budget_cents: u64,
    spend_cumulative_cents: u64,

    // Channel B: Artifact Closure Set
    artifact_closure: HashSet<PathBuf>,

    // Channel C: Token Bucket
    bucket_capacity: u64,
    available_tokens: f64,
    refill_rate_per_sec: f64,
    last_refill: Instant,

    // Rule 5: VibeHard compilation context & session ticket
    pub vibehard_context: bool,
    pub session_ticket: Option<String>,
}

impl IntentStack {
    pub fn new(initial_closure: impl IntoIterator<Item = impl AsRef<Path>>) -> Self {
        let mut forbidden_ports = HashSet::new();
        forbidden_ports.insert(8000); // HARD INVARIANT: Port 8000 is strictly reserved for Reachy Mini hardware

        let mut closure = HashSet::new();
        for p in initial_closure {
            closure.insert(Self::normalize_path(p.as_ref()));
        }

        Self {
            forbidden_ports,
            spend_budget_cents: 5000, // $50.00 default cap
            spend_cumulative_cents: 0,
            artifact_closure: closure,
            bucket_capacity: 100,
            available_tokens: 100.0,
            refill_rate_per_sec: 1.0, // 1 token per second
            last_refill: Instant::now(),
            vibehard_context: false,
            session_ticket: None,
        }
    }

    /// Channel A: Evaluate negative invariants (Port 8000, spend, critical tamper protection)
    pub fn check_channel_a(
        &mut self,
        ports: &[u16],
        additional_cost_cents: u64,
        target_paths: &[impl AsRef<Path>],
    ) -> Vec<IntentViolation> {
        let mut violations = Vec::new();

        // 1. Port invariant
        for &port in ports {
            if self.forbidden_ports.contains(&port) {
                violations.push(IntentViolation::ChannelA {
                    rule: "PORT_8000_FORBIDDEN".to_string(),
                    detail: format!(
                        "PORT {port} IS STRICTLY FORBIDDEN (reserved for Reachy Mini hardware)"
                    ),
                });
            }
        }

        // 2. Spend limit
        if self.spend_cumulative_cents + additional_cost_cents > self.spend_budget_cents {
            violations.push(IntentViolation::ChannelA {
                rule: "SPEND_LIMIT_EXCEEDED".to_string(),
                detail: format!(
                    "Spend would reach {} cents, exceeding budget of {} cents",
                    self.spend_cumulative_cents + additional_cost_cents,
                    self.spend_budget_cents
                ),
            });
        } else {
            self.spend_cumulative_cents += additional_cost_cents;
        }

        // 3. Prohibited tamper paths
        for path in target_paths {
            let p_str = path.as_ref().to_string_lossy();
            if p_str.contains(".gate/HARD_VERIFY_PASS") || p_str.contains("hardtruth.sock") {
                violations.push(IntentViolation::ChannelA {
                    rule: "TAMPER_PROTECTION".to_string(),
                    detail: format!("Direct modification of protected security artifact {p_str} is forbidden"),
                });
            }
        }

        violations
    }

    /// Check if a path is exempt from Channel B closure lockdown:
    /// - Scratch files: `.gemini/`, `/tmp/`, `scratch/`
    /// - Test files: `tests/`
    /// - Configuration files: `config/`, `.json`, `.yaml`, `.yml`, `.toml`
    pub fn is_channel_b_exempt(path: &Path) -> bool {
        let p_str = path.to_string_lossy().replace('\\', "/");

        // 1. Scratch files (.gemini/, /tmp/, scratch/)
        if p_str.contains(".gemini/")
            || p_str.starts_with("/tmp/")
            || p_str.starts_with("tmp/")
            || p_str.contains("/tmp/")
            || p_str.starts_with("scratch/")
            || p_str.contains("/scratch/")
            || path.components().any(|c| {
                let s = c.as_os_str().to_string_lossy();
                s == ".gemini" || s == "tmp" || s == "scratch"
            })
        {
            return true;
        }

        // 2. Test files (tests/)
        if p_str.starts_with("tests/")
            || p_str.contains("/tests/")
            || path.components().any(|c| c.as_os_str().to_string_lossy() == "tests")
        {
            return true;
        }

        // 3. Configuration files (config/, .json, .yaml, .yml, .toml)
        if p_str.starts_with("config/")
            || p_str.contains("/config/")
            || path.components().any(|c| c.as_os_str().to_string_lossy() == "config")
        {
            return true;
        }

        if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
            let ext_lower = ext.to_lowercase();
            if matches!(ext_lower.as_str(), "json" | "yaml" | "yml" | "toml") {
                return true;
            }
        }

        false
    }

    /// Channel B: Validate that mutation targets belong to the current artifact closure set
    pub fn check_channel_b(&self, target_paths: &[impl AsRef<Path>]) -> Vec<IntentViolation> {
        let mut violations = Vec::new();

        // If closure is empty, allow bootstrap
        if self.artifact_closure.is_empty() {
            return violations;
        }

        for path in target_paths {
            let p = path.as_ref();
            if Self::is_channel_b_exempt(p) || Self::is_tracked_in_git(p) {
                continue;
            }

            let norm = Self::normalize_path(p);
            if !self.artifact_closure.contains(&norm) {
                // Check if any closure path is a parent directory
                let in_parent = self.artifact_closure.iter().any(|c| norm.starts_with(c));
                if !in_parent {
                    violations.push(IntentViolation::ChannelB {
                        unapproved_path: norm.to_string_lossy().to_string(),
                        allowed_closure_size: self.artifact_closure.len(),
                    });
                }
            }
        }

        violations
    }

    /// Channel B: Automatically expand artifact closure set from compiler errors & stack traces
    pub fn expand_closure_from_error_trace(&mut self, trace: &str) -> Vec<PathBuf> {
        let mut newly_added = Vec::new();

        // Pattern 1: rustc `--> src/foo.rs:12:5`
        let rustc_re = Regex::new(r"-->\s+([^\s:]+\.[a-zA-Z0-9]+):\d+").unwrap();
        for cap in rustc_re.captures_iter(trace) {
            if let Some(m) = cap.get(1) {
                let p = Self::normalize_path(Path::new(m.as_str()));
                if self.artifact_closure.insert(p.clone()) {
                    newly_added.push(p);
                }
            }
        }

        // Pattern 2: typescript/node `at ... (path/to/file.ts:12:3)`
        let ts_re = Regex::new(r"\((/[^\s:]+\.[a-zA-Z0-9]+):\d+:\d+\)").unwrap();
        for cap in ts_re.captures_iter(trace) {
            if let Some(m) = cap.get(1) {
                let p = Self::normalize_path(Path::new(m.as_str()));
                if self.artifact_closure.insert(p.clone()) {
                    newly_added.push(p);
                }
            }
        }

        // Pattern 3: python `File "path/to/file.py", line 12`
        let py_re = Regex::new(r#"File "([^"]+\.[a-zA-Z0-9]+)", line \d+"#).unwrap();
        for cap in py_re.captures_iter(trace) {
            if let Some(m) = cap.get(1) {
                let p = Self::normalize_path(Path::new(m.as_str()));
                if self.artifact_closure.insert(p.clone()) {
                    newly_added.push(p);
                }
            }
        }

        newly_added
    }

    /// Channel C: Consume tokens from the token bucket bounding unguided exploration
    pub fn check_channel_c(&mut self, tokens_to_consume: u64) -> Vec<IntentViolation> {
        self.refill_bucket();
        let mut violations = Vec::new();

        if (tokens_to_consume as f64) > self.available_tokens {
            violations.push(IntentViolation::ChannelC {
                tokens_requested: tokens_to_consume,
                tokens_available: self.available_tokens.floor() as u64,
            });
        } else {
            self.available_tokens -= tokens_to_consume as f64;
        }

        violations
    }

    fn refill_bucket(&mut self) {
        let now = Instant::now();
        let elapsed_secs = now.duration_since(self.last_refill).as_secs_f64();
        self.last_refill = now;

        self.available_tokens = (self.available_tokens + elapsed_secs * self.refill_rate_per_sec)
            .min(self.bucket_capacity as f64);
    }

    pub fn set_vibehard_context(&mut self, active: bool) {
        self.vibehard_context = active;
    }

    pub fn set_session_ticket(&mut self, ticket: Option<String>) {
        self.session_ticket = ticket;
    }

    pub fn has_ticket_in_parents(path: &Path) -> bool {
        let mut curr = if path.is_dir() {
            Some(path)
        } else {
            path.parent()
        };

        while let Some(dir) = curr {
            if dir.as_os_str().is_empty() {
                break;
            }
            let candidate = dir.join(".gate/session_ticket");
            if candidate.exists() {
                if let Ok(metadata) = candidate.metadata() {
                    if metadata.len() > 0 {
                        return true;
                    }
                }
            }
            curr = dir.parent();
        }

        false
    }

    pub fn has_valid_context(&self) -> bool {
        if self.vibehard_context || self.session_ticket.is_some() {
            return true;
        }

        if std::env::var("VIBEHARD_BUILD_ACTIVE").map(|v| v == "1").unwrap_or(false)
            || std::env::var("VIBEHARD_COMPILATION_CONTEXT").is_ok()
            || std::env::var("VIBEHARD_SESSION_TICKET").is_ok()
        {
            return true;
        }

        let candidate_ticket_paths = [
            Path::new(".gate/session_ticket"),
            Path::new(".gate/ticket"),
            Path::new(".gate/session.json"),
            Path::new(".gate/compilation_ticket.json"),
        ];

        for tp in &candidate_ticket_paths {
            if tp.exists() {
                if let Ok(metadata) = tp.metadata() {
                    if metadata.len() > 0 {
                        return true;
                    }
                }
            }
        }

        let agency_ticket = Path::new("/Users/ai/rooms/n8n Agency/.gate/session_ticket");
        let agency_ticket_valid = agency_ticket.exists()
            && agency_ticket.metadata().map(|m| m.len() > 0).unwrap_or(false);

        if agency_ticket_valid {
            if let Ok(cwd) = std::env::current_dir() {
                let cwd_str = cwd.to_string_lossy();
                if cwd_str.contains("/Users/ai/rooms/n8n Agency") || cwd_str.starts_with("/Users/ai/rooms/n8n Agency") {
                    return true;
                }
            }
            for p in &self.artifact_closure {
                let p_str = p.to_string_lossy();
                if p_str.contains("/Users/ai/rooms/n8n Agency") || p_str.starts_with("/Users/ai/rooms/n8n Agency") {
                    return true;
                }
            }
        }

        false
    }

    pub fn has_valid_context_for_path(&self, path: &Path) -> bool {
        if self.has_valid_context() {
            return true;
        }

        let agency_ticket = Path::new("/Users/ai/rooms/n8n Agency/.gate/session_ticket");
        let agency_ticket_valid = agency_ticket.exists()
            && agency_ticket.metadata().map(|m| m.len() > 0).unwrap_or(false);

        let p_str = path.to_string_lossy();
        if agency_ticket_valid && (p_str.contains("/Users/ai/rooms/n8n Agency") || p_str.starts_with("/Users/ai/rooms/n8n Agency")) {
            return true;
        }

        Self::has_ticket_in_parents(path)
    }

    pub fn has_script_extension(path: &Path) -> bool {
        if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
            let ext_lower = ext.to_lowercase();
            matches!(
                ext_lower.as_str(),
                "js" | "ts" | "py" | "sh" | "rs" | "jsx" | "tsx" | "mjs" | "cjs"
            )
        } else {
            false
        }
    }

    pub fn is_script_or_service_path(path: &Path) -> bool {
        if !Self::has_script_extension(path) {
            return false;
        }

        let p_str = path.to_string_lossy().replace('\\', "/");
        let norm = p_str.trim_start_matches("./");

        norm.starts_with("services/")
            || norm.starts_with("apps/")
            || norm.starts_with("scripts/")
            || norm.contains("/services/")
            || norm.contains("/apps/")
            || norm.contains("/scripts/")
            || path.components().any(|c| {
                let s = c.as_os_str().to_string_lossy();
                s == "services" || s == "apps" || s == "scripts"
            })
    }

    pub fn is_tracked_in_git(path: &Path) -> bool {
        let parent_dir = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        let filename = path.file_name().unwrap_or_else(|| path.as_os_str());

        let output = std::process::Command::new("git")
            .arg("-C")
            .arg(parent_dir)
            .args(["ls-files", "--error-unmatch"])
            .arg(filename)
            .output();
        match output {
            Ok(out) => out.status.success(),
            Err(_) => false,
        }
    }

    /// Check if a mutation attempts to create a new script or service in services/, apps/, or scripts/
    pub fn is_new_script_or_service(&self, path: &Path) -> bool {
        if Self::is_channel_b_exempt(path) {
            return false;
        }

        if !Self::is_script_or_service_path(path) {
            return false;
        }

        // If the path does not exist on disk, it is definitely a new script/service creation
        if !path.exists() {
            return true;
        }

        // If it exists on disk, check if it was pre-existing / tracked in git.
        // If it's untracked, it was synthesized without prior source control tracking.
        !Self::is_tracked_in_git(path)
    }

    /// Enforce Rule 5: If a mutation attempts to create a new script or service in
    /// `services/`, `apps/`, or `scripts/`, it MUST have a valid VibeHard compilation
    /// context or `.gate/` session ticket.
    pub fn check_rule_5(&self, target_paths: &[impl AsRef<Path>]) -> Option<String> {
        if self.has_valid_context() {
            return None;
        }

        for path in target_paths {
            let p = path.as_ref();
            if self.has_valid_context_for_path(p) {
                continue;
            }
            if self.is_new_script_or_service(p) {
                return Some(
                    "REJECT: Rule 5 Violation: Direct script synthesis in services/ or apps/ is forbidden. All software synthesis must run through VibeHard (bun src/cli.ts build) or be compiled as a declarative n8n DAG.".to_string(),
                );
            }
        }

        None
    }

    pub fn check_rule_5_violations(&self, target_paths: &[impl AsRef<Path>]) -> Vec<IntentViolation> {
        let mut violations = Vec::new();
        if self.has_valid_context() {
            return violations;
        }

        for path in target_paths {
            let p = path.as_ref();
            if self.has_valid_context_for_path(p) {
                continue;
            }
            if self.is_new_script_or_service(p) {
                violations.push(IntentViolation::Rule5 {
                    unapproved_path: p.to_string_lossy().to_string(),
                    detail: "REJECT: Rule 5 Violation: Direct script synthesis in services/ or apps/ is forbidden. All software synthesis must run through VibeHard (bun src/cli.ts build) or be compiled as a declarative n8n DAG.".to_string(),
                });
            }
        }

        violations
    }

    /// Check if a model string violates the Lead Architect & Planner policy
    pub fn is_forbidden_planner_model(model: &str) -> bool {
        let lower = model.to_lowercase();
        lower.contains("flash") || lower.contains("gemini-3.1-pro") || lower.contains("gemini-3.8-flash")
    }

    /// Check if a planner model is forbidden under the Frontier Planning Invariant
    pub fn check_planner_model(&self, model: &str) -> Option<IntentViolation> {
        if Self::is_forbidden_planner_model(model) {
            Some(IntentViolation::PlannerModel {
                model: model.to_string(),
                detail: FRONTIER_PLANNING_VIOLATION.to_string(),
            })
        } else {
            None
        }
    }

    /// Plan-phase evaluation: evaluates Channel A, Channel C, and Rule 5 invariants for a proposed plan
    pub fn evaluate_plan(
        &mut self,
        ports: &[u16],
        additional_cost_cents: u64,
        target_paths: &[impl AsRef<Path>],
        tokens_to_consume: u64,
    ) -> IntentStackResult {
        let mut violations = Vec::new();

        violations.extend(self.check_channel_a(ports, additional_cost_cents, target_paths));
        violations.extend(self.check_channel_c(tokens_to_consume));
        violations.extend(self.check_rule_5_violations(target_paths));

        IntentStackResult {
            allowed: violations.is_empty(),
            violations,
            tokens_remaining: self.available_tokens.floor() as u64,
            closure_size: self.artifact_closure.len(),
        }
    }

    /// Unified evaluation across all 3 channels + Rule 5 lock
    pub fn evaluate(
        &mut self,
        ports: &[u16],
        additional_cost_cents: u64,
        target_paths: &[impl AsRef<Path>],
        tokens_to_consume: u64,
    ) -> IntentStackResult {
        let mut violations = Vec::new();

        violations.extend(self.check_channel_a(ports, additional_cost_cents, target_paths));
        violations.extend(self.check_channel_b(target_paths));
        violations.extend(self.check_channel_c(tokens_to_consume));
        violations.extend(self.check_rule_5_violations(target_paths));

        IntentStackResult {
            allowed: violations.is_empty(),
            violations,
            tokens_remaining: self.available_tokens.floor() as u64,
            closure_size: self.artifact_closure.len(),
        }
    }

    /// Add a path manually to the closure
    pub fn add_to_closure(&mut self, path: impl AsRef<Path>) {
        self.artifact_closure.insert(Self::normalize_path(path.as_ref()));
    }

    /// Current closure set
    pub fn closure(&self) -> &HashSet<PathBuf> {
        &self.artifact_closure
    }

    fn normalize_path(path: &Path) -> PathBuf {
        let mut norm = PathBuf::new();
        for comp in path.components() {
            match comp {
                std::path::Component::CurDir => {}
                _ => norm.push(comp),
            }
        }
        norm
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_channel_a_forbidden_port_8000() {
        let mut stack = IntentStack::new(vec!["src/main.rs"]);
        let res = stack.evaluate(&[8000], 0, &["src/main.rs"], 1);
        assert!(!res.allowed);
        assert!(res.violations.iter().any(|v| match v {
            IntentViolation::ChannelA { rule, .. } => rule == "PORT_8000_FORBIDDEN",
            _ => false,
        }));
    }

    #[test]
    fn test_channel_b_artifact_closure_and_expansion() {
        let mut stack = IntentStack::new(vec!["src/lib.rs"]);

        // Mutating unapproved file fails
        let res1 = stack.evaluate(&[8080], 0, &["src/unapproved.rs"], 1);
        assert!(!res1.allowed);

        // Trace expansion adds new files
        let trace = r#"
error[E0425]: cannot find value in this scope
  --> src/unapproved.rs:4:5
        "#;
        let added = stack.expand_closure_from_error_trace(trace);
        assert_eq!(added.len(), 1);

        // Now mutation is allowed
        let res2 = stack.evaluate(&[8080], 0, &["src/unapproved.rs"], 1);
        assert!(res2.allowed);
    }

    #[test]
    fn test_channel_c_token_bucket() {
        let mut stack = IntentStack::new(vec!["src/main.rs"]);
        // Attempting to consume more than bucket capacity
        let res = stack.evaluate(&[3000], 0, &["src/main.rs"], 500);
        assert!(!res.allowed);
        assert!(res.violations.iter().any(|v| matches!(v, IntentViolation::ChannelC { .. })));
    }

    #[test]
    fn test_rule_5_rejects_unverified_script_creation() {
        let mut stack = IntentStack::new(vec!["services/payment_worker.py"]);
        let res = stack.evaluate(&[3000], 0, &["services/payment_worker.py"], 1);
        assert!(!res.allowed);
        assert!(res.violations.iter().any(|v| matches!(v, IntentViolation::Rule5 { .. })));
    }

    #[test]
    fn test_rule_5_allows_script_creation_with_vibehard_context() {
        let mut stack = IntentStack::new(vec!["services/payment_worker.py"]);
        stack.set_vibehard_context(true);
        let res = stack.evaluate(&[3000], 0, &["services/payment_worker.py"], 1);
        assert!(res.allowed);
    }

    #[test]
    fn test_rule_5_allows_script_creation_with_session_ticket() {
        let mut stack = IntentStack::new(vec!["apps/my_worker.rs"]);
        stack.set_session_ticket(Some("ticket-abc-123".to_string()));
        let res = stack.evaluate(&[3000], 0, &["apps/my_worker.rs"], 1);
        assert!(res.allowed);
    }

    #[test]
    fn test_forbidden_planner_model_detection() {
        assert!(IntentStack::is_forbidden_planner_model("gemini-3.8-flash"));
        assert!(IntentStack::is_forbidden_planner_model("gemini-3.8-flash-high"));
        assert!(IntentStack::is_forbidden_planner_model("gemini-3.1-pro"));
        assert!(IntentStack::is_forbidden_planner_model("flash"));
        assert!(IntentStack::is_forbidden_planner_model("gemini-flash-1.5"));
        assert!(IntentStack::is_forbidden_planner_model("deepseek-v4-flash"));

        assert!(!IntentStack::is_forbidden_planner_model("claude-opus-5.5"));
        assert!(!IntentStack::is_forbidden_planner_model("deepseek-v4-pro"));
        assert!(!IntentStack::is_forbidden_planner_model("kimi-k3"));
        assert!(!IntentStack::is_forbidden_planner_model("mimo-pro"));
        assert!(!IntentStack::is_forbidden_planner_model("gpt-5.6-terra"));
    }

    #[test]
    fn test_evaluate_plan_invariants() {
        let mut stack = IntentStack::new(vec!["src/main.rs"]);
        // Valid plan with new files not in closure yet
        let res = stack.evaluate_plan(&[3000], 0, &["new_feature.rs", "docs.md"], 1);
        assert!(res.allowed);

        // Plan violating Port 8000
        let res_bad_port = stack.evaluate_plan(&[8000], 0, &["new_feature.rs"], 1);
        assert!(!res_bad_port.allowed);
    }

    #[test]
    fn test_is_tracked_in_git_resolution() {
        // Enclosing repo tracking test for local files
        assert!(IntentStack::is_tracked_in_git(Path::new("src/lib.rs")));
        assert!(!IntentStack::is_tracked_in_git(Path::new("src/completely_untracked_nonexistent.xyz")));
    }

    #[test]
    fn test_channel_b_exemptions() {
        // Scratch files
        assert!(IntentStack::is_channel_b_exempt(Path::new("/Users/ai/.gemini/antigravity/brain/test/scratch/test.js")));
        assert!(IntentStack::is_channel_b_exempt(Path::new("/tmp/test.py")));
        assert!(IntentStack::is_channel_b_exempt(Path::new("scratch/scratch_script.js")));

        // Test files
        assert!(IntentStack::is_channel_b_exempt(Path::new("tests/integration_test.rs")));
        assert!(IntentStack::is_channel_b_exempt(Path::new("src/tests/subtest.rs")));

        // Config files
        assert!(IntentStack::is_channel_b_exempt(Path::new("config/daemon.toml")));
        assert!(IntentStack::is_channel_b_exempt(Path::new("settings.json")));
        assert!(IntentStack::is_channel_b_exempt(Path::new("compose.yaml")));
        assert!(IntentStack::is_channel_b_exempt(Path::new("pipeline.yml")));
        assert!(IntentStack::is_channel_b_exempt(Path::new("Cargo.toml")));

        // Non-exempt normal source file
        assert!(!IntentStack::is_channel_b_exempt(Path::new("src/normal_untracked.rs")));

        // Stack evaluation with closure should allow exempt files
        let mut stack = IntentStack::new(vec!["src/main.rs"]);
        let res = stack.evaluate(&[3000], 0, &["/Users/ai/.gemini/antigravity/brain/test/scratch/test.js", "config/test.json"], 1);
        assert!(res.allowed);
    }

    #[test]
    fn test_has_valid_context_env_var() {
        let stack = IntentStack::new(vec!["src/main.rs"]);
        std::env::set_var("VIBEHARD_BUILD_ACTIVE", "1");
        assert!(stack.has_valid_context());
        std::env::remove_var("VIBEHARD_BUILD_ACTIVE");
    }

    #[test]
    fn test_has_valid_context_for_path_in_n8n_agency() {
        let stack = IntentStack::new(vec!["src/main.rs"]);
        let agency_path = Path::new("/Users/ai/rooms/n8n Agency/services/billing_catalog.js");
        if Path::new("/Users/ai/rooms/n8n Agency/.gate/session_ticket").exists() {
            assert!(stack.has_valid_context_for_path(agency_path));
        }
    }
}
