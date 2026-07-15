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

use anyhow::{bail, Context, Result};
use std::fs;
use std::process::{Command, ExitCode};

use tempfile::TempDir;

use super::child_launcher::ChildLauncher;
use super::config::MvpConfig;
use super::ephemeral_key::mint_ephemeral_key_scoped;
use super::sc_headers::ScHeaders;
use super::tokens::load as load_tokens;

pub async fn run(
    args: &[String],
    model_override: Option<&str>,
    headers: &ScHeaders,
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
    let codex_home = build_temp_codex_home(&base_url, &model, headers)
        .context("Failed to write temporary codex config")?;

    if !headers.is_empty() {
        eprintln!(
            "[INFO] Injecting x-sc-* headers: {}",
            headers.iter().map(|(n, _)| n).collect::<Vec<_>>().join(", ")
        );
    }
    if headers.iter().any(|(n, _)| n == super::sc_headers::H_MODEL_PIN) {
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

    // Drop the temp dirs deterministically so they do not survive the wrapper
    // exit. `TempDir::drop` auto-deletes and SWALLOWS any FS error (use
    // `.close()` if surfacing that error ever matters); the explicit drops just
    // pin the lifetime past `run_with_revoke` so the child could read them.
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

pub(crate) fn build_temp_codex_home(
    base_url: &str,
    model: &str,
    headers: &ScHeaders,
) -> Result<TempDir> {
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
{http_headers}"#,
        context_window = codex_context_window_for(model),
        http_headers = sc_http_headers_toml(headers),
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
    use super::super::sc_headers::ScHeaders;
    use std::fs;

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

    #[test]
    fn temp_config_disables_project_root_markers() {
        let dir = build_temp_codex_home(
            "https://example.test/openai/v1",
            "openai.gpt-5.4",
            &ScHeaders::none(),
        )
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
        let dir = build_temp_codex_home(
            "https://example.test/openai/v1",
            "openai.gpt-5.5",
            &ScHeaders::none(),
        )
        .expect("build_temp_codex_home");
        let body = fs::read_to_string(dir.path().join("config.toml")).expect("read config");
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
        const ID_SET: &[u8] =
            b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-";
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
            let dir = build_temp_codex_home("https://example.test/openai/v1", "openai.gpt-5.4", &h)
                .expect("build_temp_codex_home");
            let body = fs::read_to_string(dir.path().join("config.toml")).unwrap();
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
        assert_eq!(parse_codex_version("codex-cli 0.142.0-beta.1"), Some((0, 142, 0)));
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
        assert!(!(old >= CODEX_HTTP_HEADERS_MIN), "old codex must fail the gate despite the node banner");
        // A colon after the name is tolerated.
        assert_eq!(parse_codex_version("codex: 0.141.0"), Some((0, 141, 0)));
    }

    #[test]
    fn no_http_headers_table_when_no_flags() {
        let dir = build_temp_codex_home(
            "https://example.test/openai/v1",
            "openai.gpt-5.4",
            &ScHeaders::none(),
        )
        .unwrap();
        let body = fs::read_to_string(dir.path().join("config.toml")).unwrap();
        assert!(!body.contains("http_headers"), "unexpected http_headers:\n{body}");
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
