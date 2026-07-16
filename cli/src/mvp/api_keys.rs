//! Phase C: CLI operations for long-lived API keys.
//!
//! `stratoclave api-key create [--name N] [--scope S]... [--expires-days D]`
//! `stratoclave api-key list [--include-revoked]`
//! `stratoclave api-key revoke <key_hash>`
//!
//! On creation, the plaintext key is returned exactly once. The CLI prints it
//! with emphasis so the user can paste it into the gateway API key field
//! (e.g. Claude Desktop cowork).

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

/// Revoke one of the caller's own keys by its `key_id` (the value shown by
/// `api-key list`). The bare `/api/mvp/me/api-keys/{key_hash}` route was removed
/// (returns 410 Gone) for log-hygiene + ownership-race reasons, so this MUST
/// target `/by-key-id/{key_id}` — the previous path silently 410'd, leaving a
/// "revoked" key live (Fable contract audit A2b). The backend declares the
/// segment as `{key_id:path}`, so a key_id is passed verbatim.
pub async fn revoke(key_id: String) -> Result<()> {
    let client = ApiClient::new()?;
    client
        .delete(&format!("/api/mvp/me/api-keys/by-key-id/{}", key_id))
        .await?;
    println!("[OK] revoked {key_id}");
    Ok(())
}

// Admin-proxy issuance / management subcommands (reserved for future extension; listed in CLI help)
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

/// Admin-revoke ANY key by its `key_id`. The admin bare-`{key_hash}` route is
/// 410 Gone (there was never a working fallback), so this targets
/// `/by-key-id/{key_id}` — the only live admin revoke route (Fable contract
/// audit A2a: the previous path was a guaranteed 404/410, so admin revoke never
/// worked at all).
pub async fn admin_revoke(key_id: String) -> Result<()> {
    let client = ApiClient::new()?;
    client
        .delete(&format!("/api/mvp/admin/api-keys/by-key-id/{}", key_id))
        .await?;
    println!("[OK] admin-revoked {key_id}");
    Ok(())
}

/// List the API keys owned by a specific user (admin view).
/// GET /api/mvp/admin/users/{user_id}/api-keys
pub async fn admin_list_user(user_id: &str, include_revoked: bool) -> Result<()> {
    let client = ApiClient::new()?;
    let base = format!("/api/mvp/admin/users/{user_id}/api-keys");
    let path = if include_revoked {
        format!("{base}?include_revoked=true")
    } else {
        base
    };
    let res: Value = client.get_json(&path).await?;
    // The endpoint returns a bare list of key summaries.
    let keys = res.as_array().cloned().unwrap_or_default();
    if keys.is_empty() {
        println!("(user {user_id} has no api keys)");
        return Ok(());
    }
    for k in keys {
        let key_id = k.get("key_id").and_then(|v| v.as_str()).unwrap_or("");
        let name = k.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let revoked = k.get("revoked_at").and_then(|v| v.as_str()).unwrap_or("");
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
        println!("  {key_id}{status_tag}  name={name}  scopes={scopes}");
    }
    Ok(())
}

/// Issue an API key ON BEHALF OF a user (admin). The backend clips the scopes
/// to the target user's role grants; requesting more than the user can hold is
/// rejected. Requires an interactive (Cognito) session — an API key cannot mint
/// keys (privilege-escalation guard, enforced server-side too).
/// POST /api/mvp/admin/users/{user_id}/api-keys
pub async fn admin_create_on_behalf(
    user_id: &str,
    name: Option<String>,
    scopes: Vec<String>,
    expires_days: Option<u32>,
) -> Result<()> {
    let client = ApiClient::new()?;
    let mut body = json!({ "name": name.unwrap_or_default() });
    if !scopes.is_empty() {
        body["scopes"] = json!(scopes);
    }
    if let Some(d) = expires_days {
        body["expires_in_days"] = json!(d);
    }
    let path = format!("/api/mvp/admin/users/{user_id}/api-keys");
    let res: Value = client
        .post_json(&path, &body)
        .await
        .context("POST /api/mvp/admin/users/{user_id}/api-keys failed")?;
    let plain = res
        .get("plaintext_key")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("response missing plaintext_key"))?;
    let key_id = res.get("key_id").and_then(|v| v.as_str()).unwrap_or("");
    let granted = res
        .get("scopes")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|s| s.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    let expires_at = res
        .get("expires_at")
        .and_then(|v| v.as_str())
        .unwrap_or("no expiration");
    println!();
    println!("  API key issued for user {user_id} (shown once):");
    println!("   {plain}");
    println!("   key_id     : {key_id}");
    println!("   scopes     : {granted}");
    println!("   expires_at : {expires_at}");
    println!();
    Ok(())
}
