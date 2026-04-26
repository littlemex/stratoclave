//! MVP 用のトークン永続化.
//!
//! 既存 `SavedTokens` とは別に、`~/.stratoclave/mvp_tokens.json` を使う.
//! パーミッション 0600.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MvpTokens {
    pub access_token: String,
    pub id_token: Option<String>,
    pub refresh_token: Option<String>,
    pub expires_at: u64,
    pub email: String,
}

fn token_path() -> Result<PathBuf> {
    let home = dirs::home_dir().context("Cannot resolve home directory")?;
    let dir = home.join(".stratoclave");
    fs::create_dir_all(&dir).context("Create ~/.stratoclave")?;
    Ok(dir.join("mvp_tokens.json"))
}

pub fn save(tokens: &MvpTokens) -> Result<()> {
    let path = token_path()?;
    let body = serde_json::to_string_pretty(tokens)?;
    fs::write(&path, body).context("Write mvp_tokens.json")?;
    set_secure_permissions(&path)?;
    Ok(())
}

pub fn load() -> Result<MvpTokens> {
    let path = token_path()?;
    let body = fs::read_to_string(&path).context(format!(
        "Cannot read {}. Run `stratoclave auth login-mvp` first.",
        path.display()
    ))?;
    let parsed: MvpTokens = serde_json::from_str(&body).context("Parse mvp_tokens.json")?;
    Ok(parsed)
}

pub fn clear() -> Result<()> {
    let path = token_path()?;
    if path.exists() {
        fs::remove_file(&path)?;
    }
    Ok(())
}

#[cfg(unix)]
fn set_secure_permissions(path: &std::path::Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let perms = fs::Permissions::from_mode(0o600);
    fs::set_permissions(path, perms)?;
    Ok(())
}

#[cfg(not(unix))]
fn set_secure_permissions(_path: &std::path::Path) -> Result<()> {
    Ok(())
}
