//! `stratoclave setup <api_endpoint>` subcommand.
//!
//! Single bootstrap entry point for the OSS CLI. Hits the
//! `GET /.well-known/stratoclave-config` of the supplied URL (the
//! CloudFront origin shared by an admin) and writes the response into
//! `~/.stratoclave/config.toml`.
//!
//! Flow:
//!   1. validate the URL (http/https only, no userinfo, SSRF denylist)
//!   2. fetch `{api_endpoint}/.well-known/stratoclave-config` (10 s timeout)
//!   3. enforce `schema_version == "1"`
//!   4. detect an existing `config.toml` → require `--force` or prompt
//!   5. on `--dry-run`, print the rendered TOML and exit
//!   6. back up the prior file as `config.toml.bak.<unix-ts>`
//!   7. write the new file (mode 0o600) and print a summary
//!
//! With `--codex`, additionally backs up `~/.codex/config.toml` and
//! appends a `[model_providers.stratoclave]` block so the system-wide
//! `codex` binary can talk to this deployment without going through
//! `stratoclave codex`. Top-level `model_provider` is only changed
//! after an interactive prompt.

use anyhow::{anyhow, bail, Context, Result};
use serde::Deserialize;
use std::fs;
use std::io::{self, Write};
use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Response schema for `GET /.well-known/stratoclave-config`.
///
/// `cli.codex` is `Option<CodexHints>` so old CLI binaries that hit a
/// new backend (or new CLI binaries that hit an old backend without
/// `CODEX_ENABLED`) deserialize cleanly.
#[derive(Debug, Deserialize)]
struct StratoclaveConfig {
    schema_version: String,
    api_endpoint: String,
    cognito: CognitoInfo,
    cli: CliHints,
}

#[derive(Debug, Deserialize)]
struct CognitoInfo {
    user_pool_id: String,
    client_id: String,
    domain: String,
    region: String,
}

#[derive(Debug, Deserialize)]
struct CodexHints {
    default_model: String,
    openai_base_path: String,
    #[allow(dead_code)]
    supported_regions: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct CliHints {
    default_model: String,
    callback_port: u16,
    #[serde(default)]
    codex: Option<CodexHints>,
}

/// `stratoclave setup <api_endpoint>` entry point.
pub async fn run(
    api_endpoint: String,
    force: bool,
    dry_run: bool,
    codex: bool,
) -> Result<()> {
    // 1. Validate the URL.
    let api_endpoint = validate_url(&api_endpoint)?;

    // 2. Fetch the discovery document.
    let discovery_url = format!(
        "{}/.well-known/stratoclave-config",
        api_endpoint.trim_end_matches('/')
    );
    println!("[INFO] Fetching config from {} ...", discovery_url);

    let config = fetch_config(&discovery_url).await?;

    // 3. Lock to schema_version "1".
    if config.schema_version != "1" {
        bail!(
            "This CLI expects schema_version=1 but received {:?}. \
             You may need to update the CLI.",
            config.schema_version
        );
    }

    // 4. Resolve the destination path.
    let config_dir = resolve_config_dir()?;
    let config_path = config_dir.join("config.toml");

    // 5. Render the new TOML.
    let toml_content = render_toml(&config);

    // 6. --dry-run prints the rendered TOML and exits.
    if dry_run {
        println!("[INFO] --dry-run: not writing to {}", config_path.display());
        println!("---");
        print!("{}", toml_content);
        if !toml_content.ends_with('\n') {
            println!();
        }
        println!("---");
        if codex {
            if let Some(codex_hints) = &config.cli.codex {
                println!("[INFO] --dry-run: would also patch ~/.codex/config.toml");
                println!("---");
                print!("{}", render_codex_toml_block(&api_endpoint, codex_hints));
                println!("---");
            } else {
                println!(
                    "[WARN] --codex requested but the deployment does not advertise \
                     codex support; ~/.codex/config.toml would be left unchanged."
                );
            }
        }
        return Ok(());
    }

    // 7. Existing config.toml — confirm before clobbering.
    if config_path.exists() {
        if !force {
            if !is_stdin_tty() {
                bail!(
                    "~/.stratoclave/config.toml already exists and stdin is not a TTY. \
                     Re-run with --force to overwrite."
                );
            }
            if !prompt_overwrite(&config_path)? {
                println!("[INFO] Aborted. Existing config.toml was not modified.");
                return Ok(());
            }
        }
        // Move the prior file aside before writing the new one.
        let backup = backup_existing(&config_path)?;
        println!("[INFO] Backed up existing config to {}", backup.display());
    }

    // 8. Prepare the config directory (mode 0o700 on Unix).
    ensure_config_dir(&config_dir)?;

    // 9. Write the new config.toml.
    fs::write(&config_path, &toml_content)
        .with_context(|| format!("Failed to write {}", config_path.display()))?;

    // 10. Set 0o600 on Unix.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let perms = fs::Permissions::from_mode(0o600);
        let _ = fs::set_permissions(&config_path, perms);
    }

