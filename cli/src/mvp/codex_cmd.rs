//! `stratoclave codex -- [args]` subcommand.
//!
//! Launches OpenAI codex as a child process with `CODEX_HOME` pointing at a
//! directory we own, containing a `config.toml` we generate: a `stratoclave`
//! model provider targeting the deployment's `/openai/v1/responses` endpoint.
//! The user's persistent `~/.codex/config.toml` is therefore **never** loaded
//! for this invocation.
//!
//! That directory is durable by default (`~/.stratoclave/codex-state`, see
//! `codex_home`), because `CODEX_HOME` also holds the sessions `codex resume`
//! reads and the directory-trust answers codex records. `config.toml` is the only
//! file the wrapper owns there and is rewritten per run;
//! `--ephemeral-codex-state` goes back to a temp dir that is deleted on exit.
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

use anyhow::{bail, Context, Result};
use std::process::{Command, ExitCode};

use super::child_launcher::ChildLauncher;
use super::codex_home::{CodexHome, PreservedConfig};
use super::config::MvpConfig;
use super::ephemeral_key::mint_ephemeral_key_scoped;
use super::sc_headers::ScHeaders;
use super::tokens::load as load_tokens;

pub async fn run(
    args: &[String],
    model_override: Option<&str>,
    headers: &ScHeaders,
    state_dir: Option<&str>,
    ephemeral_state: bool,
) -> Result<ExitCode> {
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

    // The `http_headers` provider key that carries our x-sc-* headers is only
    // honored by codex-cli >= 0.141. On an older codex the headers are silently
    // dropped — a VSR pin would vanish with no error (Fable #64 rev1 H1). So
    // when any x-sc flag is set, preflight the version and hard-error if the
    // binary is too old rather than launch into a silent policy bypass.
    if !headers.is_empty() {
        preflight_codex_supports_http_headers()?;
    }

    // Build the temp config + escape workspace BEFORE minting the key, so a
    // filesystem failure here never leaves a live ephemeral key un-revoked
    // (Fable #64 rev1 L1: nothing fallible must sit between mint and
    // run_with_revoke).
    // No extra `.context` here: the errors from `prepare` already name the
    // directory and the reason, and only the outermost message is printed.
    let codex_home = CodexHome::prepare(state_dir, ephemeral_state)?;
    // The renderer receives the keys codex owns in an existing config (trust
    // answers first among them); they are carried across the rewrite while the
    // wrapper regenerates only its own.
    codex_home
        .write_config(|preserved| codex_config_body(&base_url, &model, headers, preserved))?;
    if codex_home.is_durable() {
        eprintln!(
            "[INFO] codex state (sessions, history, directory trust) persists in {}; \
             resume with `stratoclave codex -- resume --last` (plain `codex resume` \
             reads ~/.codex and will not find it), or pass --ephemeral-codex-state \
             to keep nothing.",
            codex_home.path().display()
        );
    }

    if !headers.is_empty() {
        eprintln!(
            "[INFO] Injecting x-sc-* headers: {}",
            headers
                .iter()
                .map(|(n, _)| n)
                .collect::<Vec<_>>()
                .join(", ")
        );
    }
    if headers
        .iter()
        .any(|(n, _)| n == super::sc_headers::H_MODEL_PIN)
    {
        eprintln!(
            "[WARN] --model-pin is a hard, no-cascade pin applied to every request; \
             codex's prompt-budget window is still derived from --model, so pin and \
             model should refer to the same family."
        );
    }

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
    //
    // Created BEFORE the key mint (Fable #64 rev1 L1): it is fallible, and no
    // fallible step may sit between mint and run_with_revoke.
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

    // Mint the scoped wrapper key LAST among fallible setup steps. Only the
    // two best-effort `eprintln!`s below sit between here and run_with_revoke;
    // they can only fail if stderr is closed, in which case the process is
    // already being torn down and the 30-min key TTL bounds the exposure
    // (Fable #64 rev2 NEW-L3).
    let key = mint_ephemeral_key_scoped(
        &config.api_endpoint,
        &tokens.access_token,
        "stratoclave-codex-wrapper",
        &["responses:send"],
    )
    .await
    .context("Failed to mint ephemeral wrapper key for codex")?;

    eprintln!(
        "[INFO] Launching codex via Stratoclave proxy (base_url={}, model={}, key={})",
        base_url, model, key.key_id
    );
    eprintln!(
        "[INFO] Child process uses an ephemeral responses-only API key; \
         the Cognito bearer is not exported and the user's ~/.codex/config.toml \
         is untouched."
    );

    let mut launcher = ChildLauncher::new("codex")
        .env("CODEX_HOME", codex_home.path())
        .env("STRATOCLAVE_OPENAI_KEY", &key.plaintext_key)
        .scrub_stratoclave_tokens()
        .scrub_aws_identity()
        .bypass_hint(
            "Check that codex is still resolving the `stratoclave` model provider: \
             a `-c model_provider=...` override, a profile, or an OPENAI_* key in \
             the environment will send the run straight to the provider.",
        )
        // codex's own retry loop treats every non-2xx from the provider as a
        // transient disconnect: on a real 402 (tenant out of budget) it prints
        // "ERROR: Reconnecting... 1/5" .. "5/5" BEFORE it ever shows the actual
        // refusal, which reads exactly like a broken network. The raw refusal
        // does eventually print (codex logs "unexpected status <code> ...:
        // <json>" once retries are exhausted), so nothing is lost — but it is
        // easy to miss under five misleading retry lines. Once that line
        // arrives, immediately follow it with the same body rendered as prose
        // (wall / reason / whether it can be raised), so a reader does not have
        // to parse raw JSON out of a log line to learn this was not a network
        // problem. See `extract_terminal_4xx_body` / `format_gateway_rejection`.
        .on_stderr_line(|line| {
            let (status, body) = extract_terminal_4xx_body(line)?;
            Some(format_gateway_rejection(status, &body))
        });
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

    // Drop deterministically so an ephemeral CODEX_HOME and the escape workspace
    // do not survive the wrapper exit. `TempDir::drop` auto-deletes and SWALLOWS
    // any FS error (use `.close()` if surfacing that error ever matters); the
    // explicit drops just pin the lifetime past `run_with_revoke` so the child
    // could read them. A durable CODEX_HOME holds no TempDir, so it stays.
    drop(escape_workspace);
    drop(codex_home);
    result
}

