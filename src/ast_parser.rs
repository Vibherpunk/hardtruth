use std::io::Cursor;
use brush_parser::ParserOptions;
use serde::{Deserialize, Serialize};

/// Result of evaluating a shell command's exit code reachability
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AstReachabilityResult {
    /// Whether any non-zero exit code of the underlying command will reach the caller
    pub exit_reachable: bool,
    /// Specific exit-code masking operators detected
    pub masked_operators: Vec<String>,
    /// Whether this command is safe to execute for receipt witnessing
    pub allowed: bool,
    /// Explanation of findings
    pub diagnostics: String,
}

/// Shell AST parser and exit reachability evaluator
pub struct ShellAstEvaluator;

impl ShellAstEvaluator {
    /// Evaluate a shell command string for exit masking operators
    pub fn evaluate(command: &str) -> AstReachabilityResult {
        let trimmed = command.trim();
        let mut masked_operators = Vec::new();

        // 1. Formal AST parse using brush_parser
        let reader = Cursor::new(trimmed.as_bytes());
        let options = ParserOptions::default();
        let mut parser = brush_parser::Parser::new(reader, &options);

        let ast_parsed = match parser.parse_program() {
            Ok(prog) => {
                Self::inspect_ast(&prog, &mut masked_operators);
                true
            }
            Err(_) => false,
        };

        // 2. Exact syntactic pattern inspection (catches compound and pipeline masking)
        Self::inspect_tokens(trimmed, &mut masked_operators);

        masked_operators.dedup();

        let exit_reachable = masked_operators.is_empty();
        let allowed = exit_reachable;

        let diagnostics = if exit_reachable {
            if ast_parsed {
                "AST verified clean: exit code reachability preserved with zero masking operators".to_string()
            } else {
                "Command verified: no exit masking operators detected".to_string()
            }
        } else {
            format!(
                "AST violation: exit code masked by operators: {}",
                masked_operators.join(", ")
            )
        };

        AstReachabilityResult {
            exit_reachable,
            masked_operators,
            allowed,
            diagnostics,
        }
    }

    fn inspect_ast(prog: &brush_parser::ast::Program, masks: &mut Vec<String>) {
        for cmd in &prog.complete_commands {
            for item in &cmd.0 {
                // item.0 is AndOrList
                for and_or in &item.0.additional {
                    match and_or {
                        brush_parser::ast::AndOr::Or(pipeline) => {
                            let pipe_str = format!("{pipeline}");
                            let trimmed_pipe = pipe_str.trim();
                            if trimmed_pipe == "true"
                                || trimmed_pipe == ":"
                                || trimmed_pipe == "exit 0"
                                || trimmed_pipe.starts_with("exit 0")
                                || trimmed_pipe.contains("echo")
                            {
                                masks.push(format!("|| {trimmed_pipe}"));
                            }
                        }
                        brush_parser::ast::AndOr::And(_) => {}
                    }
                }
            }
        }
    }

    fn inspect_tokens(cmd: &str, masks: &mut Vec<String>) {
        let norm = cmd.replace('\n', " ");

        // Masking patterns:
        // 1. ; true or ; :
        let semi_true = regex::Regex::new(r";\s*(?:true|:)\s*(?:;|$)").unwrap();
        if semi_true.is_match(&norm) {
            masks.push("; true".to_string());
        }

        // 2. || true or || :
        let or_true = regex::Regex::new(r"\|\|\s*(?:true|:)\s*(?:;|$)").unwrap();
        if or_true.is_match(&norm) {
            masks.push("|| true".to_string());
        }

        // 3. || exit 0
        let or_exit_zero = regex::Regex::new(r"\|\|\s*exit\s+0\s*(?:;|$)").unwrap();
        if or_exit_zero.is_match(&norm) {
            masks.push("|| exit 0".to_string());
        }

        // 4. ; exit 0 at the end of a command sequence
        let semi_exit_zero = regex::Regex::new(r";\s*exit\s+0\s*$").unwrap();
        if semi_exit_zero.is_match(&norm) {
            masks.push("; exit 0".to_string());
        }

        // 5. Pipe into cat or true without pipefail
        let pipe_mask = regex::Regex::new(r"\|\s*(?:cat|true|grep|sed)\b").unwrap();
        if pipe_mask.is_match(&norm) && !norm.contains("pipefail") {
            masks.push("| masking_pipeline_without_pipefail".to_string());
        }

        // 6. Disabling errexit: set +e
        if norm.contains("set +e") {
            masks.push("set +e".to_string());
        }

        // 7. Forbidden Port 8000 invariant: Never probe, bind, or modify port 8000
        let port_8000 = regex::Regex::new(r"(?::8000\b|\b8000\b)").unwrap();
        if port_8000.is_match(&norm) {
            masks.push("PORT_8000_FORBIDDEN".to_string());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_clean_commands_reachability() {
        let res1 = ShellAstEvaluator::evaluate("cargo test");
        assert!(res1.exit_reachable);
        assert!(res1.allowed);
        assert!(res1.masked_operators.is_empty());

        let res2 = ShellAstEvaluator::evaluate("cargo check && cargo build --release");
        assert!(res2.exit_reachable);
        assert!(res2.allowed);
    }

    #[test]
    fn test_masking_operators_detected() {
        // Test ; true
        let res1 = ShellAstEvaluator::evaluate("npm test; true");
        assert!(!res1.exit_reachable);
        assert!(!res1.allowed);
        assert!(res1.masked_operators.iter().any(|m| m.contains("true")));

        // Test || exit 0
        let res2 = ShellAstEvaluator::evaluate("cargo test || exit 0");
        assert!(!res2.exit_reachable);
        assert!(!res2.allowed);
        assert!(res2.masked_operators.iter().any(|m| m.contains("exit 0")));

        // Test || true
        let res3 = ShellAstEvaluator::evaluate("pytest || true");
        assert!(!res3.exit_reachable);
        assert!(!res3.allowed);

        // Test pipe masking
        let res4 = ShellAstEvaluator::evaluate("cargo test 2>&1 | cat");
        assert!(!res4.exit_reachable);
        assert!(!res4.allowed);
    }

    #[test]
    fn test_forbidden_port_8000_detected() {
        let res1 = ShellAstEvaluator::evaluate("curl http://127.0.0.1:8000");
        assert!(!res1.exit_reachable);
        assert!(!res1.allowed);
        assert!(res1.masked_operators.iter().any(|m| m == "PORT_8000_FORBIDDEN"));

        let res2 = ShellAstEvaluator::evaluate("nc -zv 127.0.0.1 8000");
        assert!(!res2.exit_reachable);
        assert!(!res2.allowed);
        assert!(res2.masked_operators.iter().any(|m| m == "PORT_8000_FORBIDDEN"));
    }
}
