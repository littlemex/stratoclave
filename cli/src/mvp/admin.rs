//! Admin subcommands: user / tenant / usage.
//!
//! `stratoclave admin user create | list | show | delete | assign-tenant | set-credit`
//! `stratoclave admin tenant create | list | show | delete | set-owner | members | usage`
//! `stratoclave admin usage show [--tenant T] [--user U] [--since X] [--until Y]`

use anyhow::{anyhow, Result};
use serde_json::{json, Value};

use super::api::ApiClient;

/// email で Users テーブルをスキャンして user_id (Cognito sub) を解決するヘルパ.
///
/// `--team-lead-email foo@example.com` のように指定された場合、Backend は
/// team_lead_user_id として Cognito sub を期待するため、CLI 側で email → sub を
/// 変換する必要がある (v2.1 §4.1)。
///
/// `admin:users:read` 権限が必要 (admin 経路限定)。
async fn resolve_user_id_by_email(client: &ApiClient, email: &str) -> Result<String> {
    let needle = email.to_ascii_lowercase();
    let mut cursor: Option<String> = None;
    for _ in 0..20 {
        // ページング上限 20 iteration × 100 users = 2000 件までは探索
        let mut path = String::from("/api/mvp/admin/users?limit=100");
        if let Some(c) = &cursor {
            path.push_str(&format!("&cursor={c}"));
        }
        let res: Value = client.get_json(&path).await?;
        if let Some(users) = res.get("users").and_then(|v| v.as_array()) {
            for u in users {
                let user_email = u
                    .get("email")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_ascii_lowercase();
                if user_email == needle {
                    return u
                        .get("user_id")
                        .and_then(|v| v.as_str())
                        .map(String::from)
                        .ok_or_else(|| anyhow!("user_id missing for email {email}"));
                }
            }
        }
        match res.get("next_cursor").and_then(|v| v.as_str()) {
            Some(c) if !c.is_empty() => cursor = Some(c.to_string()),
            _ => break,
        }
    }
    Err(anyhow!(
        "No user found with email={email}. List users first: `stratoclave admin user list`"
    ))
}

/// `--team-lead` / `--team-lead-email` を解決して Cognito sub を返す.
///
/// - `team_lead_id` が指定されていればそのまま返す (sub or `admin-owned`)
/// - `team_lead_email` が指定されていれば email → user_id を API で解決
/// - 両方未指定なら `admin-owned` を返す (Admin 経路のデフォルト)
/// - 両方指定されていた場合は明示エラー (あいまいさ排除)
async fn resolve_team_lead(
    client: &ApiClient,
    team_lead_id: Option<&str>,
    team_lead_email: Option<&str>,
) -> Result<String> {
    match (team_lead_id, team_lead_email) {
        (Some(_), Some(_)) => Err(anyhow!(
            "--team-lead and --team-lead-email are mutually exclusive"
        )),
        (Some(id), None) => Ok(id.to_string()),
        (None, Some(email)) => resolve_user_id_by_email(client, email).await,
        (None, None) => Ok("admin-owned".to_string()),
    }
}

// ============================================================
// USER
// ============================================================
pub async fn user_create(
    email: &str,
    role: &str,
    tenant_id: Option<&str>,
    total_credit: Option<u64>,
) -> Result<()> {
    let client = ApiClient::new()?;
    let mut body = json!({
        "email": email,
        "role": role,
    });
    if let Some(tid) = tenant_id {
        body["tenant_id"] = Value::String(tid.to_string());
    }
    if let Some(credit) = total_credit {
        body["total_credit"] = Value::Number(credit.into());
    }
    let res: Value = client.post_json("/api/mvp/admin/users", &body).await?;
    print_kv(&res, &["email", "user_id", "role", "org_id", "temporary_password", "user_pool_id"]);
    Ok(())
}

