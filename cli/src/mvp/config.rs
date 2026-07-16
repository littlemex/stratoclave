//! MVP configuration loader for the CLI.
//!
//! `messages_url` / `admin_users_url` predate the Phase 2 unified
//! `api()` helper; they remain here for backward compatibility with
//! older callers (gated by `#[allow(dead_code)]`).
#![allow(dead_code)]

use anyhow::{anyhow, Result};
use std::env;

/// Configuration values consumed by the CLI's MVP commands.
pub struct MvpConfig {
    /// Public API endpoint, e.g. `https://d123.cloudfront.net` or `http://alb-xxx`.
    pub api_endpoint: String,
    /// Default Anthropic model surfaced to `claude` (`ANTHROPIC_MODEL`).
    pub default_model: String,
    /// Default OpenAI model surfaced to `codex`. Defaults to `openai.gpt-5.4`.
    pub default_codex_model: String,
    /// Sub-path under `api_endpoint` that exposes the OpenAI Responses API
    /// (e.g. `/openai/v1`). When unset, defaults to `/openai/v1`.
    pub codex_openai_base_path: Option<String>,
}

impl MvpConfig {
    /// Build an `MvpConfig` from env vars first, then `~/.stratoclave/config.toml`.
    pub fn load() -> Result<Self> {
        let toml_snapshot = load_from_config_toml();

        // 1. Public API endpoint (required).
        let api_endpoint = env::var("STRATOCLAVE_API_ENDPOINT")
            .ok()
            .filter(|s| !s.is_empty())
            .or_else(|| {
                toml_snapshot
                    .as_ref()
                    .and_then(|c| c.api_endpoint.clone())
            })
            .ok_or_else(|| {
                anyhow!(
                    "API endpoint not configured. Set STRATOCLAVE_API_ENDPOINT env var \
                    or configure `api_endpoint` in ~/.stratoclave/config.toml"
                )
            })?;

        // 2. Default Anthropic model (Claude).
        let default_model = env::var("STRATOCLAVE_DEFAULT_MODEL")
            .ok()
            .filter(|s| !s.is_empty())
            .or_else(|| {
                toml_snapshot
                    .as_ref()
                    .and_then(|c| c.default_model.clone())
            })
            .unwrap_or_else(|| "claude-opus-4-7".to_string());

        // 3. Default codex / OpenAI model.
        let default_codex_model = env::var("STRATOCLAVE_DEFAULT_CODEX_MODEL")
            .ok()
            .filter(|s| !s.is_empty())
            .or_else(|| {
                toml_snapshot
                    .as_ref()
                    .and_then(|c| c.default_codex_model.clone())
            })
            .unwrap_or_else(|| "openai.gpt-5.4".to_string());

        // 4. OpenAI base path under the api_endpoint.
        let codex_openai_base_path = env::var("STRATOCLAVE_CODEX_OPENAI_BASE_PATH")
            .ok()
            .filter(|s| !s.is_empty())
            .or_else(|| {
                toml_snapshot
                    .as_ref()
                    .and_then(|c| c.codex_openai_base_path.clone())
            });

        Ok(Self {
            api_endpoint: api_endpoint.trim_end_matches('/').to_string(),
            default_model,
            default_codex_model,
            codex_openai_base_path,
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

    pub fn me_permissions_url(&self) -> String {
        format!("{}/api/mvp/me/permissions", self.api_endpoint)
    }

    pub fn admin_users_url(&self) -> String {
        format!("{}/api/mvp/admin/users", self.api_endpoint)
    }

    /// Generic URL builder for `/api/mvp/*` paths.
    /// `path` should start with `/`.
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
    default_codex_model: Option<String>,
    codex_openai_base_path: Option<String>,
}

fn load_from_config_toml() -> Option<TomlSnapshot> {
    let dir = dirs::home_dir()?.join(".stratoclave");
    let path = dir.join("config.toml");
    if !path.exists() {
        return None;
    }
    let text = std::fs::read_to_string(&path).ok()?;
    let parsed: toml::Value = toml::from_str(&text).ok()?;

    // `stratoclave setup` writes a nested schema (`[api] endpoint = ...`,
    // `[defaults] model = ...`). We also accept the older flat schema
    // (`api_endpoint` / `default_model`) for backward compatibility.
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

    let default_codex_model = parsed
        .get("defaults")
        .and_then(|v| v.as_table())
        .and_then(|t| t.get("codex_model"))
        .and_then(|v| v.as_str())
        .map(String::from);

    let codex_openai_base_path = parsed
        .get("codex")
        .and_then(|v| v.as_table())
        .and_then(|t| t.get("openai_base_path"))
        .and_then(|v| v.as_str())
        .map(String::from);

    Some(TomlSnapshot {
        api_endpoint,
        default_model,
        default_codex_model,
        codex_openai_base_path,
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
        orig_codex_model: Option<std::ffi::OsString>,
        orig_codex_path: Option<std::ffi::OsString>,
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
            match self.orig_codex_model.take() {
                Some(v) => env::set_var("STRATOCLAVE_DEFAULT_CODEX_MODEL", v),
                None => env::remove_var("STRATOCLAVE_DEFAULT_CODEX_MODEL"),
            }
            match self.orig_codex_path.take() {
                Some(v) => env::set_var("STRATOCLAVE_CODEX_OPENAI_BASE_PATH", v),
                None => env::remove_var("STRATOCLAVE_CODEX_OPENAI_BASE_PATH"),
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
            orig_codex_model: env::var_os("STRATOCLAVE_DEFAULT_CODEX_MODEL"),
            orig_codex_path: env::var_os("STRATOCLAVE_CODEX_OPENAI_BASE_PATH"),
        };
        env::set_var("HOME", home_guard._tmp.path());
        env::remove_var("STRATOCLAVE_API_ENDPOINT");
        env::remove_var("STRATOCLAVE_DEFAULT_MODEL");
        env::remove_var("STRATOCLAVE_DEFAULT_CODEX_MODEL");
        env::remove_var("STRATOCLAVE_CODEX_OPENAI_BASE_PATH");
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
        assert_eq!(cfg.default_codex_model, "openai.gpt-5.4");
        assert!(cfg.codex_openai_base_path.is_none());
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
        assert_eq!(cfg.default_codex_model, "openai.gpt-5.4");
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

    #[test]
    fn load_codex_overrides_from_toml_and_env() {
        let toml = r#"
[api]
endpoint = "https://example.cloudfront.net"

[defaults]
codex_model = "openai.gpt-5.5"

[codex]
openai_base_path = "/custom/openai/v1"
"#;
        let (_h, _g) = setup_home(toml);
        let cfg = MvpConfig::load().expect("codex schema should load");
        assert_eq!(cfg.default_codex_model, "openai.gpt-5.5");
        assert_eq!(
            cfg.codex_openai_base_path.as_deref(),
            Some("/custom/openai/v1")
        );
    }
}
