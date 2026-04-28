//! Cognito User Pool authentication provider (Phase 1 legacy: browser / device flow).
//!
//! Phase 2 (v2.1) 以降は `mvp/auth.rs` の User/Pass + Backend /api/mvp/auth/login を使う。
//! 本モジュールはビルド互換のため残存。
#![allow(dead_code)]

use anyhow::{bail, Context, Result};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use sha2::{Digest, Sha256};
use std::collections::HashMap;

use super::provider::{AuthMethod, AuthProvider, AuthToken, SavedTokens};
use crate::config::AppConfig;

/// Cognito authentication provider
pub struct CognitoProvider;

impl AuthProvider for CognitoProvider {
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
        tokens: &'a SavedTokens,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Option<AuthToken>>> + Send + 'a>>
    {
        Box::pin(async move { self.refresh_impl(config, tokens).await })
    }
}

impl CognitoProvider {
    async fn authenticate_impl(&self, config: &AppConfig) -> Result<AuthToken> {
        // Run browser authentication flow
        let token_response = browser_auth_flow_internal(config).await?;

        // Calculate expiration
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let expires_at = token_response.expires_in.map(|ei| now + ei);

        // Extract ID token (or fallback to access token)
        let bearer_token = resolve_id_token(
            token_response.id_token.clone(),
            token_response.access_token.clone(),
        );

        Ok(AuthToken {
            bearer_token,
            expires_at,
            refresh_token: token_response.refresh_token,
            method: AuthMethod::Cognito,
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

    async fn refresh_impl(
        &self,
        config: &AppConfig,
        tokens: &SavedTokens,
    ) -> Result<Option<AuthToken>> {
        let refresh_token = match &tokens.refresh_token {
            Some(rt) => rt,
            None => return Ok(None),
        };

        let token_response = refresh_access_token_internal(config, refresh_token).await?;

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let expires_at = token_response.expires_in.map(|ei| now + ei);

        let bearer_token = resolve_id_token(
            token_response.id_token.clone(),
            token_response.access_token.clone(),
        );

        Ok(Some(AuthToken {
            bearer_token,
            expires_at,
            refresh_token: token_response.refresh_token.or(Some(refresh_token.clone())),
            method: AuthMethod::Cognito,
        }))
    }
}

/// PKCE pair
struct PkcePair {
    code_verifier: String,
    code_challenge: String,
}

/// Generate PKCE pair (RFC 7636)
fn generate_pkce_pair() -> PkcePair {
    let random_bytes: [u8; 32] = rand::random();
    let code_verifier = URL_SAFE_NO_PAD.encode(random_bytes);

    let mut hasher = Sha256::new();
    hasher.update(code_verifier.as_bytes());
    let hash = hasher.finalize();
    let code_challenge = URL_SAFE_NO_PAD.encode(hash);

    PkcePair {
        code_verifier,
        code_challenge,
    }
}

/// Cognito token response
#[derive(Debug, serde::Deserialize)]
struct TokenResponse {
    access_token: String,
    id_token: Option<String>,
    refresh_token: Option<String>,
    expires_in: Option<u64>,
    #[allow(dead_code)]
    token_type: Option<String>,
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

/// Wait for callback on local HTTP server
fn wait_for_callback(
    server: &tiny_http::Server,
    redirect_host: &str,
    timeout_secs: u64,
) -> Result<String> {
    let request = server
        .recv_timeout(std::time::Duration::from_secs(timeout_secs))
        .map_err(|e| anyhow::anyhow!("Request receive error: {}", e))?;

    match request {
        Some(req) => {
            let url_str = format!("http://{}{}", redirect_host, req.url());
            let parsed = url::Url::parse(&url_str).context("Failed to parse callback URL")?;
            let params: HashMap<String, String> = parsed.query_pairs().into_owned().collect();

            if let Some(code) = params.get("code") {
                let response = tiny_http::Response::from_string(
                    "<html><body><h1>Authentication Successful</h1><p>You can close this window.</p></body></html>",
                )
                .with_header(
                    "Content-Type: text/html; charset=utf-8"
                        .parse::<tiny_http::Header>()
                        .unwrap(),
                );
                let _ = req.respond(response);
                Ok(code.clone())
            } else {
                let response =
                    tiny_http::Response::from_string("Authentication failed").with_status_code(400);
                let _ = req.respond(response);
                bail!("No authorization code received")
            }
        }
        None => bail!("Authentication timeout ({}s)", timeout_secs),
    }
}

/// Run browser-based authentication flow (internal implementation)
async fn browser_auth_flow_internal(app_config: &AppConfig) -> Result<TokenResponse> {
    // 1. Generate PKCE pair
    let pkce = generate_pkce_pair();

    // 2. Start local callback server (try multiple ports if needed)
    let mut port = app_config.redirect_port;
    let server = loop {
        let bind_addr = format!("{}:{}", app_config.redirect_host, port);
        match tiny_http::Server::http(&bind_addr) {
            Ok(server) => break server,
            Err(_e) if port < app_config.redirect_port + 10 => {
                eprintln!("[INFO] Port {} in use, trying {}...", port, port + 1);
                port += 1;
            }
            Err(e) => {
                bail!(
                    "Failed to start callback server (tried ports {}-{}): {}",
                    app_config.redirect_port,
                    port,
                    e
                );
            }
        }
    };

    // Update redirect_uri with actual port
    let actual_redirect_uri = format!("http://{}:{}/callback", app_config.redirect_host, port);

    // Build authorization URL with actual port
    let params = [
        ("client_id", app_config.client_id.as_str()),
        ("response_type", "code"),
        ("scope", "openid email profile"),
        ("redirect_uri", actual_redirect_uri.as_str()),
        ("code_challenge", pkce.code_challenge.as_str()),
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
    let auth_url = format!("{}/oauth2/authorize?{}", app_config.cognito_domain, query);

    // 3. Open browser
    eprintln!("Opening browser for authentication...");
    if let Err(e) = open::that(&auth_url) {
        eprintln!("[WARNING] Failed to open browser: {}", e);
        eprintln!("Please open this URL manually:");
        eprintln!("{}", auth_url);
    }

    // 4. Wait for callback
    let auth_timeout = app_config.timeouts.auth_callback_secs();
    eprintln!(
        "Waiting for authentication callback on port {} (timeout: {}s)...",
        port, auth_timeout
    );
    let code = wait_for_callback(&server, &app_config.redirect_host, auth_timeout)?;

    // 5. Exchange code for tokens
    let token_url = format!("{}/oauth2/token", app_config.cognito_domain);
    let params = [
        ("grant_type", "authorization_code"),
        ("client_id", app_config.client_id.as_str()),
        ("code", code.as_str()),
        ("redirect_uri", actual_redirect_uri.as_str()),
        ("code_verifier", pkce.code_verifier.as_str()),
    ];

    let client = reqwest::Client::builder()
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()
        .unwrap_or_else(|_| reqwest::Client::new());
    let response = client
        .post(&token_url)
        .form(&params)
        .send()
        .await
        .context("Token exchange request failed")?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        bail!("Token exchange failed (HTTP {}): {}", status, body);
    }

    let token_response: TokenResponse = response
        .json()
        .await
        .context("Failed to parse token response")?;

    Ok(token_response)
}

/// Refresh access token (internal implementation)
async fn refresh_access_token_internal(
    config: &AppConfig,
    refresh_token: &str,
) -> Result<TokenResponse> {
    let token_url = format!("{}/oauth2/token", config.cognito_domain);

    let params = [
        ("grant_type", "refresh_token"),
        ("client_id", config.client_id.as_str()),
        ("refresh_token", refresh_token),
    ];

    let client = reqwest::Client::builder()
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()
        .unwrap_or_else(|_| reqwest::Client::new());
    let response = client
        .post(&token_url)
        .form(&params)
        .send()
        .await
        .context("Token refresh request failed")?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        bail!("Token refresh failed (HTTP {}): {}", status, body);
    }

    let token_response: TokenResponse = response
        .json()
        .await
        .context("Failed to parse refresh token response")?;

    Ok(token_response)
}

/// Device Authorization Response (RFC 8628)
#[derive(Debug, serde::Deserialize)]
pub struct DeviceAuthorizationResponse {
    pub device_code: String,
    pub user_code: String,
    pub verification_uri: String,
    #[allow(dead_code)]
    pub verification_uri_complete: Option<String>,
    pub expires_in: Option<u64>,
    pub interval: Option<u64>,
}

/// Run device authorization flow (RFC 8628)
pub async fn device_auth_flow(app_config: &AppConfig) -> Result<AuthToken> {
    let device_auth_url = format!("{}/oauth2/deviceAuthorization", app_config.cognito_domain);

    let client = reqwest::Client::builder()
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()
        .unwrap_or_else(|_| reqwest::Client::new());

    // 1. Request device authorization
    let params = [("client_id", app_config.client_id.as_str())];
    let response = client
        .post(&device_auth_url)
        .form(&params)
        .send()
        .await
        .context("Device authorization request failed")?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        bail!("Device authorization failed (HTTP {}): {}", status, body);
    }

    let device_resp: DeviceAuthorizationResponse = response
        .json()
        .await
        .context("Failed to parse device authorization response")?;

    // 2. Display user_code and verification_uri
    eprintln!("To authenticate, visit: {}", device_resp.verification_uri);
    eprintln!("Enter code: {}", device_resp.user_code);

    // 3. Poll for token
    let poll_interval = std::time::Duration::from_secs(device_resp.interval.unwrap_or(5));
    let expires_in = device_resp.expires_in.unwrap_or(600);
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(expires_in);

    let token_url = format!("{}/oauth2/token", app_config.cognito_domain);

    loop {
        if std::time::Instant::now() > deadline {
            bail!("Device authorization timed out");
        }

        tokio::time::sleep(poll_interval).await;

        let poll_params = [
            (
                "grant_type",
                "urn:ietf:params:oauth:grant-type:device_code",
            ),
            ("client_id", app_config.client_id.as_str()),
            ("device_code", device_resp.device_code.as_str()),
        ];

        let poll_response = client
            .post(&token_url)
            .form(&poll_params)
            .send()
            .await
            .context("Token poll request failed")?;

        if poll_response.status().is_success() {
            let token_response: TokenResponse = poll_response
                .json()
                .await
                .context("Failed to parse token response")?;

            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs();
            let expires_at = token_response.expires_in.map(|ei| now + ei);

            let bearer_token = resolve_id_token(
                token_response.id_token.clone(),
                token_response.access_token.clone(),
            );

            eprintln!("[OK] Device authentication successful.");
            return Ok(AuthToken {
                bearer_token,
                expires_at,
                refresh_token: token_response.refresh_token,
                method: AuthMethod::Cognito,
            });
        }

        // Check for "authorization_pending" or "slow_down"
        let body = poll_response.text().await.unwrap_or_default();
        if body.contains("authorization_pending") {
            continue;
        } else if body.contains("slow_down") {
            tokio::time::sleep(std::time::Duration::from_secs(5)).await;
            continue;
        } else if body.contains("expired_token") {
            bail!("Device code expired. Please try again.");
        } else {
            bail!("Device authorization failed: {}", body);
        }
    }
}
