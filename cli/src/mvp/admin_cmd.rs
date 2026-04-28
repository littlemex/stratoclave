//! Phase 1 時代の単発 admin user create 実装 (互換維持のため残存、main.rs から呼ばれない).
//!
//! Phase 2 (v2.1) の本命は `mvp/admin.rs::user_create` (`stratoclave admin user create`).
#![allow(dead_code)]

use anyhow::{Context, Result};
use serde::Serialize;

use super::config::MvpConfig;
use super::tokens::load as load_tokens;

#[derive(Debug, Serialize)]
struct CreateUserReq<'a> {
    email: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    total_credit: Option<u64>,
}

pub async fn create_user(email: &str, total_credit: Option<u64>) -> Result<()> {
    let config = MvpConfig::load()?;
    let tokens = load_tokens()?;

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()?;

    let resp = client
        .post(config.admin_users_url())
        .bearer_auth(&tokens.access_token)
        .json(&CreateUserReq { email, total_credit })
        .send()
        .await
        .context("POST /api/mvp/admin/users failed")?;

    let status = resp.status();
    let body: serde_json::Value = resp.json().await?;
    if !status.is_success() {
        return Err(anyhow::anyhow!(
            "Admin user creation failed (status={}, body={})",
            status,
            body
        ));
    }

    println!("[OK] User created.");
    println!("  email:              {}", body.get("email").and_then(|v| v.as_str()).unwrap_or(""));
    println!("  user_id:            {}", body.get("user_id").and_then(|v| v.as_str()).unwrap_or(""));
    println!("  org_id:             {}", body.get("org_id").and_then(|v| v.as_str()).unwrap_or(""));
    println!("  user_pool_id:       {}", body.get("user_pool_id").and_then(|v| v.as_str()).unwrap_or(""));
    println!("  temporary_password: {}", body.get("temporary_password").and_then(|v| v.as_str()).unwrap_or(""));
    println!();
    println!("Share the temporary_password with the user via a secure channel.");
    println!("They must set a new password on first login (`stratoclave auth login-mvp`).");
    Ok(())
}
