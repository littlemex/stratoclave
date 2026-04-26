//! Output formatting module.
//!
//! Phase 2 (v2.1) 以降の admin / team-lead / usage コマンドは直接 `println!` で整形出力しており、
//! 本モジュールは pipe / chat モードで human/json を出し分けるために残存。
#![allow(dead_code)]

use crate::OutputFormat;

/// Print data to stdout based on output format.
pub fn print_output(data: &serde_json::Value, format: OutputFormat) {
    match format {
        OutputFormat::Human => {
            print_human(data);
        }
        OutputFormat::Json => {
            println!(
                "{}",
                serde_json::to_string_pretty(data).unwrap_or_else(|_| data.to_string())
            );
        }
    }
}

fn print_human(data: &serde_json::Value) {
    match data {
        serde_json::Value::Array(arr) => {
            if arr.is_empty() {
                println!("No items found.");
                return;
            }
            // Print as simple list
            for item in arr {
                if let Some(obj) = item.as_object() {
                    let parts: Vec<String> = obj
                        .iter()
                        .map(|(k, v)| {
                            format!(
                                "{}: {}",
                                k,
                                match v {
                                    serde_json::Value::String(s) => s.clone(),
                                    other => other.to_string(),
                                }
                            )
                        })
                        .collect();
                    println!("{}", parts.join(" | "));
                } else {
                    println!("{}", item);
                }
            }
        }
        serde_json::Value::Object(obj) => {
            for (k, v) in obj {
                println!(
                    "{}: {}",
                    k,
                    match v {
                        serde_json::Value::String(s) => s.clone(),
                        other => other.to_string(),
                    }
                );
            }
        }
        serde_json::Value::String(s) => {
            println!("{}", s);
        }
        other => {
            println!("{}", other);
        }
    }
}
