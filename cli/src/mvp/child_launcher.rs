//! Wrapper-subcommand spawner shared by `stratoclave claude` and
//! `stratoclave codex`.
//!
//! Both wrappers do exactly the same dance: locate the child binary on
//! `PATH`, scrub the parent process's identity-bearing env vars so the
//! child cannot pivot back into the user's stratoclave or AWS session,
//! spawn the child, wait for it, and revoke the ephemeral wrapper key
//! on exit (regardless of how the child died).
//!
//! Pulling that lifecycle into one place is a security control: the
//! env-scrub list is the bulwark that prevents a Claude / codex child
//! (or any subprocess it execs — MCP servers, tool processes) from
//! exfiltrating the user's Cognito tokens or AWS profile by reading
//! `/proc/<pid>/environ`. If those scrub calls were duplicated across
//! `claude_cmd.rs` and `codex_cmd.rs`, a future security fix that adds
//! one entry would silently miss the other wrapper.

use anyhow::{anyhow, Result};
use std::ffi::{OsStr, OsString};
use std::path::PathBuf;
use std::process::{Command, ExitCode, Stdio};

use super::ephemeral_key::revoke_ephemeral_key;

/// Cognito bearer material stripped by `scrub_stratoclave_tokens`. Deliberately
/// does NOT include `STRATOCLAVE_OPENAI_KEY` (the child's only credential) —
/// see `scrub_never_removes_wrapper_overrides`.
const SCRUB_STRATOCLAVE_TOKENS: &[&str] = &[
    "STRATOCLAVE_ACCESS_TOKEN",
    "STRATOCLAVE_ID_TOKEN",
    "STRATOCLAVE_REFRESH_TOKEN",
];

/// AWS / direct-Bedrock escape hatches stripped by `scrub_aws_identity` so the
/// child cannot bypass stratoclave with the user's own credentials.
const SCRUB_AWS_IDENTITY: &[&str] = &[
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    // `claude` has a Bedrock-direct fallback path we never want active.
    "CLAUDE_CODE_USE_BEDROCK",
    // `codex` reads AWS_BEARER_TOKEN_BEDROCK; strip it so a leaked Bedrock API
    // key cannot accidentally bypass stratoclave.
    "AWS_BEARER_TOKEN_BEDROCK",
];

/// Env keys the wrappers set on the child via `.env()` and rely on surviving
/// the scrub. If a scrub list ever names one of these, the scrub (which runs
/// after the overrides and clears explicit values too) would silently break
/// the child — this list is the tripwire, asserted in tests.
#[cfg(test)]
const WRAPPER_OVERRIDE_KEYS: &[&str] = &[
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CODEX_HOME",
    "STRATOCLAVE_OPENAI_KEY",
];

/// Optional groups of env vars to remove from the child environment.
#[derive(Default, Debug, Clone, Copy)]
struct ScrubFlags {
    stratoclave_tokens: bool,
    aws_identity: bool,
}

/// Builder for spawning a wrapper child process under stratoclave.
pub struct ChildLauncher {
    binary: String,
    /// Additional `KEY=VALUE` pairs added to the child env.
    env_overrides: Vec<(String, OsString)>,
    /// Explicit keys to clear from the inherited child env (caller intent, not
    /// a scrub group). Applied AFTER overrides + scrub so it always wins.
    env_removes: Vec<String>,
    scrub: ScrubFlags,
    /// Optional working directory for the child process. When set, the
    /// child is spawned with `Command::current_dir(...)` instead of
    /// inheriting the parent's `cwd`.
    cwd: Option<PathBuf>,
}

impl ChildLauncher {
    pub fn new(binary: &str) -> Self {
        Self {
            binary: binary.to_string(),
            env_overrides: Vec::new(),
            env_removes: Vec::new(),
            scrub: ScrubFlags::default(),
            cwd: None,
        }
    }

    pub fn env(mut self, key: &str, value: impl AsRef<OsStr>) -> Self {
        self.env_overrides
            .push((key.to_string(), value.as_ref().to_os_string()));
        self
    }

    /// Clear an inherited env var on the child (e.g. a pre-existing
    /// ANTHROPIC_CUSTOM_HEADERS whose lines were all filtered out — the child
    /// must not inherit the raw value). Applied last, so it overrides any prior
    /// `.env()` of the same key.
    pub fn env_remove(mut self, key: &str) -> Self {
        self.env_removes.push(key.to_string());
        self
    }

    /// Override the working directory the child is spawned in.
    pub fn cwd(mut self, dir: impl Into<PathBuf>) -> Self {
        self.cwd = Some(dir.into());
        self
    }

