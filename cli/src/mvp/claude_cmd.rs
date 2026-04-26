//! `stratoclave claude -- [args]` サブコマンド.
//!
//! Claude Code を子プロセスとして起動し、ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY / ANTHROPIC_MODEL
//! を注入することで、Bedrock プロキシ経由でリクエストを送らせる.

use anyhow::{anyhow, Result};
use std::process::{Command, ExitCode, Stdio};

use super::config::MvpConfig;
use super::tokens::load as load_tokens;

pub async fn run(args: &[String], model_override: Option<&str>) -> Result<ExitCode> {
    let config = MvpConfig::load()?;
    let tokens = load_tokens()?;

    // base_url は `/v1/messages` を受ける前段まで、つまり `${api_endpoint}`
    let base_url = config.api_endpoint.clone();

    let model = model_override
        .map(String::from)
        .unwrap_or_else(|| config.default_model.clone());

    let claude = find_claude_binary();

    eprintln!(
        "[INFO] Launching claude via Stratoclave proxy (base_url={}, model={})",
        base_url, model
    );

    let mut cmd = Command::new(&claude);
    cmd.args(args);
    cmd.env("ANTHROPIC_BASE_URL", &base_url);
    cmd.env("ANTHROPIC_API_KEY", &tokens.access_token);
    cmd.env("ANTHROPIC_MODEL", &model);
    // Claude Code が Bedrock 直叩きに切り替わらないよう明示的に無効化
    cmd.env_remove("CLAUDE_CODE_USE_BEDROCK");
    cmd.env_remove("AWS_REGION");
    cmd.stdin(Stdio::inherit());
    cmd.stdout(Stdio::inherit());
    cmd.stderr(Stdio::inherit());

    let status = cmd.status().map_err(|e| anyhow!("Failed to spawn claude: {}", e))?;
    let code = status.code().unwrap_or(1) as u8;
    Ok(ExitCode::from(code))
}

fn find_claude_binary() -> String {
    if let Ok(output) = Command::new("which").arg("claude").output() {
        if output.status.success() {
            if let Ok(path) = String::from_utf8(output.stdout) {
                let path = path.trim();
                if !path.is_empty() && std::path::Path::new(path).exists() {
                    return path.to_string();
                }
            }
        }
    }
    let candidates = [
        format!(
            "{}/.local/bin/claude",
            std::env::var("HOME").unwrap_or_default()
        ),
        "/usr/local/bin/claude".to_string(),
    ];
    for c in candidates {
        if std::path::Path::new(&c).exists() {
            return c;
        }
    }
    "claude".to_string()
}
