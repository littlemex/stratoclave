//! saml2aws authentication provider
//!
//! Integrates saml2aws CLI tool with Stratoclave backend.
//! Executes saml2aws login, reads STS credentials from ~/.aws/credentials,
//! and exchanges them for a JWT via POST /api/auth/sts.
//!
//! Phase 2 (v2.1) 以降は `mvp/auth.rs` (Cognito User/Pass) がデフォルト。
//! 本モジュールは AuthMethod enum の `Saml2Aws` バリアントとして設定ファイルに残るため
//! コンパイルはされるが、通常経路では呼ばれない。
#![allow(dead_code)]

use anyhow::{bail, Context, Result};
use configparser::ini::Ini;
use serde::{Deserialize, Serialize};
use std::process::Stdio;
use tokio::process::Command;

use super::provider::{AuthMethod, AuthProvider, AuthToken, SavedTokens};
use crate::config::{AppConfig, Saml2AwsConfig};

/// saml2aws authentication provider
pub struct Saml2AwsProvider;

impl AuthProvider for Saml2AwsProvider {
    fn authenticate<'a>(
        &'a self,
        config: &'a AppConfig,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<AuthToken>> + Send + 'a>> {
        Box::pin(async move { self.authenticate_impl(config).await })
    }

    fn is_token_valid(&self, tokens: &SavedTokens) -> bool {
        self.is_token_valid_impl(tokens)
    }

    fn refresh<'a>(
        &'a self,
        config: &'a AppConfig,
        _tokens: &'a SavedTokens,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Option<AuthToken>>> + Send + 'a>>
    {
        Box::pin(async move { self.refresh_impl(config).await })
    }
}

impl Saml2AwsProvider {
    async fn authenticate_impl(&self, config: &AppConfig) -> Result<AuthToken> {
        // Use default config if not specified
        let default_config = crate::config::Saml2AwsConfig::default();
        let saml_config = config.saml2aws.as_ref().unwrap_or(&default_config);

        // Try to read existing AWS credentials first (from aws sso login or saml2aws login)
        eprintln!("[INFO] Reading AWS credentials from ~/.aws/credentials...");
        let credentials_result = read_aws_credentials(&saml_config.profile).await;

        let credentials = match credentials_result {
            Ok(creds) => creds,
            Err(_) => {
                // Credentials not found or invalid, run saml2aws login
                eprintln!("[INFO] AWS credentials not found or invalid for profile '{}'", saml_config.profile);
                eprintln!("[INFO] You can use either:");
                eprintln!("       - aws sso login --profile {}", saml_config.profile);
                eprintln!("       - saml2aws login --profile {}", saml_config.profile);
                eprintln!();
                eprintln!("[INFO] Attempting saml2aws login...");

                // Check if saml2aws is installed
                check_saml2aws_installed().await?;

                // Run saml2aws login
                run_saml2aws_login(saml_config).await?;

                // Retry reading credentials
                read_aws_credentials(&saml_config.profile).await?
            }
        };

        // 4. Exchange STS credentials for JWT via POST /api/auth/sts
        let jwt_response = exchange_sts_for_jwt(config, &credentials).await?;

        // 5. Return AuthToken
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let expires_at = now + jwt_response.expires_in;

        Ok(AuthToken {
            bearer_token: jwt_response.access_token,
            expires_at: Some(expires_at),
            refresh_token: None, // saml2aws does not support refresh
            method: AuthMethod::Saml2Aws,
        })
    }

    fn is_token_valid_impl(&self, tokens: &SavedTokens) -> bool {
        match tokens.expires_at {
            Some(exp) => {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs();
                exp > now + 60 // 60 second buffer
            }
            None => false,
        }
    }

    async fn refresh_impl(&self, config: &AppConfig) -> Result<Option<AuthToken>> {
        // saml2aws does not support token refresh
        // User must re-authenticate with saml2aws login
        eprintln!("[INFO] saml2aws does not support token refresh. Re-authenticating...");
        let auth_token = self.authenticate_impl(config).await?;
        Ok(Some(auth_token))
    }
}