    /// Strip any stratoclave-issued bearer / identity tokens from the
    /// child environment. The wrapper key in `STRATOCLAVE_OPENAI_KEY` /
    /// `ANTHROPIC_API_KEY` is set by the caller AFTER this strip via
    /// `.env(...)`, so it survives.
    pub fn scrub_stratoclave_tokens(mut self) -> Self {
        self.scrub.stratoclave_tokens = true;
        self
    }

    /// Remove AWS profile / region indicators that would let the child
    /// fall back to the user's AWS credentials (e.g. `claude code` has
    /// a `CLAUDE_CODE_USE_BEDROCK=1` mode that talks to Bedrock directly,
    /// bypassing stratoclave entirely).
    pub fn scrub_aws_identity(mut self) -> Self {
        self.scrub.aws_identity = true;
        self
    }

    /// Spawn the child, wait for it, and revoke the ephemeral key on the
    /// way out. The exit code propagates through `ExitCode`.
    pub async fn run_with_revoke(
        self,
        args: &[String],
        base_url: &str,
        bearer: &str,
        key_id: &str,
    ) -> Result<ExitCode> {
        let path = find_binary(&self.binary).ok_or_else(|| {
            anyhow!(
                "could not locate `{}` on PATH or common install dirs",
                self.binary
            )
        })?;

        let mut cmd = Command::new(&path);
        cmd.args(args);

        if let Some(dir) = &self.cwd {
            cmd.current_dir(dir);
        }

        for (k, v) in &self.env_overrides {
            cmd.env(k, v);
        }

        // NOTE ordering: env_remove runs AFTER the .env() overrides above and
        // clears explicitly-set values too, so a scrub name that collided with
        // an override would silently nuke it. The lists below MUST never name a
        // key the wrappers set (ANTHROPIC_*, STRATOCLAVE_OPENAI_KEY,
        // CODEX_HOME, ...); `scrub_never_removes_wrapper_overrides` locks that in.
        if self.scrub.stratoclave_tokens {
            for k in SCRUB_STRATOCLAVE_TOKENS {
                cmd.env_remove(k);
            }
        }
        if self.scrub.aws_identity {
            for k in SCRUB_AWS_IDENTITY {
                cmd.env_remove(k);
            }
        }

        // Caller-requested removals last, so an explicit env_remove of a key
        // always wins over a prior .env() of the same key.
        for k in &self.env_removes {
            cmd.env_remove(k);
        }

        cmd.stdin(Stdio::inherit());
        cmd.stdout(Stdio::inherit());
        cmd.stderr(Stdio::inherit());

        let spawn_result = cmd.status();

        // Best-effort revoke regardless of how the child exited; the
        // backend TTL is the safety net if this call fails.
        let revoke_result = revoke_ephemeral_key(base_url, bearer, key_id).await;

        match spawn_result {
            Ok(status) => {
                if let Err(e) = revoke_result {
                    eprintln!(
                        "[WARN] Ephemeral wrapper key revoke failed ({}). It will \
                         auto-expire via the backend TTL.",
                        e
                    );
                }
                let code = status.code().unwrap_or(1) as u8;
                Ok(ExitCode::from(code))
            }
            Err(e) => {
                if let Err(re) = revoke_result {
                    eprintln!("[WARN] Ephemeral wrapper key revoke failed: {}", re);
                }
                Err(anyhow!("Failed to spawn `{}`: {}", self.binary, e))
            }
        }
    }
}

/// Resolve the child binary by name, falling back to common installer
/// paths when `which` does not turn it up.
fn find_binary(name: &str) -> Option<String> {
    if let Ok(output) = Command::new("which").arg(name).output() {
        if output.status.success() {
            if let Ok(path) = String::from_utf8(output.stdout) {
                let path = path.trim();
                if !path.is_empty() && PathBuf::from(path).exists() {
                    return Some(path.to_string());
                }
            }
        }
    }
    let home = std::env::var("HOME").unwrap_or_default();
    let candidates = [
        format!("{}/.local/bin/{}", home, name),
        format!("/usr/local/bin/{}", name),
        format!("/opt/homebrew/bin/{}", name),
    ];
    for c in candidates {
        if PathBuf::from(&c).exists() {
            return Some(c);
        }
    }
    Some(name.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// L3 (Fable #64 rev1): env_remove runs after the .env() overrides and
    /// clears explicitly-set values, so a scrub list must never name a key the
    /// wrappers rely on (or the child loses its credential / config silently).
    #[test]
    fn scrub_never_removes_wrapper_overrides() {
        for key in WRAPPER_OVERRIDE_KEYS {
            assert!(
                !SCRUB_STRATOCLAVE_TOKENS.contains(key),
                "scrub_stratoclave_tokens must not remove wrapper override {key}"
            );
            assert!(
                !SCRUB_AWS_IDENTITY.contains(key),
                "scrub_aws_identity must not remove wrapper override {key}"
            );
        }
    }
}
