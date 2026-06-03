//! `stratoclave codex -- [args]` subcommand.
//!
//! Launches OpenAI codex as a child process with `CODEX_HOME` pointing
//! at a fresh temp dir we own. That dir contains exactly one file —
//! `config.toml` — describing a `stratoclave` model provider that
//! targets the deployment's `/openai/v1/responses` endpoint. The user's
//! persistent `~/.codex/config.toml` is therefore **never** loaded for
//! this invocation.
//!
//! Why a temp `CODEX_HOME` rather than `-c key=value` overrides? codex
//! resolves model providers as nested TOML; expressing
//! `[model_providers.stratoclave]` via `-c` would require five separate
//! `-c model_providers.stratoclave.<key>=<value>` flags, every one
//! shell-quoted, every one a foot-gun. A single config file is the
//! simpler contract.
//!
//! Lifecycle:
//!
//!   1. Mints an ephemeral, `responses:send`-only `sk-stratoclave-*`
//!      key via `mvp::ephemeral_key::mint_ephemeral_key_scoped`.
//!   2. Creates a temp dir (auto-cleaned on Drop) and writes
//!      `config.toml` pointing codex at the stratoclave base URL and
//!      `env_key = "STRATOCLAVE_OPENAI_KEY"` for the bearer.
//!   3. Spawns codex with `CODEX_HOME=<tempdir>` and the env-key set;
//!      revokes the wrapper key on exit via `ChildLauncher`.
//!
//! Note we deliberately do NOT pass `--ignore-user-config` — that flag
//! tells codex to skip `$CODEX_HOME/config.toml`, which is exactly the
//! file we just wrote. Pointing `CODEX_HOME` at a fresh temp dir is
//! sufficient: the user's `~/.codex/config.toml` is never visible
//! because `~/.codex` is no longer the resolved home.

use anyhow::{Context, Result};
use std::fs;
use std::process::ExitCode;

use tempfile::TempDir;

use super::child_launcher::ChildLauncher;
use super::config::MvpConfig;
use super::ephemeral_key::mint_ephemeral_key_scoped;
use super::tokens::load as load_tokens;

pub async fn run(args: &[String], model_override: Option<&str>) -> Result<ExitCode> {
    let config = MvpConfig::load()?;
    let tokens = load_tokens()?;

    let model = model_override
        .map(String::from)
        .unwrap_or_else(|| config.default_codex_model.clone());

    let base_url = format!(
        "{}{}",
        config.api_endpoint.trim_end_matches('/'),
        config
            .codex_openai_base_path
            .as_deref()
            .unwrap_or("/openai/v1"),
    );

    // Mint the scoped wrapper key first; on failure we never spawn the
    // child and never need to revoke.
    let key = mint_ephemeral_key_scoped(
        &config.api_endpoint,
        &tokens.access_token,
        "stratoclave-codex-wrapper",
        &["responses:send"],
    )
    .await
    .context("Failed to mint ephemeral wrapper key for codex")?;

    let codex_home = build_temp_codex_home(&base_url, &model)
        .context("Failed to write temporary codex config")?;

    eprintln!(
        "[INFO] Launching codex via Stratoclave proxy (base_url={}, model={}, key={})",
        base_url, model, key.key_id
    );
    eprintln!(
        "[INFO] Child process uses an ephemeral responses-only API key; \
         the Cognito bearer is not exported and the user's ~/.codex/config.toml \
         is untouched."
    );

    // codex 0.136 (verified against installed binary, 2026-06-04)
    // resolves a "project-local config" by walking the cwd's ancestors.
    // When that walk reaches a directory containing `.codex/config.toml`
    // it loads that file under the *project* scope — and `model_provider`
    // / `model_providers` are documented as user-only keys, so they are
    // ignored with a noisy warning:
    //
    //   ⚠ Ignored unsupported project-local config keys in
    //     /Users/<you>/.codex/config.toml: model_provider, model_providers.
    //
    // The user's cwd is typically `$HOME`, which contains
    // `~/.codex/config.toml` by definition, so the walk hits it on every
    // launch. Setting `project_root_markers = []` in the *user-level*
    // config (CODEX_HOME) is not enough — codex still inspects each
    // ancestor's `.codex/` directly, independent of marker files.
    //
    // The robust fix is to *escape* `$HOME` for the codex process: spawn
    // it inside an empty temp directory so the ancestor walk has no
    // `.codex/` to find. We only do this when the launcher's cwd would
    // otherwise be `$HOME`, so day-to-day `cd /path/to/your/repo &&
    // stratoclave codex …` keeps working with the user's real workspace.
    let escape_workspace = if cwd_is_home() {
        Some(
            tempfile::Builder::new()
                .prefix("stratoclave-codex-cwd-")
                .tempdir()
                .context("create temp cwd to escape $HOME")?,
        )
    } else {
        None
    };

    let mut launcher = ChildLauncher::new("codex")
        .env("CODEX_HOME", codex_home.path())
        .env("STRATOCLAVE_OPENAI_KEY", &key.plaintext_key)
        .scrub_stratoclave_tokens()
        .scrub_aws_identity();
    if let Some(ws) = &escape_workspace {
        launcher = launcher.cwd(ws.path());
    }

    let result = launcher
        .run_with_revoke(
            args,
            &config.api_endpoint,
            &tokens.access_token,
            &key.key_id,
        )
        .await;

    // Drop the temp dirs deterministically so they do not survive the
    // wrapper exit. `TempDir` auto-deletes on Drop, but the explicit
    // drop keeps the failure surface visible if the FS rejects it.
    drop(escape_workspace);
    drop(codex_home);
    result
}