/// Check if saml2aws command is installed, and offer to install if not
async fn check_saml2aws_installed() -> Result<()> {
    let output = Command::new("which")
        .arg("saml2aws")
        .output()
        .await
        .context("Failed to execute 'which saml2aws'")?;

    if !output.status.success() {
        eprintln!("[INFO] saml2aws command not found.");
        eprintln!("[INFO] Would you like to install saml2aws automatically? (y/n)");

        use std::io::{self, BufRead};
        let stdin = io::stdin();
        let mut input = String::new();
        stdin.lock().read_line(&mut input)?;

        if input.trim().to_lowercase() == "y" {
            install_saml2aws().await?;
        } else {
            bail!(
                "saml2aws installation cancelled. Please install manually:\n\
                https://github.com/Versent/saml2aws#install"
            );
        }
    }

    Ok(())
}

/// Install saml2aws automatically
async fn install_saml2aws() -> Result<()> {
    eprintln!("[INFO] Installing saml2aws...");

    // Detect OS
    let os = std::env::consts::OS;

    match os {
        "macos" => {
            // Check if Homebrew is available
            let brew_check = Command::new("which")
                .arg("brew")
                .output()
                .await
                .context("Failed to check for Homebrew")?;

            if brew_check.status.success() {
                eprintln!("[INFO] Installing via Homebrew...");
                let status = Command::new("brew")
                    .arg("install")
                    .arg("saml2aws")
                    .stdin(Stdio::inherit())
                    .stdout(Stdio::inherit())
                    .stderr(Stdio::inherit())
                    .status()
                    .await
                    .context("Failed to execute 'brew install saml2aws'")?;

                if !status.success() {
                    bail!("Homebrew installation failed. Please install manually.");
                }

                eprintln!("[OK] saml2aws installed successfully via Homebrew.");
            } else {
                bail!(
                    "Homebrew not found. Please install saml2aws manually:\n\
                    https://github.com/Versent/saml2aws#install"
                );
            }
        }
        "linux" => {
            eprintln!("[INFO] Installing via curl...");

            // Download via the official release URL *and* verify the
            // tarball against the checksum file the same release ships.
            // Without the verification step, a MITM could swap in a
            // hostile binary that we would install as root into
            // ~/.local/bin. `sha256sum --ignore-missing --check` fails
            // closed on mismatch.
            let install_cmd = r#"
                set -euo pipefail
                CURRENT_VERSION=$(curl -Ls https://api.github.com/repos/Versent/saml2aws/releases/latest \
                    | grep '"tag_name"' | head -n1 | cut -d'v' -f2 | cut -d'"' -f1)
                if [ -z "$CURRENT_VERSION" ]; then
                    echo "[ERROR] Could not determine latest saml2aws version" >&2
                    exit 1
                fi
                TMP="$(mktemp -d)"
                trap 'rm -rf "$TMP"' EXIT INT TERM
                TARBALL="saml2aws_${CURRENT_VERSION}_linux_amd64.tar.gz"
                BASE="https://github.com/Versent/saml2aws/releases/download/v${CURRENT_VERSION}"
                echo "[INFO] downloading $TARBALL"
                curl -fsSL "$BASE/$TARBALL" -o "$TMP/$TARBALL"
                echo "[INFO] downloading checksum file"
                curl -fsSL "$BASE/saml2aws_${CURRENT_VERSION}_checksums.txt" \
                    -o "$TMP/checksums.txt"
                (cd "$TMP" && sha256sum --ignore-missing --check checksums.txt)
                mkdir -p "$HOME/.local/bin"
                tar -xzf "$TMP/$TARBALL" -C "$HOME/.local/bin"
                chmod u+x "$HOME/.local/bin/saml2aws"
            "#;

            let status = Command::new("sh")
                .arg("-c")
                .arg(install_cmd)
                .stdin(Stdio::inherit())
                .stdout(Stdio::inherit())
                .stderr(Stdio::inherit())
                .status()
                .await
                .context("Failed to install saml2aws")?;

            if !status.success() {
                bail!(
                    "Installation failed. sha256 verification may have rejected \
                     the tarball. Please install manually."
                );
            }

            eprintln!("[OK] saml2aws installed to ~/.local/bin/saml2aws");
            eprintln!("[INFO] Make sure ~/.local/bin is in your PATH");
        }
        _ => {
            bail!(
                "Automatic installation not supported on {}. Please install manually:\n\
                https://github.com/Versent/saml2aws#install",
                os
            );
        }
    }

    Ok(())
}

