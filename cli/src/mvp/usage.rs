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
        "recorded_at", "tenant_name", "model_id", "input", "output"
    );
    for e in &entries {
        println!(
            "{:<28} {:<35} {:<40} {:>8} {:>8}",
            e.get("recorded_at").and_then(|v| v.as_str()).unwrap_or(""),
            e.get("tenant_name").and_then(|v| v.as_str()).unwrap_or(""),
            e.get("model_id").and_then(|v| v.as_str()).unwrap_or(""),
            e.get("input_tokens").and_then(|v| v.as_i64()).unwrap_or(0),
            e.get("output_tokens").and_then(|v| v.as_i64()).unwrap_or(0),
        );
    }
    Ok(())
}
