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

// ===========================================================================
// External authorize / capture / void / get (P0 authcap)
// ===========================================================================
//
// A reference client for the external billing API AND our own E2E test tool.
// The typed structs (deny_unknown_fields) are the CLI half of the authcap
// contract: a shape change on the backend breaks these deserializes.

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AuthorizeResponse {
    pub authorization_id: String,
    pub amount_microusd: i64,
    pub expires_at_epoch: i64,
    pub status: String,
    /// True when a duplicate Idempotency-Key replayed the original (no new hold).
    #[serde(default)]
    pub replayed: bool,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CaptureResponse {
    pub authorization_id: String,
    pub captured_microusd: i64,
    pub terminal: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct VoidResponse {
    pub authorization_id: String,
    pub terminal: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AuthorizationStatus {
    pub authorization_id: String,
    pub tenant_id: String,
    pub amount_microusd: i64,
    pub status: String,
    #[serde(default)]
    pub terminal: Option<String>,
    #[serde(default)]
    pub captured_microusd: Option<i64>,
}

/// A random Idempotency-Key when the caller does not supply one, so a naive
/// `billing authorize` still gets exactly-once semantics per invocation. A
/// caller that wants a retry to dedupe passes `--idempotency-key`.
fn random_idempotency_key() -> String {
    use rand::RngExt;
    let mut rng = rand::rng();
    let hex: String = (0..32)
        .map(|_| format!("{:x}", rng.random_range(0..16)))
        .collect();
    format!("cli-{hex}")
}

/// `stratoclave billing authorize --amount <microusd> [--ttl] [--desc] [--idempotency-key]`
pub async fn authorize(
    amount_microusd: i64,
    ttl_seconds: Option<i64>,
    description: Option<String>,
    idempotency_key: Option<String>,
    workflow_run_id: Option<String>,
    json: bool,
) -> Result<()> {
    let client = ApiClient::new()?;
    let key = idempotency_key.unwrap_or_else(random_idempotency_key);
    let mut body = serde_json::json!({ "amount_microusd": amount_microusd });
    if let Some(t) = ttl_seconds {
        body["ttl_seconds"] = t.into();
    }
    if let Some(d) = description {
        body["description"] = d.into();
    }
    if let Some(w) = workflow_run_id {
        body["workflow_run_id"] = w.into();
    }
    let r: AuthorizeResponse = client
        .post_json_with_headers(
            "/api/mvp/billing/authorize",
            &body,
            &[("Idempotency-Key", &key)],
        )
        .await?;
    if json {
        println!("{}", serde_json::to_string_pretty(&r)?);
        return Ok(());
    }
    println!("=== Authorization {} ===", r.authorization_id);
    println!("  status:     {}", r.status);
    if r.replayed {
        println!("  (replayed — duplicate Idempotency-Key, no new hold)");
    }
    println!("  amount:     {}", fmt_usd(r.amount_microusd));
    println!("  expires_at: {} (epoch)", r.expires_at_epoch);
    println!("  idempotency-key: {key}");
    Ok(())
}

/// `stratoclave billing capture <authorization_id> --actual <microusd>`
pub async fn capture(authorization_id: &str, actual_microusd: i64, json: bool) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!(
        "/api/mvp/billing/authorizations/{}/capture",
        urlencode(authorization_id)
    );
    let body = serde_json::json!({ "actual_amount_microusd": actual_microusd });
    let r: CaptureResponse = client.post_json(&path, &body).await?;
    if json {
        println!("{}", serde_json::to_string_pretty(&r)?);
        return Ok(());
    }
    println!("=== Captured {} ===", r.authorization_id);
    println!("  captured: {}", fmt_usd(r.captured_microusd));
    println!("  terminal: {}", r.terminal);
    Ok(())
}

/// `stratoclave billing void <authorization_id>`
pub async fn void(authorization_id: &str, json: bool) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!(
        "/api/mvp/billing/authorizations/{}/void",
        urlencode(authorization_id)
    );
    let r: VoidResponse = client.post_json(&path, &serde_json::json!({})).await?;
    if json {
        println!("{}", serde_json::to_string_pretty(&r)?);
        return Ok(());
    }
    println!(
        "=== Voided {} (terminal {}) ===",
        r.authorization_id, r.terminal
    );
    Ok(())
}

/// `stratoclave billing get <authorization_id>`
pub async fn get(authorization_id: &str, json: bool) -> Result<()> {
    let client = ApiClient::new()?;
    let path = format!(
        "/api/mvp/billing/authorizations/{}",
        urlencode(authorization_id)
    );
    let r: AuthorizationStatus = client.get_json(&path).await?;
    if json {
        println!("{}", serde_json::to_string_pretty(&r)?);
        return Ok(());
    }
    println!(
        "=== Authorization {} (tenant {}) ===",
        r.authorization_id, r.tenant_id
    );
    println!("  status:  {}", r.status);
    println!("  amount:  {}", fmt_usd(r.amount_microusd));
    if let Some(t) = &r.terminal {
        println!("  terminal: {t}");
    }
    if let Some(c) = r.captured_microusd {
        println!("  captured: {}", fmt_usd(c));
    }
    Ok(())
}

/// Minimal path-segment encoding for the ids we allow ([A-Za-z0-9._:-]); anything
/// else is percent-encoded so a crafted run_id can't inject query/path syntax.
/// The `auth_...` token uses urlsafe-base64 (`A-Za-z0-9-_` + `=`), all allowed
/// here except `=` which base64url of our payload never emits at the tail we use.
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

    // ---- authcap contract gate ----

    const AUTHZ_STATUS_FIXTURE: &str =
        include_str!("../../../contracts/billing/authorization_status.json");

    #[test]
    fn authorization_status_fixture_deserializes() {
        let s: AuthorizationStatus = serde_json::from_str(AUTHZ_STATUS_FIXTURE).unwrap();
        assert_eq!(s.status, "captured");
        assert_eq!(s.terminal.as_deref(), Some("SETTLE"));
        assert_eq!(s.captured_microusd, Some(700_000));
        assert!(s.authorization_id.starts_with("auth_"));
    }

    #[test]
    fn authorize_response_shape() {
        let r: AuthorizeResponse = serde_json::from_str(
            r#"{"authorization_id":"auth_x","amount_microusd":500000,"expires_at_epoch":123,"status":"authorized"}"#,
        )
        .unwrap();
        assert_eq!(r.status, "authorized");
        assert_eq!(r.amount_microusd, 500_000);
    }

    #[test]
    fn capture_response_shape() {
        let r: CaptureResponse = serde_json::from_str(
            r#"{"authorization_id":"auth_x","captured_microusd":700000,"terminal":"SETTLE"}"#,
        )
        .unwrap();
        assert_eq!(r.terminal, "SETTLE");
        assert_eq!(r.captured_microusd, 700_000);
    }

    #[test]
    fn authorization_status_rejects_unknown_field() {
        // deny_unknown_fields: an unexpected key (e.g. a leaked cost) fails the
        // deserialize at the client boundary, same drift gate as the run views.
        let leaked = r#"{"authorization_id":"auth_x","tenant_id":"t","amount_microusd":1,
            "status":"authorized","provider_cost_microusd":9}"#;
        let res: Result<AuthorizationStatus, _> = serde_json::from_str(leaked);
        assert!(res.is_err());
    }

    // ---- PENDING protocol: replayed=true / new status values ----
    //
    // The PENDING protocol (docs/design/pending-protocol.md) can now answer an
    // idempotent duplicate authorize with `replayed: true` (mvp/_pipeline.py
    // `_pending_replay_result` / `ExternalAuthorizeResult(replayed=True)`), and
    // `GET /authorizations/{id}` can report `status` of "authorized",
    // "captured", "voided", or "expired" (mvp/billing_authorize.py
    // `get_authorization`). These structs are `deny_unknown_fields`, so any
    // response shape drift on these new paths breaks the CLI build/test the
    // same way the existing cross-layer contract gate does.

    #[test]
    fn authorize_response_replayed_true_shape() {
        // A duplicate Idempotency-Key replay of an authorize
        // (billing_authorize.py: `replayed=result.replayed` from
        // `_pending_replay_result` / `ExternalAuthorizeResult(replayed=True)`).
        let r: AuthorizeResponse = serde_json::from_str(
            r#"{"authorization_id":"auth_x","amount_microusd":500000,
                "expires_at_epoch":123,"status":"authorized","replayed":true}"#,
        )
        .unwrap();
        assert!(r.replayed);
        assert_eq!(r.status, "authorized");
    }

    #[test]
    fn authorize_response_defaults_replayed_false_when_absent() {
        // `#[serde(default)]` on `replayed`: a legacy/omitted field must not
        // fail deserialization and must default to false (a fresh, non-replayed
        // authorize never sends the key at all in some backend versions).
        let r: AuthorizeResponse = serde_json::from_str(
            r#"{"authorization_id":"auth_y","amount_microusd":1,
                "expires_at_epoch":1,"status":"authorized"}"#,
        )
        .unwrap();
        assert!(!r.replayed);
    }

    #[test]
    fn authorization_status_all_status_values_deserialize() {
        // `get_authorization` (billing_authorize.py) reports one of these four
        // `status` strings depending on the terminal read
        // (authorized/captured/voided/expired). None of them are constrained by
        // an enum on the CLI side (status is a plain String), so this pins that
        // all four round-trip through the typed struct without a deny_unknown_fields
        // rejection or a parse failure.
        for (status, extra) in [
            ("authorized", ""),
            ("captured", r#","terminal":"SETTLE","captured_microusd":700000"#),
            ("voided", r#","terminal":"RELEASE""#),
            ("expired", r#","terminal":"RECLAIM""#),
        ] {
            let body = format!(
                r#"{{"authorization_id":"auth_x","tenant_id":"t","amount_microusd":1000000,
                    "status":"{status}"{extra}}}"#
            );
            let s: AuthorizationStatus = serde_json::from_str(&body)
                .unwrap_or_else(|e| panic!("status={status} failed to deserialize: {e}"));
            assert_eq!(s.status, status);
        }
    }

    #[test]
    fn capture_response_shape_unaffected_by_pending_protocol() {
        // Capture's response shape (`CaptureResponse`) is unchanged by the
        // PENDING protocol — the new 402/410/503/409 outcomes are HTTP errors
        // handled by `api::raise_http`, never a 200 body. This test documents
        // that boundary: a successful capture (terminal=SETTLE) still
        // deserializes exactly as before.
        let r: CaptureResponse = serde_json::from_str(
            r#"{"authorization_id":"auth_x","captured_microusd":300000,"terminal":"SETTLE"}"#,
        )
        .unwrap();
        assert_eq!(r.terminal, "SETTLE");
        assert_eq!(r.captured_microusd, 300_000);
    }

    #[test]
    fn void_response_shape_unaffected_by_pending_protocol() {
        let r: VoidResponse = serde_json::from_str(
            r#"{"authorization_id":"auth_x","terminal":"RELEASE"}"#,
        )
        .unwrap();
        assert_eq!(r.terminal, "RELEASE");
    }
}
