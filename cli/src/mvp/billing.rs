//! Per-run billing breakdown (`stratoclave billing run show <run_id>`), Layer 5-d.
//!
//! The typed structs are the CLI half of the cross-layer contract gate: the
//! tenant view has NO `provider_cost_microusd` / `margin_microusd` fields and is
//! marked `#[serde(deny_unknown_fields)]`, so if the backend ever regressed and
//! returned cost/margin on the tenant endpoint, `run_show` would FAIL to
//! deserialize — the leak is caught at the client boundary, not silently
//! rendered. The same golden fixtures the backend emits
//! (contracts/billing/run_tenant.json / run_admin.json) are parsed in the unit
//! tests below, so an API shape change breaks CLI + UI + backend together.

use std::collections::BTreeMap;

use anyhow::Result;
use serde::{Deserialize, Serialize};

use super::api::ApiClient;

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RatingComponent {
    pub tokens: i64,
    pub rate_microusd_per_mtok: i64,
    pub cost_microusd: i64,
}

/// TENANT view — deliberately has NO cost/margin fields. `deny_unknown_fields`
/// turns any leaked cost key into a hard deserialize error.
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RunEventTenant {
    pub event_type: String,
    #[serde(default)]
    pub settle_reason: Option<String>,
    #[serde(default)]
    pub model_id: Option<String>,
    #[serde(default)]
    pub pricing_version: Option<String>,
    #[serde(default)]
    pub pricing_key: Option<String>,
    pub settled_microusd: i64,
    pub components: BTreeMap<String, RatingComponent>,
    pub ts_ms: i64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RunBreakdownTenant {
    pub tenant_id: String,
    pub run_id: String,
    pub total_settled_microusd: i64,
    pub events: Vec<RunEventTenant>,
}

/// ADMIN view — adds provider cost + margin (may be negative).
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RunEventAdmin {
    pub event_type: String,
    #[serde(default)]
    pub settle_reason: Option<String>,
    #[serde(default)]
    pub model_id: Option<String>,
    #[serde(default)]
    pub pricing_version: Option<String>,
    #[serde(default)]
    pub pricing_key: Option<String>,
    pub settled_microusd: i64,
    pub components: BTreeMap<String, RatingComponent>,
    pub ts_ms: i64,
    #[serde(default)]
    pub provider_cost_microusd: Option<i64>,
    #[serde(default)]
    pub margin_microusd: Option<i64>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RunBreakdownAdmin {
    pub tenant_id: String,
    pub run_id: String,
    pub total_settled_microusd: i64,
    #[serde(default)]
    pub total_provider_cost_microusd: Option<i64>,
    #[serde(default)]
    pub total_margin_microusd: Option<i64>,
    pub events: Vec<RunEventAdmin>,
}

fn fmt_usd(microusd: i64) -> String {
    // micro-USD -> dollars, sign-preserving.
    let neg = microusd < 0;
    let abs = microusd.unsigned_abs();
    let dollars = abs / 1_000_000;
    let frac = abs % 1_000_000;
    format!("{}${}.{:06}", if neg { "-" } else { "" }, dollars, frac)
}

/// `stratoclave billing run show <run_id>` — the caller's own run (redacted).
pub async fn run_show(run_id: &str, json: bool) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!("/api/mvp/me/billing/runs/{}", urlencode(run_id));
    // Always deserialize into the TYPED tenant struct first (deny_unknown_fields),
    // even for --json: a raw serde_json::Value passthrough would let a leaked
    // cost/margin field print straight through, bypassing the redaction gate
    // (Fable L5-d review M1). Re-serialize the typed value for --json output.
    let b: RunBreakdownTenant = client.get_json(&path).await?;
    if json {
        println!("{}", serde_json::to_string_pretty(&b)?);
        return Ok(());
    }
    println!("=== Billing: run {} (tenant {}) ===", b.run_id, b.tenant_id);
    println!("  total charged: {}", fmt_usd(b.total_settled_microusd));
    for (i, ev) in b.events.iter().enumerate() {
        println!(
            "\n  [{i}] {} ({}) model={} version={} ts={} charged={}",
            ev.event_type,
            ev.settle_reason.as_deref().unwrap_or("-"),
            ev.model_id.as_deref().unwrap_or("-"),
            ev.pricing_version.as_deref().unwrap_or("-"),
            ev.ts_ms,
            fmt_usd(ev.settled_microusd),
        );
        let _ = &ev.pricing_key; // part of the contract; not shown in the table
        for (name, c) in &ev.components {
            if c.tokens == 0 {
                continue;
            }
            println!(
                "      {name}: {} tok @ {}/MTok = {}",
                c.tokens,
                fmt_usd(c.rate_microusd_per_mtok),
                fmt_usd(c.cost_microusd),
            );
        }
    }
    Ok(())
}

/// `stratoclave admin billing run show --tenant <tid> <run_id>` — incl. cost/margin.
pub async fn admin_run_show(tenant: &str, run_id: &str, json: bool) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!(
        "/api/mvp/admin/billing/runs/{}?tenant_id={}",
        urlencode(run_id),
        urlencode(tenant),
    );
    let b: RunBreakdownAdmin = client.get_json(&path).await?;
    if json {
        println!("{}", serde_json::to_string_pretty(&b)?);
        return Ok(());
    }
    println!("=== Billing (admin): run {} (tenant {}) ===", b.run_id, b.tenant_id);
    println!("  total charged:  {}", fmt_usd(b.total_settled_microusd));
    if let Some(pc) = b.total_provider_cost_microusd {
        println!("  provider cost:  {}", fmt_usd(pc));
    }
    if let Some(mg) = b.total_margin_microusd {
        println!("  margin:         {}", fmt_usd(mg));
    }
    for (i, ev) in b.events.iter().enumerate() {
        println!(
            "\n  [{i}] {} ({}) model={} version={} ts={} charged={} cost={} margin={}",
            ev.event_type,
            ev.settle_reason.as_deref().unwrap_or("-"),
            ev.model_id.as_deref().unwrap_or("-"),
            ev.pricing_version.as_deref().unwrap_or("-"),
            ev.ts_ms,
            fmt_usd(ev.settled_microusd),
            ev.provider_cost_microusd.map(fmt_usd).unwrap_or_else(|| "-".into()),
            ev.margin_microusd.map(fmt_usd).unwrap_or_else(|| "-".into()),
        );
        let _ = (&ev.pricing_key, &ev.components); // in contract; not tabulated here
    }
    Ok(())
}

