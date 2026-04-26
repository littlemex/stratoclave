//! Authentication module (Phase 1 以前の legacy OIDC / saml2aws フロー).
//!
//! Phase 2 (v2.1) 以降は `mvp/auth.rs` (Cognito User/Pass + Backend /api/mvp/auth/login) が
//! デフォルト。本モジュールは以下の目的で残されている:
//! - `AuthMethod` / `SavedTokens` enum が `config.rs` で参照される (AuthMethod の値列挙のため)
//! - `authenticate()` / `get_token()` が `commands/{pipe,chat,ui}.rs` の pipe / 対話モードで使用
//!
//! 他の関数はビルド互換のため残存しているが呼ばれない。#[allow(dead_code)] で warning 抑制。
#![allow(dead_code)]

mod cognito_provider;
mod provider;
mod saml2aws_provider;
mod token;

pub use provider::{AuthMethod, AuthProvider, SavedTokens};
pub use token::{is_token_valid, load_tokens, save_tokens};

use anyhow::{bail, Result};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;

use crate::config::AppConfig;
use cognito_provider::CognitoProvider;
use saml2aws_provider::Saml2AwsProvider;

/// Authenticate: load saved tokens, refresh if needed, or start browser flow
pub async fn authenticate(app_config: &AppConfig) -> Result<String> {
    // Check STRATOCLAVE_AUTH_TOKEN env var first
    if let Ok(token) = std::env::var("STRATOCLAVE_AUTH_TOKEN") {
        if !token.is_empty() {
            return Ok(token);
        }
    }

    // 1. Check saved tokens
    if let Ok(Some(saved)) = load_tokens(&app_config.config_dir) {
        // Determine provider based on saved token method
        let provider: Box<dyn AuthProvider> = match saved.method {
            AuthMethod::Cognito => Box::new(CognitoProvider),
            AuthMethod::Saml2Aws => Box::new(Saml2AwsProvider),
            AuthMethod::AwsProfile => {
                bail!("AWS Profile authentication not yet implemented")
            }
        };

        if provider.is_token_valid(&saved) {
            return Ok(resolve_id_token(saved.id_token, saved.access_token));
        }

        // 2. Try refresh
        if let Some(ref _refresh_token) = saved.refresh_token {
            eprintln!("Refreshing token...");
            match provider.refresh(app_config, &saved).await {
                Ok(Some(new_token)) => {
                    let saved_new = SavedTokens {
                        access_token: new_token.bearer_token.clone(),
                        id_token: None, // Will be in bearer_token
                        refresh_token: new_token.refresh_token,
                        expires_at: new_token.expires_at,
                        method: new_token.method,
                    };
                    let _ = save_tokens(&app_config.config_dir, &saved_new);
                    return Ok(new_token.bearer_token);
                }
                Ok(None) => {
                    eprintln!("[WARNING] Token refresh not supported for this method");
                }
                Err(e) => {
                    eprintln!("[WARNING] Token refresh failed: {}", e);
                }
            }
        }

        // Token is expired and refresh failed.
        // Check if AUTH_MODE=none, in which case we warn but continue.
        if std::env::var("AUTH_MODE").unwrap_or_default() == "none"
            && !saved.access_token.is_empty()
        {
            eprintln!("[WARNING] Token expired but AUTH_MODE=none, continuing with expired token.");
            return Ok(resolve_id_token(saved.id_token, saved.access_token));
        }

        // Otherwise, return error for expired token
        bail!("Authentication token expired. Please run 'stratoclave auth login' to re-authenticate.")
    }

    // 3. No valid token found
    bail!("No valid authentication token found. Run `stratoclave auth login` to authenticate.")
}

/// Extract the ID token for API authentication, warning if falling back to access token.
fn resolve_id_token(id_token: Option<String>, access_token: String) -> String {
    match id_token {
        Some(id_token) => id_token,
        None => {
            eprintln!("[WARNING] ID token not available. Using access token as fallback, which may fail OIDC validation on the backend.");
            access_token
        }
    }
}

