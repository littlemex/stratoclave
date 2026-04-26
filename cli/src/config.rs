//! Configuration management module.
//!
//! Phase 2 (v2.1): 本モジュールは pipe / chat / ui モードのみで使用。
//! 新しい admin / team-lead / usage サブコマンドは `mvp/config.rs` (`MvpConfig`) を使う。
//! 旧トークン永続化 helper (load_tokens / save_tokens 等) は互換のため残存、`#[allow(dead_code)]`。
#![allow(dead_code)]

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::env;
use std::fs;
use std::path::PathBuf;

use crate::auth::SavedTokens;
use crate::policy::POLICY;

/// Timeout configuration for HTTP and authentication operations
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Timeouts {
    /// Total HTTP request timeout in seconds
    pub http_total: Option<u64>,
    /// Connection timeout in seconds
    pub connection: Option<u64>,
    /// SSE chunk read timeout in seconds
    pub sse_chunk: Option<u64>,
    /// Authentication callback timeout in seconds
    pub auth_callback: Option<u64>,
}

impl Default for Timeouts {
    fn default() -> Self {
        Self {
            http_total: Some(10),
            connection: Some(5),
            sse_chunk: Some(20),
            auth_callback: Some(300),
        }
    }
}

impl Timeouts {
    pub fn http_total_secs(&self) -> u64 {
        self.http_total.unwrap_or(10)
    }

    pub fn connection_secs(&self) -> u64 {
        self.connection.unwrap_or(5)
    }

    pub fn sse_chunk_secs(&self) -> u64 {
        self.sse_chunk.unwrap_or(20)
    }

    pub fn auth_callback_secs(&self) -> u64 {
        self.auth_callback.unwrap_or(300)
    }
}

/// Configuration loaded from config file
#[derive(Debug, Serialize, Deserialize)]
pub struct ConfigFile {
    pub cognito_domain: Option<String>,
    pub client_id: Option<String>,
    pub cloudfront_url: Option<String>,
    #[serde(default)]
    pub api_endpoint: Option<String>,
    #[serde(default)]
    pub admin_ui_url: Option<String>,
    #[serde(default)]
    pub default_model: Option<String>,
    #[serde(default = "default_callback_port")]
    pub callback_port: u16,
    #[serde(default = "default_redirect_host")]
    pub redirect_host: String,
    #[serde(default)]
    pub auth_method: Option<crate::auth::AuthMethod>,
    #[serde(default)]
    pub saml2aws: Option<Saml2AwsConfig>,
}

/// saml2aws authentication configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Saml2AwsConfig {
    #[serde(default = "default_saml_profile")]
    pub profile: String,
    #[serde(default)]
    pub cognito_identity_pool_id: Option<String>,
    #[serde(default = "default_true")]
    pub skip_prompt: bool,
    pub idp_account: Option<String>,
    pub role_arn: Option<String>,
}

impl Default for Saml2AwsConfig {
    fn default() -> Self {
        Self {
            profile: default_saml_profile(),
            cognito_identity_pool_id: None,
            skip_prompt: true,
            idp_account: None,
            role_arn: None,
        }
    }
}

fn default_saml_profile() -> String {
    "default".to_string()
}

fn default_true() -> bool {
    true
}

fn default_callback_port() -> u16 {
    18080
}

fn default_redirect_host() -> String {
    "127.0.0.1".to_string()
}

/// Application configuration
#[derive(Clone)]
pub struct AppConfig {
    pub client_id: String,
    pub cognito_domain: String,
    pub redirect_port: u16,
    pub redirect_host: String,
    pub redirect_uri: String,
    pub api_endpoint: String,
    pub admin_ui_url: Option<String>,
    pub default_model: Option<String>,
    pub config_dir: PathBuf,
    pub timeouts: Timeouts,
    pub auth_method: crate::auth::AuthMethod,
    pub saml2aws: Option<Saml2AwsConfig>,
}