/// Minimal path-segment encoding for the ids we allow ([A-Za-z0-9._:-]); anything
/// else is percent-encoded so a crafted run_id can't inject query/path syntax.
fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'.' | b'_' | b':' | b'-' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    // The golden fixtures the backend emits (contracts/billing/*.json). Compiled
    // into the test binary so a shape change in the backend contract breaks this
    // build/test — the cross-layer drift gate.
    const TENANT_FIXTURE: &str =
        include_str!("../../../contracts/billing/run_tenant.json");
    const ADMIN_FIXTURE: &str =
        include_str!("../../../contracts/billing/run_admin.json");

    #[test]
    fn tenant_fixture_deserializes() {
        let b: RunBreakdownTenant = serde_json::from_str(TENANT_FIXTURE).unwrap();
        assert!(b.total_settled_microusd > 0);
        assert!(!b.events.is_empty());
    }

    #[test]
    fn admin_fixture_deserializes_with_cost_and_margin() {
        let b: RunBreakdownAdmin = serde_json::from_str(ADMIN_FIXTURE).unwrap();
        assert!(b.total_provider_cost_microusd.is_some());
        assert!(b.total_margin_microusd.is_some());
        assert!(b.events[0].provider_cost_microusd.is_some());
    }

    #[test]
    fn tenant_struct_rejects_leaked_cost_field() {
        // NEGATIVE test: if the API leaked provider_cost onto the tenant shape,
        // deny_unknown_fields makes deserialization FAIL — the CLI catches the
        // redaction regression at the boundary.
        let leaked = r#"{
            "tenant_id":"t","run_id":"r","total_settled_microusd":1,
            "events":[{"event_type":"SETTLE","settled_microusd":1,
                "components":{},"ts_ms":0,"provider_cost_microusd":5}]
        }"#;
        let res: Result<RunBreakdownTenant, _> = serde_json::from_str(leaked);
        assert!(res.is_err(), "tenant struct must reject a leaked cost field");
    }

    #[test]
    fn fmt_usd_handles_negative_margin() {
        assert_eq!(fmt_usd(1_500_000), "$1.500000");
        assert_eq!(fmt_usd(-4_000_000), "-$4.000000");
    }
}
