//! Self usage subcommand (`stratoclave usage show`).

use anyhow::Result;
use serde_json::Value;

use super::api::ApiClient;

pub async fn show(since_days: u32, history_limit: u32) -> Result<()> {
    let client = ApiClient::new()?;

    let summary: Value = client
        .get_json(&format!("/api/mvp/me/usage-summary?since_days={since_days}"))
        .await?;
    println!("=== Summary (last {since_days} days) ===");
    println!(
        "  tenant:          {}",
        summary.get("tenant_id").and_then(|v| v.as_str()).unwrap_or("")
    );
    println!(
        "  total_credit:    {}",
        summary.get("total_credit").and_then(|v| v.as_i64()).unwrap_or(0)
    );
    println!(
        "  credit_used:     {}",
        summary.get("credit_used").and_then(|v| v.as_i64()).unwrap_or(0)
    );
    println!(
        "  remaining:       {}",
        summary.get("remaining_credit").and_then(|v| v.as_i64()).unwrap_or(0)
    );
    println!(
        "  sample_size:     {}",
        summary.get("sample_size").and_then(|v| v.as_i64()).unwrap_or(0)
    );
    // P0-11: only surface the fallback line when there's something to report.
    let fallback_count = summary.get("fallback_count").and_then(|v| v.as_i64()).unwrap_or(0);
    if fallback_count > 0 {
        println!("  fallbacks:       {fallback_count} request(s) served by a fallback model");
    }
    if let Some(map) = summary.get("by_model").and_then(|v| v.as_object()) {
        println!("  by_model:");
        for (k, v) in map {
            println!("    {k}: {}", v.as_i64().unwrap_or(0));
        }
    }
    if let Some(map) = summary.get("by_tenant").and_then(|v| v.as_object()) {
        println!("  by_tenant:");
        for (k, v) in map {
            println!("    {k}: {}", v.as_i64().unwrap_or(0));
        }
    }

    let hist: Value = client
        .get_json(&format!(
            "/api/mvp/me/usage-history?since_days={since_days}&limit={history_limit}"
        ))
        .await?;
    let entries = hist.get("history").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    println!("\n=== Recent {} entries ===", entries.len());
    println!(
        "{:<28} {:<35} {:<40} {:>8} {:>8}",
        "recorded_at", "tenant_name", "model (effective)", "input", "output"
    );
    for e in &entries {
        // P0-11: when the request cascaded to a fallback, show the effective
        // model with a "⇐ requested" suffix so the substitution is visible.
        // fallback_occurred is three-valued: Some(true)=fallback, Some(false)/
        // null(legacy)=render plainly.
        let effective = e.get("model_id").and_then(|v| v.as_str()).unwrap_or("");
        let model_col = if e.get("fallback_occurred").and_then(|v| v.as_bool()) == Some(true) {
            let requested = e.get("requested_model_id").and_then(|v| v.as_str()).unwrap_or("?");
            format!("{effective} ⇐ {requested}")
        } else {
            effective.to_string()
        };
        println!(
            "{:<28} {:<35} {:<40} {:>8} {:>8}",
            e.get("recorded_at").and_then(|v| v.as_str()).unwrap_or(""),
            e.get("tenant_name").and_then(|v| v.as_str()).unwrap_or(""),
            model_col,
            e.get("input_tokens").and_then(|v| v.as_i64()).unwrap_or(0),
            e.get("output_tokens").and_then(|v| v.as_i64()).unwrap_or(0),
        );
    }
    Ok(())
}
