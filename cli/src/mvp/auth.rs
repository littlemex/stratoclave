//! Cognito User/Pass authentication (CLI).
//!
//! Flow:
//!   1. Obtain email / password (via prompt or `--email` / `--password`)
//!   2. POST /api/mvp/auth/login to trigger Cognito InitiateAuth
//!   3. If NEW_PASSWORD_REQUIRED, prompt for a new password and POST /api/mvp/auth/respond
//!   4. Save the resulting JWT to ~/.stratoclave/mvp_tokens.json with mode 0600
//!
//! OS keychain integration (macOS: `security` CLI) is opt-in; credentials are
//! stored only when the --save-password flag is passed.

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use std::io::{self, Write};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use super::config::MvpConfig;
use super::tokens::{save, MvpTokens};

#[derive(Debug, Serialize)]
struct LoginReq<'a> {
    email: &'a str,
    password: &'a str,
}

#[derive(Debug, Serialize)]
struct RespondReq<'a> {
    email: &'a str,
    new_password: &'a str,
    session: &'a str,
}

#[derive(Debug, Deserialize)]
struct LoginResp {
    status: String,
    session: Option<String>,
    access_token: Option<String>,
    id_token: Option<String>,
    refresh_token: Option<String>,
    expires_in: Option<u64>,
    #[serde(rename = "challenge_name")]
    _challenge_name: Option<String>,
}

pub struct LoginOptions {
    pub email: Option<String>,
    pub password: Option<String>,
    pub save_password: bool,
}

pub async fn login(opts: LoginOptions) -> Result<()> {
    let config = MvpConfig::load()?;

    // email
    let email = match opts.email {
        Some(e) => e,
        None => prompt("Email: ")?,
    };

    // password resolution order:
    //   1. STRATOCLAVE_PASSWORD env var (preferred for scripts so
    //      the value never appears in /proc/<pid>/cmdline)
    //   2. --password flag (DEPRECATED, A-01-cli — emits a warning to
    //      stderr that operators should be reading)
    //   3. OS keychain (Keyring), if previously saved
    //   4. interactive prompt with TTY echo off
    let password = if let Ok(env_pw) = std::env::var("STRATOCLAVE_PASSWORD") {
        env_pw
    } else if let Some(p) = opts.password {
        eprintln!(
            "[WARN] --password is deprecated and exposes the password on the \
             process list. Use STRATOCLAVE_PASSWORD or the interactive prompt \
             instead. See `stratoclave auth login --help`."
        );
        p
    } else {
        keychain_load(&email).ok().flatten().map_or_else(
            || prompt_password("Password: "),
            Ok,
        )?
    };

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()?;
    let resp: LoginResp = client
        .post(config.login_url())
        .json(&LoginReq {
            email: &email,
            password: &password,
        })
        .send()
        .await
        .context("POST /api/mvp/auth/login failed")?
        .error_for_status()
        .context("Login failed (check credentials)")?
        .json()
        .await?;

    let final_resp = match resp.status.as_str() {
        "authenticated" => resp,
        "new_password_required" => {
            eprintln!("[INFO] Temporary password detected. Please set a new password.");
            let new_password = prompt_password("New password: ")?;
            let confirm = prompt_password("Confirm new password: ")?;
            if new_password != confirm {
                return Err(anyhow!("Passwords do not match"));
            }
            let session = resp
                .session
                .ok_or_else(|| anyhow!("Cognito session missing in login response"))?;
            client
                .post(config.respond_url())
                .json(&RespondReq {
                    email: &email,
                    new_password: &new_password,
                    session: &session,
                })
                .send()
                .await
                .context("POST /api/mvp/auth/respond failed")?
                .error_for_status()?
                .json::<LoginResp>()
                .await?
        }
        other => return Err(anyhow!("Unknown login status: {}", other)),
    };

    if final_resp.status != "authenticated" {
        return Err(anyhow!(
            "Authentication not completed (status={})",
            final_resp.status
        ));
    }

    let access = final_resp
        .access_token
        .ok_or_else(|| anyhow!("access_token missing"))?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
    let expires = final_resp.expires_in.unwrap_or(3600);
    let tokens = MvpTokens {
        access_token: access,
        id_token: final_resp.id_token,
        refresh_token: final_resp.refresh_token,
        expires_at: now + expires,
        email: email.clone(),
    };
    save(&tokens)?;
    eprintln!("[OK] Logged in as {}. Token saved to ~/.stratoclave/mvp_tokens.json", email);

    if opts.save_password {
        match keychain_save(&email, &password) {
            Ok(_) => eprintln!("[OK] Password saved to OS keychain (service=stratoclave)"),
            Err(e) => eprintln!("[WARN] Failed to save password to keychain: {}", e),
        }
    }
    Ok(())
}

