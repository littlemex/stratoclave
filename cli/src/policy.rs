//! Enterprise Policy Module
//!
//! Build-time embedded policy settings (build.rs で生成される `OUT_DIR/policy.rs` を取り込む).
//! Phase 2 (v2.1) 時点では一部フィールドのみ参照されるため、dead_code 警告を全体で抑制する。
#![allow(dead_code)]

include!(concat!(env!("OUT_DIR"), "/policy.rs"));