/// Login: start authentication flow (browser-based)
pub async fn login() -> Result<String> {
    let mut app_config = AppConfig::load(None)?;

    // Auto-configure saml2aws if method is Saml2Aws but config is missing
    if matches!(app_config.auth_method, AuthMethod::Saml2Aws) && app_config.saml2aws.is_none() {
        eprintln!("[INFO] Using default saml2aws configuration (profile=default)");
        app_config.saml2aws = Some(crate::config::Saml2AwsConfig::default());
    }

    // Check if we already have a valid token
    if let Ok(Some(saved)) = load_tokens(&app_config.config_dir) {
        if is_token_valid(&saved) {
            eprintln!("[INFO] Already authenticated.");
            return Ok(resolve_id_token(saved.id_token, saved.access_token));
        }
    }

    // Select provider based on auth_method
    let auth_token = match app_config.auth_method {
        AuthMethod::Cognito => {
            let provider = CognitoProvider;
            eprintln!("[INFO] Starting browser authentication flow...");
            provider.authenticate(&app_config).await?
        }
        AuthMethod::Saml2Aws => {
            let provider = Saml2AwsProvider;
            eprintln!("[INFO] Starting saml2aws authentication flow...");
            provider.authenticate(&app_config).await?
        }
        AuthMethod::AwsProfile => {
            bail!("AWS Profile authentication not yet implemented")
        }
    };

    // Save tokens
    let saved = SavedTokens {
        access_token: auth_token.bearer_token.clone(),
        id_token: None, // bearer_token is already the ID token
        refresh_token: auth_token.refresh_token,
        expires_at: auth_token.expires_at,
        method: auth_token.method,
    };
    let _ = save_tokens(&app_config.config_dir, &saved);

    // Display Admin UI URL
    if let Some(ref admin_ui_url) = app_config.admin_ui_url {
        eprintln!("\n[INFO] Admin UI: {}", admin_ui_url);
    } else if !app_config.api_endpoint.is_empty() {
        // Derive Admin UI URL from API endpoint if not explicitly set
        let admin_ui_url = derive_admin_ui_url(&app_config.api_endpoint);
        eprintln!("\n[INFO] Admin UI: {}", admin_ui_url);
    }

    Ok(auth_token.bearer_token)
}

/// Login with device authorization flow
pub async fn login_device() -> Result<String> {
    let app_config = AppConfig::load(None)?;
    let auth_token = cognito_provider::device_auth_flow(&app_config).await?;

    // Save tokens
    let saved = SavedTokens {
        access_token: auth_token.bearer_token.clone(),
        id_token: None,
        refresh_token: auth_token.refresh_token,
        expires_at: auth_token.expires_at,
        method: auth_token.method,
    };
    let _ = save_tokens(&app_config.config_dir, &saved);

    Ok(auth_token.bearer_token)
}

/// Get current authentication token
pub async fn get_token() -> Result<String> {
    let app_config = AppConfig::load(None)?;
    // Check saved tokens
    if let Ok(Some(saved)) = load_tokens(&app_config.config_dir) {
        if is_token_valid(&saved) {
            return Ok(resolve_id_token(saved.id_token, saved.access_token));
        }
    }

    bail!("Not authenticated. Run `stratoclave login` to authenticate.")
}

/// Logout: remove saved tokens
pub fn logout() -> Result<()> {
    let app_config = AppConfig::load(None)?;
    let tokens_path = app_config.config_dir.join("tokens.json");

    if tokens_path.exists() {
        std::fs::remove_file(&tokens_path)?;
    }

    Ok(())
}

