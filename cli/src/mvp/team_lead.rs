//! Team Lead subcommands (自分の所有 Tenant のみ).
//!
//! `stratoclave team-lead tenant create | list | show | members | usage`

use anyhow::Result;
use serde_json::{json, Value};

use super::admin::print_usage_bucket;
use super::api::ApiClient;

pub async fn tenant_create(name: &str, default_credit: Option<u64>) -> Result<()> {
    let client = ApiClient::new()?;
    let mut body = json!({"name": name});
    if let Some(c) = default_credit {
        body["default_credit"] = Value::Number(c.into());
    }
    let res: Value = client.post_json("/api/mvp/team-lead/tenants", &body).await?;
    println!("[OK] Tenant created");
    for k in ["tenant_id", "name", "default_credit", "status", "created_at"] {
        if let Some(v) = res.get(k) {
            println!("  {k}: {}", fmt_value(v));
        }
    }
    Ok(())
}

pub async fn tenant_list() -> Result<()> {
    let client = ApiClient::new()?;
    let res: Value = client.get_json("/api/mvp/team-lead/tenants").await?;
    let tenants = res.get("tenants").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    println!(
        "{:<40} {:<30} {:>12} {:<10}",
        "tenant_id", "name", "default_credit", "status"
    );
    for t in &tenants {
        println!(
            "{:<40} {:<30} {:>12} {:<10}",
            t.get("tenant_id").and_then(|v| v.as_str()).unwrap_or(""),
            t.get("name").and_then(|v| v.as_str()).unwrap_or(""),
            t.get("default_credit").and_then(|v| v.as_i64()).unwrap_or(0),
            t.get("status").and_then(|v| v.as_str()).unwrap_or(""),
        );
    }
    println!("({} tenants)", tenants.len());
    Ok(())
}

pub async fn tenant_show(tenant_id: &str) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/team-lead/tenants/{tenant_id}");
    let res: Value = client.get_json(&path).await?;
    for k in ["tenant_id", "name", "default_credit", "status", "created_at", "updated_at"] {
        if let Some(v) = res.get(k) {
            println!("  {k}: {}", fmt_value(v));
        }
    }
    Ok(())
}

pub async fn tenant_members(tenant_id: &str) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/team-lead/tenants/{tenant_id}/members");
    let res: Value = client.get_json(&path).await?;
    let members = res.get("members").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    println!(
        "{:<40} {:<10} {:>12} {:>12} {:>12}",
        "email", "role", "total", "used", "remaining"
    );
    for m in &members {
        println!(
            "{:<40} {:<10} {:>12} {:>12} {:>12}",
            m.get("email").and_then(|v| v.as_str()).unwrap_or(""),
            m.get("role").and_then(|v| v.as_str()).unwrap_or(""),
            m.get("total_credit").and_then(|v| v.as_i64()).unwrap_or(0),
            m.get("credit_used").and_then(|v| v.as_i64()).unwrap_or(0),
            m.get("remaining_credit").and_then(|v| v.as_i64()).unwrap_or(0),
        );
    }
    println!("({} members)", members.len());
    Ok(())
}

pub async fn tenant_usage(tenant_id: &str, since_days: u32) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!(
        "/api/mvp/team-lead/tenants/{tenant_id}/usage?since_days={since_days}"
    );
    let res: Value = client.get_json(&path).await?;
    print_usage_bucket(&res);
    Ok(())
}

fn fmt_value(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}
