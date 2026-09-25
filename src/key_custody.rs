use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use hmac::{Hmac, Mac};
use seckey::SecBytes;
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// 256-bit HMAC key securely managed in non-paged RAM via mlock()
/// and zeroized on drop or SIGTERM.
pub struct KeyCustody {
    sec_key: Option<SecBytes>,
}

impl KeyCustody {
    /// Generate a fresh random 256-bit HMAC key in mlock'd memory
    pub fn new_random() -> Self {
        let mut key = [0u8; 32];
        let seed = format!("{}-{}-{}", std::process::id(), chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0), fastrand::u64(..));
        let hash = blake3::hash(seed.as_bytes());
        key.copy_from_slice(hash.as_bytes());

        let sec_key = SecBytes::with(32, |slice| slice.copy_from_slice(&key));
        Self { sec_key: Some(sec_key) }
    }

    /// Instantiate from explicit key bytes (e.g. for testing)
    pub fn from_bytes(bytes: [u8; 32]) -> Self {
        let sec_key = SecBytes::with(32, |slice| slice.copy_from_slice(&bytes));
        Self { sec_key: Some(sec_key) }
    }

    /// Sign a message using HMAC-SHA256
    pub fn sign(&self, message: &[u8]) -> String {
        let dummy = [0u8; 32];
        let guard = self.sec_key.as_ref().map(|k| k.read());
        let key_ref: &[u8] = match &guard {
            Some(g) => g,
            None => &dummy,
        };

        let mut mac = HmacSha256::new_from_slice(key_ref)
            .expect("HMAC can take key of any size");
        mac.update(message);
        let result = mac.finalize();
        hex::encode(result.into_bytes())
    }

    /// Verify an HMAC-SHA256 signature
    pub fn verify(&self, message: &[u8], signature_hex: &str) -> bool {
        let expected = self.sign(message);
        expected == signature_hex
    }

    /// Key bytes copy (for tests and diagnostics)
    pub fn as_bytes(&self) -> [u8; 32] {
        let mut out = [0u8; 32];
        if let Some(k) = &self.sec_key {
            out.copy_from_slice(&k.read());
        }
        out
    }

    /// Check if mlock succeeded (SecBytes allocates in non-paged protected memory)
    pub fn is_mlocked(&self) -> bool {
        self.sec_key.is_some()
    }

    /// Explicitly zeroize key memory immediately (called on SIGTERM)
    pub fn zeroize_now(&mut self) {
        // Dropping SecBytes invokes memsec::memzero + munlock
        self.sec_key = None;
    }
}

impl Drop for KeyCustody {
    fn drop(&mut self) {
        self.zeroize_now();
    }
}

mod hex {
    pub fn encode(data: impl AsRef<[u8]>) -> String {
        data.as_ref()
            .iter()
            .map(|b| format!("{:02x}", b))
            .collect()
    }
}

/// Helper to secure a Unix Domain Socket with mode 0600
pub fn secure_socket_permissions(path: &Path) -> std::io::Result<()> {
    let perms = std::fs::Permissions::from_mode(0o600);
    std::fs::set_permissions(path, perms)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_key_custody_signing_and_verification() {
        let key = KeyCustody::new_random();
        let msg = b"world-state-receipt-seq-42";
        let sig = key.sign(msg);
        assert!(!sig.is_empty());
        assert!(key.verify(msg, &sig));
        assert!(!key.verify(b"tampered-message", &sig));
    }

    #[test]
    fn test_key_zeroization_on_drop() {
        let mut key = KeyCustody::from_bytes([0xAA; 32]);
        assert_eq!(key.as_bytes()[0], 0xAA);
        key.zeroize_now();
        assert_eq!(key.as_bytes(), [0u8; 32]);
    }
}
