use clap::Parser;
use std::path::PathBuf;
use hardtruth::daemon::HardtruthDaemon;

#[derive(Parser, Debug)]
#[command(name = "hardtruthd")]
#[command(author = "Adam Matar <adam@skidnir.io>")]
#[command(version = "0.2.0")]
#[command(about = "High-performance resident anti-hallucination and integrity daemon", long_about = None)]
struct Args {
    /// Custom path for the Unix Domain Socket (default: ~/Library/Caches/skidnir/hardtruth.sock)
    #[arg(short, long)]
    socket: Option<PathBuf>,

    /// Root directory of the repository to witness and track
    #[arg(short, long)]
    root: Option<PathBuf>,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();
    println!("Starting hardtruthd resident anti-hallucination daemon v0.2.0...");

    let daemon = HardtruthDaemon::new(args.socket, args.root);
    daemon.run().await
}
