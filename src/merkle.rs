use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use serde::{Deserialize, Serialize};
use crate::key_custody::KeyCustody;

/// A tamper-sealed execution receipt witnessed by hardtruthd
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Receipt {
    pub seq: u64,
    pub timestamp: i64,
    pub claim: String,
    pub command: String,
    pub exit_code: i32,
    pub world_digest: String,
    pub signature: String,
}

/// Verification verdict for a stated claim
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum ClaimStatus {
    /// Receipt exists with exit 0 AND world_digest(receipt) == world_digest(now)
    Verified {
        seq: u64,
        receipt: Receipt,
    },
    /// Receipt exists with exit 0 BUT world_digest diverged since receipt
    Stale {
        seq: u64,
        receipt_digest: String,
        current_digest: String,
    },
    /// No valid receipt witnessed with exit 0
    Unverified {
        last_seq: Option<u64>,
        reason: String,
    },
}

/// Merkle world-state engine tracking files and dependency hashes with Blake3
#[derive(Debug, Clone)]
pub struct WorldState {
    root_dir: PathBuf,
}

impl WorldState {
    pub fn new(root_dir: impl AsRef<Path>) -> Self {
        Self {
            root_dir: root_dir.as_ref().to_path_buf(),
        }
    }

    /// Compute Blake3 hash of a single file
    pub fn hash_file(path: impl AsRef<Path>) -> io::Result<String> {
        let bytes = fs::read(path)?;
        let hash = blake3::hash(&bytes);
        Ok(hash.to_hex().to_string())
    }

    /// Recursively scan root directory and compute the root Blake3 Merkle world digest
    pub fn compute_world_digest(&self) -> io::Result<String> {
        let mut file_map = BTreeMap::new();
        self.collect_files(&self.root_dir, &mut file_map)?;

        // Hash the sorted list of (relative_path, file_hash)
        let mut hasher = blake3::Hasher::new();
        for (rel_path, hash) in file_map {
            hasher.update(rel_path.as_bytes());
            hasher.update(b":");
            hasher.update(hash.as_bytes());
            hasher.update(b"\n");
        }

        Ok(hasher.finalize().to_hex().to_string())
    }

    fn collect_files(&self, current: &Path, map: &mut BTreeMap<String, String>) -> io::Result<()> {
        if !current.exists() {
            return Ok(());
        }

        if current.is_file() {
            let rel = current.strip_prefix(&self.root_dir)
                .unwrap_or(current)
                .to_string_lossy()
                .to_string();
            let h = Self::hash_file(current)?;
            map.insert(rel, h);
            return Ok(());
        }

        for entry in fs::read_dir(current)? {
            let entry = entry?;
            if entry.file_type()?.is_symlink() {
                continue;
            }
            let path = entry.path();
            let name = entry.file_name();
            let name_str = name.to_string_lossy();

            // Skip transient / build / OS dirs
            if name_str.starts_with('.')
                || name_str == "target"
                || name_str == "node_modules"
                || name_str == "dist"
                || name_str == "build"
                || name_str == "Library"
                || name_str == "Applications"
                || name_str == "Pictures"
                || name_str == "Movies"
                || name_str == "Music"
                || name_str == "Downloads"
            {
                continue;
            }

            if path.is_dir() {
                self.collect_files(&path, map)?;
            } else if path.is_file() {
                let rel = path.strip_prefix(&self.root_dir)
                    .unwrap_or(&path)
                    .to_string_lossy()
                    .to_string();
                let h = Self::hash_file(&path)?;
                map.insert(rel, h);
            }
        }

        Ok(())
    }
}

/// Ledger recording tamper-sealed execution receipts
pub struct ReceiptLedger {
    receipts: Vec<Receipt>,
    seq_counter: u64,
}

impl ReceiptLedger {
    pub fn new() -> Self {
        Self {
            receipts: Vec::new(),
            seq_counter: 0,
        }
    }

    /// Record and sign an execution receipt
    pub fn record_receipt(
        &mut self,
        claim: impl Into<String>,
        command: impl Into<String>,
        exit_code: i32,
        world_digest: impl Into<String>,
        key: &KeyCustody,
    ) -> Receipt {
        self.seq_counter += 1;
        let seq = self.seq_counter;
        let timestamp = chrono::Utc::now().timestamp();
        let claim = claim.into();
        let command = command.into();
        let world_digest = world_digest.into();

        let payload = format!("{seq}:{timestamp}:{claim}:{command}:{exit_code}:{world_digest}");
        let signature = key.sign(payload.as_bytes());

        let receipt = Receipt {
            seq,
            timestamp,
            claim,
            command,
            exit_code,
            world_digest,
            signature,
        };

        self.receipts.push(receipt.clone());
        receipt
    }

