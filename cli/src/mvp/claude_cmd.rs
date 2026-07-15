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
use super::sc_headers::ScHeaders;
use super::tokens::load as load_tokens;

/// What the wrapper should do with the child's ANTHROPIC_CUSTOM_HEADERS.
///
/// Three-valued on purpose (Fable #64 rev2 NEW-M1): the child *inherits* the
/// parent's env, so "leave the var unset" is NOT the same as "set it empty" —
/// if we merely decline to set it, the child sees the parent's raw, unfiltered
/// value. So when we had an inherited value but every line was filtered out
/// (all-x-sc / all-CR), we must actively REMOVE the var, not leave it unset.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum CustomHeaders {
    /// Export this exact value (validated + merged).
    Set(String),
    /// Actively clear the var so the child can't inherit an unfiltered value.
    Remove,
    /// No inherited value and no flags — nothing to do; leave the env as-is.
    LeaveUnset,
}

/// Compute the child's ANTHROPIC_CUSTOM_HEADERS, safely MERGING our validated
/// x-sc-* lines onto any pre-existing value the user set (e.g.
/// `anthropic-beta: <feature>`).
///
/// Format is newline-separated `Name: Value` lines (Claude Code / Anthropic
/// SDK). Merge discipline (the security crux — Fable #64 rev1 M2 / rev2 NEW-L1):
///   * our own values are newline/control-char free by `ScHeaders`'
///     construction, so appending them can never split a header line;
///   * from the INHERITED value we drop any line that (a) is blank, (b) names
///     an `x-sc-*` header (case-insensitive — our flags own that namespace),
///     or (c) contains ANY control / line-separator code point (`< 0x20`,
///     `0x7F`, U+0085/NEL, U+2028, U+2029) — not just CR/LF — so no exotic
///     line-break can smuggle a second header into a value.
/// Other inherited headers are preserved verbatim.
///
/// `inherited_present` records whether the var existed at all (including a
/// non-UTF-8 value that `raw` can't represent), so we know to REMOVE vs
/// LEAVE-UNSET when nothing survives filtering.
pub(crate) fn build_anthropic_custom_headers(
    headers: &ScHeaders,
    inherited: Option<&str>,
    inherited_present: bool,
) -> CustomHeaders {
    fn is_dangerous(c: char) -> bool {
        (c as u32) < 0x20
            || c == '\u{7f}'
            || matches!(c, '\u{85}' | '\u{2028}' | '\u{2029}')
    }

    let mut lines: Vec<String> = Vec::new();

    if let Some(raw) = inherited {
        for line in raw.split('\n') {
            let trimmed = line.trim_end_matches('\r');
            if trimmed.trim().is_empty() || trimmed.chars().any(is_dangerous) {
                continue; // blank, or carries a control / line-break code point
            }
            let name = trimmed.split(':').next().unwrap_or("").trim();
            if name.to_ascii_lowercase().starts_with("x-sc-") {
                continue; // our flags own the x-sc-* namespace
            }
            lines.push(trimmed.to_string());
        }
    }

    for (name, value) in headers.iter() {
        lines.push(format!("{name}: {value}"));
    }

    if !lines.is_empty() {
        CustomHeaders::Set(lines.join("\n"))
    } else if inherited_present {
        // The var existed but nothing survived filtering — clear it so the
        // child cannot inherit the raw (unfiltered / non-UTF-8) value.
        CustomHeaders::Remove
    } else {
        CustomHeaders::LeaveUnset
    }
}

