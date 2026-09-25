use std::env;
use hardtruth::daemon::HardtruthDaemon;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = env::args().collect();
    let prog_name = args.first().cloned().unwrap_or_default();

    if prog_name.ends_with("hardtruth-gate") || args.get(1).map(|s| s.as_str()) == Some("gate") {
        eprintln!("Direct hardtruth-gate CLI invocation. Use `hardtruth-gate` binary for CLI.");
    }

    println!("Starting hardtruthd resident anti-hallucination daemon v0.2.0...");
    let daemon = HardtruthDaemon::new(None, None);
    daemon.run().await
}
