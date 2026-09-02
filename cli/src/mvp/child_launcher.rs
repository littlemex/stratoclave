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
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Command, ExitCode, Stdio};

use super::ephemeral_key::{key_was_used, revoke_ephemeral_key};

/// Cognito bearer material stripped by `scrub_stratoclave_tokens`. Deliberately
/// does NOT include `STRATOCLAVE_OPENAI_KEY` (the child's only credential) —
/// see `scrub_never_removes_wrapper_overrides`.
///
/// `STRATOCLAVE_AUTH_TOKEN` is the env short-circuit bearer read by
/// `auth::authenticate` — it MUST be scrubbed or a child inherits the full
/// Cognito bearer via /proc/<pid>/environ (Fable security review H1).
/// `ANTHROPIC_AUTH_TOKEN` is honored by Claude Code as a direct Anthropic
/// bearer; scrubbing it stops a child bypassing the gateway with a parent's
/// real Anthropic credential (review M3). Note these are distinct from
/// `ANTHROPIC_API_KEY`, which the wrapper sets to the ephemeral key AFTER this
/// strip, so it survives (asserted by scrub_never_removes_wrapper_overrides).
const SCRUB_STRATOCLAVE_TOKENS: &[&str] = &[
    "STRATOCLAVE_ACCESS_TOKEN",
    "STRATOCLAVE_ID_TOKEN",
    "STRATOCLAVE_REFRESH_TOKEN",
    "STRATOCLAVE_AUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
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
    // Off-env credential sources the AWS SDK chain would otherwise pick up
    // (Fable security review H2): profile/config files, STS web-identity, and
    // the ECS/EKS container-credential endpoints.
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
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
    // We SET this on the child under aws_identity scrub; it must never appear in
    // a scrub list (Fable round-2 #5: the set currently survives only by
    // ordering accident — this tripwire makes the invariant explicit).
    "AWS_EC2_METADATA_DISABLED",
];

/// The finding half of the bypass warning: what the backend reported, in terms
/// that hold for any child. Agent-specific advice arrives via `bypass_hint`.
fn bypass_finding(binary: &str) -> String {
    format!(
        "Stratoclave recorded no requests for this session's key. If `{binary}` did \
         answer, its traffic did not go through the gateway, so it was not metered, \
         attributed, or budget-checked."
    )
}

/// How long a child must run before "no requests recorded" says anything. Below
/// this it is far more likely the user asked for `--version` or quit immediately.
const MIN_SESSION_FOR_USAGE_CHECK: std::time::Duration = std::time::Duration::from_secs(3);

/// Second generic line: not every zero-usage run is a bypass.
const BYPASS_UPSTREAM_NOTE: &str =
    "If the agent reported an error instead of answering, look upstream of the \
     application as well: a WAF or CDN 403 never reaches the ledger either.";

/// Optional groups of env vars to remove from the child environment.
#[derive(Default, Debug, Clone, Copy)]
struct ScrubFlags {
    stratoclave_tokens: bool,
    aws_identity: bool,
}

/// Boxed per-line stderr hook (see the `stderr_line_hook` field doc). Named so
/// clippy's `type_complexity` lint has a single spot to point at.
type StderrLineHook = Box<dyn Fn(&str) -> Option<String> + Send>;

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
    /// Agent-specific advice appended to the gateway-usage warning. The launcher
    /// is shared, so it states the finding ("no requests recorded") and leaves
    /// the "here is where to look in *your* agent" part to the caller — a codex
    /// user must not be handed Claude Code troubleshooting.
    bypass_hint: Option<String>,
    /// Optional per-line hook on the child's stderr. When set, stderr is piped
    /// (not inherited): every line the child writes is still relayed to the
    /// real stderr verbatim, in order, as it arrives — the child's own output
    /// is unchanged — but `hook` also sees each line and may return an extra
    /// line to print immediately after it. stdin/stdout stay `Stdio::inherit()`
    /// regardless, so a full-screen TUI child (raw mode, alt-screen — codex's
    /// interactive UI renders there) is unaffected; only line-oriented stderr
    /// diagnostics are observable this way. See `codex_cmd::run` for the one
    /// caller today: turning codex's own "unexpected status 402 ...: {json}"
    /// line — which arrives only after codex's *own* retry loop has already
    /// spent its attempts logging misleading "Reconnecting..." lines — into a
    /// clarified, human-readable rejection reason.
    stderr_line_hook: Option<StderrLineHook>,
}

impl ChildLauncher {
    pub fn new(binary: &str) -> Self {
        Self {
            binary: binary.to_string(),
            env_overrides: Vec::new(),
            env_removes: Vec::new(),
            scrub: ScrubFlags::default(),
            cwd: None,
            bypass_hint: None,
            stderr_line_hook: None,
        }
    }

    /// Agent-specific advice printed when the gateway recorded no requests for
    /// this run's key (see `run_with_revoke`).
    pub fn bypass_hint(mut self, hint: &str) -> Self {
        self.bypass_hint = Some(hint.to_string());
        self
    }