    // 11. Print a summary.
    print_summary(&config_path, &config);

    // 12. Optional: patch ~/.codex/config.toml.
    if codex {
        match &config.cli.codex {
            Some(codex_hints) => {
                patch_codex_config(&api_endpoint, codex_hints, force)?;
            }
            None => {
                println!(
                    "[WARN] --codex requested but the deployment does not advertise \
                     codex support (well-known did not include cli.codex). \
                     Leaving ~/.codex/config.toml unchanged."
                );
            }
        }
    }

    Ok(())
}

// ------------------------------------------------------------------
// URL validation
// ------------------------------------------------------------------

fn validate_url(raw: &str) -> Result<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        bail!("api_endpoint must not be empty");
    }

    // P0-5 (2026-04 security review): `stratoclave setup <url>` is the
    // single bootstrap channel — whatever URL is accepted here ends up
    // in `~/.stratoclave/config.toml` and drives every subsequent
    // `auth login` / `/v1/messages` / `setup` refresh. The old
    // validator tolerated `http://` and every host string the caller
    // wanted, which let an attacker:
    //
    //   * downgrade the connection to plain HTTP and sniff Cognito
    //     passwords / access tokens, or
    //   * hand a URL like `http://attacker.example/` that the bootstrap
    //     fetch followed through a `302 -> http://169.254.169.254/...`
    //     to read the EC2 IMDS and harvest role credentials, or
    //   * point the bootstrap at `http://localhost:8080/` and trick the
    //     CLI into talking to any local listener.
    //
    // We now enforce (a) HTTPS only, except for explicit `localhost` /
    // `127.0.0.1` for local development, (b) an SSRF denylist covering
    // IMDS, link-local, private ranges, and AWS-internal TLDs, and
    // (c) no `user:password@` userinfo component.
    //
    // Redirect following is disabled separately at the reqwest Client
    // layer so a permitted host cannot bounce the bootstrap into the
    // denylist post-hoc.
    let parsed = url::Url::parse(trimmed).map_err(|e| {
        anyhow!("api_endpoint is not a valid URL: {}", e)
    })?;

    let scheme = parsed.scheme();
    if scheme != "http" && scheme != "https" {
        bail!(
            "api_endpoint must start with http:// or https:// (got {:?})",
            trimmed
        );
    }
    if parsed.username() != "" || parsed.password().is_some() {
        bail!("api_endpoint must not contain userinfo (user:password@host)");
    }

    let host = parsed
        .host_str()
        .map(|h| h.to_ascii_lowercase())
        .filter(|h| !h.is_empty())
        .ok_or_else(|| anyhow!("api_endpoint must include a host"))?;

    let loopback_host = matches!(host.as_str(), "localhost" | "127.0.0.1" | "::1");
    if scheme == "http" && !loopback_host {
        bail!(
            "api_endpoint must use https:// for non-loopback hosts (got {:?}). \
             The bootstrap config is written to disk and reused for every \
             subsequent auth call, so plaintext HTTP would leak every token.",
            trimmed
        );
    }

    // SSRF denylist. We block the AWS IMDS endpoint, link-local, the
    // cloud metadata hostnames used by other providers (defence in
    // depth — we only deploy on AWS today), and typical *.internal /
    // *.local zones so a rogue DNS record cannot bootstrap a CLI into
    // an internal-only service.
    const DENY_HOSTS: &[&str] = &[
        "169.254.169.254",       // EC2 / EKS IMDS
        "metadata.google.internal", // GCP metadata
        "metadata.azure.com",    // Azure IMDS (new)
        "169.254.170.2",         // ECS task metadata
    ];
    if DENY_HOSTS.iter().any(|deny| host == *deny) {
        bail!(
            "api_endpoint host {:?} is blocked (metadata / SSRF surface).",
            host
        );
    }

    // Block any RFC 1918 / link-local literal IP except the explicit
    // loopback addresses above. This catches `http://10.0.0.1/` style
    // attempts that would otherwise sneak through the `http -> localhost`
    // carve-out because they have a literal IP host.
    if host.chars().next().map(|c| c.is_ascii_digit()).unwrap_or(false) {
        if let Ok(ip) = host.parse::<std::net::Ipv4Addr>() {
            let is_loopback = ip.is_loopback();
            if !is_loopback
                && (ip.is_private()
                    || ip.is_link_local()
                    || ip.is_unspecified()
                    || ip.is_broadcast()
                    || ip.is_documentation())
            {
                bail!(
                    "api_endpoint must not point at a private/link-local IP ({})",
                    ip
                );
            }
        }
    }

    // Explicit host suffix denylist for AWS-internal / intranet TLDs.
    const DENY_SUFFIXES: &[&str] = &[".internal", ".local", ".localdomain"];
    if DENY_SUFFIXES.iter().any(|suf| host.ends_with(suf)) && !loopback_host {
        bail!(
            "api_endpoint host {:?} uses a reserved internal suffix.",
            host
        );
    }

    // Common typo: trailing /v1.
    let path = parsed.path().trim_end_matches('/');
    if path.ends_with("/v1") {
        bail!(
            "The URL should be the base endpoint, not include /v1. \
             Try removing /v1 from the end: {}",
            trimmed
        );
    }

    // Strip trailing slash before returning.
    Ok(trimmed.trim_end_matches('/').to_string())
}

