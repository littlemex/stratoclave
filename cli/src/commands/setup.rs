//! `stratoclave setup <api_endpoint>` サブコマンド.
//!
//! OSS 版の CLI 利用者が初回 bootstrap する唯一の経路。
//! 指定された API エンドポイント (Admin から共有された CloudFront URL) に対して
//! `GET /.well-known/stratoclave-config` を叩き、レスポンス JSON を
//! `~/.stratoclave/config.toml` として書き出す。
//!
//! フロー:
//!   1. URL を validate (http/https scheme、URL parse 可能)
//!   2. `{api_endpoint}/.well-known/stratoclave-config` を取得 (timeout 10s)
//!   3. schema_version == "1" を検証
//!   4. 既存 config.toml の存在確認 → --force または対話確認
//!   5. --dry-run なら stdout に出力して終了
//!   6. 既存 config.toml を config.toml.bak.<timestamp> にバックアップ
//!   7. 新しい config.toml を書き込み、サマリを表示

use anyhow::{anyhow, bail, Context, Result};
use serde::Deserialize;
use std::fs;
use std::io::{self, Write};
use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// `.well-known/stratoclave-config` のレスポンススキーマ.
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
struct CliHints {
    default_model: String,
    callback_port: u16,
}

/// `stratoclave setup <api_endpoint>` エントリーポイント.
pub async fn run(api_endpoint: String, force: bool, dry_run: bool) -> Result<()> {
    // 1. URL を validate
    let api_endpoint = validate_url(&api_endpoint)?;

    // 2. {api_endpoint}/.well-known/stratoclave-config を取得
    let discovery_url = format!(
        "{}/.well-known/stratoclave-config",
        api_endpoint.trim_end_matches('/')
    );
    println!("[INFO] Fetching config from {} ...", discovery_url);

    let config = fetch_config(&discovery_url).await?;

    // 3. schema_version 検証
    if config.schema_version != "1" {
        bail!(
            "This CLI expects schema_version=1 but received {:?}. \
             You may need to update the CLI.",
            config.schema_version
        );
    }

    // 4. 書き込み先パスを決定
    let config_dir = resolve_config_dir()?;
    let config_path = config_dir.join("config.toml");

    // 5. TOML 文字列を生成
    let toml_content = render_toml(&config);

    // 6. --dry-run なら stdout に出力して終了
    if dry_run {
        println!("[INFO] --dry-run: not writing to {}", config_path.display());
        println!("---");
        print!("{}", toml_content);
        if !toml_content.ends_with('\n') {
            println!();
        }
        println!("---");
        return Ok(());
    }

    // 7. 既存 config.toml の確認
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
        // 既存ファイルをバックアップ
        let backup = backup_existing(&config_path)?;
        println!("[INFO] Backed up existing config to {}", backup.display());
    }

    // 8. ディレクトリを準備 (mode 0o700)
    ensure_config_dir(&config_dir)?;

    // 9. config.toml を書き込み
    fs::write(&config_path, &toml_content)
        .with_context(|| format!("Failed to write {}", config_path.display()))?;

    // 10. パーミッションを 0o600 に設定 (Unix のみ)
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let perms = fs::Permissions::from_mode(0o600);
        let _ = fs::set_permissions(&config_path, perms);
    }

    // 11. サマリを表示
    print_summary(&config_path, &config);

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

    // scheme check
    if !(trimmed.starts_with("http://") || trimmed.starts_with("https://")) {
        bail!(
            "api_endpoint must start with http:// or https:// (got {:?})",
            trimmed
        );
    }

    // URL parse check
    let parsed = url::Url::parse(trimmed).map_err(|e| {
        anyhow!("api_endpoint is not a valid URL: {}", e)
    })?;

    // host check
    match parsed.host_str() {
        None => bail!("api_endpoint must include a host"),
        Some(h) if h.is_empty() => bail!("api_endpoint must include a host"),
        _ => {}
    }

    // よくある typo: /v1 付き
    let path = parsed.path().trim_end_matches('/');
    if path.ends_with("/v1") {
        bail!(
            "The URL should be the base endpoint, not include /v1. \
             Try removing /v1 from the end: {}",
            trimmed
        );
    }

    // 末尾の / は除去して返す
    Ok(trimmed.trim_end_matches('/').to_string())
}

// ------------------------------------------------------------------
// Discovery fetch
// ------------------------------------------------------------------

async fn fetch_config(url: &str) -> Result<StratoclaveConfig> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
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
    format!(
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
         model = \"{default_model}\"\n\
         \n\
         [callback]\n\
         host = \"127.0.0.1\"\n\
         port = {callback_port}\n\
         \n\
         [timeouts]\n\
         http_total = 10\n\
         connection = 5\n\
         sse_chunk = 20\n\
         auth_callback = 300\n",
        timestamp = timestamp,
        api_endpoint = cfg.api_endpoint,
        client_id = cfg.cognito.client_id,
        cognito_domain = cfg.cognito.domain,
        region = cfg.cognito.region,
        user_pool_id = cfg.cognito.user_pool_id,
        default_model = cfg.cli.default_model,
        callback_port = cfg.cli.callback_port,
    )
}

fn now_iso8601() -> String {
    // chrono を入れたくないので簡易 ISO 8601 (UTC) を手組み
    let dur = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = dur.as_secs() as i64;
    // 簡易 datetime: 単に epoch 秒を出す代わりに、date -u っぽく。
    // コード内で外部コマンドは使えないので epoch 秒をフォールバックとして出す。
    // より良い表現のために以下の小さな変換を実装する。
    format_epoch_utc(secs)
}

/// epoch 秒を "YYYY-MM-DDTHH:MM:SSZ" (UTC) に変換する簡易実装.
/// chrono を追加しないために手書きしている。1970 年 1 月 1 日 UTC を基準とする。
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

    // 1970-01-01 からの日数を Y-M-D に変換
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
        let err = validate_url("example.cloudfront.net").unwrap_err();
        let msg = format!("{}", err);
        assert!(msg.contains("http://") && msg.contains("https://"));
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