impl Default for AppConfig {
    fn default() -> Self {
        let redirect_port: u16 = env::var("STRATOCLAVE_CALLBACK_PORT")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(18080);
        let redirect_host =
            env::var("STRATOCLAVE_REDIRECT_HOST").unwrap_or_else(|_| "127.0.0.1".to_string());
        let config_dir = resolve_config_dir();
        Self {
            client_id: env::var("STRATOCLAVE_CLIENT_ID").unwrap_or_default(),
            cognito_domain: env::var("STRATOCLAVE_COGNITO_DOMAIN").unwrap_or_default(),
            redirect_port,
            redirect_host: redirect_host.clone(),
            redirect_uri: format!("http://{}:{}/callback", redirect_host, redirect_port),
            api_endpoint: env::var("STRATOCLAVE_API_ENDPOINT").unwrap_or_default(),
            admin_ui_url: env::var("STRATOCLAVE_ADMIN_UI_URL").ok(),
            default_model: None,
            config_dir,
            timeouts: Timeouts::default(),
            auth_method: crate::auth::AuthMethod::default(),
            saml2aws: None,
        }
    }
}

impl AppConfig {
    /// Load configuration from file, with environment variable overrides
    pub fn load(config_path: Option<PathBuf>) -> Result<Self> {
        let mut app_config = Self::default();

        // Try to load config from file
        if let Some(ref path) = config_path {
            if path.exists() {
                let content = fs::read_to_string(path)
                    .with_context(|| format!("Failed to read config file: {:?}", path))?;
                // Try TOML format first, then JSON
                if let Ok((cfg, timeouts)) = Self::parse_toml_config(&content) {
                    app_config.apply_config_file(cfg);
                    app_config.timeouts = timeouts;
                } else if let Ok(cfg) = serde_json::from_str::<ConfigFile>(&content) {
                    app_config.apply_config_file(cfg);
                }
            }
        } else {
            // Try default config locations
            let config_dir = &app_config.config_dir;

            // Try ~/.stratoclave/config.toml
            let toml_path = config_dir.join("config.toml");
            if toml_path.exists() {
                if let Ok(content) = fs::read_to_string(&toml_path) {
                    if let Ok((cfg, timeouts)) = Self::parse_toml_config(&content) {
                        app_config.apply_config_file(cfg);
                        app_config.timeouts = timeouts;
                    }
                }
            } else {
                // Fallback: ~/.config/stratoclave/config.json
                if let Some(home) = dirs::home_dir() {
                    let json_path = home
                        .join(".config")
                        .join("stratoclave")
                        .join("config.json");
                    if json_path.exists() {
                        if let Ok(content) = fs::read_to_string(&json_path) {
                            if let Ok(cfg) = serde_json::from_str::<ConfigFile>(&content) {
                                app_config.apply_config_file(cfg);
                            }
                        }
                    }
                }
            }
        }

        // Environment variables override config file
        if let Ok(endpoint) = env::var("STRATOCLAVE_API_ENDPOINT") {
            if !endpoint.is_empty() {
                app_config.api_endpoint = endpoint;
            }
        }
        if let Ok(client_id) = env::var("STRATOCLAVE_CLIENT_ID") {
            if !client_id.is_empty() {
                app_config.client_id = client_id;
            }
        }
        if let Ok(domain) = env::var("STRATOCLAVE_COGNITO_DOMAIN") {
            if !domain.is_empty() {
                app_config.cognito_domain = domain;
            }
        }

        Ok(app_config)
    }

    fn parse_toml_config(content: &str) -> Result<(ConfigFile, Timeouts)> {
        // Parse TOML using serde
        use toml::Value;

        let toml_value: Value = toml::from_str(content)
            .context("Failed to parse TOML configuration")?;

        let mut cognito_domain = None;
        let mut client_id = None;
        let mut endpoint = None;
        let mut default_model = None;
        let mut callback_port = default_callback_port();
        let mut redirect_host = default_redirect_host();
        let mut timeouts = Timeouts::default();
        let mut auth_method = None;
        let mut saml2aws = None;

        // Extract [auth] section
        if let Some(auth) = toml_value.get("auth").and_then(|v| v.as_table()) {
            if let Some(domain) = auth.get("cognito_domain").and_then(|v| v.as_str()) {
                cognito_domain = Some(domain.to_string());
            }
            if let Some(cid) = auth.get("client_id").and_then(|v| v.as_str()) {
                client_id = Some(cid.to_string());
            }
            if let Some(method) = auth.get("auth_method").and_then(|v| v.as_str()) {
                auth_method = match method {
                    "cognito" => Some(crate::auth::AuthMethod::Cognito),
                    "saml2aws" => Some(crate::auth::AuthMethod::Saml2Aws),
                    "aws_profile" => Some(crate::auth::AuthMethod::AwsProfile),
                    _ => None,
                };
            }

            // Extract [auth.saml2aws] subsection
            if let Some(saml_table) = auth.get("saml2aws").and_then(|v| v.as_table()) {
                let profile = saml_table.get("profile")
                    .and_then(|v| v.as_str())
                    .unwrap_or("default")
                    .to_string();
                let cognito_identity_pool_id = saml_table.get("cognito_identity_pool_id")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
                let skip_prompt = saml_table.get("skip_prompt")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(true);
                let idp_account = saml_table.get("idp_account")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
                let role_arn = saml_table.get("role_arn")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());

                saml2aws = Some(Saml2AwsConfig {
                    profile,
                    cognito_identity_pool_id,
                    skip_prompt,
                    idp_account,
                    role_arn,
                });
            }
        }