/// Run saml2aws login command
async fn run_saml2aws_login(config: &Saml2AwsConfig) -> Result<()> {
    eprintln!("[INFO] Running saml2aws login...");

    let mut cmd = Command::new("saml2aws");
    cmd.arg("login");
    cmd.arg("--profile").arg(&config.profile);

    if config.skip_prompt {
        cmd.arg("--skip-prompt");
    }

    if let Some(ref idp_account) = config.idp_account {
        cmd.arg("--idp-account").arg(idp_account);
    }

    if let Some(ref role_arn) = config.role_arn {
        cmd.arg("--role").arg(role_arn);
    }

    // Inherit stdin/stdout/stderr to allow user interaction
    let status = cmd
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status()
        .await
        .context("Failed to execute saml2aws login")?;

    if !status.success() {
        bail!("saml2aws login failed with exit code: {:?}", status.code());
    }

    eprintln!("[OK] saml2aws login successful.");
    Ok(())
}

/// AWS credentials from ~/.aws/credentials
#[derive(Debug)]
struct AwsCredentials {
    access_key_id: String,
    secret_access_key: String,
    session_token: String,
}

/// Read AWS credentials from ~/.aws/credentials
async fn read_aws_credentials(profile: &str) -> Result<AwsCredentials> {
    let home_dir = dirs::home_dir().context("Failed to get home directory")?;
    let credentials_path = home_dir.join(".aws").join("credentials");

    if !credentials_path.exists() {
        bail!(
            "AWS credentials file not found: {:?}\n\
            Please run 'saml2aws login' first.",
            credentials_path
        );
    }

    // Parse INI file
    let mut ini = Ini::new();
    ini.load(&credentials_path)
        .map_err(|e| anyhow::anyhow!("Failed to parse AWS credentials file: {:?}: {}", credentials_path, e))?;

    // Extract credentials
    let access_key_id = ini
        .get(profile, "aws_access_key_id")
        .with_context(|| format!("aws_access_key_id not found in profile '{}'", profile))?;

    let secret_access_key = ini
        .get(profile, "aws_secret_access_key")
        .with_context(|| format!("aws_secret_access_key not found in profile '{}'", profile))?;

    let session_token = ini
        .get(profile, "aws_session_token")
        .with_context(|| format!("aws_session_token not found in profile '{}' (temporary credentials required)", profile))?;

    Ok(AwsCredentials {
        access_key_id,
        secret_access_key,
        session_token,
    })
}

/// Request body for POST /api/auth/sts
#[derive(Debug, Serialize)]
struct STSAuthRequest {
    grant_type: String,
    access_key_id: String,
    secret_access_key: String,
    session_token: String,
    region: String,
}

/// Response from POST /api/auth/sts
#[derive(Debug, Deserialize)]
struct STSAuthResponse {
    id_token: String,
    access_token: String,
    expires_in: u64,
    token_type: String,
}

/// Exchange STS credentials for JWT via POST /api/auth/token (unified endpoint)
async fn exchange_sts_for_jwt(
    config: &AppConfig,
    credentials: &AwsCredentials,
) -> Result<STSAuthResponse> {
    eprintln!("[INFO] Exchanging STS credentials for JWT...");

    let url = format!("{}/api/auth/token", config.api_endpoint);
    eprintln!("[DEBUG] POST URL: {}", url);

    let request_body = STSAuthRequest {
        grant_type: "sts_credentials".to_string(),
        access_key_id: credentials.access_key_id.clone(),
        secret_access_key: credentials.secret_access_key.clone(),
        session_token: credentials.session_token.clone(),
        region: "us-east-1".to_string(), // TODO: make configurable
    };

    let client = reqwest::Client::builder()
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()
        .unwrap_or_else(|_| reqwest::Client::new());
    let response = client
        .post(&url)
        .json(&request_body)
        .send()
        .await
        .context("Failed to send POST /api/auth/token request")?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        bail!(
            "POST /api/auth/token failed (HTTP {}): {}",
            status,
            body
        );
    }

    let jwt_response: STSAuthResponse = response
        .json()
        .await
        .context("Failed to parse JWT response")?;

    eprintln!("[OK] JWT token obtained successfully.");
    Ok(jwt_response)
}