// ------------------------------------------------------------------
// Discovery fetch
// ------------------------------------------------------------------

async fn fetch_config(url: &str) -> Result<StratoclaveConfig> {
    // P0-5 (2026-04 security review): refuse to follow redirects during
    // bootstrap. `validate_url` already ensured the initial host is
    // safe, but `reqwest`'s default is to chase up to 10 redirects —
    // which defeats the validation if the server responds
    // `302 -> http://169.254.169.254/...`. `redirect(Policy::none())`
    // treats any 3xx as a hard error, forcing the attacker to own a
    // permitted host directly.
    //
    // We intentionally do NOT set `https_only(true)` here because the
    // caller is already vetted by `validate_url`, which carves out
    // `http://localhost` for local backends. Enforcing `https_only` on
    // the Client would break local development while `validate_url`
    // has already proven the scheme/host combination is safe.
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .context("Failed to build HTTP client")?;

    let resp = client
        .get(url)
        .header("Accept", "application/json")
        .send()
        .await
        .map_err(|e| {
            if e.is_connect() || e.is_timeout() {
                anyhow!(
                    "Could not reach {}. Double-check the URL with your administrator. ({})",
                    url,
                    e
                )
            } else {
                anyhow!("Failed to fetch {}: {}", url, e)
            }
        })?;

    let status = resp.status();
    if !status.is_success() {
        if status.as_u16() == 404 {
            bail!(
                "This Stratoclave deployment does not support /.well-known/stratoclave-config \
                 (HTTP 404). Please ask your administrator to update the Backend to the latest version."
            );
        }
        bail!(
            "Failed to fetch config from {}: HTTP {}",
            url,
            status
        );
    }

    let body = resp
        .text()
        .await
        .with_context(|| format!("Failed to read response body from {}", url))?;

    serde_json::from_str::<StratoclaveConfig>(&body).map_err(|e| {
        anyhow!(
            "Unexpected response format from {}. Is this really a Stratoclave deployment? \
             (parse error: {})",
            url,
            e
        )
    })
}

// ------------------------------------------------------------------
// TOML rendering
// ------------------------------------------------------------------

fn render_toml(cfg: &StratoclaveConfig) -> String {
    let timestamp = now_iso8601();
    let mut out = format!(
        "# Stratoclave CLI configuration\n\
         # Generated by `stratoclave setup` on {timestamp}\n\
         # Do not commit this file to version control.\n\
         \n\
         [api]\n\
         endpoint = \"{api_endpoint}\"\n\
         \n\
         [auth]\n\
         auth_method = \"cognito\"\n\
         client_id = \"{client_id}\"\n\
         cognito_domain = \"{cognito_domain}\"\n\
         # region / user_pool_id are non-secret discovery fields, kept for reference.\n\
         region = \"{region}\"\n\
         user_pool_id = \"{user_pool_id}\"\n\
         \n\
         [defaults]\n\
         model = \"{default_model}\"\n",
        timestamp = timestamp,
        api_endpoint = cfg.api_endpoint,
        client_id = cfg.cognito.client_id,
        cognito_domain = cfg.cognito.domain,
        region = cfg.cognito.region,
        user_pool_id = cfg.cognito.user_pool_id,
        default_model = cfg.cli.default_model,
    );

    // Include codex defaults when the deployment advertises them, so a
    // freshly-bootstrapped CLI can run `stratoclave codex` without any
    // additional configuration.
    if let Some(codex) = &cfg.cli.codex {
        out.push_str(&format!(
            "codex_model = \"{}\"\n\n[codex]\nopenai_base_path = \"{}\"\n",
            codex.default_model, codex.openai_base_path,
        ));
    } else {
        out.push('\n');
    }

    out.push_str(&format!(
        "[callback]\nhost = \"127.0.0.1\"\nport = {callback_port}\n\n\
         [timeouts]\nhttp_total = 10\nconnection = 5\nsse_chunk = 20\nauth_callback = 300\n",
        callback_port = cfg.cli.callback_port,
    ));

    out
}

