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
    eprintln!("  email:              {}", body.get("email").and_then(|v| v.as_str()).unwrap_or(""));
    eprintln!("  user_id:            {}", body.get("user_id").and_then(|v| v.as_str()).unwrap_or(""));
    eprintln!("  org_id:             {}", body.get("org_id").and_then(|v| v.as_str()).unwrap_or(""));
    eprintln!("  user_pool_id:       {}", body.get("user_pool_id").and_then(|v| v.as_str()).unwrap_or(""));

    // P0-6 (2026-04 security review): the temporary password used to go
    // straight to stdout (`println!`), where it survives in shell
    // history, tmux scrollback, `script(1)` transcripts, CI job logs,
    // and (depending on the terminal) screen recordings. Mitigations:
    //
    //   * Write the credential to stderr so `admin user create > out.json`
    //     does not capture it into a file.
    //   * Only print if stderr is a real TTY and the user confirms with
    //     a keystroke. Any non-interactive caller (CI, automation) gets
    //     a clear instruction to set `EXPOSE_TEMPORARY_PASSWORD=true`
    //     on the backend and to read the password from the JSON body
    //     programmatically, so there is no silent print.
    if let Some(pw) = body.get("temporary_password").and_then(|v| v.as_str()) {
        if !pw.is_empty() {
            reveal_secret_via_tty("temporary_password", pw)?;
        } else {
            eprintln!();
            eprintln!(
                "  temporary_password: <not returned by backend — set \
                 EXPOSE_TEMPORARY_PASSWORD=true to receive it, or use \
                 `admin user reset-password`>",
            );
        }
    }
    eprintln!();
    eprintln!("Share the password with the user over a secure channel.");
    eprintln!("They must set a new password on first login (`stratoclave auth login-mvp`).");
    Ok(())
}

/// Reveal a one-time secret on the controlling TTY only.
///
/// P0-6: stdout / stderr capture in CI logs is an exfil channel for
/// admin-issued credentials. If stderr is not a TTY we refuse to print
/// the value and print a guidance message instead; the admin can
/// re-run the command interactively on a real terminal to receive it.
/// On a TTY we prompt before revealing so shoulder-surfing or a stray
/// terminal recording does not catch the value by accident.
pub(crate) fn reveal_secret_via_tty(label: &str, value: &str) -> anyhow::Result<()> {
    use std::io::IsTerminal;
    let stderr_is_tty = std::io::stderr().is_terminal();
    if !stderr_is_tty {
        eprintln!();
        eprintln!(
            "  {label}: <hidden — stderr is not a TTY. Re-run this command \
             interactively to see the value, or have the backend return it \
             in the JSON body with EXPOSE_TEMPORARY_PASSWORD=true.>",
        );
        return Ok(());
    }
    eprintln!();
    eprintln!(
        "  The backend returned a one-time {label}. Press Enter to print it \
         to this terminal, or Ctrl-C to abort."
    );
    let mut _discard = String::new();
    std::io::stdin().read_line(&mut _discard)?;
    eprintln!("  {label}: {value}");
    Ok(())
}
