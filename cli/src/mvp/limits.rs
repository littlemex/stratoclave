//! Money-ceiling raises from the command line: request, decide, revoke.
//!
//! The mechanism ships here before any screen depends on it, which is the point of
//! doing the CLI first: a screen encodes assumptions about a flow, and encoding
//! them before the flow has been exercised means discovering the wrong ones twice.
//!
//! Amounts are entered as human dollar strings and converted to whole USD cents
//! locally, never through a float, then sent as micro-USD — the unit the backend
//! stores. `parse_usd_to_cents` is shared with the pool-budget commands so a figure
//! typed the same way means the same number in both.
//!
//! WHAT IS DELIBERATELY NOT PRINTED. A request's own justification and the client
//! token never appear in any output here, because the backend does not return them:
//! they are kept out of every log, metric and error body, and a CLI that echoed
//! them back would be the sink that undid that. A DECISION's comment IS printed —
//! it is addressed to the requester and is the whole reason a rejection is worth
//! reading.

use anyhow::{anyhow, Result};
use serde_json::{json, Value};

use super::admin::parse_usd_to_cents;
use super::api::ApiClient;

const MICRO_USD_PER_CENT: u64 = 10_000;

/// A stable idempotency key for one submission attempt.
///
/// The daily slot stores it, so re-running the same command with the same token
/// returns the request the first run created instead of being refused for having
/// already used today's allowance. Generated per invocation unless the caller
/// supplies one: a token derived from the arguments would make two genuinely
/// different asks on the same day collapse into one, and a token that changed on
/// every retry would burn the day's slot on a network blip.
fn fresh_token() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("cli-{nanos:x}")
}

fn micro_usd(limit_usd: &str) -> Result<u64> {
    let cents = parse_usd_to_cents(limit_usd)?;
    cents
        .checked_mul(MICRO_USD_PER_CENT)
        .ok_or_else(|| anyhow!("{limit_usd} is too large to express in micro-USD"))
}

/// `pub(crate)` (F3): the 402 handler in `client.rs` renders a `raise_hint`'s
/// dollar figures with this SAME formatter, so a request-does-not-fit
/// message and a `stratoclave limits raise` receipt never disagree about
/// how a micro-USD figure becomes a string.
pub(crate) fn usd(micro: i64) -> String {
    let sign = if micro < 0 { "-" } else { "" };
    let m = micro.unsigned_abs();
    format!("{sign}${}.{:02}", m / 1_000_000, (m % 1_000_000) / 10_000)
}

fn s(v: &Value, key: &str) -> String {
    v.get(key)
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string()
}

fn i(v: &Value, key: &str) -> i64 {
    v.get(key).and_then(|x| x.as_i64()).unwrap_or(0)
}

fn print_request(r: &Value) {
    println!("  request:   {}", s(r, "request_id"));
    println!("  status:    {}", s(r, "status"));
    println!("  tenant:    {}", s(r, "tenant_id"));
    println!("  asked:     {}", usd(i(r, "asked_amount_microusd")));
    println!("  reason:    {}", s(r, "reason_code"));
    if r.get("approved_amount_microusd").is_some() {
        let approved = i(r, "approved_amount_microusd");
        let asked = i(r, "asked_amount_microusd");
        // Say plainly when the approved figure is not the figure that was asked
        // for. A requester told APPROVED and left to plan against her own number is
        // the grievance this whole feature is about.
        let note = if approved < asked {
            "  (less than you asked for)"
        } else {
            ""
        };
        println!("  approved:  {}{note}", usd(approved));
    }
    if r.get("expires_at").is_some() {
        println!("  expires:   {} (epoch)", i(r, "expires_at"));
    }
    if let Some(c) = r.get("decision_comment").and_then(|v| v.as_str()) {
        println!("  decision:  {c}");
    }
}