pub async fn user_list(
    role: Option<&str>,
    tenant_id: Option<&str>,
    limit: u32,
) -> Result<()> {
    let client = ApiClient::new()?;
    let mut qs: Vec<String> = vec![format!("limit={limit}")];
    if let Some(r) = role {
        qs.push(format!("role={r}"));
    }
    if let Some(tid) = tenant_id {
        qs.push(format!("tenant_id={tid}"));
    }
    let path = format!("/api/mvp/admin/users?{}", qs.join("&"));
    let res: Value = client.get_json(&path).await?;
    let users = res.get("users").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    println!("{:<40} {:<32} {:<24} {:<10} {:>12} {:>12}", "email", "user_id", "tenant", "roles", "total", "remaining");
    for u in &users {
        let email = u.get("email").and_then(|v| v.as_str()).unwrap_or("");
        let uid = u.get("user_id").and_then(|v| v.as_str()).unwrap_or("");
        let org = u.get("org_id").and_then(|v| v.as_str()).unwrap_or("");
        let roles = u
            .get("roles")
            .and_then(|v| v.as_array())
            .map(|a| a.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join(","))
            .unwrap_or_default();
        let total = u.get("total_credit").and_then(|v| v.as_i64()).unwrap_or(0);
        let remaining = u.get("remaining_credit").and_then(|v| v.as_i64()).unwrap_or(0);
        println!("{email:<40} {uid:<32} {org:<24} {roles:<10} {total:>12} {remaining:>12}");
    }
    if let Some(cursor) = res.get("next_cursor").and_then(|v| v.as_str()) {
        println!("\n[next_cursor] {cursor}");
    }
    println!("({} users)", users.len());
    Ok(())
}

pub async fn user_show(user_id: &str) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/admin/users/{user_id}");
    let res: Value = client.get_json(&path).await?;
    print_kv(
        &res,
        &["email", "user_id", "roles", "org_id", "total_credit", "credit_used", "remaining_credit", "created_at"],
    );
    Ok(())
}

pub async fn user_delete(user_id: &str) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/admin/users/{user_id}");
    client.delete(&path).await?;
    println!("[OK] Deleted user_id={user_id}");
    Ok(())
}

pub async fn user_assign_tenant(
    user_id: &str,
    tenant_id: &str,
    new_role: &str,
    total_credit: Option<u64>,
) -> Result<()> {
    let client = ApiClient::new()?;
    let mut body = json!({"tenant_id": tenant_id, "new_role": new_role});
    if let Some(c) = total_credit {
        body["total_credit"] = Value::Number(c.into());
    }
    let path = format!("/api/mvp/admin/users/{user_id}/tenant");
    let res: Value = client.put_json(&path, &body).await?;
    println!("[OK] User reassigned to tenant {tenant_id}");
    print_kv(
        &res,
        &["email", "user_id", "org_id", "total_credit", "credit_used", "remaining_credit"],
    );
    println!("[NOTE] User must re-login (JWT invalidated by AdminUserGlobalSignOut)");
    Ok(())
}

pub async fn user_set_credit(user_id: &str, total_credit: u64, reset_used: bool) -> Result<()> {
    let client = ApiClient::new()?;
    let body = json!({"total_credit": total_credit, "reset_used": reset_used});
    let path = format!("/api/mvp/admin/users/{user_id}/credit");
    let res: Value = client.patch_json(&path, &body).await?;
    println!("[OK] Credit updated for user {user_id}");
    print_kv(
        &res,
        &["email", "org_id", "total_credit", "credit_used", "remaining_credit"],
    );
    Ok(())
}

// ============================================================
// TENANT
// ============================================================
pub async fn tenant_create(
    name: &str,
    team_lead_user_id: Option<&str>,
    team_lead_email: Option<&str>,
    default_credit: Option<u64>,
) -> Result<()> {
    let client = ApiClient::new()?;
    let resolved_team_lead =
        resolve_team_lead(&client, team_lead_user_id, team_lead_email).await?;
    if team_lead_email.is_some() {
        println!("[INFO] Resolved team-lead email -> user_id: {resolved_team_lead}");
    }

    let mut body = json!({
        "name": name,
        "team_lead_user_id": resolved_team_lead,
    });
    if let Some(c) = default_credit {
        body["default_credit"] = Value::Number(c.into());
    }
    let res: Value = client.post_json("/api/mvp/admin/tenants", &body).await?;
    println!("[OK] Tenant created");
    print_kv(
        &res,
        &["tenant_id", "name", "team_lead_user_id", "default_credit", "status", "created_at"],
    );
    Ok(())
}