        // Extract [api] section
        if let Some(api) = toml_value.get("api").and_then(|v| v.as_table()) {
            if let Some(ep) = api.get("endpoint").and_then(|v| v.as_str()) {
                endpoint = Some(ep.to_string());
            }
        }

        // Extract [ui] section
        let mut admin_ui_url = None;
        if let Some(ui) = toml_value.get("ui").and_then(|v| v.as_table()) {
            if let Some(url) = ui.get("admin_url").and_then(|v| v.as_str()) {
                admin_ui_url = Some(url.to_string());
            }
        }

        // Extract [defaults] section
        if let Some(defaults) = toml_value.get("defaults").and_then(|v| v.as_table()) {
            if let Some(model) = defaults.get("model").and_then(|v| v.as_str()) {
                default_model = Some(model.to_string());
            }
        }

        // Extract [callback] section
        if let Some(callback) = toml_value.get("callback").and_then(|v| v.as_table()) {
            if let Some(port) = callback.get("port").and_then(|v| v.as_integer()) {
                callback_port = port as u16;
            }
            if let Some(host) = callback.get("host").and_then(|v| v.as_str()) {
                redirect_host = host.to_string();
            }
        }

        // Extract [timeouts] section
        if let Some(t) = toml_value.get("timeouts").and_then(|v| v.as_table()) {
            if let Some(v) = t.get("http_total").and_then(|v| v.as_integer()) {
                timeouts.http_total = Some(v as u64);
            }
            if let Some(v) = t.get("connection").and_then(|v| v.as_integer()) {
                timeouts.connection = Some(v as u64);
            }
            if let Some(v) = t.get("sse_chunk").and_then(|v| v.as_integer()) {
                timeouts.sse_chunk = Some(v as u64);
            }
            if let Some(v) = t.get("auth_callback").and_then(|v| v.as_integer()) {
                timeouts.auth_callback = Some(v as u64);
            }
        }

        let config_file = ConfigFile {
            cognito_domain,
            client_id,
            cloudfront_url: endpoint.clone(),
            api_endpoint: endpoint,
            admin_ui_url,
            default_model,
            callback_port,
            redirect_host,
            auth_method,
            saml2aws,
        };