/// Minimum codex-cli version whose `[model_providers.*].http_headers` key is
/// honored. Below this, the key is silently ignored and our x-sc-* headers —
/// including a VSR model pin — would vanish with no error.
const CODEX_HTTP_HEADERS_MIN: (u32, u32, u32) = (0, 141, 0);

/// Parse a `(major, minor, patch)` triple from a `codex --version` line such
/// as `codex-cli 0.141.0`. Returns None if no dotted numeric version is found.
fn parse_dotted_version(tok: &str) -> Option<(u32, u32, u32)> {
    let core = tok.trim_start_matches('v');
    let mut it = core.split('.');
    let (a, b) = (it.next()?, it.next()?);
    let (major, minor) = (a.parse::<u32>().ok()?, b.parse::<u32>().ok()?);
    // Patch is optional; strip any trailing non-digits (pre-release suffix).
    let patch = it
        .next()
        .map(|p| {
            p.chars()
                .take_while(|c| c.is_ascii_digit())
                .collect::<String>()
                .parse::<u32>()
                .unwrap_or(0)
        })
        .unwrap_or(0);
    Some((major, minor, patch))
}

/// Parse codex's OWN version from `codex --version` output. ANCHORED to the
/// `codex`/`codex-cli` token (Fable review): a bare "first dotted token
/// anywhere" scan false-accepts a shim/runtime banner (e.g. `node v20.5.1`
/// printed before `codex-cli 0.136.2`), which would pass the >=0.141 gate and
/// silently launch an old codex that drops the x-sc-* headers — exactly the
/// H1 failure the gate exists to prevent. We take the dotted version token
/// immediately following a `codex`/`codex-cli` token; only if no such anchor
/// exists do we fall back to the first dotted token (best-effort, still
/// followed by warn-and-proceed at the call site on ambiguity).
fn parse_codex_version(output: &str) -> Option<(u32, u32, u32)> {
    let toks: Vec<&str> = output.split_whitespace().collect();
    for w in toks.windows(2) {
        let name = w[0].trim_end_matches(':').to_ascii_lowercase();
        if name == "codex" || name == "codex-cli" {
            if let Some(v) = parse_dotted_version(w[1]) {
                return Some(v);
            }
        }
    }
    // No anchored `codex <version>` found — fall back to the first dotted token.
    toks.iter().find_map(|t| parse_dotted_version(t))
}