pub async fn tenant_list(limit: u32) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/admin/tenants?limit={limit}");
    let res: Value = client.get_json(&path).await?;
    let tenants = res
        .get("tenants")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    println!(
        "{:<40} {:<30} {:<38} {:>12} {:<10}",
        "tenant_id", "name", "team_lead_user_id", "default_credit", "status"
    );
    for t in &tenants {
        println!(
            "{:<40} {:<30} {:<38} {:>12} {:<10}",
            t.get("tenant_id").and_then(|v| v.as_str()).unwrap_or(""),
            t.get("name").and_then(|v| v.as_str()).unwrap_or(""),
            t.get("team_lead_user_id").and_then(|v| v.as_str()).unwrap_or(""),
            t.get("default_credit").and_then(|v| v.as_i64()).unwrap_or(0),
            t.get("status").and_then(|v| v.as_str()).unwrap_or(""),
        );
    }
    println!("({} tenants)", tenants.len());
    Ok(())
}

pub async fn tenant_show(tenant_id: &str) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/admin/tenants/{tenant_id}");
    let res: Value = client.get_json(&path).await?;
    print_kv(
        &res,
        &[
            "tenant_id",
            "name",
            "team_lead_user_id",
            "default_credit",
            "status",
            "created_at",
            "updated_at",
            "created_by",
        ],
    );
    Ok(())
}

pub async fn tenant_delete(tenant_id: &str) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/admin/tenants/{tenant_id}");
    client.delete(&path).await?;
    println!("[OK] Tenant archived: {tenant_id}");
    Ok(())
}

pub async fn tenant_set_owner(
    tenant_id: &str,
    team_lead_user_id: Option<&str>,
    team_lead_email: Option<&str>,
) -> Result<()> {
    let client = ApiClient::new()?;
    let resolved = resolve_team_lead(&client, team_lead_user_id, team_lead_email).await?;
    if team_lead_email.is_some() {
        println!("[INFO] Resolved team-lead email -> user_id: {resolved}");
    }
    let body = json!({"team_lead_user_id": resolved});
    let path = format!("/api/mvp/admin/tenants/{tenant_id}/owner");
    let res: Value = client.put_json(&path, &body).await?;
    println!("[OK] Owner reassigned");
    print_kv(&res, &["tenant_id", "name", "team_lead_user_id", "updated_at"]);
    Ok(())
}

pub async fn tenant_members(tenant_id: &str) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/admin/tenants/{tenant_id}/users");
    let res: Value = client.get_json(&path).await?;
    let members = res.get("members").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    println!(
        "{:<40} {:<32} {:<10} {:>12} {:>12} {:<10}",
        "email", "user_id", "role", "total", "remaining", "status"
    );
    for m in &members {
        println!(
            "{:<40} {:<32} {:<10} {:>12} {:>12} {:<10}",
            m.get("email").and_then(|v| v.as_str()).unwrap_or(""),
            m.get("user_id").and_then(|v| v.as_str()).unwrap_or(""),
            m.get("role").and_then(|v| v.as_str()).unwrap_or(""),
            m.get("total_credit").and_then(|v| v.as_i64()).unwrap_or(0),
            m.get("remaining_credit").and_then(|v| v.as_i64()).unwrap_or(0),
            m.get("status").and_then(|v| v.as_str()).unwrap_or(""),
        );
    }
    println!("({} members)", members.len());
    Ok(())
}

pub async fn tenant_usage(tenant_id: &str, since_days: u32) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!(
        "/api/mvp/admin/tenants/{tenant_id}/usage?since_days={since_days}"
    );
    let res: Value = client.get_json(&path).await?;
    print_usage_bucket(&res);
    Ok(())
}

