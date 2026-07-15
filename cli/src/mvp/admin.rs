//! Admin subcommands: user / tenant / usage.
//!
//! `stratoclave admin user create | list | show | delete | assign-tenant | set-credit`
//! `stratoclave admin tenant create | list | show | delete | set-owner | members | usage`
//! `stratoclave admin usage show [--tenant T] [--user U] [--since X] [--until Y]`

use anyhow::{anyhow, Result};
use serde_json::{json, Value};

use super::api::ApiClient;

/// Helper that scans the Users table to resolve a user_id (Cognito sub) by email.
///
/// When `--team-lead-email foo@example.com` is specified, the backend expects a
/// Cognito sub as `team_lead_user_id`, so the CLI must translate email → sub
/// before sending the request (v2.1 §4.1).
///
/// Requires the `admin:users:read` permission (admin path only).
async fn resolve_user_id_by_email(client: &ApiClient, email: &str) -> Result<String> {
    let needle = email.to_ascii_lowercase();
    let mut cursor: Option<String> = None;
    for _ in 0..20 {
        // Pagination cap: 20 iterations × 100 users = up to 2 000 users searched
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

/// Resolves `--team-lead` / `--team-lead-email` to a Cognito sub.
///
/// - If `team_lead_id` is provided, return it as-is (a sub or `admin-owned`).
/// - If `team_lead_email` is provided, resolve email → user_id via the API.
/// - If neither is provided, return `admin-owned` (default for the admin path).
/// - If both are provided, return an explicit error to avoid ambiguity.
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
    // P0-6: print every field except `temporary_password` via the
    // generic helper; the password goes through the TTY-guarded
    // reveal path so it does not land in shell history / CI logs.
    print_kv(&res, &["email", "user_id", "role", "org_id", "user_pool_id"]);
    if let Some(pw) = res.get("temporary_password").and_then(|v| v.as_str()) {
        if !pw.is_empty() {
            crate::mvp::admin_cmd::reveal_secret_via_tty("temporary_password", pw)?;
        }
    }
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
// TENANT POOL BUDGET (A-1)
// ============================================================
/// Set (create or update) the tenant's dollar pool budget for a period.
///
/// `limit_usd` is a human dollar string ("500", "$500", "500.50"); it is
/// converted to whole USD cents locally (never via float) and sent to the
/// backend, which stores it as integer micro-USD. When the tenant has a pool
/// for the period, every inference reserves its dollar cost from the pool
/// atomically with the per-user token debit — a ceiling a credential broker
/// cannot enforce because it has no request-time choke point.
pub async fn tenant_pool_budget_set(
    tenant_id: &str,
    limit_usd: &str,
    period: Option<&str>,
    status: &str,
) -> Result<()> {
    let limit_usd_cents = parse_usd_to_cents(limit_usd)?;
    let client = ApiClient::new()?;
    let mut body = json!({
        "limit_usd_cents": limit_usd_cents,
        "status": status,
    });
    if let Some(p) = period {
        body["period"] = Value::String(p.to_string());
    }
    let path = format!("/api/mvp/admin/tenants/{tenant_id}/pool-budget");
    let res: Value = client.put_json(&path, &body).await?;
    println!("[OK] Pool budget set");
    print_pool_budget(&res);
    Ok(())
}

/// Show the tenant's pool budget and live usage for a period.
///
/// The backend returns 404 when no pool is set for the period (pool budgeting
/// is opt-in; absence means only per-user token budgets apply).
pub async fn tenant_pool_budget_show(tenant_id: &str, period: Option<&str>) -> Result<()> {
    let client = ApiClient::new()?;
    let path = match period {
        Some(p) => format!("/api/mvp/admin/tenants/{tenant_id}/pool-budget?period={p}"),
        None => format!("/api/mvp/admin/tenants/{tenant_id}/pool-budget"),
    };
    let res: Value = client.get_json(&path).await?;
    print_pool_budget(&res);
    Ok(())
}

// ============================================================
// ROUTING CONFIG (admin) — P0-11 chain / quotas / allowlist
// ============================================================

/// Show the tenant routing config, or a per-user override when `user` is set.
///
/// This is the config that P0-11 enforcement (per-model quota + cascading
/// fallback) reads on every request; before this command it could only be
/// hand-edited in DynamoDB.
pub async fn routing_config_get(tenant_id: &str, user: Option<&str>) -> Result<()> {
    let client = ApiClient::new()?;
    let path = match user {
        Some(u) => format!("/api/mvp/admin/tenants/{tenant_id}/users/{u}/routing-config"),
        None => format!("/api/mvp/admin/tenants/{tenant_id}/routing-config"),
    };
    let res: Value = client.get_json(&path).await?;
    println!("{}", serde_json::to_string_pretty(&res)?);
    Ok(())
}

/// Replace the routing config from a JSON file (or stdin with "-").
///
/// PUT semantics (full replace). The backend validates every model id against
/// the registry, quota limits >= 0, and (for a user override) that the chain is
/// an order-preserving subsequence of the tenant chain — a 400 names the
/// offending field so a typo can never land an un-enforceable config.
pub async fn routing_config_set(tenant_id: &str, file: &str, user: Option<&str>) -> Result<()> {
    let raw = if file == "-" {
        use std::io::Read;
        let mut buf = String::new();
        std::io::stdin()
            .read_to_string(&mut buf)
            .map_err(|e| anyhow!("failed to read stdin: {e}"))?;
        buf
    } else {
        std::fs::read_to_string(file).map_err(|e| anyhow!("cannot read {file}: {e}"))?
    };
    let body: Value =
        serde_json::from_str(&raw).map_err(|e| anyhow!("invalid JSON in {file}: {e}"))?;

    let client = ApiClient::new()?;
    let path = match user {
        Some(u) => format!("/api/mvp/admin/tenants/{tenant_id}/users/{u}/routing-config"),
        None => format!("/api/mvp/admin/tenants/{tenant_id}/routing-config"),
    };
    let res: Value = client.put_json(&path, &body).await?;
    println!("[OK] Routing config set");
    println!("{}", serde_json::to_string_pretty(&res)?);
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
    // Sort by token count descending for a stable, readable display
    let mut entries: Vec<(&String, i64)> = map
        .iter()
        .map(|(k, v)| (k, v.as_i64().unwrap_or(0)))
        .collect();
    entries.sort_by(|a, b| b.1.cmp(&a.1));
    for (k, n) in entries {
        println!("    {k}: {n}");
    }
}

/// Parse a human dollar string ("500", "$500", "500.50", "1,000") into whole
/// USD cents, without ever touching a float. Rejects sub-cent precision and
/// negative values so an operator cannot silently set a wrong ceiling.
///
/// The backend takes `limit_usd_cents` and multiplies by 10_000 to store
/// micro-USD; keeping the cent conversion integer-exact here means the pool
/// ceiling is precise end to end.
pub(crate) fn parse_usd_to_cents(input: &str) -> Result<u64> {
    let cleaned: String = input
        .trim()
        .chars()
        .filter(|c| *c != '$' && *c != ',' && !c.is_whitespace())
        .collect();
    if cleaned.is_empty() {
        return Err(anyhow!("empty dollar amount"));
    }
    if cleaned.starts_with('-') {
        return Err(anyhow!("dollar amount must not be negative: {input}"));
    }
    let (dollars_str, cents_str) = match cleaned.split_once('.') {
        Some((d, c)) => (d, c),
        None => (cleaned.as_str(), ""),
    };
    // Empty integer part ("$.50") means zero dollars.
    let dollars: u64 = if dollars_str.is_empty() {
        0
    } else {
        dollars_str
            .parse()
            .map_err(|_| anyhow!("invalid dollar amount: {input}"))?
    };
    let cents: u64 = match cents_str.len() {
        0 => 0,
        1 => {
            // "500.5" == 50 cents
            let n: u64 = cents_str
                .parse()
                .map_err(|_| anyhow!("invalid cents in amount: {input}"))?;
            n * 10
        }
        2 => cents_str
            .parse()
            .map_err(|_| anyhow!("invalid cents in amount: {input}"))?,
        _ => {
            return Err(anyhow!(
                "sub-cent precision is not allowed: {input} (use at most 2 decimals)"
            ))
        }
    };
    dollars
        .checked_mul(100)
        .and_then(|d| d.checked_add(cents))
        .ok_or_else(|| anyhow!("dollar amount too large: {input}"))
}

/// Render integer USD cents as a `$X.YY` string (display only, no float).
pub(crate) fn format_cents_as_usd(cents: i64) -> String {
    let neg = cents < 0;
    let abs = cents.unsigned_abs();
    let sign = if neg { "-" } else { "" };
    format!("{sign}${}.{:02}", abs / 100, abs % 100)
}

fn print_pool_budget(v: &Value) {
    let period = v.get("period").and_then(|x| x.as_str()).unwrap_or("");
    let status = v.get("status").and_then(|x| x.as_str()).unwrap_or("");
    let limit_cents = v
        .get("pool_limit_usd_cents")
        .and_then(|x| x.as_i64())
        .unwrap_or(0);
    let remaining_cents = v
        .get("remaining_usd_cents")
        .and_then(|x| x.as_i64())
        .unwrap_or(0);
    let limit_micro = v
        .get("pool_limit_microusd")
        .and_then(|x| x.as_i64())
        .unwrap_or(0);
    let reserved_micro = v
        .get("pool_reserved_microusd")
        .and_then(|x| x.as_i64())
        .unwrap_or(0);
    let settled_micro = v
        .get("pool_settled_microusd")
        .and_then(|x| x.as_i64())
        .unwrap_or(0);
    let remaining_micro = v
        .get("remaining_microusd")
        .and_then(|x| x.as_i64())
        .unwrap_or(0);

    println!(
        "  tenant_id:  {}",
        v.get("tenant_id").and_then(|x| x.as_str()).unwrap_or("")
    );
    println!("  period:     {period}");
    println!("  status:     {status}");
    println!(
        "  limit:      {} ({limit_micro} micro-USD)",
        format_cents_as_usd(limit_cents)
    );
    println!("  reserved:   {reserved_micro} micro-USD (in-flight requests)");
    println!("  settled:    {settled_micro} micro-USD (recorded spend)");
    println!(
        "  remaining:  {} ({remaining_micro} micro-USD)",
        format_cents_as_usd(remaining_cents)
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_plain_integer_dollars() {
        assert_eq!(parse_usd_to_cents("500").unwrap(), 50_000);
    }

    #[test]
    fn parse_strips_dollar_sign_and_commas() {
        assert_eq!(parse_usd_to_cents("$1,000").unwrap(), 100_000);
    }

    #[test]
    fn parse_two_decimal_cents() {
        assert_eq!(parse_usd_to_cents("500.50").unwrap(), 50_050);
        assert_eq!(parse_usd_to_cents("0.01").unwrap(), 1);
    }

    #[test]
    fn parse_one_decimal_is_tenths_of_a_dollar() {
        // "500.5" is $500.50, i.e. 50_050 cents — not 505.
        assert_eq!(parse_usd_to_cents("500.5").unwrap(), 50_050);
    }

    #[test]
    fn parse_leading_decimal_is_zero_dollars() {
        assert_eq!(parse_usd_to_cents("$.50").unwrap(), 50);
    }

    #[test]
    fn parse_rejects_sub_cent_precision() {
        assert!(parse_usd_to_cents("1.234").is_err());
    }

    #[test]
    fn parse_rejects_negative() {
        assert!(parse_usd_to_cents("-5").is_err());
    }

    #[test]
    fn parse_rejects_garbage() {
        assert!(parse_usd_to_cents("abc").is_err());
        assert!(parse_usd_to_cents("").is_err());
    }

    #[test]
    fn format_cents_roundtrips_display() {
        assert_eq!(format_cents_as_usd(50_000), "$500.00");
        assert_eq!(format_cents_as_usd(50_050), "$500.50");
        assert_eq!(format_cents_as_usd(1), "$0.01");
        assert_eq!(format_cents_as_usd(0), "$0.00");
    }
}