fn now_iso8601() -> String {
    // Hand-rolled ISO 8601 (UTC) to avoid pulling in chrono.
    let dur = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = dur.as_secs() as i64;
    format_epoch_utc(secs)
}

/// Format a Unix epoch in seconds as "YYYY-MM-DDTHH:MM:SSZ" (UTC).
/// Hand-implemented so we don't need to add chrono just for the
/// generated-on header in the bootstrap config. Anchored at
/// 1970-01-01T00:00:00Z.
fn format_epoch_utc(secs: i64) -> String {
    if secs < 0 {
        return "1970-01-01T00:00:00Z".to_string();
    }
    let s = secs as u64;
    let sec_of_day = (s % 86_400) as u32;
    let mut days = (s / 86_400) as i64;

    let hour = sec_of_day / 3600;
    let min = (sec_of_day % 3600) / 60;
    let sec = sec_of_day % 60;

    // Convert "days since 1970-01-01" into Y-M-D.
    let mut year: i64 = 1970;
    loop {
        let year_days = if is_leap(year) { 366 } else { 365 };
        if days < year_days {
            break;
        }
        days -= year_days;
        year += 1;
    }
    let days_in_months = if is_leap(year) {
        [31u32, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    } else {
        [31u32, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    };
    let mut remaining = days as u32;
    let mut month = 1u32;
    for &dim in &days_in_months {
        if remaining < dim {
            break;
        }
        remaining -= dim;
        month += 1;
    }
    let day = remaining + 1;
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        year, month, day, hour, min, sec
    )
}

fn is_leap(year: i64) -> bool {
    (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0)
}

// ------------------------------------------------------------------
// Filesystem helpers
// ------------------------------------------------------------------

fn resolve_config_dir() -> Result<PathBuf> {
    if let Ok(dir) = std::env::var("STRATOCLAVE_CONFIG_DIR") {
        return Ok(PathBuf::from(dir));
    }
    dirs::home_dir()
        .map(|h| h.join(".stratoclave"))
        .ok_or_else(|| anyhow!("Could not resolve home directory"))
}

fn ensure_config_dir(dir: &PathBuf) -> Result<()> {
    if !dir.exists() {
        fs::create_dir_all(dir)
            .with_context(|| format!("Failed to create directory {}", dir.display()))?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let perms = fs::Permissions::from_mode(0o700);
        let _ = fs::set_permissions(dir, perms);
    }
    Ok(())
}

fn backup_existing(path: &PathBuf) -> Result<PathBuf> {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let parent = path.parent().ok_or_else(|| anyhow!("no parent dir"))?;
    let filename = path
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("config.toml");
    let backup = parent.join(format!("{}.bak.{}", filename, ts));
    fs::rename(path, &backup).with_context(|| {
        format!(
            "Failed to rename existing config to {}",
            backup.display()
        )
    })?;
    Ok(backup)
}

// ------------------------------------------------------------------
// UX helpers
// ------------------------------------------------------------------

fn is_stdin_tty() -> bool {
    atty::is(atty::Stream::Stdin)
}

fn prompt_overwrite(path: &PathBuf) -> Result<bool> {
    eprint!(
        "~/.stratoclave/config.toml already exists at {}.\n\
         Overwrite? [y/N] ",
        path.display()
    );
    io::stderr().flush().ok();
    let mut buf = String::new();
    io::stdin()
        .read_line(&mut buf)
        .context("Failed to read confirmation from stdin")?;
    let answer = buf.trim().to_lowercase();
    Ok(answer == "y" || answer == "yes")
}

fn print_summary(path: &PathBuf, cfg: &StratoclaveConfig) {
    println!();
    println!("Saved to {}", path.display());
    println!("  api_endpoint      = {}", cfg.api_endpoint);
    println!("  cognito.domain    = {}", cfg.cognito.domain);
    println!("  cognito.region    = {}", cfg.cognito.region);
    println!("  cli.default_model = {}", cfg.cli.default_model);
    println!();
    println!("Next steps:");
    println!("  stratoclave auth login --email you@example.com");
    println!("  # or");
    println!("  stratoclave auth sso --profile your-sso-profile");
}

// ------------------------------------------------------------------
// ~/.codex/config.toml patcher (--codex)
// ------------------------------------------------------------------

const CODEX_BLOCK_HEADER: &str = "[model_providers.stratoclave]";
const CODEX_PROVIDER_NAME: &str = "stratoclave";

fn render_codex_toml_block(api_endpoint: &str, codex: &CodexHints) -> String {
    let base_url = format!(
        "{}{}",
        api_endpoint.trim_end_matches('/'),
        codex.openai_base_path,
    );
    let context_window =
        crate::mvp::codex_cmd::codex_context_window_for(&codex.default_model);
    format!(
        "# Added by `stratoclave setup --codex`\n\
         # Bedrock's OpenAI Responses endpoint does not implement the\n\
         # `web_search` tool today; codex must not send it as a tool\n\
         # type or every request returns a 400 validation_error.\n\
         web_search = \"disabled\"\n\
         # codex 0.136 walks up from `cwd` looking for a project-local\n\
         # `.codex/config.toml`. When the user is anywhere under $HOME\n\
         # the search reaches `~/.codex/config.toml` itself and emits\n\
         # \"Ignored unsupported project-local config keys\" for any\n\
         # `model_provider` / `model_providers` entries. Disabling the\n\
         # marker list short-circuits the walk so only this file loads.\n\
         project_root_markers = []\n\
         # codex's built-in model catalog does not list the OpenAI\n\
         # GPT-5 family; without an explicit context window codex\n\
         # warns \"Model metadata for ... not found. Defaulting to\n\
         # fallback metadata\" on every startup.\n\
         model_context_window = {context_window}\n\
         \n\
         {header}\n\
         name                   = \"Stratoclave (OpenAI via Bedrock)\"\n\
         base_url               = \"{base_url}\"\n\
         wire_api               = \"responses\"\n\
         env_key                = \"STRATOCLAVE_OPENAI_KEY\"\n\
         request_max_retries    = 3\n\
         stream_max_retries     = 5\n\
         stream_idle_timeout_ms = 600000\n",
        header = CODEX_BLOCK_HEADER,
        base_url = base_url,
        context_window = context_window,
    )
}

fn resolve_codex_config_dir() -> Result<PathBuf> {
    if let Ok(dir) = std::env::var("CODEX_HOME") {
        return Ok(PathBuf::from(dir));
    }
    dirs::home_dir()
        .map(|h| h.join(".codex"))
        .ok_or_else(|| anyhow!("Could not resolve home directory for ~/.codex"))
}

/// Append the `[model_providers.stratoclave]` block to `~/.codex/config.toml`.
///
/// - If the file does not exist, write it with just the block.
/// - If the block already exists (string match on the header), no-op
///   (operator can hand-edit; we never silently rewrite their settings).
/// - If `model_provider` is set to something other than "stratoclave",
///   prompt the user before changing it (or skip when not a TTY).
fn patch_codex_config(
    api_endpoint: &str,
    codex: &CodexHints,
    force: bool,
) -> Result<()> {
    let codex_dir = resolve_codex_config_dir()?;
    let codex_path = codex_dir.join("config.toml");

    if !codex_dir.exists() {
        fs::create_dir_all(&codex_dir).with_context(|| {
            format!("Failed to create codex config dir {}", codex_dir.display())
        })?;
    }

    let block = render_codex_toml_block(api_endpoint, codex);

    if !codex_path.exists() {
        fs::write(&codex_path, &block).with_context(|| {
            format!("Failed to write {}", codex_path.display())
        })?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let perms = fs::Permissions::from_mode(0o600);
            let _ = fs::set_permissions(&codex_path, perms);
        }
        println!(
            "[INFO] Wrote {} with stratoclave provider block.",
            codex_path.display()
        );
        return Ok(());
    }

    let original = fs::read_to_string(&codex_path)
        .with_context(|| format!("Failed to read {}", codex_path.display()))?;

    // Always back up before any modification (force or not).
    let backup = backup_existing_codex(&codex_path)?;
    println!(
        "[INFO] Backed up existing codex config to {}",
        backup.display()
    );

    let mut updated = original.clone();
    if updated.contains(CODEX_BLOCK_HEADER) {
        println!(
            "[INFO] {} already has a [model_providers.stratoclave] block; \
             leaving it as-is.",
            codex_path.display()
        );
    } else {
        if !updated.ends_with('\n') {
            updated.push('\n');
        }
        updated.push('\n');
        updated.push_str(&block);
        println!(
            "[INFO] Appended [model_providers.stratoclave] block to {}.",
            codex_path.display()
        );
    }

    let current_provider = read_top_level_string(&updated, "model_provider");
    let want_change = match current_provider.as_deref() {
        Some(CODEX_PROVIDER_NAME) => false,
        _ => true,
    };
    if want_change {
        let proceed = if force {
            true
        } else if !is_stdin_tty() {
            // Non-interactive without --force: leave the existing
            // top-level provider alone. The new block is still appended
            // so users can opt in by editing model_provider themselves.
            println!(
                "[INFO] Existing top-level `model_provider` is {:?}; \
                 leaving unchanged (re-run with --force or interactively to switch).",
                current_provider.as_deref().unwrap_or("(unset)")
            );
            false
        } else {
            prompt_codex_provider_switch(current_provider.as_deref())?
        };
        if proceed {
            updated = upsert_top_level_string(&updated, "model_provider", CODEX_PROVIDER_NAME);
            println!(
                "[INFO] Set top-level model_provider = \"{}\".",
                CODEX_PROVIDER_NAME
            );
        }
    }

    if updated != original {
        fs::write(&codex_path, &updated).with_context(|| {
            format!("Failed to write {}", codex_path.display())
        })?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let perms = fs::Permissions::from_mode(0o600);
            let _ = fs::set_permissions(&codex_path, perms);
        }
    }

    Ok(())
}