/// Hard-fail when the installed codex is too old to honor `http_headers`, so a
/// requested x-sc-* header (esp. a VSR model pin) is never silently dropped
/// (Fable #64 rev1 H1). If the version can't be determined, warn and proceed —
/// the alternative (blocking on an unparseable `--version`) is more hostile
/// than a warning, and the backend still validates whatever does arrive.
fn preflight_codex_supports_http_headers() -> Result<()> {
    let out = match Command::new("codex").arg("--version").output() {
        Ok(o) => o,
        Err(e) => {
            eprintln!(
                "[WARN] Could not run `codex --version` to verify x-sc-* header \
                 support ({e}); proceeding, but headers require codex >= 0.141."
            );
            return Ok(());
        }
    };
    // A non-zero exit means `--version` didn't do what we think; don't trust a
    // version token scraped from an error/usage banner (Fable #64 rev2 NEW-L2).
    if !out.status.success() {
        eprintln!(
            "[WARN] `codex --version` exited non-zero; cannot verify x-sc-* header \
             support (requires codex >= 0.141). Proceeding."
        );
        return Ok(());
    }
    // Parse stdout FIRST (the real version line), only falling back to stderr,
    // so a runtime/shim banner on stdout can't false-accept.
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    let parsed = parse_codex_version(&stdout).or_else(|| parse_codex_version(&stderr));
    match parsed {
        Some(v) if v >= CODEX_HTTP_HEADERS_MIN => Ok(()),
        Some((a, b, c)) => bail!(
            "codex {a}.{b}.{c} does not support the `http_headers` provider key \
             (needs >= {}.{}.{}); the x-sc-* headers (incl. --model-pin) would be \
             silently dropped. Upgrade codex, or drop the flags.",
            CODEX_HTTP_HEADERS_MIN.0,
            CODEX_HTTP_HEADERS_MIN.1,
            CODEX_HTTP_HEADERS_MIN.2,
        ),
        None => {
            eprintln!(
                "[WARN] Could not parse codex version from `codex --version`; \
                 x-sc-* headers require codex >= 0.141."
            );
            Ok(())
        }
    }
}

/// Any 4xx codex will not talk itself out of by retrying, EXCEPT 429: rate
/// limiting is the one 4xx that genuinely can go away with a delay, so it is
/// left to codex's own retry loop rather than flagged as a policy refusal.
fn is_terminal_4xx(status: u16) -> bool {
    (400..500).contains(&status) && status != 429
}

/// codex-cli 0.141.0 logs a failed provider call on ONE line of the shape:
///
///   ERROR: unexpected status <code> <reason phrase>: <json>, url: <url>
///
/// (verified 2026-09-02 against a live gateway 402 — see the codex_cmd.rs
/// module report; codex does not distinguish this from a network error in
/// its own retry loop, hence "Reconnecting..." above it). Returns the status
/// and the embedded JSON body when the line matches AND the status is a
/// terminal 4xx; `None` for anything else, including codex's "Reconnecting...
/// N/5" lines (which carry no status at all) and retryable / success statuses.
fn extract_terminal_4xx_body(line: &str) -> Option<(u16, String)> {
    const MARKER: &str = "unexpected status ";
    let after_marker = &line[line.find(MARKER)? + MARKER.len()..];
    let status_str: String = after_marker
        .chars()
        .take_while(|c| c.is_ascii_digit())
        .collect();
    if status_str.len() != 3 {
        return None;
    }
    let status: u16 = status_str.parse().ok()?;
    if !is_terminal_4xx(status) {
        return None;
    }
    let brace_start = after_marker.find('{')?;
    let json = extract_balanced_json(&after_marker[brace_start..])?;
    Some((status, json))
}

/// Return the substring of `s` (which must start with `{`) up to and
/// including the matching closing brace, honouring JSON string/escape syntax
/// so a `{`/`}` inside a quoted message — or in the `, url: ...` text codex
/// appends after the JSON on the same line — cannot mis-close it early.
fn extract_balanced_json(s: &str) -> Option<String> {
    let mut depth = 0i32;
    let mut in_string = false;
    let mut escaped = false;
    for (i, c) in s.char_indices() {
        if in_string {
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                in_string = false;
            }
            continue;
        }
        match c {
            '"' => in_string = true,
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(s[..i + 1].to_string());
                }
            }
            _ => {}
        }
    }
    None
}