// ============================================================
// USAGE LOGS (admin)
// ============================================================
pub async fn usage_logs(
    tenant_id: Option<&str>,
    user_id: Option<&str>,
    since: Option<&str>,
    until: Option<&str>,
    limit: u32,
) -> Result<()> {
    let client = ApiClient::new()?;
    let mut qs: Vec<String> = vec![format!("limit={limit}")];
    if let Some(v) = tenant_id {
        qs.push(format!("tenant_id={v}"));
    }
    if let Some(v) = user_id {
        qs.push(format!("user_id={v}"));
    }
    if let Some(v) = since {
        qs.push(format!("since={v}"));
    }
    if let Some(v) = until {
        qs.push(format!("until={v}"));
    }
    let path = format!("/api/mvp/admin/usage-logs?{}", qs.join("&"));
    let res: Value = client.get_json(&path).await?;
    let logs = res.get("logs").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    println!(
        "{:<28} {:<40} {:<40} {:>8} {:>8} {:>8}",
        "recorded_at", "user_email", "model_id", "input", "output", "total"
    );
    for l in &logs {
        println!(
            "{:<28} {:<40} {:<40} {:>8} {:>8} {:>8}",
            l.get("recorded_at").and_then(|v| v.as_str()).unwrap_or(""),
            l.get("user_email").and_then(|v| v.as_str()).unwrap_or(""),
            l.get("model_id").and_then(|v| v.as_str()).unwrap_or(""),
            l.get("input_tokens").and_then(|v| v.as_i64()).unwrap_or(0),
            l.get("output_tokens").and_then(|v| v.as_i64()).unwrap_or(0),
            l.get("total_tokens").and_then(|v| v.as_i64()).unwrap_or(0),
        );
    }
    if let Some(cursor) = res.get("next_cursor").and_then(|v| v.as_str()) {
        println!("\n[next_cursor] {cursor}");
    }
    println!("({} logs)", logs.len());
    Ok(())
}

// ============================================================
// helpers
// ============================================================
fn print_kv(v: &Value, keys: &[&str]) {
    for k in keys {
        if let Some(val) = v.get(k) {
            let s = match val {
                Value::String(s) => s.clone(),
                Value::Array(a) => a
                    .iter()
                    .map(|x| x.as_str().map(String::from).unwrap_or_else(|| x.to_string()))
                    .collect::<Vec<_>>()
                    .join(","),
                other => other.to_string(),
            };
            println!("  {k}: {s}");
        }
    }
}

pub(crate) fn print_usage_bucket(v: &Value) {
    let tenant_id = v.get("tenant_id").and_then(|x| x.as_str()).unwrap_or("");
    let sample_size = v.get("sample_size").and_then(|x| x.as_i64()).unwrap_or(0);

    println!("  tenant_id: {tenant_id}");
    println!("  total_tokens:  {}", v.get("total_tokens").and_then(|x| x.as_i64()).unwrap_or(0));
    println!("  input_tokens:  {}", v.get("input_tokens").and_then(|x| x.as_i64()).unwrap_or(0));
    println!("  output_tokens: {}", v.get("output_tokens").and_then(|x| x.as_i64()).unwrap_or(0));
    println!("  sample_size:   {sample_size}");

    if sample_size == 0 {
        println!("  (no usage recorded in the selected window)");
        return;
    }

    print_breakdown("by_model", v.get("by_model"));
    print_breakdown("by_user", v.get("by_user"));
    print_breakdown("by_user_email", v.get("by_user_email"));
}

fn print_breakdown(label: &str, value: Option<&Value>) {
    let Some(map) = value.and_then(|x| x.as_object()) else {
        return;
    };
    if map.is_empty() {
        return;
    }
    println!("  {label}:");
    // トークン数の降順で安定表示
    let mut entries: Vec<(&String, i64)> = map
        .iter()
        .map(|(k, v)| (k, v.as_i64().unwrap_or(0)))
        .collect();
    entries.sort_by(|a, b| b.1.cmp(&a.1));
    for (k, n) in entries {
        println!("    {k}: {n}");
    }
}
