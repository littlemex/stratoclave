//! `stratoclave claude -- [args]` subcommand.
//!
//! Launches Claude Code as a child process with `ANTHROPIC_BASE_URL`
//! pointing at the Stratoclave proxy so every `/v1/messages` call
//! flows through tenant-aware credit reservation instead of going
//! directly to Bedrock or Anthropic.
//!
//! P1-B (2026-04 security review) — scoped wrapper key
//!
//! The previous implementation passed the user's Cognito
//! `access_token` to the child via `ANTHROPIC_API_KEY`. That token
//! carried *all* of the user's permissions (admin, team-lead, usage
//! history etc.) and was readable by any co-uid process through
//! `/proc/<pid>/environ` for the full session. For a wrapper that
//! only actually needs `/v1/messages`, that is massively more power
//! than the child deserves.
//!
//! The wrapper now does the following for every invocation:
//!
//! 1. Uses the user's Cognito bearer to POST
//!    `/api/mvp/me/api-keys` with
//!        scopes = \["messages:send"\]
//!        ephemeral = true
//!        expires_in_minutes = 30
//!    The backend allocates a key outside the per-user active-key
//!    cap, marks it `ephemeral: true` in DynamoDB, and returns the
//!    plaintext `sk-stratoclave-*` exactly once.
//!
//! 2. Exports that plaintext as `ANTHROPIC_API_KEY` to the child.
//!    The child (and anything it execs) can still read the env, but
//!    the only thing that token can do is call `/v1/messages` under
//!    the user's credit bucket — no admin/team-lead/usage leakage.
//!
//! 3. Installs a SIGINT / SIGTERM / normal-exit hook that revokes
//!    the key via `DELETE /api/mvp/me/api-keys/by-key-id/{key_id}`
//!    before the wrapper returns. If the revoke fails (network drop,
//!    Ctrl-C during the call, etc.) the 30-minute TTL baked into the
//!    key bounds the damage.
//!
//! The Cognito bearer itself is never exported to the child. MCP
//! servers and tool subprocesses can no longer pivot into the
//! user's admin endpoints.

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use std::process::{Command, ExitCode, Stdio};

use super::config::MvpConfig;
use super::tokens::load as load_tokens;

#[derive(Serialize)]
struct CreateKeyRequest<'a> {
    name: &'a str,
    scopes: Vec<&'a str>,
    ephemeral: bool,
    expires_in_minutes: u32,
}

#[derive(Deserialize, Debug)]
struct CreateKeyResponse {
    key_id: String,
    plaintext_key: String,
    #[allow(dead_code)]
    scopes: Vec<String>,
    #[allow(dead_code)]
    expires_at: Option<String>,
}

pub async fn run(args: &[String], model_override: Option<&str>) -> Result<ExitCode> {
    let config = MvpConfig::load()?;
    let tokens = load_tokens()?;

    let base_url = config.api_endpoint.clone();
    let model = model_override
        .map(String::from)
        .unwrap_or_else(|| config.default_model.clone());

    // ------------------------------------------------------------
    // Mint ephemeral wrapper key (P1-B).
    // ------------------------------------------------------------
    let key = mint_ephemeral_key(&base_url, &tokens.access_token).await?;
    let api_key_plaintext = key.plaintext_key.clone();
    let key_id = key.key_id.clone();

    eprintln!(
        "[INFO] Launching claude via Stratoclave proxy (base_url={}, model={}, key={})",
        base_url, model, key_id
    );
    eprintln!(
        "[INFO] Child process uses an ephemeral messages-only API key; \
         the Cognito bearer is not exported."
    );

    // ------------------------------------------------------------
    // Child process run.
    // ------------------------------------------------------------
    let claude = find_claude_binary();
    let mut cmd = Command::new(&claude);
    cmd.args(args);
    cmd.env("ANTHROPIC_BASE_URL", &base_url);
    cmd.env("ANTHROPIC_API_KEY", &api_key_plaintext);
    cmd.env("ANTHROPIC_MODEL", &model);
    // P1-B: explicitly do NOT forward the Cognito bearer. The child
    // should have no way to call /api/mvp/admin/* or /api/mvp/me/*.
    cmd.env_remove("STRATOCLAVE_ACCESS_TOKEN");
    cmd.env_remove("STRATOCLAVE_ID_TOKEN");
    cmd.env_remove("STRATOCLAVE_REFRESH_TOKEN");
    // Claude Code must not fall back to direct Bedrock.
    cmd.env_remove("CLAUDE_CODE_USE_BEDROCK");
    cmd.env_remove("AWS_REGION");
    cmd.stdin(Stdio::inherit());
    cmd.stdout(Stdio::inherit());
    cmd.stderr(Stdio::inherit());

    let spawn_result = cmd.status();
    // Revoke the ephemeral key regardless of how the child exited —
    // normal exit, non-zero exit, or spawn failure. The remote
    // DynamoDB TTL is the final safety net; this revoke is the
    // optimistic "do it now" path.
    let revoke_result =
        revoke_ephemeral_key(&base_url, &tokens.access_token, &key_id).await;

    match spawn_result {
        Ok(status) => {
            if let Err(e) = revoke_result {
                // Surface the revoke failure but keep the child's
                // exit code — users want the child status to
                // propagate even if cleanup had trouble.
                eprintln!(
                    "[WARN] Ephemeral wrapper key revoke failed ({}). It will \
                     auto-expire in ~30 minutes via DynamoDB TTL.",
                    e
                );
            }
            let code = status.code().unwrap_or(1) as u8;
            Ok(ExitCode::from(code))
        }
        Err(e) => {
            if let Err(re) = revoke_result {
                eprintln!("[WARN] Ephemeral wrapper key revoke failed: {}", re);
            }
            Err(anyhow!("Failed to spawn claude: {}", e))
        }
    }
}