        Ok((config_file, timeouts))
    }

    fn apply_config_file(&mut self, cfg: ConfigFile) {
        // Apply auth_method if specified
        if let Some(method) = cfg.auth_method {
            self.auth_method = method;
        }

        // Apply saml2aws config if specified
        if let Some(saml_cfg) = cfg.saml2aws {
            self.saml2aws = Some(saml_cfg);
        }

        if let Some(ref domain) = cfg.cognito_domain {
            if !domain.is_empty() {
                self.cognito_domain = if domain.starts_with("https://") || domain.starts_with("http://") {
                    domain.clone()
                } else {
                    format!("https://{}", domain)
                };
            }
        }
        if let Some(ref cid) = cfg.client_id {
            if !cid.is_empty() {
                self.client_id = cid.clone();
            }
        }
        if let Some(ref ep) = cfg.api_endpoint {
            if !ep.is_empty() && self.api_endpoint.is_empty() {
                self.api_endpoint = ep.clone();
            }
        } else if let Some(ref cf) = cfg.cloudfront_url {
            if !cf.is_empty() && self.api_endpoint.is_empty() {
                self.api_endpoint = cf.clone();
            }
        }
        if let Some(ref url) = cfg.admin_ui_url {
            if !url.is_empty() {
                self.admin_ui_url = Some(url.clone());
            }
        }
        if let Some(ref model) = cfg.default_model {
            if !model.is_empty() {
                self.default_model = Some(model.clone());
            }
        }
        self.redirect_port = cfg.callback_port;
        self.redirect_host = cfg.redirect_host.clone();
        self.redirect_uri = format!(
            "http://{}:{}/callback",
            cfg.redirect_host, cfg.callback_port
        );
    }

    /// Resolve API endpoint (policy > env > config)
    pub fn resolve_base_url(&self) -> String {
        if let Some(fixed) = POLICY.fixed_api_endpoint {
            if !fixed.is_empty() {
                return fixed.to_string();
            }
        }
        if let Ok(url) = env::var("ANTHROPIC_BASE_URL") {
            if POLICY.is_env_var_allowed("ANTHROPIC_BASE_URL") && !url.is_empty() {
                return url;
            }
        }
        self.api_endpoint.clone()
    }

    /// Resolve model
    pub fn resolve_model(&self) -> Option<String> {
        if let Some(fixed) = POLICY.fixed_default_model {
            if !fixed.is_empty() {
                return Some(fixed.to_string());
            }
        }
        // Priority: BEDROCK_MODEL_ID > ANTHROPIC_MODEL > STRATOCLAVE_MODEL > config.toml
        env::var("BEDROCK_MODEL_ID")
            .ok()
            .or_else(|| env::var("ANTHROPIC_MODEL").ok())
            .or_else(|| env::var("STRATOCLAVE_MODEL").ok())
            .or_else(|| self.default_model.clone())
    }

    pub fn resolve_system(&self) -> Option<String> {
        if POLICY.is_env_var_allowed("ANTHROPIC_SYSTEM") {
            env::var("ANTHROPIC_SYSTEM").ok()
        } else {
            None
        }
    }

    pub fn resolve_max_tokens(&self) -> Option<u32> {
        if POLICY.is_env_var_allowed("ANTHROPIC_MAX_TOKENS") {
            env::var("ANTHROPIC_MAX_TOKENS")
                .ok()
                .and_then(|v| v.parse().ok())
        } else {
            None
        }
    }

    pub fn resolve_temperature(&self) -> Option<f32> {
        if POLICY.is_env_var_allowed("ANTHROPIC_TEMPERATURE") {
            env::var("ANTHROPIC_TEMPERATURE")
                .ok()
                .and_then(|v| v.parse().ok())
        } else {
            None
        }
    }
}

/// Resolve config directory: STRATOCLAVE_CONFIG_DIR > ~/.stratoclave
fn resolve_config_dir() -> PathBuf {
    if let Ok(dir) = env::var("STRATOCLAVE_CONFIG_DIR") {
        return PathBuf::from(dir);
    }
    dirs::home_dir()
        .map(|h| h.join(".stratoclave"))
        .unwrap_or_else(|| PathBuf::from(".stratoclave"))
}

/// Get token file path
fn tokens_path(config_dir: &PathBuf) -> PathBuf {
    config_dir.join("tokens.json")
}

/// Check and warn about insecure permissions on config directory and token file
#[cfg(unix)]
fn check_permissions_security(config_dir: &PathBuf) {
    use std::os::unix::fs::PermissionsExt;

    // Check directory permissions
    if let Ok(dir_meta) = fs::metadata(config_dir) {
        let dir_mode = dir_meta.permissions().mode() & 0o777;
        if dir_mode != 0o700 {
            eprintln!(
                "[WARNING] Insecure permissions on config directory {:?}: 0o{:o} (expected 0o700)",
                config_dir, dir_mode
            );
        }
    }

    // Check token file permissions
    let token_path = tokens_path(config_dir);
    if token_path.exists() {
        if let Ok(file_meta) = fs::metadata(&token_path) {
            let file_mode = file_meta.permissions().mode() & 0o777;
            if file_mode != 0o600 {
                eprintln!(
                    "[WARNING] Insecure permissions on token file {:?}: 0o{:o} (expected 0o600)",
                    token_path, file_mode
                );
            }
        }
    }
}

/// Load saved tokens
pub fn load_tokens(config_dir: &PathBuf) -> Result<Option<SavedTokens>> {
    let path = tokens_path(config_dir);
    if !path.exists() {
        return Ok(None);
    }

    // Check permissions security
    #[cfg(unix)]
    check_permissions_security(config_dir);

    let content = fs::read_to_string(&path)
        .with_context(|| format!("Failed to read token file: {:?}", path))?;
    let tokens: SavedTokens =
        serde_json::from_str(&content).with_context(|| "Failed to parse token file")?;
    Ok(Some(tokens))
}

