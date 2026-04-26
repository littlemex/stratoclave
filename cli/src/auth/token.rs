//! Token storage and management utilities

use anyhow::{Context, Result};
use std::path::Path;

use super::provider::SavedTokens;

/// Load saved tokens from config directory
pub fn load_tokens(config_dir: &Path) -> Result<Option<SavedTokens>> {
    let tokens_path = config_dir.join("tokens.json");

    if !tokens_path.exists() {
        return Ok(None);
    }

    let content = std::fs::read_to_string(&tokens_path)
        .context("Failed to read tokens file")?;

    let tokens: SavedTokens = serde_json::from_str(&content)
        .context("Failed to parse tokens JSON")?;

    Ok(Some(tokens))
}

/// Save tokens to config directory
pub fn save_tokens(config_dir: &Path, tokens: &SavedTokens) -> Result<()> {
    std::fs::create_dir_all(config_dir)
        .context("Failed to create config directory")?;

    let tokens_path = config_dir.join("tokens.json");
    let content = serde_json::to_string_pretty(tokens)
        .context("Failed to serialize tokens")?;

    std::fs::write(&tokens_path, content)
        .context("Failed to write tokens file")?;

    Ok(())
}

/// Check if saved tokens are valid (not expired)
pub fn is_token_valid(tokens: &SavedTokens) -> bool {
    match tokens.expires_at {
        Some(exp) => {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs();
            exp > now + 60 // 60 second buffer
        }
        None => false,
    }
}