pub async fn whoami() -> Result<()> {
    let config = MvpConfig::load()?;
    let tokens = super::tokens::load()?;

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()?;
    let resp = client
        .get(config.me_url())
        .bearer_auth(&tokens.access_token)
        .send()
        .await?
        .error_for_status()
        .context("GET /api/mvp/me failed (token expired?)")?;

    let body: serde_json::Value = resp.json().await?;
    println!("email: {}", body.get("email").and_then(|v| v.as_str()).unwrap_or(""));
    println!("user_id: {}", body.get("user_id").and_then(|v| v.as_str()).unwrap_or(""));
    println!("org_id: {}", body.get("org_id").and_then(|v| v.as_str()).unwrap_or(""));
    println!(
        "roles: {}",
        body.get("roles")
            .and_then(|v| v.as_array())
            .map(|a| a
                .iter()
                .filter_map(|v| v.as_str())
                .collect::<Vec<_>>()
                .join(","))
            .unwrap_or_default()
    );
    println!(
        "total_credit: {}",
        body.get("total_credit").and_then(|v| v.as_u64()).unwrap_or(0)
    );
    println!(
        "credit_used: {}",
        body.get("credit_used").and_then(|v| v.as_u64()).unwrap_or(0)
    );
    println!(
        "remaining_credit: {}",
        body.get("remaining_credit").and_then(|v| v.as_u64()).unwrap_or(0)
    );
    Ok(())
}

pub fn logout() -> Result<()> {
    super::tokens::clear()?;
    println!("[OK] Local tokens cleared");
    Ok(())
}

// ---- Prompts ----

fn prompt(msg: &str) -> Result<String> {
    print!("{}", msg);
    io::stdout().flush()?;
    let mut buf = String::new();
    io::stdin().read_line(&mut buf)?;
    Ok(buf.trim().to_string())
}

fn prompt_password(msg: &str) -> Result<String> {
    // Read input with terminal echo disabled. Because rpassword is not a
    // dependency, this is implemented by invoking stty (works on macOS and Linux).
    print!("{}", msg);
    io::stdout().flush()?;
    let prev = run_stty(&["-g"]).ok();
    let _ = run_stty(&["-echo"]);
    let mut buf = String::new();
    let res = io::stdin().read_line(&mut buf);
    if let Some(prev) = prev {
        let _ = run_stty(&[&prev]);
    } else {
        let _ = run_stty(&["echo"]);
    }
    println!();
    res?;
    Ok(buf.trim().to_string())
}

fn run_stty(args: &[&str]) -> Result<String> {
    let mut cmd = Command::new("stty");
    cmd.args(args);
    cmd.stdin(Stdio::inherit());
    let output = cmd.output().context("stty not available")?;
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

// ---- Keychain (macOS security CLI) ----

#[cfg(target_os = "macos")]
pub fn keychain_save(email: &str, password: &str) -> Result<()> {
    let status = Command::new("security")
        .args([
            "add-generic-password",
            "-s", "stratoclave",
            "-a", email,
            "-w", password,
            "-U", // Update if exists
        ])
        .status()?;
    if !status.success() {
        return Err(anyhow!("security add-generic-password failed"));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
pub fn keychain_load(email: &str) -> Result<Option<String>> {
    let output = Command::new("security")
        .args([
            "find-generic-password",
            "-s", "stratoclave",
            "-a", email,
            "-w",
        ])
        .output()?;
    if !output.status.success() {
        return Ok(None);
    }
    Ok(Some(String::from_utf8_lossy(&output.stdout).trim().to_string()))
}

#[cfg(not(target_os = "macos"))]
pub fn keychain_save(_email: &str, _password: &str) -> Result<()> {
    Err(anyhow!("Keychain save is currently only supported on macOS (MVP)"))
}

#[cfg(not(target_os = "macos"))]
pub fn keychain_load(_email: &str) -> Result<Option<String>> {
    Ok(None)
}