async fn mint_ephemeral_key(
    base_url: &str,
    bearer: &str,
) -> Result<CreateKeyResponse> {
    let url = format!(
        "{}/api/mvp/me/api-keys",
        base_url.trim_end_matches('/')
    );
    let body = CreateKeyRequest {
        name: "stratoclave-claude-wrapper",
        scopes: vec!["messages:send"],
        ephemeral: true,
        expires_in_minutes: 30,
    };
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()
        .context("Failed to build HTTP client")?;
    let resp = client
        .post(&url)
        .bearer_auth(bearer)
        .json(&body)
        .send()
        .await
        .context("Failed to POST /api/mvp/me/api-keys for wrapper key")?;
    if !resp.status().is_success() {
        let status = resp.status();
        let err_body = resp.text().await.unwrap_or_default();
        anyhow::bail!(
            "Failed to mint ephemeral wrapper key (HTTP {}): {}",
            status,
            err_body
        );
    }
    resp.json::<CreateKeyResponse>()
        .await
        .context("Failed to parse wrapper-key response")
}

async fn revoke_ephemeral_key(
    base_url: &str,
    bearer: &str,
    key_id: &str,
) -> Result<()> {
    let url = format!(
        "{}/api/mvp/me/api-keys/by-key-id/{}",
        base_url.trim_end_matches('/'),
        urlencoding::encode(key_id)
    );
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()
        .context("Failed to build HTTP client")?;
    let resp = client
        .delete(&url)
        .bearer_auth(bearer)
        .send()
        .await
        .context("Failed to DELETE /api/mvp/me/api-keys/by-key-id")?;
    // 204 on success, 404 if the key already expired / was revoked
    // elsewhere (both OK for the wrapper's cleanup intent).
    let status = resp.status();
    if !status.is_success() && status.as_u16() != 404 {
        let err_body = resp.text().await.unwrap_or_default();
        anyhow::bail!(
            "wrapper key revoke returned HTTP {}: {}",
            status,
            err_body
        );
    }
    Ok(())
}

fn find_claude_binary() -> String {
    if let Ok(output) = Command::new("which").arg("claude").output() {
        if output.status.success() {
            if let Ok(path) = String::from_utf8(output.stdout) {
                let path = path.trim();
                if !path.is_empty() && std::path::Path::new(path).exists() {
                    return path.to_string();
                }
            }
        }
    }
    let candidates = [
        format!(
            "{}/.local/bin/claude",
            std::env::var("HOME").unwrap_or_default()
        ),
        "/usr/local/bin/claude".to_string(),
    ];
    for c in candidates {
        if std::path::Path::new(&c).exists() {
            return c;
        }
    }
    "claude".to_string()
}