fn print_grant(g: &Value) {
    println!("  grant:     {}", s(g, "grant_id"));
    println!("  status:    {}", s(g, "status"));
    println!("  amount:    {}", usd(i(g, "approved_amount_microusd")));
    println!("  expires:   {} (epoch)", i(g, "expires_at"));
    println!("  period:    {}", s(g, "period"));
    if g.get("revoke_blocked")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        // The stuck case, named on the grant rather than only counted by a metric.
        // A count tells an operator that something is stuck; only this tells them
        // which grant and why, which is the form of the answer that leads to a fix.
        println!(
            "  BLOCKED:   its capacity could not be returned ({} attempt(s)): {}",
            i(g, "revoke_attempts"),
            s(g, "revoke_blocked_reason")
        );
    }
}

// ============================================================
// The requester
// ============================================================

/// File a raise against your own tenant's money ceiling.
///
/// One per person per tenant per UTC day. A refusal names the request holding the
/// day and when it resets, with the zone spelled out.
pub async fn raise_request(
    limit_usd: &str,
    reason: &str,
    comment: Option<&str>,
    client_token: Option<&str>,
) -> Result<()> {
    let mut body = json!({
        "asked_amount_microusd": micro_usd(limit_usd)?,
        "reason_code": reason,
        "client_token": client_token.map(str::to_string).unwrap_or_else(fresh_token),
    });
    if let Some(c) = comment {
        body["comment"] = Value::String(c.to_string());
    }
    let client = ApiClient::new()?;
    let res: Value = client.post_json("/api/mvp/me/limit-raises", &body).await?;
    println!("[OK] Limit raise filed");
    print_request(&res);
    Ok(())
}

/// Your own raises for your current tenant.
pub async fn raise_list() -> Result<()> {
    let client = ApiClient::new()?;
    let res: Value = client.get_json("/api/mvp/me/limit-raises").await?;
    let empty = vec![];
    let items = res
        .get("requests")
        .and_then(|v| v.as_array())
        .unwrap_or(&empty);
    if items.is_empty() {
        println!("(no limit raises)");
    }
    for r in items {
        print_request(r);
        println!();
    }
    if let Some(codes) = res.get("reason_codes").and_then(|v| v.as_array()) {
        let names: Vec<String> = codes
            .iter()
            .filter_map(|c| c.as_str().map(str::to_string))
            .collect();
        println!("accepted reasons: {}", names.join(", "));
    }
    Ok(())
}

/// Take back your own pending raise, which frees the day's slot.
pub async fn raise_withdraw(request_id: &str) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/me/limit-raises/{request_id}/withdraw");
    let res: Value = client.post_json(&path, &json!({})).await?;
    println!("[OK] Withdrawn; today's slot is free again");
    print_request(&res);
    Ok(())
}

// ============================================================
// The approver
// ============================================================
//
// `scope` selects the admin form or the team-lead form of every path below. It is
// the CALLER's declaration of which authority they are exercising, not a guess from
// their roles, and the backend treats it the same way: the team-lead routes bind
// the approval transaction to tenant ownership, the admin routes do not. An admin
// who reaches for the team-lead form gets the ownership check, which is what makes
// the two forms mean different things.

fn base(scope: &str) -> &'static str {
    if scope == "team-lead" {
        "team-lead"
    } else {
        "admin"
    }
}

pub async fn raise_list_for_tenant(
    scope: &str,
    tenant_id: &str,
    status: Option<&str>,
) -> Result<()> {
    let client = ApiClient::new()?;
    let mut path = format!(
        "/api/mvp/{}/limit-raises?tenant_id={tenant_id}",
        base(scope)
    );
    if let Some(st) = status {
        path.push_str(&format!("&status={st}"));
    }
    let res: Value = client.get_json(&path).await?;
    let empty = vec![];
    let items = res
        .get("requests")
        .and_then(|v| v.as_array())
        .unwrap_or(&empty);
    if items.is_empty() {
        println!("(no limit raises for {tenant_id})");
    }
    for r in items {
        print_request(r);
        println!();
    }
    Ok(())
}

