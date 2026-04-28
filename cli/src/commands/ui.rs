//! UI command — open the Stratoclave web UI with a single-use handoff ticket.
//!
//! P0-8 follow-up (2026-04 security review):
//! The previous implementation appended `?token=<access_token>` to the
//! URL. That turned every `stratoclave ui open` into a session-fixation
//! primitive — anyone who could lure a user to
//! `https://…/?token=<attacker-jwt>` pinned the victim's SPA to the
//! attacker's identity. The SPA now refuses `?token=` entirely.
//!
//! The replacement flow is a single-use opaque ticket:
//!   1. CLI POSTs its tokens to `/api/mvp/auth/ui-ticket` (authenticated
//!      with the current bearer). The backend binds the tokens to a
//!      fresh 256-bit CSPRNG nonce (hashed) and returns the plaintext.
//!   2. CLI opens `https://<host>/?ui_ticket=<plaintext>`.
//!   3. The SPA POSTs the plaintext to
//!      `/api/mvp/auth/ui-ticket/consume`. The backend atomically
//!      deletes the record (single-use) and returns the bundled
//!      tokens.  The SPA writes them into sessionStorage and strips the
//!      `ui_ticket` query parameter before any DOM frame sees it.
//!
//! Ticket TTL is 30 s, enforced by DynamoDB; a lost plaintext becomes
//! a 404 almost immediately.

use anyhow::{bail, Context, Result};
use clap::Subcommand;
use serde::{Deserialize, Serialize};

use crate::config::AppConfig;
use crate::mvp::config::MvpConfig;
use crate::mvp::tokens;

#[derive(Subcommand, Debug)]
pub enum UiCommand {
    /// Open the UI in the default browser
    Open,
    /// Print the URL (with a fresh handoff ticket) instead of opening the browser
    Url,
}

#[derive(Serialize)]
struct MintRequest<'a> {
    access_token: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    id_token: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    refresh_token: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    expires_in: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    token_type: Option<&'a str>,
}

#[derive(Deserialize, Debug)]
struct MintResponse {
    ticket: String,
    #[allow(dead_code)]
    expires_at: u64,
    #[allow(dead_code)]
    expires_in: u64,
}

pub async fn run(cmd: UiCommand, config: &AppConfig) -> Result<()> {
    let saved = tokens::load().context(
        "`stratoclave auth login` を実行してからもう一度 `stratoclave ui open` を試してください",
    )?;
    let access_token = saved.access_token.clone();
    if access_token.is_empty() {
        bail!("アクセストークンが空です。再度 `stratoclave auth login` を実行してください");
    }

    // 期限切れなら明示エラー (Frontend 側でも検出するが CLI で先に気付けるよう)
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    if saved.expires_at > 0 && saved.expires_at <= now {
        bail!(
            "アクセストークンの有効期限が切れています。`stratoclave auth login` を再実行してください"
        );
    }

    let base_url = resolve_base_url(config)?;
    let ticket = mint_ticket(&base_url, &saved).await?;

    let separator = if base_url.contains('?') { '&' } else { '?' };
    let url_with_ticket = format!(
        "{}{}ui_ticket={}",
        base_url.trim_end_matches('/'),
        separator,
        ticket
    );

    match cmd {
        UiCommand::Open => {
            eprintln!("[INFO] Opening Stratoclave UI: {}", base_url);
            if let Err(e) = open::that(&url_with_ticket) {
                bail!("ブラウザを起動できませんでした: {}", e);
            }
            eprintln!("[OK] UI opened (ticket expires in ~30 s)");
        }
        UiCommand::Url => {
            println!("{}", url_with_ticket);
        }
    }

    Ok(())
}

async fn mint_ticket(base_url: &str, saved: &tokens::MvpTokens) -> Result<String> {
    let mint_url = format!(
        "{}/api/mvp/auth/ui-ticket",
        base_url.trim_end_matches('/')
    );
    let body = MintRequest {
        access_token: &saved.access_token,
        id_token: saved.id_token.as_deref(),
        refresh_token: saved.refresh_token.as_deref(),
        expires_in: saved.expires_at.checked_sub(
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0),
        ),
        token_type: Some("Bearer"),
    };

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()
        .context("Failed to build HTTP client")?;
    let resp = client
        .post(&mint_url)
        .bearer_auth(&saved.access_token)
        .json(&body)
        .send()
        .await
        .context("Failed to reach /api/mvp/auth/ui-ticket")?;

    if !resp.status().is_success() {
        let status = resp.status();
        let err_body = resp.text().await.unwrap_or_default();
        bail!(
            "Failed to mint UI handoff ticket (HTTP {}): {}",
            status,
            err_body
        );
    }

    let minted: MintResponse = resp
        .json()
        .await
        .context("Failed to parse /ui-ticket response as JSON")?;
    if minted.ticket.is_empty() {
        bail!("Backend returned an empty UI ticket");
    }
    Ok(minted.ticket)
}

fn resolve_base_url(config: &AppConfig) -> Result<String> {
    if let Some(url) = config.admin_ui_url.as_ref() {
        if !url.is_empty() {
            return Ok(url.clone());
        }
    }
    if !config.api_endpoint.is_empty() {
        return Ok(config.api_endpoint.clone());
    }
    // Final fallback: MvpConfig が環境変数 / config.toml から api_endpoint を拾う
    let mvp = MvpConfig::load().context(
        "UI の URL が解決できません。`api_endpoint` を ~/.stratoclave/config.toml か \
         STRATOCLAVE_API_ENDPOINT 環境変数で設定してください",
    )?;
    Ok(mvp.api_endpoint)
}