/// Render the gateway's `credit_exhausted` 402 (or any other terminal 4xx)
/// body as prose, distinguishing "the gateway refused this on purpose" from
/// "the network broke". Every figure/name it prints is read verbatim out of
/// the body the gateway actually sent (`mvp/_pipeline.py::_refusal_body` on
/// the backend) — never invented — so it is silent about a field rather than
/// guessing when that field is genuinely absent (e.g. a 404 has no `wall`).
fn format_gateway_rejection(status: u16, body_json: &str) -> String {
    let parsed: Option<serde_json::Value> = serde_json::from_str(body_json).ok();
    let detail = parsed.as_ref().and_then(|v| v.get("detail")).or(parsed.as_ref());
    let get_str = |key: &str| detail.and_then(|d| d.get(key)).and_then(|v| v.as_str());
    let message = get_str("message");
    let wall = get_str("wall");
    let blocker = get_str("blocker");
    let grantable = detail
        .and_then(|d| d.get("grantable"))
        .and_then(|v| v.as_bool());

    let mut out = format!(
        "[STRATOCLAVE] HTTP {status} from the gateway is a policy refusal, not a \
         network error — the retries above will not fix it."
    );
    if let Some(m) = message {
        out.push_str(&format!("\n[STRATOCLAVE]   {m}"));
    }
    if wall.is_some() || blocker.is_some() || grantable.is_some() {
        let mut fields = String::new();
        if let Some(w) = wall {
            fields.push_str(&format!(" wall={w}"));
        }
        if let Some(b) = blocker {
            fields.push_str(&format!(" blocker={b}"));
        }
        if let Some(g) = grantable {
            fields.push_str(&format!(" grantable={g}"));
        }
        out.push_str(&format!("\n[STRATOCLAVE]  {fields}"));
    }
    match grantable {
        Some(true) => out.push_str(
            "\n[STRATOCLAVE]   Ask an admin to raise it: `stratoclave limit-raise request \
             --limit-usd <amount> --reason <reason>`.",
        ),
        Some(false) => out.push_str(
            "\n[STRATOCLAVE]   This wall cannot be raised by request; wait for the period \
             to roll over, or ask an admin to change the policy directly.",
        ),
        None => {}
    }
    out
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

/// Render the optional `http_headers` inline table for the stratoclave
/// provider block. Empty string when no headers are set, so the generated
/// TOML is byte-identical to the pre-feature output in the common case.
///
/// TOML safety: values are emitted verbatim inside basic ("...") strings.
/// Basic strings require escaping only for `"`, `\`, and control chars — all
/// of which are excluded by the ScHeaders grammars ([A-Za-z0-9._:-] /
/// [A-Za-z0-9._:/-]). The grammar therefore closes TOML injection entirely;
/// no escaping pass is needed, and prop_codex_toml_roundtrip proves it with
/// the `toml` crate as oracle. An inline table (rather than a
/// `[model_providers.stratoclave.http_headers]` sub-table header) keeps this
/// a plain key inside the provider table, avoiding TOML's "bare keys must
/// precede sub-tables" ordering pitfall.
pub(crate) fn sc_http_headers_toml(headers: &ScHeaders) -> String {
    let pairs: Vec<String> = headers
        .iter()
        .map(|(name, value)| format!(r#""{name}" = "{value}""#))
        .collect();
    if pairs.is_empty() {
        String::new()
    } else {
        format!("http_headers           = {{ {} }}\n", pairs.join(", "))
    }
}

/// Render the `config.toml` the wrapper owns.
///
/// `preserved` carries the keys codex wrote that the wrapper does not manage (see
/// `codex_home`). Its two halves land on opposite sides of the provider table:
/// bare keys must precede the first table header, or TOML binds them to that
/// table and a preserved `approval_policy` silently becomes
/// `model_providers.stratoclave.approval_policy`.
pub(crate) fn codex_config_body(
    base_url: &str,
    model: &str,
    headers: &ScHeaders,
    preserved: &PreservedConfig,
) -> String {
    format!(
        r#"# Auto-generated by `stratoclave codex` — do not edit above the carried-over
# section: this file is rewritten on every proxied run. Directory-trust answers
# and other codex-owned keys are preserved.

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
{preserved_top}
[model_providers.stratoclave]
name                   = "Stratoclave (OpenAI via Bedrock)"
base_url               = "{base_url}"
wire_api               = "responses"
env_key                = "STRATOCLAVE_OPENAI_KEY"
request_max_retries    = 3
stream_max_retries     = 5
stream_idle_timeout_ms = 600000
{http_headers}{preserved_tables}"#,
        context_window = codex_context_window_for(model),
        http_headers = sc_http_headers_toml(headers),
        preserved_top = preserved.top_level,
        preserved_tables = preserved.tables,
    )
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
        "openai.gpt-5.6-sol" | "gpt-5.6-sol" => 400_000,
        "openai.gpt-5.6-terra" | "gpt-5.6-terra" => 400_000,
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
    use super::super::sc_headers::ScHeaders;
    use super::*;

    // Dependency-free deterministic xorshift64* PRNG (see sc_headers tests).
    struct Rng(u64);
    impl Rng {
        fn new(seed: u64) -> Self {
            Rng(seed | 1)
        }
        fn next_u64(&mut self) -> u64 {
            let mut x = self.0;
            x ^= x >> 12;
            x ^= x << 25;
            x ^= x >> 27;
            self.0 = x;
            x.wrapping_mul(0x2545_F491_4F6C_DD1D)
        }
        fn range_incl(&mut self, lo: usize, hi: usize) -> usize {
            lo + (self.next_u64() % (hi - lo + 1) as u64) as usize
        }
        fn below(&mut self, n: usize) -> usize {
            (self.next_u64() % n as u64) as usize
        }
    }

    /// The gateway's own registry decides which OpenAI ids it will serve, and
    /// this table decides what window codex is told about them. Two files, one
    /// fact, and nothing compared them: the table still named `gpt-5.4` and
    /// `gpt-5.5` after the registry had moved to the 5.6 family, so every real
    /// call fell through to the 200k default and codex budgeted prompts against
    /// half the window it had. Read the registry and require an entry, rather
    /// than trusting that whoever adds the next model remembers this file.
    ///
    /// The window VALUES cannot be derived — the registry does not record them —
    /// so this checks coverage, not correctness of the number. A new id with the
    /// wrong window is a different mistake, and one a reader can at least see.
    #[test]
    fn every_served_openai_model_has_an_explicit_context_window() {
        let registry = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../backend/mvp/defaults/models.json");
        let text = std::fs::read_to_string(&registry)
            .unwrap_or_else(|e| panic!("cannot read {}: {e}", registry.display()));
        let ids: Vec<String> = text
            .split('"')
            .filter(|t| t.starts_with("openai."))
            .map(|t| t.to_string())
            .collect();
        assert!(
            !ids.is_empty(),
            "found no openai.* ids in {} — this check would pass vacuously",
            registry.display()
        );
        for id in ids {
            let short = id.trim_start_matches("openai.");
            assert_eq!(
                codex_context_window_for(&id),
                400_000,
                "{id} is served by the gateway but falls back to the 200k default"
            );
            assert_eq!(
                codex_context_window_for(short),
                400_000,
                "{short} (the short alias codex may be given) falls back to the 200k default"
            );
        }
    }


    #[test]
    fn temp_config_disables_project_root_markers() {
        let body = codex_config_body(
            "https://example.test/openai/v1",
            "openai.gpt-5.4",
            &ScHeaders::none(),
            &PreservedConfig::default(),
        );
        assert!(
            body.contains("project_root_markers = []"),
            "expected project_root_markers = [] to short-circuit ~/.codex walk; got:\n{}",
            body
        );
    }

    #[test]
    fn temp_config_pins_model_context_window() {
        let body = codex_config_body(
            "https://example.test/openai/v1",
            "openai.gpt-5.5",
            &ScHeaders::none(),
            &PreservedConfig::default(),
        );
        assert!(
            body.contains("model_context_window = 400000"),
            "expected model_context_window = 400000 for openai.gpt-5.5; got:\n{}",
            body
        );
    }

    // P4: for any validated ScHeaders, the generated config.toml parses and
    // its http_headers table deserializes to exactly the input map. Uses the
    // `toml` crate (already a dep) as an independent oracle — this proves the
    // grammar closes TOML injection (no escaping needed).
    #[test]
    fn prop_codex_toml_roundtrip() {
        const ID_SET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-";
        let mut rng = Rng::new(0x70_11);
        for _ in 0..200 {
            let mut gen = |max: usize, slash: bool| -> String {
                let len = rng.range_incl(1, max);
                (0..len)
                    .map(|_| {
                        if slash && rng.below(8) == 0 {
                            '/'
                        } else {
                            ID_SET[rng.below(ID_SET.len())] as char
                        }
                    })
                    .collect()
            };
            let (g, w, p) = (gen(64, false), gen(64, false), gen(128, true));
            let h = ScHeaders::validated(Some(g.clone()), Some(w.clone()), Some(p.clone()))
                .expect("generated values must validate");
            let body = codex_config_body(
                "https://example.test/openai/v1",
                "openai.gpt-5.4",
                &h,
                &PreservedConfig::default(),
            );
            let parsed: toml::Value = toml::from_str(&body).expect("generated TOML must parse");
            let ht = parsed["model_providers"]["stratoclave"]["http_headers"]
                .as_table()
                .expect("http_headers table");
            assert_eq!(ht["x-sc-group-id"].as_str(), Some(g.as_str()));
            assert_eq!(ht["x-sc-workflow-run-id"].as_str(), Some(w.as_str()));
            assert_eq!(ht["x-sc-model-pin"].as_str(), Some(p.as_str()));
            assert_eq!(ht.len(), 3);
        }
    }

    #[test]
    fn version_parse_and_gate() {
        assert_eq!(parse_codex_version("codex-cli 0.141.0"), Some((0, 141, 0)));
        assert_eq!(parse_codex_version("codex-cli 0.136.2"), Some((0, 136, 2)));
        assert_eq!(parse_codex_version("codex 1.2"), Some((1, 2, 0)));
        assert_eq!(
            parse_codex_version("codex-cli 0.142.0-beta.1"),
            Some((0, 142, 0))
        );
        assert_eq!(parse_codex_version("no version here"), None);
        // Gate: 0.141.0 is the floor; 0.140.x is too old, 0.141+/1.x are fine.
        assert!((0, 141, 0) >= CODEX_HTTP_HEADERS_MIN);
        assert!((0, 142, 0) >= CODEX_HTTP_HEADERS_MIN);
        assert!((1, 0, 0) >= CODEX_HTTP_HEADERS_MIN);
        assert!(!((0, 140, 9) >= CODEX_HTTP_HEADERS_MIN));
    }

    #[test]
    fn version_parse_anchors_to_codex_token_not_banner() {
        // Fable review: a shim/runtime banner printed before the real line must
        // NOT false-accept. The parse must return codex's OWN version, so an old
        // codex is correctly gated out even when a newer-looking token precedes.
        assert_eq!(
            parse_codex_version("Now using node v20.5.1\ncodex-cli 0.136.2"),
            Some((0, 136, 2)),
        );
        // Property this pins: banner tokens before the codex anchor are ignored.
        let old = parse_codex_version("node v22.1.0\ncodex-cli 0.140.0").unwrap();
        assert!(
            !(old >= CODEX_HTTP_HEADERS_MIN),
            "old codex must fail the gate despite the node banner"
        );
        // A colon after the name is tolerated.
        assert_eq!(parse_codex_version("codex: 0.141.0"), Some((0, 141, 0)));
    }

    #[test]
    fn no_http_headers_table_when_no_flags() {
        let body = codex_config_body(
            "https://example.test/openai/v1",
            "openai.gpt-5.4",
            &ScHeaders::none(),
            &PreservedConfig::default(),
        );
        assert!(
            !body.contains("http_headers"),
            "unexpected http_headers:\n{body}"
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

    /// The exact line codex-cli 0.141.0 printed against a live gateway 402
    /// (scquota, 2026-09-02, pool budget forced to $0.01 to reproduce). Pinned
    /// verbatim so a change in either codex's log format or our parser is
    /// caught by a real-world shape, not a hand-simplified stand-in.
    const REAL_402_LINE: &str = r#"ERROR: unexpected status 402 Payment Required: {"detail":{"type":"credit_exhausted","reason":"request_does_not_fit_pool_limit","message":"This request's reservation exceeds the tenant's entire budget for the period; no amount of available headroom would admit it. Reduce the request size or ask your admin to raise the budget. This request is $0.45 short of this tenant's pool.","wall":"tenant_dollar_pool","blocker":"tenant_pool","grantable":true,"raise_hint":{"candidates":[{"blocker":"tenant_pool","wall":"tenant_dollar_pool","model_id":"openai.gpt-5.6-sol","estimated_cost_microusd":469701,"shortfall_microusd":459701,"grantable":true,"grant_expired":false}],"remaining_cap_microusd":10000,"reason_codes":["onboarding","usage_spike","migration","incident_response","other"],"minimum_raise_microusd":459701,"unattempted_model_ids":[],"tenant_id":"default-org","requested_model_id":"openai.gpt-5.6-sol","target_shortfall_microusd":459701,"router_mode":"fallback_disabled","pricing_version":"builtin","priced_at":"2026-09-02T17:22:04+00:00"}}}, url: https://d1234.cloudfront.net/openai/v1/responses"#;

    #[test]
    fn extracts_real_402_line_from_live_gateway() {
        let (status, body) = extract_terminal_4xx_body(REAL_402_LINE)
            .expect("must extract the pinned real 402 line");
        assert_eq!(status, 402);
        let parsed: serde_json::Value = serde_json::from_str(&body).expect("valid JSON");
        assert_eq!(parsed["detail"]["wall"], "tenant_dollar_pool");
        assert_eq!(parsed["detail"]["blocker"], "tenant_pool");
        assert_eq!(parsed["detail"]["grantable"], true);
    }

    #[test]
    fn formats_real_402_as_prose_naming_wall_and_grantability() {
        let (status, body) = extract_terminal_4xx_body(REAL_402_LINE).unwrap();
        let rendered = format_gateway_rejection(status, &body);
        assert!(rendered.contains("HTTP 402"));
        assert!(rendered.contains("not a network error"));
        assert!(rendered.contains("wall=tenant_dollar_pool"));
        assert!(rendered.contains("blocker=tenant_pool"));
        assert!(rendered.contains("grantable=true"));
        // The message field is carried verbatim, not summarized away.
        assert!(rendered.contains("$0.45 short of this tenant's pool"));
        // grantable=true -> tells the reader HOW to raise it.
        assert!(rendered.contains("limit-raise request"));
    }

    #[test]
    fn reconnecting_lines_never_match() {
        for n in 1..=5 {
            assert!(extract_terminal_4xx_body(&format!("ERROR: Reconnecting... {n}/5")).is_none());
        }
    }

    #[test]
    fn retryable_statuses_are_not_flagged_as_terminal() {
        // 429 (rate limit) and every 5xx genuinely can go away with a retry;
        // only 400-499 minus 429 is a policy refusal codex should stop
        // hammering on its own.
        assert!(!is_terminal_4xx(429));
        assert!(!is_terminal_4xx(500));
        assert!(!is_terminal_4xx(503));
        assert!(!is_terminal_4xx(200));
        for status in [400, 401, 402, 403, 404, 409, 413, 422] {
            assert!(is_terminal_4xx(status), "{status} should be terminal");
        }
    }

    #[test]
    fn extract_terminal_4xx_body_ignores_retryable_status_even_with_json() {
        let line = r#"ERROR: unexpected status 429 Too Many Requests: {"detail":{"message":"slow down"}}, url: https://example.test/"#;
        assert!(extract_terminal_4xx_body(line).is_none());
    }

    #[test]
    fn extract_balanced_json_stops_at_matching_brace_not_first_close() {
        // A `}` inside a quoted string, and trailing text with its own
        // (unbalanced-looking) punctuation, must not truncate the JSON early.
        let s = r#"{"message":"a } b","n":1}, url: https://x/{not-json}"#;
        let json = extract_balanced_json(s).expect("balanced JSON");
        assert_eq!(json, r#"{"message":"a } b","n":1}"#);
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["message"], "a } b");
    }

    #[test]
    fn format_gateway_rejection_never_invents_fields_it_does_not_have() {
        // A minimal body with no `wall`/`blocker`/`grantable` at all (e.g. a
        // bare 404) must not fabricate them.
        let rendered = format_gateway_rejection(404, r#"{"detail":{"message":"not found"}}"#);
        assert!(rendered.contains("HTTP 404"));
        assert!(rendered.contains("not found"));
        assert!(!rendered.contains("wall="));
        assert!(!rendered.contains("grantable="));
    }

    #[test]
    fn format_gateway_rejection_survives_unparseable_body() {
        // Even if the body is not JSON at all, the function must not panic
        // and must still say SOMETHING distinguishing this from a network
        // error (the status line alone).
        let rendered = format_gateway_rejection(402, "not json");
        assert!(rendered.contains("HTTP 402"));
        assert!(rendered.contains("not a network error"));
    }
}