/// Return `true` when the current process cwd resolves to `$HOME`. We
/// canonicalize both sides to defeat `~/Foo` vs `/Users/you/Foo`
/// differences and symlink farms.
fn cwd_is_home() -> bool {
    let home = match dirs::home_dir() {
        Some(h) => h,
        None => return false,
    };
    let cwd = match std::env::current_dir() {
        Ok(c) => c,
        Err(_) => return false,
    };
    let h = std::fs::canonicalize(&home).unwrap_or(home);
    let c = std::fs::canonicalize(&cwd).unwrap_or(cwd);
    h == c
}

pub(crate) fn build_temp_codex_home(base_url: &str, model: &str) -> Result<TempDir> {
    let dir = TempDir::new().context("TempDir::new for CODEX_HOME")?;
    let body = format!(
        r#"# Auto-generated by `stratoclave codex` — do not edit.
# Lives in a temp `CODEX_HOME` that is deleted when the wrapper exits.

model_provider = "stratoclave"
model = "{model}"

# Bedrock's OpenAI Responses endpoint does not implement the
# `web_search` tool type today. Disabling it here keeps codex from
# sending that tool in its request payload — without this, every
# `/v1/responses` call returns a 400 "Tool type 'web_search' is not
# supported".
web_search = "disabled"

# codex 0.136 walks up from `cwd` looking for a project-local
# `.codex/config.toml`. When the wrapper is invoked from a directory
# under `$HOME`, the search reaches `~/.codex/config.toml` and
# treats it as a project-local override, which produces a noisy
# "Ignored unsupported project-local config keys" warning for any
# `model_provider` / `model_providers` entries the user has there.
# Disabling the marker list short-circuits the walk so only this
# temp `CODEX_HOME/config.toml` is loaded.
project_root_markers = []

# Codex's built-in model catalog does not list `openai.gpt-5.x`,
# which causes a "Model metadata for ... not found. Defaulting to
# fallback metadata" warning at startup. Setting an explicit
# context window suppresses the fallback and pins the value the
# OpenAI Responses route advertises for the GPT-5 family.
model_context_window = {context_window}

[model_providers.stratoclave]
name                   = "Stratoclave (OpenAI via Bedrock)"
base_url               = "{base_url}"
wire_api               = "responses"
env_key                = "STRATOCLAVE_OPENAI_KEY"
request_max_retries    = 3
stream_max_retries     = 5
stream_idle_timeout_ms = 600000
"#,
        context_window = codex_context_window_for(model),
    );
    let cfg_path = dir.path().join("config.toml");
    fs::write(&cfg_path, body).with_context(|| {
        format!("write temp codex config to {}", cfg_path.display())
    })?;
    Ok(dir)
}

/// Codex needs an explicit `model_context_window` for any model id
/// that is not in its built-in catalog. The values here mirror the
/// public spec for the GPT-5 family on Bedrock; non-matching ids fall
/// back to a 200k window so codex still has a non-zero number to
/// reason about (codex itself only uses this for prompt budgeting).
pub(crate) fn codex_context_window_for(model: &str) -> u64 {
    match model {
        "openai.gpt-5.4" | "gpt-5.4" => 400_000,
        "openai.gpt-5.5" | "gpt-5.5" => 400_000,
        _ => 200_000,
    }
}

#[cfg(test)]
mod tests {
    //! Pin the codex_cmd config-file generator so the two warnings the
    //! field hit in the wild do not regress:
    //!
    //!   1. "Ignored unsupported project-local config keys in
    //!      ~/.codex/config.toml: model_provider, model_providers" —
    //!      caused by codex 0.136 walking up from cwd to find
    //!      `.codex/config.toml`. With `project_root_markers = []`
    //!      the walk is short-circuited and the user's home `~/.codex`
    //!      is no longer treated as a project-local override.
    //!
    //!   2. "Model metadata for `openai.gpt-5.x` not found. Defaulting
    //!      to fallback metadata; this can degrade performance and
    //!      cause issues" — caused by codex's built-in catalog not
    //!      knowing about GPT-5 on Bedrock. An explicit
    //!      `model_context_window` keeps codex from falling back.
    //!
    //! These tests check the literal TOML bytes, not behavior, because
    //! they are easy to break by accident and the only consumer is the
    //! external codex binary. A behavioral test would need to spawn
    //! codex itself, which the test harness deliberately avoids.
    use super::*;
    use std::fs;

    #[test]
    fn temp_config_disables_project_root_markers() {
        let dir = build_temp_codex_home("https://example.test/openai/v1", "openai.gpt-5.4")
            .expect("build_temp_codex_home");
        let body = fs::read_to_string(dir.path().join("config.toml")).expect("read config");
        assert!(
            body.contains("project_root_markers = []"),
            "expected project_root_markers = [] to short-circuit ~/.codex walk; got:\n{}",
            body
        );
    }

    #[test]
    fn temp_config_pins_model_context_window() {
        let dir = build_temp_codex_home("https://example.test/openai/v1", "openai.gpt-5.5")
            .expect("build_temp_codex_home");
        let body = fs::read_to_string(dir.path().join("config.toml")).expect("read config");
        assert!(
            body.contains("model_context_window = 400000"),
            "expected model_context_window = 400000 for openai.gpt-5.5; got:\n{}",
            body
        );
    }

    #[test]
    fn context_window_table_matches_known_models() {
        assert_eq!(codex_context_window_for("openai.gpt-5.4"), 400_000);
        assert_eq!(codex_context_window_for("gpt-5.4"), 400_000);
        assert_eq!(codex_context_window_for("openai.gpt-5.5"), 400_000);
        assert_eq!(codex_context_window_for("gpt-5.5"), 400_000);
        // Unknown model falls back to a non-zero default so codex
        // still has a finite budget to plan against.
        assert_eq!(codex_context_window_for("future-model"), 200_000);
    }
}
