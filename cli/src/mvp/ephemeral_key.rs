//! Ephemeral `sk-stratoclave-*` key minting / revoking, scope-parameterized.
//!
//! Both the `claude` and `codex` wrapper subcommands need the same
//! "spawn a child process holding a single-purpose API key" pattern, but
//! with different scopes (`messages:send` vs `responses:send`). The
//! per-scope minting was previously inlined in `claude_cmd.rs`; lifting
//! it here keeps the security-critical request shape (ephemeral=true,
//! 30-min TTL, scope subset) authored exactly once.
//!
//! Backend contract (see `backend/mvp/me_api_keys.py`):
//!
//!   POST /api/mvp/me/api-keys
//!     body  { name, scopes:[…], ephemeral:true, expires_in_minutes }
//!     auth  Bearer <Cognito access_token>
//!     200   { key_id, plaintext_key, scopes, expires_at }
//!
//!   DELETE /api/mvp/me/api-keys/by-key-id/{key_id}
//!     auth  Bearer <Cognito access_token>
//!     204   — also accepts 404 (the key already TTL'd or was revoked).
//!
//! The plaintext key is shown exactly once and only ever passed to the
//! child via env (`ANTHROPIC_API_KEY` or `STRATOCLAVE_OPENAI_KEY`); the
//! Cognito bearer is never exported into the child environment.

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};

#[derive(Serialize)]
struct CreateKeyRequest<'a> {
    name: &'a str,
    scopes: &'a [&'a str],
    ephemeral: bool,
    expires_in_minutes: u32,
}

#[derive(Deserialize)]
pub struct CreateKeyResponse {
    pub key_id: String,
    pub plaintext_key: String,
    #[allow(dead_code)]
    pub scopes: Vec<String>,
    #[allow(dead_code)]
    pub expires_at: Option<String>,
}

// Manual Debug that REDACTS the plaintext key (Fable security review M2): the
// derived Debug would print the live key on any stray `{:?}` in error/tracing
// paths. Everything else is safe to show.
impl std::fmt::Debug for CreateKeyResponse {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CreateKeyResponse")
            .field("key_id", &self.key_id)
            .field("plaintext_key", &"<redacted>")
            .field("scopes", &self.scopes)
            .field("expires_at", &self.expires_at)
            .finish()
    }
}

const DEFAULT_TTL_MINUTES: u32 = 30;

fn http_client() -> Result<reqwest::Client> {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()
        .context("Failed to build HTTP client")
}

/// Mint an ephemeral, scope-narrowed API key for a wrapper child process.
///
/// `name` must be unique-ish (e.g. "stratoclave-claude-wrapper") so the
/// audit log can attribute usage. `scopes` must be a subset of the
/// caller's role permissions; `_resolve_scopes` in the backend rejects
/// requests that escalate.
pub async fn mint_ephemeral_key_scoped(
    base_url: &str,
    bearer: &str,
    name: &str,
    scopes: &[&str],
) -> Result<CreateKeyResponse> {
    if scopes.is_empty() {
        return Err(anyhow!(
            "mint_ephemeral_key_scoped called with empty scopes; refusing"
        ));
    }
    let url = format!(
        "{}/api/mvp/me/api-keys",
        base_url.trim_end_matches('/')
    );
    let body = CreateKeyRequest {
        name,
        scopes,
        ephemeral: true,
        expires_in_minutes: DEFAULT_TTL_MINUTES,
    };
    let resp = http_client()?
        .post(&url)
        .bearer_auth(bearer)
        .json(&body)
        .send()
        .await
        .context("Failed to POST /api/mvp/me/api-keys for wrapper key")?;
    if !resp.status().is_success() {
        let status = resp.status();
        let err_body = resp.text().await.unwrap_or_default();
        return Err(anyhow!(
            "Failed to mint ephemeral wrapper key (HTTP {}): {}",
            status,
            err_body
        ));
    }
    resp.json::<CreateKeyResponse>()
        .await
        .context("Failed to parse wrapper-key response")
}

/// Revoke an ephemeral key by its `key_id`. Treats HTTP 404 as success
/// because the backend's TTL TTLs the key out independently of this call,
/// and a revoke that races with TTL expiry is the expected steady state.
pub async fn revoke_ephemeral_key(
    base_url: &str,
    bearer: &str,
    key_id: &str,
) -> Result<()> {
    let url = format!(
        "{}/api/mvp/me/api-keys/by-key-id/{}",
        base_url.trim_end_matches('/'),
        urlencoding::encode(key_id)
    );
    let resp = http_client()?
        .delete(&url)
        .bearer_auth(bearer)
        .send()
        .await
        .context("Failed to DELETE /api/mvp/me/api-keys/by-key-id")?;
    let status = resp.status();
    if !status.is_success() && status.as_u16() != 404 {
        let err_body = resp.text().await.unwrap_or_default();
        return Err(anyhow!(
            "wrapper key revoke returned HTTP {}: {}",
            status,
            err_body
        ));
    }
    Ok(())
}