/// Approve a raise.
///
/// `expires_at` is epoch seconds and the backend refuses anything less than five
/// minutes out or past the end of the billing period — the second bound because a
/// grant that outlived its period would have its capacity destroyed at the boundary
/// rather than released. `--comment` is required when approving for less than was
/// asked, because otherwise the requester is handed a figure she did not choose
/// with no way to find out how it was reached.
pub async fn raise_approve(
    scope: &str,
    request_id: &str,
    limit_usd: &str,
    expires_at: i64,
    comment: Option<&str>,
) -> Result<()> {
    let mut body = json!({
        "approved_amount_microusd": micro_usd(limit_usd)?,
        "expires_at": expires_at,
    });
    if let Some(c) = comment {
        body["decision_comment"] = Value::String(c.to_string());
    }
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/{}/limit-raises/{request_id}/approve", base(scope));
    let res: Value = client.post_json(&path, &body).await?;
    println!("[OK] Approved");
    if let Some(r) = res.get("request") {
        print_request(r);
    }
    if let Some(g) = res.get("grant") {
        print_grant(g);
    }
    Ok(())
}

/// Reject a raise, with a reason the requester is given.
///
/// The comment is required rather than optional: a rejection with no reason is
/// indistinguishable from the feature being broken, and the requester has nothing
/// to act on either way.
pub async fn raise_reject(scope: &str, request_id: &str, comment: &str) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/{}/limit-raises/{request_id}/reject", base(scope));
    let res: Value = client
        .post_json(&path, &json!({ "decision_comment": comment }))
        .await?;
    println!("[OK] Rejected; the requester's daily slot is free again");
    print_request(&res);
    Ok(())
}

/// A tenant's grants, with the reconciliation beside them.
///
/// The reconciliation is printed with the list rather than fetched separately,
/// because the question about a grant inventory is always whether it adds up to
/// what the pool row says — and a list shown without that answer is a list nobody
/// can trust.
pub async fn grant_list(scope: &str, tenant_id: &str) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!(
        "/api/mvp/{}/limit-grants?tenant_id={tenant_id}",
        base(scope)
    );
    let res: Value = client.get_json(&path).await?;
    let empty = vec![];
    let items = res
        .get("grants")
        .and_then(|v| v.as_array())
        .unwrap_or(&empty);
    if items.is_empty() {
        println!("(no grants for {tenant_id})");
    }
    for g in items {
        print_grant(g);
        println!();
    }
    if let Some(rec) = res.get("reconciliation") {
        let clean = rec.get("clean").and_then(|v| v.as_bool()).unwrap_or(false);
        println!(
            "reconciliation: {}",
            if clean { "clean" } else { "DISAGREES" }
        );
        let empty2 = vec![];
        for row in rec
            .get("rows")
            .and_then(|v| v.as_array())
            .unwrap_or(&empty2)
        {
            println!(
                "  {} granted={} bearing={} drift={} cap={}{} remaining={}",
                s(row, "target_sk"),
                usd(i(row, "pool_granted_microusd")),
                usd(i(row, "capacity_bearing_sum_microusd")),
                usd(i(row, "drift_microusd")),
                usd(i(row, "effective_grant_cap_microusd")),
                if row
                    .get("cap_is_derived")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false)
                {
                    " (derived from the baseline)"
                } else {
                    " (set by hand)"
                },
                usd(i(row, "remaining_cap_microusd")),
            );
        }
        for orph in rec
            .get("orphans")
            .and_then(|v| v.as_array())
            .unwrap_or(&empty2)
        {
            println!(
                "  ORPHAN {} bears capacity and its pool row is missing",
                s(orph, "target_sk")
            );
        }
    }
    Ok(())
}

/// End a live grant early, giving its capacity back now.
///
/// `--tenant` is required because a grant row is partitioned by tenant and a point
/// write needs its partition. It is not what the authority is checked against: the
/// backend binds that to the tenant it reads from the grant row itself.
pub async fn grant_revoke(
    scope: &str,
    tenant_id: &str,
    grant_id: &str,
    reason: Option<&str>,
) -> Result<()> {
    let mut body = json!({});
    if let Some(r) = reason {
        body["reason"] = Value::String(r.to_string());
    }
    let client = ApiClient::new()?;
    let path = format!(
        "/api/mvp/{}/limit-grants/{grant_id}/revoke?tenant_id={tenant_id}",
        base(scope)
    );
    let res: Value = client.post_json(&path, &body).await?;
    println!("[OK] Revoked; the capacity is back on the tenant's ceiling");
    print_grant(&res);
    Ok(())
}