/// Save tokens to file
pub fn save_tokens(config_dir: &PathBuf, tokens: &SavedTokens) -> Result<()> {
    if !config_dir.exists() {
        fs::create_dir_all(config_dir)
            .with_context(|| format!("Failed to create directory: {:?}", config_dir))?;
    }

    // Set directory permissions to 0o700 (owner-only access)
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let dir_perms = fs::Permissions::from_mode(0o700);
        if let Err(e) = fs::set_permissions(config_dir, dir_perms) {
            eprintln!("Warning: Failed to set directory permissions for {:?}: {}", config_dir, e);
        }
    }

    let path = tokens_path(config_dir);
    let content = serde_json::to_string_pretty(tokens).context("Failed to serialize tokens")?;
    fs::write(&path, content)
        .with_context(|| format!("Failed to write token file: {:?}", path))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let perms = fs::Permissions::from_mode(0o600);
        let _ = fs::set_permissions(&path, perms);
    }

    Ok(())
}

/// Delete tokens file
pub fn delete_tokens(config_dir: &PathBuf) -> Result<()> {
    let path = tokens_path(config_dir);
    if path.exists() {
        fs::remove_file(&path).with_context(|| "Failed to delete tokens file")?;
    }
    Ok(())
}

/// Check if saved token is still valid (not expired, with 60s buffer)
pub fn is_token_valid(tokens: &SavedTokens) -> bool {
    match tokens.expires_at {
        Some(expires_at) => {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs();
            now < expires_at.saturating_sub(60)
        }
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- Test: save_tokens creates directory with 0o700 ---

    #[cfg(unix)]
    #[test]
    fn test_save_tokens_sets_directory_permissions_0o700() {
        use std::os::unix::fs::PermissionsExt;

        let temp_dir = std::env::temp_dir()
            .join(format!("stratoclave_perm_test_{}", rand::random::<u64>()));

        // Ensure it doesn't exist
        let _ = std::fs::remove_dir_all(&temp_dir);

        let tokens = SavedTokens {
            access_token: "test-token".to_string(),
            id_token: None,
            refresh_token: None,
            expires_at: Some(9999999999),
            method: crate::auth::AuthMethod::default(),
        };

        let result = save_tokens(&temp_dir, &tokens);
        assert!(result.is_ok(), "save_tokens should succeed, got: {:?}", result.err());

        // Verify directory permissions
        let metadata = std::fs::metadata(&temp_dir).expect("should read dir metadata");
        let mode = metadata.permissions().mode() & 0o777;
        assert_eq!(mode, 0o700, "Directory should have 0o700 permissions, got: {:o}", mode);

        // Verify token file permissions (0o600)
        let token_path = temp_dir.join("tokens.json");
        let file_metadata = std::fs::metadata(&token_path).expect("should read file metadata");
        let file_mode = file_metadata.permissions().mode() & 0o777;
        assert_eq!(file_mode, 0o600, "Token file should have 0o600 permissions, got: {:o}", file_mode);

        // Cleanup
        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    // --- Test: save and load tokens round-trip ---

    #[test]
    fn test_save_and_load_tokens_round_trip() {
        let temp_dir = std::env::temp_dir()
            .join(format!("stratoclave_round_trip_{}", rand::random::<u64>()));

        let tokens = SavedTokens {
            access_token: "my-access-token".to_string(),
            id_token: Some("my-id-token".to_string()),
            refresh_token: Some("my-refresh-token".to_string()),
            expires_at: Some(9999999999),
            method: crate::auth::AuthMethod::default(),
        };

        save_tokens(&temp_dir, &tokens).expect("save should succeed");

        let loaded = load_tokens(&temp_dir)
            .expect("load should succeed")
            .expect("should find saved tokens");

        assert_eq!(loaded.access_token, "my-access-token");
        assert_eq!(loaded.id_token.as_deref(), Some("my-id-token"));
        assert_eq!(loaded.refresh_token.as_deref(), Some("my-refresh-token"));
        assert_eq!(loaded.expires_at, Some(9999999999));

        // Cleanup
        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    // --- Test: load_tokens returns None when no file ---

    #[test]
    fn test_load_tokens_returns_none_when_no_file() {
        let temp_dir = std::env::temp_dir()
            .join(format!("stratoclave_no_file_{}", rand::random::<u64>()));
        let _ = std::fs::create_dir_all(&temp_dir);

        let result = load_tokens(&temp_dir).expect("load should not error");
        assert!(result.is_none(), "Should return None when no tokens file");

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    // --- Test: delete_tokens ---

    #[test]
    fn test_delete_tokens_removes_file() {
        let temp_dir = std::env::temp_dir()
            .join(format!("stratoclave_delete_{}", rand::random::<u64>()));

        let tokens = SavedTokens {
            access_token: "to-delete".to_string(),
            id_token: None,
            refresh_token: None,
            expires_at: None,
            method: crate::auth::AuthMethod::default(),
        };

        save_tokens(&temp_dir, &tokens).expect("save should succeed");
        assert!(tokens_path(&temp_dir).exists());

        delete_tokens(&temp_dir).expect("delete should succeed");
        assert!(!tokens_path(&temp_dir).exists());

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    // --- Test: is_token_valid ---

    #[test]
    fn test_is_token_valid_future_expiry() {
        let tokens = SavedTokens {
            access_token: "test".to_string(),
            id_token: None,
            refresh_token: None,
            expires_at: Some(9999999999),
            method: crate::auth::AuthMethod::default(),
        };
        assert!(is_token_valid(&tokens));
    }

    #[test]
    fn test_is_token_valid_past_expiry() {
        let tokens = SavedTokens {
            access_token: "test".to_string(),
            id_token: None,
            refresh_token: None,
            expires_at: Some(1000),
            method: crate::auth::AuthMethod::default(),
        };
        assert!(!is_token_valid(&tokens));
    }

    #[test]
    fn test_is_token_valid_no_expiry() {
        let tokens = SavedTokens {
            access_token: "test".to_string(),
            id_token: None,
            refresh_token: None,
            expires_at: None,
            method: crate::auth::AuthMethod::default(),
        };
        assert!(!is_token_valid(&tokens));
    }

    // --- Test: AppConfig default values ---

    #[test]
    fn test_app_config_default() {
        std::env::remove_var("STRATOCLAVE_CLIENT_ID");
        std::env::remove_var("STRATOCLAVE_COGNITO_DOMAIN");
        std::env::remove_var("STRATOCLAVE_API_ENDPOINT");
        std::env::remove_var("STRATOCLAVE_CALLBACK_PORT");
        std::env::remove_var("STRATOCLAVE_REDIRECT_HOST");

        let config = AppConfig::default();
        assert_eq!(config.redirect_port, 18080);
        assert_eq!(config.redirect_host, "127.0.0.1");
        assert!(config.redirect_uri.contains("18080"));
        assert!(config.redirect_uri.contains("127.0.0.1"));
    }

    // --- Test: TOML config parsing ---

    #[test]
    fn test_parse_toml_config() {
        let toml = r#"
[auth]
cognito_domain = "auth.example.com"
client_id = "test-client-id"

[api]
endpoint = "https://api.example.com"

[defaults]
model = "claude-3"

[callback]
port = 19090
host = "localhost"
"#;
        let result = AppConfig::parse_toml_config(toml);
        assert!(result.is_ok());
        let (cfg, timeouts) = result.unwrap();
        assert_eq!(cfg.cognito_domain.as_deref(), Some("auth.example.com"));
        assert_eq!(cfg.client_id.as_deref(), Some("test-client-id"));
        assert_eq!(cfg.api_endpoint.as_deref(), Some("https://api.example.com"));
        assert_eq!(cfg.default_model.as_deref(), Some("claude-3"));
        assert_eq!(cfg.callback_port, 19090);
        assert_eq!(cfg.redirect_host, "localhost");
        // Default timeouts when [timeouts] section is not present
        assert_eq!(timeouts.http_total_secs(), 10);
        assert_eq!(timeouts.connection_secs(), 5);
        assert_eq!(timeouts.sse_chunk_secs(), 20);
        assert_eq!(timeouts.auth_callback_secs(), 300);
    }

    #[test]
    fn test_parse_toml_config_with_timeouts() {
        let toml = r#"
[auth]
cognito_domain = "auth.example.com"
client_id = "test-client-id"

[api]
endpoint = "https://api.example.com"

[timeouts]
http_total = 30
connection = 10
sse_chunk = 60
auth_callback = 600
"#;
        let result = AppConfig::parse_toml_config(toml);
        assert!(result.is_ok());
        let (_cfg, timeouts) = result.unwrap();
        assert_eq!(timeouts.http_total_secs(), 30);
        assert_eq!(timeouts.connection_secs(), 10);
        assert_eq!(timeouts.sse_chunk_secs(), 60);
        assert_eq!(timeouts.auth_callback_secs(), 600);
    }
}