fn backup_existing_codex(path: &PathBuf) -> Result<PathBuf> {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let parent = path.parent().ok_or_else(|| anyhow!("no parent dir"))?;
    let filename = path
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("config.toml");
    let backup = parent.join(format!("{}.bak.{}", filename, ts));
    fs::copy(path, &backup).with_context(|| {
        format!("Failed to copy existing codex config to {}", backup.display())
    })?;
    Ok(backup)
}

fn prompt_codex_provider_switch(current: Option<&str>) -> Result<bool> {
    eprint!(
        "Set top-level `model_provider = \"stratoclave\"` in ~/.codex/config.toml? \
         Current value: {} [y/N] ",
        current.unwrap_or("(unset)")
    );
    io::stderr().flush().ok();
    let mut buf = String::new();
    io::stdin()
        .read_line(&mut buf)
        .context("Failed to read confirmation from stdin")?;
    let answer = buf.trim().to_lowercase();
    Ok(answer == "y" || answer == "yes")
}

/// Read the value of a top-level string assignment such as
/// `model_provider = "openai"`. Only inspects lines outside any `[table]`
/// section, so an inner `model_provider` under a sub-table is ignored.
fn read_top_level_string(text: &str, key: &str) -> Option<String> {
    let mut in_section = false;
    for raw in text.lines() {
        let line = raw.trim();
        if line.starts_with('#') || line.is_empty() {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            in_section = true;
            continue;
        }
        if in_section {
            continue;
        }
        if let Some((lhs, rhs)) = line.split_once('=') {
            if lhs.trim() == key {
                let v = rhs.trim();
                let v = v.trim_matches('"');
                return Some(v.to_string());
            }
        }
    }
    None
}

