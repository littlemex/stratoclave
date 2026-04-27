//! MVP 用の最小設定ロード.
//!
//! messages_url / admin_users_url は旧実装 (`mvp/admin_cmd.rs`) で使われていたが、
//! Phase 2 の新実装は `api()` ヘルパ経由で /api/mvp/* 全エンドポイントをカバーする。
//! 後方互換のため残存 (`#[allow(dead_code)]`)。
#![allow(dead_code)]

use anyhow::{anyhow, Result};
use std::env;

/// MVP で必要な設定値.
pub struct MvpConfig {
    /// API エンドポイント (例: https://d123.cloudfront.net または http://alb-xxx)
    pub api_endpoint: String,
    /// デフォルトモデル (Claude Code 起動時の ANTHROPIC_MODEL)
    pub default_model: String,
}

impl MvpConfig {
    /// 環境変数 + 既存 config.toml から値を組み立てる.
    pub fn load() -> Result<Self> {
        // 1. 環境変数優先
        let api_endpoint = env::var("STRATOCLAVE_API_ENDPOINT")
            .ok()
            .filter(|s| !s.is_empty())
            .or_else(|| load_from_config_toml().map(|c| c.api_endpoint).flatten())
            .ok_or_else(|| {
                anyhow!(
                    "API endpoint not configured. Set STRATOCLAVE_API_ENDPOINT env var \
                    or configure `api_endpoint` in ~/.stratoclave/config.toml"
                )
            })?;

        let default_model = env::var("STRATOCLAVE_DEFAULT_MODEL")
            .ok()
            .filter(|s| !s.is_empty())
            .or_else(|| load_from_config_toml().map(|c| c.default_model).flatten())
            .unwrap_or_else(|| "claude-opus-4-7".to_string());

        Ok(Self {
            api_endpoint: api_endpoint.trim_end_matches('/').to_string(),
            default_model,
        })
    }

    pub fn messages_url(&self) -> String {
        format!("{}/v1/messages", self.api_endpoint)
    }

    pub fn login_url(&self) -> String {
        format!("{}/api/mvp/auth/login", self.api_endpoint)
    }

    pub fn respond_url(&self) -> String {
        format!("{}/api/mvp/auth/respond", self.api_endpoint)
    }

    pub fn me_url(&self) -> String {
        format!("{}/api/mvp/me", self.api_endpoint)
    }

    pub fn admin_users_url(&self) -> String {
        format!("{}/api/mvp/admin/users", self.api_endpoint)
    }

    /// Generic URL builder for /api/mvp/* paths.
    /// `path` は "/" から始まる絶対パス。
    pub fn api(&self, path: &str) -> String {
        if path.starts_with('/') {
            format!("{}{}", self.api_endpoint, path)
        } else {
            format!("{}/{}", self.api_endpoint, path)
        }
    }
}

struct TomlSnapshot {
    api_endpoint: Option<String>,
    default_model: Option<String>,
}

fn load_from_config_toml() -> Option<TomlSnapshot> {
    let dir = dirs::home_dir()?.join(".stratoclave");
    let path = dir.join("config.toml");
    if !path.exists() {
        return None;
    }
    let text = std::fs::read_to_string(&path).ok()?;
    let parsed: toml::Value = toml::from_str(&text).ok()?;

    // `stratoclave setup` が書き出すネストスキーマ ([api] endpoint / [defaults] model) を優先し、
    // 旧フラットスキーマ (api_endpoint / default_model) も互換として受ける。
    let api_endpoint = parsed
        .get("api")
        .and_then(|v| v.as_table())
        .and_then(|t| t.get("endpoint"))
        .and_then(|v| v.as_str())
        .map(String::from)
        .or_else(|| {
            parsed
                .get("api_endpoint")
                .and_then(|v| v.as_str())
                .map(String::from)
        });

    let default_model = parsed
        .get("defaults")
        .and_then(|v| v.as_table())
        .and_then(|t| t.get("model"))
        .and_then(|v| v.as_str())
        .map(String::from)
        .or_else(|| {
            parsed
                .get("default_model")
                .and_then(|v| v.as_str())
                .map(String::from)
        });

    Some(TomlSnapshot {
        api_endpoint,
        default_model,
    })
}