/// Decode JWT payload (no signature verification - for local whoami)
pub fn decode_jwt_payload(token: &str) -> Result<serde_json::Value> {
    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() != 3 {
        bail!("Invalid JWT format");
    }

    // Decode payload (second part)
    let payload_bytes = URL_SAFE_NO_PAD
        .decode(parts[1])
        .or_else(|_| {
            // Try with padding
            let padded = match parts[1].len() % 4 {
                2 => format!("{}==", parts[1]),
                3 => format!("{}=", parts[1]),
                _ => parts[1].to_string(),
            };
            base64::engine::general_purpose::URL_SAFE.decode(&padded)
        })?;

    let payload: serde_json::Value = serde_json::from_slice(&payload_bytes)?;

    Ok(payload)
}

/// ID Token Claims
#[derive(Debug, serde::Deserialize)]
pub struct IdTokenClaims {
    pub sub: String,
    pub email: Option<String>,
    pub exp: u64,
}

/// Parse ID token (simple JWT parsing without verification)
pub fn parse_id_token(token: &str) -> Result<IdTokenClaims> {
    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() != 3 {
        bail!("Invalid JWT format");
    }

    // Decode payload (second part)
    let payload = parts[1];
    let decoded = URL_SAFE_NO_PAD.decode(payload)?;

    let claims: IdTokenClaims = serde_json::from_slice(&decoded)?;

    Ok(claims)
}

/// Export browser_auth_flow for commands/auth.rs
pub async fn browser_auth_flow(app_config: &AppConfig) -> Result<String> {
    let provider = CognitoProvider;
    let auth_token = provider.authenticate(app_config).await?;

    // Save tokens
    let saved = SavedTokens {
        access_token: auth_token.bearer_token.clone(),
        id_token: None,
        refresh_token: auth_token.refresh_token,
        expires_at: auth_token.expires_at,
        method: auth_token.method,
    };
    let _ = save_tokens(&app_config.config_dir, &saved);

    Ok(auth_token.bearer_token)
}

// Phase 2: device_auth_flow は main.rs から呼ばれない。unused import warning を避けるため再エクスポート撤去。

/// Derive Admin UI URL from API endpoint
fn derive_admin_ui_url(api_endpoint: &str) -> String {
    // Extract base URL and default frontend port
    if api_endpoint.starts_with("http://localhost:") || api_endpoint.starts_with("http://127.0.0.1:") {
        // Local development: default to http://localhost:3000
        "http://localhost:3000".to_string()
    } else if api_endpoint.contains("cloudfront.net") {
        // Production: CloudFront URL
        // Example: https://api.example.com -> https://example.com
        api_endpoint.replace("/api", "").replace("api.", "")
    } else {
        // Unknown: just remove /api suffix
        api_endpoint.replace("/api", "")
    }
}

// Re-export PKCE and other utilities from the old auth.rs for tests
pub fn generate_pkce_pair() -> (String, String) {
    // This is a temporary wrapper for tests
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use base64::Engine;
    use sha2::{Digest, Sha256};

    let random_bytes: [u8; 32] = rand::random();
    let code_verifier = URL_SAFE_NO_PAD.encode(random_bytes);

    let mut hasher = Sha256::new();
    hasher.update(code_verifier.as_bytes());
    let hash = hasher.finalize();
    let code_challenge = URL_SAFE_NO_PAD.encode(hash);

    (code_verifier, code_challenge)
}

pub fn build_authorization_url(config: &AppConfig, code_challenge: &str) -> String {
    let params = [
        ("client_id", config.client_id.as_str()),
        ("response_type", "code"),
        ("scope", "openid email profile"),
        ("redirect_uri", config.redirect_uri.as_str()),
        ("code_challenge", code_challenge),
        ("code_challenge_method", "S256"),
    ];

    let query = params
        .iter()
        .map(|(k, v)| {
            format!(
                "{}={}",
                url::form_urlencoded::byte_serialize(k.as_bytes()).collect::<String>(),
                url::form_urlencoded::byte_serialize(v.as_bytes()).collect::<String>()
            )
        })
        .collect::<Vec<_>>()
        .join("&");

    format!("{}/oauth2/authorize?{}", config.cognito_domain, query)
}