/// Insert or replace a top-level string assignment. Adds the line at
/// the very top of the file if missing.
fn upsert_top_level_string(text: &str, key: &str, value: &str) -> String {
    let replacement = format!("{} = \"{}\"", key, value);
    let mut in_section = false;
    let mut wrote = false;
    let mut out = String::with_capacity(text.len() + replacement.len() + 1);
    for raw in text.lines() {
        let trimmed = raw.trim_start();
        if trimmed.starts_with('[') && trimmed.contains(']') {
            in_section = true;
            out.push_str(raw);
            out.push('\n');
            continue;
        }
        let mut wrote_here = false;
        if !in_section && !wrote {
            if let Some((lhs, _)) = trimmed.split_once('=') {
                if lhs.trim() == key {
                    out.push_str(&replacement);
                    out.push('\n');
                    wrote = true;
                    wrote_here = true;
                }
            }
        }
        if !wrote_here {
            out.push_str(raw);
            out.push('\n');
        }
    }
    if !wrote {
        let mut prefixed = String::with_capacity(out.len() + replacement.len() + 1);
        prefixed.push_str(&replacement);
        prefixed.push('\n');
        prefixed.push_str(&out);
        return prefixed;
    }
    out
}

// ------------------------------------------------------------------
// Tests
// ------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_url_https() {
        let out = validate_url("https://example.cloudfront.net").unwrap();
        assert_eq!(out, "https://example.cloudfront.net");
    }

    #[test]
    fn test_validate_url_http_localhost() {
        let out = validate_url("http://localhost:8080").unwrap();
        assert_eq!(out, "http://localhost:8080");
    }

    #[test]
    fn test_validate_url_trims_trailing_slash() {
        let out = validate_url("https://example.cloudfront.net/").unwrap();
        assert_eq!(out, "https://example.cloudfront.net");
    }

    #[test]
    fn test_validate_url_rejects_missing_scheme() {
        // P0-5: the tightened validator now rejects at url::Url::parse
        // level (no scheme => RelativeUrlWithoutBase). We only assert
        // that *some* error is returned for the bare hostname input;
        // the exact wording is the underlying parse error.
        let err = validate_url("example.cloudfront.net").unwrap_err();
        let msg = format!("{}", err);
        assert!(!msg.is_empty(), "error message must not be empty");
    }

    #[test]
    fn test_validate_url_rejects_empty() {
        let err = validate_url("").unwrap_err();
        assert!(format!("{}", err).contains("empty"));
    }

    #[test]
    fn test_validate_url_rejects_v1_suffix() {
        let err = validate_url("https://example.cloudfront.net/v1").unwrap_err();
        let msg = format!("{}", err);
        assert!(msg.contains("/v1"));
    }

    #[test]
    fn test_validate_url_rejects_v1_suffix_trailing_slash() {
        let err = validate_url("https://example.cloudfront.net/v1/").unwrap_err();
        let msg = format!("{}", err);
        assert!(msg.contains("/v1"));
    }

    #[test]
    fn test_render_toml_contains_all_sections() {
        let cfg = StratoclaveConfig {
            schema_version: "1".into(),
            api_endpoint: "https://d111111abcdef8.cloudfront.net".into(),
            cognito: CognitoInfo {
                user_pool_id: "us-east-1_XXXXXXXXX".into(),
                client_id: "xxxxxxxxxxxxxxxxxxxxxxxxxx".into(),
                domain: "https://xxx.auth.us-east-1.amazoncognito.com".into(),
                region: "us-east-1".into(),
            },
            cli: CliHints {
                default_model: "us.anthropic.claude-opus-4-7".into(),
                callback_port: 18080,
                codex: None,
            },
        };
        let out = render_toml(&cfg);
        assert!(out.contains("[api]"));
        assert!(out.contains("endpoint = \"https://d111111abcdef8.cloudfront.net\""));
        assert!(out.contains("[auth]"));
        assert!(out.contains("client_id = \"xxxxxxxxxxxxxxxxxxxxxxxxxx\""));
        assert!(out.contains("cognito_domain = \"https://xxx.auth.us-east-1.amazoncognito.com\""));
        assert!(out.contains("region = \"us-east-1\""));
        assert!(out.contains("user_pool_id = \"us-east-1_XXXXXXXXX\""));
        assert!(out.contains("[defaults]"));
        assert!(out.contains("model = \"us.anthropic.claude-opus-4-7\""));
        assert!(out.contains("[callback]"));
        assert!(out.contains("port = 18080"));
        assert!(out.contains("host = \"127.0.0.1\""));
        assert!(out.contains("[timeouts]"));
    }

    #[test]
    fn test_render_toml_is_parseable() {
        let cfg = StratoclaveConfig {
            schema_version: "1".into(),
            api_endpoint: "https://example.com".into(),
            cognito: CognitoInfo {
                user_pool_id: "us-east-1_ABC".into(),
                client_id: "clientid".into(),
                domain: "https://d.auth.us-east-1.amazoncognito.com".into(),
                region: "us-east-1".into(),
            },
            cli: CliHints {
                default_model: "us.anthropic.claude-opus-4-7".into(),
                callback_port: 18080,
                codex: None,
            },
        };
        let out = render_toml(&cfg);
        let parsed: toml::Value = toml::from_str(&out).expect("rendered TOML should parse");
        assert_eq!(
            parsed["api"]["endpoint"].as_str(),
            Some("https://example.com")
        );
        assert_eq!(parsed["auth"]["client_id"].as_str(), Some("clientid"));
        assert_eq!(parsed["callback"]["port"].as_integer(), Some(18080));
    }

    #[test]
    fn test_render_toml_includes_codex_section_when_present() {
        let cfg = StratoclaveConfig {
            schema_version: "1".into(),
            api_endpoint: "https://example.com".into(),
            cognito: CognitoInfo {
                user_pool_id: "us-east-1_ABC".into(),
                client_id: "clientid".into(),
                domain: "https://d.auth.us-east-1.amazoncognito.com".into(),
                region: "us-east-1".into(),
            },
            cli: CliHints {
                default_model: "us.anthropic.claude-opus-4-7".into(),
                callback_port: 18080,
                codex: Some(CodexHints {
                    default_model: "openai.gpt-5.4".into(),
                    openai_base_path: "/openai/v1".into(),
                    supported_regions: vec!["us-east-2".into(), "us-west-2".into()],
                }),
            },
        };
        let out = render_toml(&cfg);
        let parsed: toml::Value =
            toml::from_str(&out).expect("rendered TOML with codex should parse");
        assert_eq!(
            parsed["defaults"]["codex_model"].as_str(),
            Some("openai.gpt-5.4")
        );
        assert_eq!(
            parsed["codex"]["openai_base_path"].as_str(),
            Some("/openai/v1")
        );
    }

    #[test]
    fn test_render_codex_block_uses_base_url() {
        let codex = CodexHints {
            default_model: "openai.gpt-5.4".into(),
            openai_base_path: "/openai/v1".into(),
            supported_regions: vec!["us-east-2".into()],
        };
        let block = render_codex_toml_block("https://example.cloudfront.net", &codex);
        assert!(block.contains("[model_providers.stratoclave]"));
        assert!(block.contains("base_url               = \"https://example.cloudfront.net/openai/v1\""));
        assert!(block.contains("wire_api               = \"responses\""));
        assert!(block.contains("env_key                = \"STRATOCLAVE_OPENAI_KEY\""));
        // web_search must be disabled — Bedrock OpenAI Responses does
        // not implement that tool type and a default-on web_search
        // turns every request into a 400.
        assert!(block.contains("web_search = \"disabled\""));
    }

    #[test]
    fn test_upsert_top_level_string_inserts_when_missing() {
        let original = "[foo]\nx = 1\n";
        let out = upsert_top_level_string(original, "model_provider", "stratoclave");
        // Inserted at the top so the key is outside any section.
        assert!(out.starts_with("model_provider = \"stratoclave\"\n"));
        assert!(out.contains("[foo]\nx = 1\n"));
    }

    #[test]
    fn test_upsert_top_level_string_replaces_existing() {
        let original = "model_provider = \"openai\"\n[foo]\nx = 1\n";
        let out = upsert_top_level_string(original, "model_provider", "stratoclave");
        assert!(out.contains("model_provider = \"stratoclave\""));
        assert!(!out.contains("model_provider = \"openai\""));
    }

    #[test]
    fn test_upsert_does_not_replace_keys_inside_sections() {
        // `model_provider` here lives inside [foo]; the helper must not
        // touch it (only top-level keys are upserted).
        let original = "[foo]\nmodel_provider = \"openai\"\n";
        let out = upsert_top_level_string(original, "model_provider", "stratoclave");
        // A new top-level line is prepended; the inner one stays.
        assert!(out.starts_with("model_provider = \"stratoclave\"\n"));
        assert!(out.contains("[foo]\nmodel_provider = \"openai\"\n"));
    }

    #[test]
    fn test_read_top_level_string_skips_section_keys() {
        let toml = "[foo]\nmodel_provider = \"openai\"\n";
        assert_eq!(read_top_level_string(toml, "model_provider"), None);
    }

    #[test]
    fn test_read_top_level_string_finds_top_level_key() {
        let toml = "model_provider = \"openai\"\n[foo]\nbar = 1\n";
        assert_eq!(
            read_top_level_string(toml, "model_provider"),
            Some("openai".to_string())
        );
    }

    #[test]
    fn test_format_epoch_utc_epoch_zero() {
        assert_eq!(format_epoch_utc(0), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn test_format_epoch_utc_known_value() {
        // 2025-01-01T00:00:00Z = 1735689600
        assert_eq!(format_epoch_utc(1_735_689_600), "2025-01-01T00:00:00Z");
    }

    #[test]
    fn test_format_epoch_utc_leap_day() {
        // 2024-02-29T12:34:56Z = 1709210096
        assert_eq!(format_epoch_utc(1_709_210_096), "2024-02-29T12:34:56Z");
    }
}