    /// Retrieve the most recent receipt matching a claim pattern
    pub fn latest_receipt_for_claim(&self, claim_pattern: &str) -> Option<&Receipt> {
        let norm_pattern = claim_pattern.trim().to_lowercase();
        self.receipts.iter().rev().find(|r| {
            let norm_claim = r.claim.trim().to_lowercase();
            norm_claim.contains(&norm_pattern) || norm_pattern.contains(&norm_claim)
        })
    }

    /// Verify a claim against the current world digest
    pub fn verify_claim(&self, claim: &str, current_world_digest: &str) -> ClaimStatus {
        match self.latest_receipt_for_claim(claim) {
            Some(receipt) => {
                if receipt.exit_code != 0 {
                    ClaimStatus::Unverified {
                        last_seq: Some(receipt.seq),
                        reason: format!("Last receipt exited with failure status {}", receipt.exit_code),
                    }
                } else if receipt.world_digest == current_world_digest {
                    ClaimStatus::Verified {
                        seq: receipt.seq,
                        receipt: receipt.clone(),
                    }
                } else {
                    ClaimStatus::Stale {
                        seq: receipt.seq,
                        receipt_digest: receipt.world_digest.clone(),
                        current_digest: current_world_digest.to_string(),
                    }
                }
            }
            None => ClaimStatus::Unverified {
                last_seq: None,
                reason: "No receipt witnessed in ledger for claim".to_string(),
            },
        }
    }

    /// Return all receipts
    pub fn all_receipts(&self) -> &[Receipt] {
        &self.receipts
    }

    /// Explicit hard deletion path for records to support data retention and erasure obligations
    pub fn delete_receipt(&mut self, seq: u64) -> bool {
        if let Some(pos) = self.receipts.iter().position(|r| r.seq == seq) {
            self.receipts.remove(pos);
            true
        } else {
            false
        }
    }

    /// Return count of receipts
    pub fn count(&self) -> usize {
        self.receipts.len()
    }
}

impl Default for ReceiptLedger {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_world_digest_deterministic() {
        let dir = tempdir().unwrap();
        let file_a = dir.path().join("a.rs");
        let file_b = dir.path().join("b.rs");

        fs::write(&file_a, b"fn a() {}").unwrap();
        fs::write(&file_b, b"fn b() {}").unwrap();

        let state = WorldState::new(dir.path());
        let digest1 = state.compute_world_digest().unwrap();
        let digest2 = state.compute_world_digest().unwrap();
        assert_eq!(digest1, digest2);

        // Mutating a file changes the world digest
        fs::write(&file_a, b"fn a_mutated() {}").unwrap();
        let digest3 = state.compute_world_digest().unwrap();
        assert_ne!(digest1, digest3);
    }

    #[test]
    fn test_receipt_ledger_claim_verification() {
        let key = KeyCustody::new_random();
        let mut ledger = ReceiptLedger::new();
        let world_digest_v1 = "digest_abc123";

        // Claim unverified initially
        let status = ledger.verify_claim("all tests pass", world_digest_v1);
        assert!(matches!(status, ClaimStatus::Unverified { .. }));

        // Record successful test receipt
        let r = ledger.record_receipt("all tests pass", "cargo test", 0, world_digest_v1, &key);
        assert_eq!(r.seq, 1);

        // Now verified
        let status = ledger.verify_claim("all tests pass", world_digest_v1);
        assert!(matches!(status, ClaimStatus::Verified { seq: 1, .. }));

        // World state mutates -> Stale!
        let world_digest_v2 = "digest_xyz789";
        let status = ledger.verify_claim("all tests pass", world_digest_v2);
        match status {
            ClaimStatus::Stale { seq, receipt_digest, current_digest } => {
                assert_eq!(seq, 1);
                assert_eq!(receipt_digest, world_digest_v1);
                assert_eq!(current_digest, world_digest_v2);
            }
            _ => panic!("Expected Stale claim status"),
        }
    }
}
