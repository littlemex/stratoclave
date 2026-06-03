//! `stratoclave claude -- [args]` subcommand.
//!
//! Launches Claude Code as a child process with `ANTHROPIC_BASE_URL`
//! pointing at the Stratoclave proxy so every `/v1/messages` call flows
//! through tenant-aware credit reservation instead of going directly to
//! Bedrock or Anthropic.
//!
//! P1-B (2026-04 security review) — scoped wrapper key
//!
//! The previous implementation passed the user's Cognito `access_token`
//! to the child via `ANTHROPIC_API_KEY`. That token carried *all* of the
//! user's permissions (admin / team-lead / usage history etc.) and was
//! readable by any co-uid process through `/proc/<pid>/environ` for the
//! full session. For a wrapper that only needs `/v1/messages`, the
//! previous design was massively over-privileged.
//!
//! The wrapper now:
//!
//!   1. Mints an ephemeral, `messages:send`-only `sk-stratoclave-*` key
//!      via `mvp::ephemeral_key::mint_ephemeral_key_scoped`.
//!   2. Hands that key to the child via `ANTHROPIC_API_KEY`. The child
//!      can read its env, but the only thing this token can do is call
//!      `/v1/messages` under the user's credit bucket — no admin /
//!      team-lead / usage leakage.
//!   3. Revokes the key on exit via `ChildLauncher::run_with_revoke`. If
//!      the revoke fails (network drop, Ctrl-C during the call), the
//!      30-minute TTL bounds the damage.
//!
//! The Cognito bearer is never exported into the child environment.
//! `ChildLauncher::scrub_stratoclave_tokens` and
//! `scrub_aws_identity` ensure that MCP servers and tool subprocesses
//! cannot pivot into the user's admin endpoints or fall back to direct
//! Bedrock.

use anyhow::{Context, Result};
use std::process::ExitCode;

use super::child_launcher::ChildLauncher;
use super::config::MvpConfig;
use super::ephemeral_key::mint_ephemeral_key_scoped;
use super::tokens::load as load_tokens;

pub async fn run(args: &[String], model_override: Option<&str>) -> Result<ExitCode> {
    let config = MvpConfig::load()?;
    let tokens = load_tokens()?;

    let base_url = config.api_endpoint.clone();
    let model = model_override
        .map(String::from)
        .unwrap_or_else(|| config.default_model.clone());

    // Mint the scoped wrapper key first; if this fails we never spawn
    // the child and never need to revoke.
    let key = mint_ephemeral_key_scoped(
        &base_url,
        &tokens.access_token,
        "stratoclave-claude-wrapper",
        &["messages:send"],
    )
    .await
    .context("Failed to mint ephemeral wrapper key for claude")?;

    eprintln!(
        "[INFO] Launching claude via Stratoclave proxy (base_url={}, model={}, key={})",
        base_url, model, key.key_id
    );
    eprintln!(
        "[INFO] Child process uses an ephemeral messages-only API key; \
         the Cognito bearer is not exported."
    );

    ChildLauncher::new("claude")
        .env("ANTHROPIC_BASE_URL", &base_url)
        .env("ANTHROPIC_API_KEY", &key.plaintext_key)
        .env("ANTHROPIC_MODEL", &model)
        .scrub_stratoclave_tokens()
        .scrub_aws_identity()
        .run_with_revoke(args, &base_url, &tokens.access_token, &key.key_id)
        .await
}
