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

#[cfg(test)]
mod tests {
    //! Tests for the MVP token persistence layer. These mutate $HOME and
    //! therefore run sequentially under a shared mutex.
    use super::*;
    use std::env;
    use std::sync::Mutex;

    static HOME_LOCK: Mutex<()> = Mutex::new(());

    struct HomeGuard {
        _tmp: tempfile::TempDir,
        orig_home: Option<std::ffi::OsString>,
    }

    impl Drop for HomeGuard {
        fn drop(&mut self) {
            match self.orig_home.take() {
                Some(v) => env::set_var("HOME", v),
                None => env::remove_var("HOME"),
            }
        }
    }

    fn with_temp_home() -> (HomeGuard, std::sync::MutexGuard<'static, ()>) {
        let lock = HOME_LOCK.lock().unwrap();
        let tmp = tempfile::TempDir::new().unwrap();
        let guard = HomeGuard {
            orig_home: env::var_os("HOME"),
            _tmp: tmp,
        };
        env::set_var("HOME", guard._tmp.path());
        (guard, lock)
    }

    fn sample() -> MvpTokens {
        MvpTokens {
            access_token: "eyJaccess".into(),
            id_token: Some("eyJid".into()),
            refresh_token: Some("eyJrefresh".into()),
            expires_at: 1_700_000_000,
            email: "u@example.com".into(),
        }
    }

    #[test]
    fn save_then_load_roundtrip() {
        let (_h, _lock) = with_temp_home();
        save(&sample()).unwrap();
        let got = load().unwrap();
        assert_eq!(got.access_token, "eyJaccess");
        assert_eq!(got.email, "u@example.com");
        assert_eq!(got.refresh_token.as_deref(), Some("eyJrefresh"));
    }

    #[test]
    fn clear_removes_the_token_file() {
        let (_h, _lock) = with_temp_home();
        save(&sample()).unwrap();
        clear().unwrap();
        let err = match load() {
            Ok(_) => panic!("expected load to fail after clear"),
            Err(e) => e,
        };
        assert!(err.to_string().contains("Cannot read"));
    }

    #[test]
    fn clear_when_absent_is_idempotent() {
        let (_h, _lock) = with_temp_home();
        // No prior save: clear should be a no-op.
        clear().unwrap();
        clear().unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn save_applies_0600_permissions() {
        use std::os::unix::fs::PermissionsExt;
        let (_h, _lock) = with_temp_home();
        save(&sample()).unwrap();
        let path = token_path().unwrap();
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "token file must be mode 0600");
    }
}
