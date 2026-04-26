//! Phase C: 長期 API Key の CLI 操作.
//!
//! `stratoclave api-key create [--name N] [--scope S]... [--expires-days D]`
//! `stratoclave api-key list [--include-revoked]`
//! `stratoclave api-key revoke <key_hash>`
//!
//! 発行時はプレーンテキストが 1 回だけ返る. CLI はこれを強調表示で出力し、
//! ユーザーが cowork 等の gateway API key フィールドに貼れるようにする.

use anyhow::{Context, Result};
use serde_json::{json, Value};

use super::api::ApiClient;

pub async fn create(
    name: Option<String>,
    scopes: Vec<String>,
    expires_days: Option<u32>,
) -> Result<()> {
    let client = ApiClient::new()?;
    let mut body = json!({
        "name": name.unwrap_or_default(),
    });
    if !scopes.is_empty() {
        body["scopes"] = json!(scopes);
    }
    if let Some(d) = expires_days {
        body["expires_in_days"] = json!(d);
    }
    let res: Value = client
        .post_json("/api/mvp/me/api-keys", &body)
        .await
        .context("POST /api/mvp/me/api-keys failed")?;
    let plain = res
        .get("plaintext_key")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("response missing plaintext_key"))?;
    let key_id = res.get("key_id").and_then(|v| v.as_str()).unwrap_or("");
    let expires_at = res
        .get("expires_at")
        .and_then(|v| v.as_str())
        .unwrap_or("no expiration");
    let scopes = res
        .get("scopes")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|s| s.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();

    println!();
    println!("  ==========================================================");
    println!("   API Key created (save this now — it won't be shown again)");
    println!("  ==========================================================");
    println!();
    println!("   {plain}");
    println!();
    println!("   key_id     : {key_id}");
    println!("   scopes     : {scopes}");
    println!("   expires_at : {expires_at}");
    println!();
    println!("  Paste this value into `Gateway API key` (Claude Desktop cowork)");
    println!("  or use it directly as `Authorization: Bearer <key>`.");
    println!();
    Ok(())
}

pub async fn list(include_revoked: bool) -> Result<()> {
    let client = ApiClient::new()?;
    let path = if include_revoked {
        "/api/mvp/me/api-keys?include_revoked=true"
    } else {
        "/api/mvp/me/api-keys"
    };
    let res: Value = client.get_json(path).await?;
    let active = res
        .get("active_count")
        .and_then(|v| v.as_u64())
        .unwrap_or(0);
    let max = res.get("max_per_user").and_then(|v| v.as_u64()).unwrap_or(5);
    println!("active: {active} / {max}");
    let keys = res
        .get("keys")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    if keys.is_empty() {
        println!("  (no api keys)");
        return Ok(());
    }
    for k in keys {
        let key_id = k.get("key_id").and_then(|v| v.as_str()).unwrap_or("");
        let name = k.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let expires = k
            .get("expires_at")
            .and_then(|v| v.as_str())
            .unwrap_or("no-expiration");
        let revoked = k
            .get("revoked_at")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let last_used = k
            .get("last_used_at")
            .and_then(|v| v.as_str())
            .unwrap_or("never");
        let scopes = k
            .get("scopes")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|s| s.as_str())
                    .collect::<Vec<_>>()
                    .join(",")
            })
            .unwrap_or_default();
        let status_tag = if revoked.is_empty() { "" } else { " [REVOKED]" };
        println!(
            "  {key_id}{status_tag}\n    name: {name}\n    scopes: {scopes}\n    expires_at: {expires}\n    last_used_at: {last_used}"
        );
    }
    Ok(())
}

pub async fn revoke(key_hash: String) -> Result<()> {
    let client = ApiClient::new()?;
    client
        .delete(&format!(
            "/api/mvp/me/api-keys/{}",
            key_hash
        ))
        .await?;
    println!("[OK] revoked {key_hash}");
    Ok(())
}

// Admin 代理発行/管理サブコマンド (将来拡張用、CLI のヘルプに並べる)
pub async fn admin_list_all(include_revoked: bool) -> Result<()> {
    let client = ApiClient::new()?;
    let path = if include_revoked {
        "/api/mvp/admin/api-keys?include_revoked=true"
    } else {
        "/api/mvp/admin/api-keys"
    };
    let res: Value = client.get_json(path).await?;
    let keys = res
        .get("keys")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    if keys.is_empty() {
        println!("(no api keys in system)");
        return Ok(());
    }
    for k in keys {
        let key_id = k.get("key_id").and_then(|v| v.as_str()).unwrap_or("");
        let user_id = k.get("user_id").and_then(|v| v.as_str()).unwrap_or("");
        let name = k.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let revoked = k.get("revoked_at").and_then(|v| v.as_str()).unwrap_or("");
        let status_tag = if revoked.is_empty() { "" } else { " [REVOKED]" };
        println!("  {key_id}{status_tag}  owner={user_id}  name={name}");
    }
    Ok(())
}

pub async fn admin_revoke(key_hash: String) -> Result<()> {
    let client = ApiClient::new()?;
    client
        .delete(&format!(
            "/api/mvp/admin/api-keys/{}",
            key_hash
        ))
        .await?;
    println!("[OK] admin-revoked {key_hash}");
    Ok(())
}