    /// Install a per-line stderr hook (see the `stderr_line_hook` field doc).
    /// Switches stderr from `Stdio::inherit()` to a relayed pipe for this run.
    pub fn on_stderr_line(mut self, hook: impl Fn(&str) -> Option<String> + Send + 'static) -> Self {
        self.stderr_line_hook = Some(Box::new(hook));
        self
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
            // Actively disable IMDS so a child on EC2 cannot obtain instance-role
            // credentials off-env (Fable security review H2). Env-scrub only
            // removes explicit creds; this closes the metadata-service fallback.
            // NOTE: this is hardening against accidental gateway bypass, not a
            // hard boundary — a malicious child can still read ~/.aws from disk;
            // true enforcement is network/IAM-side.
            cmd.env("AWS_EC2_METADATA_DISABLED", "true");
        }

        // Caller-requested removals last, so an explicit env_remove of a key
        // always wins over a prior .env() of the same key.
        for k in &self.env_removes {
            cmd.env_remove(k);
        }

        cmd.stdin(Stdio::inherit());
        cmd.stdout(Stdio::inherit());
        // stdin/stdout are ALWAYS inherited, hook or not — a full-screen TUI
        // child needs a real tty on both. Only stderr is ever piped, and only
        // when a hook is installed (see `on_stderr_line`); everything else
        // behaves exactly as `cmd.status()` did before this method existed.
        let hook = self.stderr_line_hook;
        if hook.is_some() {
            cmd.stderr(Stdio::piped());
        } else {
            cmd.stderr(Stdio::inherit());
        }

        let started = std::time::Instant::now();
        let spawn_result: std::io::Result<std::process::ExitStatus> = (|| {
            let mut child = cmd.spawn()?;
            let relay = hook.map(|hook| {
                // `expect`: stderr is `Stdio::piped()` in exactly this branch
                // (set two lines above), so `Child::stderr` is always `Some`.
                let stderr = child.stderr.take().expect("piped stderr");
                std::thread::spawn(move || {
                    let reader = BufReader::new(stderr);
                    for line in reader.lines().map_while(Result::ok) {
                        // Relay first: the child's own output must reach the
                        // terminal in the same order it would have with
                        // Stdio::inherit(), whether or not the hook fires.
                        eprintln!("{line}");
                        if let Some(extra) = hook(&line) {
                            eprintln!("{extra}");
                        }
                    }
                })
            });
            let status = child.wait();
            // Join AFTER wait(): the pipe's write end closes when the child
            // exits, which is what lets the reader loop above terminate.
            // Joining before wait() would deadlock on a child still writing.
            if let Some(t) = relay {
                let _ = t.join();
            }
            status
        })();
        let child_ran_for = started.elapsed();

        // Did the child actually go through the gateway? Asked BEFORE the revoke,
        // while the key record still exists. Env scrubbing cannot stop an agent
        // that re-applies a direct-provider mode from its own configuration after
        // launch; such a session answers normally while nothing is metered,
        // attributed, or budget-checked, and today there is no way to notice.
        //
        // Only worth asking when the child both started and lived long enough to
        // have said something: `--version`, `--help`, and a session the user quits
        // before typing all make zero requests legitimately, and accusing them of
        // bypassing the gateway would train everyone to ignore the warning.
        let worth_checking = spawn_result.is_ok() && child_ran_for >= MIN_SESSION_FOR_USAGE_CHECK;
        let usage_check = if worth_checking {
            Some(key_was_used(base_url, bearer, key_id).await)
        } else {
            None
        };

        // Best-effort revoke regardless of how the child exited; the
        // backend TTL is the safety net if this call fails.
        let revoke_result = revoke_ephemeral_key(base_url, bearer, key_id).await;

        match &usage_check {
            Some(Ok(false)) => {
                eprintln!("[WARN] {}", bypass_finding(&self.binary));
                if let Some(hint) = &self.bypass_hint {
                    eprintln!("[WARN] {hint}");
                }
                eprintln!("[WARN] {BYPASS_UPSTREAM_NOTE}");
            }
            // Staying silent on failure is deliberate (no false accusations), but a
            // detector that dies unnoticed is worthless, so leave a trail for
            // anyone debugging why the warning never fires.
            Some(Err(e)) if std::env::var_os("STRATOCLAVE_DEBUG").is_some() => {
                eprintln!("[DEBUG] gateway-usage check did not complete: {e:#}");
            }
            _ => {}
        }

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

    // The launcher is shared by both wrappers, so the agent-specific half of the
    // bypass warning must come from the caller. A hard-coded Claude hint here
    // would be handed to codex users (and vice versa).
    #[test]
    fn bypass_hint_is_supplied_by_the_caller_not_the_launcher() {
        // The generic text must stay agent-neutral; naming a Claude setting here
        // would hand that advice to codex users too.
        let generic = format!("{} {}", bypass_finding("codex"), BYPASS_UPSTREAM_NOTE);
        for needle in ["CLAUDE_CODE_USE_BEDROCK", "settings.json", "model_provider"] {
            assert!(
                !generic.contains(needle),
                "{needle} belongs in claude_cmd/codex_cmd, not the shared launcher"
            );
        }
        assert!(
            generic.contains("`codex`"),
            "the finding names the child: {generic}"
        );

        let launcher = ChildLauncher::new("codex").bypass_hint("agent specific advice");
        assert_eq!(
            launcher.bypass_hint.as_deref(),
            Some("agent specific advice")
        );
        assert!(ChildLauncher::new("codex").bypass_hint.is_none());
    }

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