pub async fn run(
    args: &[String],
    model_override: Option<&str>,
    headers: &ScHeaders,
) -> Result<ExitCode> {
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

    let mut launcher = ChildLauncher::new("claude")
        .env("ANTHROPIC_BASE_URL", &base_url)
        .env("ANTHROPIC_API_KEY", &key.plaintext_key)
        .env("ANTHROPIC_MODEL", &model);

    // A model pin is a HARD, no-cascade pin applied to EVERY request the child
    // makes — including Claude Code's cheap background/small-model calls (topic
    // titles etc.), which will all be forced onto the pinned model. Warn so the
    // cost/behavior consequence isn't a surprise (Fable #64 rev1 M1).
    if headers.iter().any(|(n, _)| n == super::sc_headers::H_MODEL_PIN) {
        eprintln!(
            "[WARN] --model-pin forces EVERY request (incl. background/small-model \
             calls) onto the pinned model with no cascade. Expect higher cost if the \
             pin is a large model."
        );
    }

    // Inject the validated x-sc-* headers, safely merged onto any pre-existing
    // ANTHROPIC_CUSTOM_HEADERS (preserving the user's own headers such as
    // anthropic-beta while stripping control/x-sc-* lines — see the builder).
    // Read presence via var_os so a non-UTF-8 inherited value is still detected
    // (and cleared) rather than mistaken for "no value" (Fable #64 rev2 NEW-M1).
    let inherited_os = std::env::var_os("ANTHROPIC_CUSTOM_HEADERS");
    let inherited_present = inherited_os.is_some();
    let inherited_utf8 = inherited_os.as_ref().and_then(|s| s.to_str());
    match build_anthropic_custom_headers(headers, inherited_utf8, inherited_present) {
        CustomHeaders::Set(custom) => {
            if !headers.is_empty() {
                eprintln!(
                    "[INFO] Injecting x-sc-* headers: {}",
                    headers.iter().map(|(n, _)| n).collect::<Vec<_>>().join(", ")
                );
            }
            launcher = launcher.env("ANTHROPIC_CUSTOM_HEADERS", &custom);
        }
        CustomHeaders::Remove => {
            // Inherited value existed but nothing survived filtering — clear it
            // so the child can't inherit the raw/unfiltered bytes.
            launcher = launcher.env_remove("ANTHROPIC_CUSTOM_HEADERS");
        }
        CustomHeaders::LeaveUnset => {}
    }

    launcher
        .scrub_stratoclave_tokens()
        .scrub_aws_identity()
        .run_with_revoke(args, &base_url, &tokens.access_token, &key.key_id)
        .await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(h: &ScHeaders, inherited: Option<&str>) -> String {
        match build_anthropic_custom_headers(h, inherited, inherited.is_some()) {
            CustomHeaders::Set(s) => s,
            other => panic!("expected Set, got {other:?}"),
        }
    }

    #[test]
    fn custom_headers_exact_lines_no_splitting() {
        let h = ScHeaders::validated(
            Some("team-a".into()),
            None,
            Some("openai.gpt-5.4/pin:1".into()),
        )
        .unwrap();
        let v = set(&h, None);
        let lines: Vec<&str> = v.split('\n').collect();
        assert_eq!(lines.len(), 2); // exactly one line per present header
        assert_eq!(lines[0], "x-sc-group-id: team-a");
        assert_eq!(lines[1], "x-sc-model-pin: openai.gpt-5.4/pin:1");
        // ": " split round-trips the value even though values contain ':'
        assert_eq!(lines[1].split_once(": ").unwrap().1, "openai.gpt-5.4/pin:1");
    }

    #[test]
    fn leave_unset_when_no_flags_and_no_inherited() {
        assert_eq!(
            build_anthropic_custom_headers(&ScHeaders::none(), None, false),
            CustomHeaders::LeaveUnset
        );
    }

    #[test]
    fn merge_preserves_user_headers_and_appends_ours() {
        let h = ScHeaders::validated(Some("team-a".into()), None, None).unwrap();
        let v = set(&h, Some("anthropic-beta: feature-x\nx-custom: keepme"));
        let lines: Vec<&str> = v.split('\n').collect();
        assert_eq!(
            lines,
            vec![
                "anthropic-beta: feature-x",
                "x-custom: keepme",
                "x-sc-group-id: team-a",
            ]
        );
    }

    #[test]
    fn merge_passes_through_inherited_when_no_flags() {
        // No flags but a pre-existing value: preserve it (don't drop the user's
        // headers just because we added nothing).
        let v = set(&ScHeaders::none(), Some("anthropic-beta: feature-x"));
        assert_eq!(v, "anthropic-beta: feature-x");
    }

    #[test]
    fn merge_strips_inherited_xsc_and_control_lines() {
        let h = ScHeaders::validated(Some("real-group".into()), None, None).unwrap();
        // Inherited tries to sneak an x-sc-group-id AND a CR-splitting line.
        let v = set(
            &h,
            Some("x-sc-group-id: hijack\nok: fine\nevil: a\rx-injected: 1"),
        );
        let lines: Vec<&str> = v.split('\n').collect();
        // The inherited x-sc-group-id is dropped (ours wins); the CR line is
        // dropped entirely; the clean "ok" line survives; ours is appended.
        assert_eq!(lines, vec!["ok: fine", "x-sc-group-id: real-group"]);
        assert!(!v.contains("hijack"));
        assert!(!v.contains("x-injected"));
        assert!(!v.contains('\r'));
    }

    // NEW-M1 (rev2): when every inherited line is filtered out and no flags are
    // set, the var must be REMOVED, not left unset (else the child inherits the
    // raw unfiltered value).
    #[test]
    fn remove_when_inherited_all_filtered_and_no_flags() {
        // Only an x-sc smuggle line, no flags -> nothing survives -> Remove.
        assert_eq!(
            build_anthropic_custom_headers(&ScHeaders::none(), Some("x-sc-group-id: hijack"), true),
            CustomHeaders::Remove
        );
        // A CR-splitting-only inherited value, no flags -> Remove.
        assert_eq!(
            build_anthropic_custom_headers(&ScHeaders::none(), Some("evil: a\rx-injected: 1"), true),
            CustomHeaders::Remove
        );
        // Present-but-non-UTF-8 (inherited=None but present=true) -> Remove.
        assert_eq!(
            build_anthropic_custom_headers(&ScHeaders::none(), None, true),
            CustomHeaders::Remove
        );
    }

    // NEW-L1 (rev2): exotic line-break code points inside an inherited line
    // cause that line to be dropped, not passed through.
    #[test]
    fn merge_drops_lines_with_exotic_linebreaks() {
        let inherited = "anthropic-beta: x\u{2028}x-sc-model-pin: evil";
        let v = set(&ScHeaders::validated(Some("g".into()), None, None).unwrap(), Some(inherited));
        // The whole U+2028-bearing line is dropped; only our flag remains.
        assert_eq!(v, "x-sc-group-id: g");
        assert!(!v.contains("evil"));
        assert!(!v.contains('\u{2028}'));
    }
}
