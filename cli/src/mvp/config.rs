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

#[cfg(test)]
mod tests {
    //! Regression tests for the PR #2 schema unification fix (P0-4).
    //!
    //! `stratoclave setup` writes a nested TOML table (`[api] endpoint = ...`),
    //! while older tooling uses a flat `api_endpoint = ...`. Both shapes must
    //! parse back into the same `TomlSnapshot`.
    //!
    //! The tests change $HOME to a tempdir so they never read the developer's
    //! real `~/.stratoclave/config.toml`. They also run sequentially via a
    //! mutex because Cargo parallelizes `#[test]` and these cases mutate
    //! process-wide env.
    use super::*;
    use std::env;
    use std::fs;
    use std::sync::Mutex;

    static ENV_GUARD: Mutex<()> = Mutex::new(());

    struct HomeGuard {
        _tmp: tempfile::TempDir,
        orig_home: Option<std::ffi::OsString>,
        orig_endpoint: Option<std::ffi::OsString>,
        orig_model: Option<std::ffi::OsString>,
    }

    impl Drop for HomeGuard {
        fn drop(&mut self) {
            match self.orig_home.take() {
                Some(v) => env::set_var("HOME", v),
                None => env::remove_var("HOME"),
            }
            match self.orig_endpoint.take() {
                Some(v) => env::set_var("STRATOCLAVE_API_ENDPOINT", v),
                None => env::remove_var("STRATOCLAVE_API_ENDPOINT"),
            }
            match self.orig_model.take() {
                Some(v) => env::set_var("STRATOCLAVE_DEFAULT_MODEL", v),
                None => env::remove_var("STRATOCLAVE_DEFAULT_MODEL"),
            }
        }
    }

    fn setup_home(toml: &str) -> (HomeGuard, std::sync::MutexGuard<'static, ()>) {
        let guard = ENV_GUARD.lock().unwrap();
        let tmp = tempfile::TempDir::new().expect("mktemp");
        let dir = tmp.path().join(".stratoclave");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("config.toml"), toml).unwrap();

        let home_guard = HomeGuard {
            _tmp: tmp,
            orig_home: env::var_os("HOME"),
            orig_endpoint: env::var_os("STRATOCLAVE_API_ENDPOINT"),
            orig_model: env::var_os("STRATOCLAVE_DEFAULT_MODEL"),
        };
        env::set_var("HOME", home_guard._tmp.path());
        env::remove_var("STRATOCLAVE_API_ENDPOINT");
        env::remove_var("STRATOCLAVE_DEFAULT_MODEL");
        (home_guard, guard)
    }

    #[test]
    fn load_accepts_nested_schema_from_stratoclave_setup() {
        let toml = r#"
[api]
endpoint = "https://example.cloudfront.net"

[defaults]
model = "claude-opus-4-7"
"#;
        let (_h, _g) = setup_home(toml);
        let cfg = MvpConfig::load().expect("nested schema should load");
        assert_eq!(cfg.api_endpoint, "https://example.cloudfront.net");
        assert_eq!(cfg.default_model, "claude-opus-4-7");
    }

    #[test]
    fn load_accepts_legacy_flat_schema() {
        let toml = r#"
api_endpoint = "https://legacy.cloudfront.net"
default_model = "claude-sonnet-4-6"
"#;
        let (_h, _g) = setup_home(toml);
        let cfg = MvpConfig::load().expect("flat schema should load");
        assert_eq!(cfg.api_endpoint, "https://legacy.cloudfront.net");
        assert_eq!(cfg.default_model, "claude-sonnet-4-6");
    }

    #[test]
    fn load_prefers_env_var_over_config_file() {
        let toml = r#"
[api]
endpoint = "https://file.cloudfront.net"
"#;
        let (_h, _g) = setup_home(toml);
        env::set_var("STRATOCLAVE_API_ENDPOINT", "https://env.cloudfront.net");
        let cfg = MvpConfig::load().expect("load with env override");
        assert_eq!(cfg.api_endpoint, "https://env.cloudfront.net");
    }

    #[test]
    fn load_errors_when_nothing_is_configured() {
        let (_h, _g) = setup_home("");
        let err = match MvpConfig::load() {
            Ok(_) => panic!("expected error when endpoint is absent"),
            Err(e) => e,
        };
        assert!(err.to_string().contains("API endpoint not configured"));
    }
}
