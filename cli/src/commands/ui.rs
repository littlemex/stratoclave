//! UI command - Open the Stratoclave web UI with the current Cognito access_token.
//!
//! Phase 2:
//! - Token source: `~/.stratoclave/mvp_tokens.json` (written by `stratoclave auth login`).
//! - Target URL: `admin_ui_url` from config.toml, otherwise `api_endpoint`, otherwise MvpConfig.
//! - The Frontend's AuthContext reads `?token=<access_token>` on first load and persists it.

use anyhow::{bail, Context, Result};
use clap::Subcommand;

use crate::config::AppConfig;
use crate::mvp::config::MvpConfig;
use crate::mvp::tokens;

#[derive(Subcommand, Debug)]
pub enum UiCommand {
    /// Open the UI in the default browser
    Open,
    /// Print the URL (with token) instead of opening the browser
    Url,
}

pub async fn run(cmd: UiCommand, config: &AppConfig) -> Result<()> {
    // Phase 2: `stratoclave auth login` で書き込まれた mvp_tokens.json を使う
    let saved = tokens::load().context(
        "`stratoclave auth login` を実行してからもう一度 `stratoclave ui open` を試してください",
    )?;
    let access_token = saved.access_token;
    if access_token.is_empty() {
        bail!("アクセストークンが空です。再度 `stratoclave auth login` を実行してください");
    }

    // 期限切れなら明示エラー (Frontend 側でも検出するが CLI で先に気付けるよう)
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    if saved.expires_at > 0 && saved.expires_at <= now {
        bail!(
            "アクセストークンの有効期限が切れています。`stratoclave auth login` を再実行してください"
        );
    }

    // UI URL の解決: config.admin_ui_url > config.api_endpoint > MvpConfig::load
    let base_url = resolve_base_url(config)?;

    let separator = if base_url.contains('?') { '&' } else { '?' };
    let url_with_token = format!(
        "{}{}token={}",
        base_url.trim_end_matches('/'),
        separator,
        access_token
    );

    match cmd {
        UiCommand::Open => {
            eprintln!("[INFO] Opening Stratoclave UI: {}", base_url);
            if let Err(e) = open::that(&url_with_token) {
                bail!("ブラウザを起動できませんでした: {}", e);
            }
            eprintln!("[OK] UI opened");
        }
        UiCommand::Url => {
            println!("{}", url_with_token);
        }
    }

    Ok(())
}

fn resolve_base_url(config: &AppConfig) -> Result<String> {
    if let Some(url) = config.admin_ui_url.as_ref() {
        if !url.is_empty() {
            return Ok(url.clone());
        }
    }
    if !config.api_endpoint.is_empty() {
        return Ok(config.api_endpoint.clone());
    }
    // Final fallback: MvpConfig が環境変数 / config.toml から api_endpoint を拾う
    let mvp = MvpConfig::load().context(
        "UI の URL が解決できません。`api_endpoint` を ~/.stratoclave/config.toml か \
         STRATOCLAVE_API_ENDPOINT 環境変数で設定してください",
    )?;
    Ok(mvp.api_endpoint)
}
