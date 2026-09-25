use std::path::{Path, PathBuf};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use crate::daemon::default_socket_path;
use crate::protocol::{IpcRequest, IpcResponse};

/// High-speed client connecting to hardtruthd daemon over UDS with sub-2ms latency
pub struct GateClient {
    socket_path: PathBuf,
}

impl GateClient {
    pub fn new(socket_path: Option<PathBuf>) -> Self {
        Self {
            socket_path: socket_path.unwrap_or_else(default_socket_path),
        }
    }

    /// Send a request to the resident daemon over Unix Domain Socket
    pub async fn send_request(&self, req: &IpcRequest) -> Result<IpcResponse, Box<dyn std::error::Error>> {
        if !self.socket_path.exists() {
            return Err(format!(
                "hardtruthd daemon socket not found at {}. Is hardtruthd running?",
                self.socket_path.display()
            ).into());
        }

        let mut stream = UnixStream::connect(&self.socket_path).await?;
        let req_bytes = serde_json::to_vec(req)?;

        stream.write_all(&req_bytes).await?;
        stream.write_all(b"\n").await?;
        stream.flush().await?;

        let mut buffer = Vec::new();
        let mut chunk = [0u8; 4096];

        loop {
            let n = stream.read(&mut chunk).await?;
            if n == 0 {
                break;
            }
            buffer.extend_from_slice(&chunk[..n]);
            if serde_json::from_slice::<IpcResponse>(&buffer).is_ok() {
                break;
            }
        }

        let resp: IpcResponse = serde_json::from_slice(&buffer)?;
        Ok(resp)
    }

    /// Ping the daemon
    pub async fn ping(&self) -> Result<bool, Box<dyn std::error::Error>> {
        match self.send_request(&IpcRequest::Ping).await? {
            IpcResponse::Pong { .. } => Ok(true),
            _ => Ok(false),
        }
    }

    /// Verify a claim against the current world digest
    pub async fn verify_claim(
        &self,
        claim: &str,
        world_root: Option<impl AsRef<Path>>,
    ) -> Result<IpcResponse, Box<dyn std::error::Error>> {
        let req = IpcRequest::VerifyClaim {
            claim: claim.to_string(),
            world_root: world_root.map(|p| p.as_ref().to_string_lossy().to_string()),
        };
        self.send_request(&req).await
    }

    /// Record a verified execution receipt
    pub async fn record_receipt(
        &self,
        claim: &str,
        command: &str,
        exit_code: i32,
        world_root: Option<impl AsRef<Path>>,
    ) -> Result<IpcResponse, Box<dyn std::error::Error>> {
        let req = IpcRequest::RecordReceipt {
            claim: claim.to_string(),
            command: command.to_string(),
            exit_code,
            world_root: world_root.map(|p| p.as_ref().to_string_lossy().to_string()),
        };
        self.send_request(&req).await
    }

    /// Evaluate command exit reachability
    pub async fn evaluate_command(&self, command: &str) -> Result<IpcResponse, Box<dyn std::error::Error>> {
        let req = IpcRequest::EvaluateCommand {
            command: command.to_string(),
        };
        self.send_request(&req).await
    }

    /// Rewrite context text in-place
    pub async fn rewrite_context(
        &self,
        text: &str,
        world_root: Option<impl AsRef<Path>>,
    ) -> Result<IpcResponse, Box<dyn std::error::Error>> {
        let req = IpcRequest::RewriteContext {
            text: text.to_string(),
            world_root: world_root.map(|p| p.as_ref().to_string_lossy().to_string()),
        };
        self.send_request(&req).await
    }

    /// Query daemon status
    pub async fn get_status(&self) -> Result<IpcResponse, Box<dyn std::error::Error>> {
        self.send_request(&IpcRequest::GetStatus).await
    }
}
