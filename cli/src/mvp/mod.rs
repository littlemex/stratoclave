//! Phase 2: Stratoclave CLI subcommands (clap derive).
//!
//! `stratoclave auth login / logout / whoami`
//! `stratoclave claude -- [args]`
//! `stratoclave usage show`
//! `stratoclave admin user|tenant|usage ...`
//! `stratoclave team-lead tenant ...`

pub mod admin;
pub mod admin_cmd; // 旧 single-shot create; Phase 2 では使わないが削除はせず維持
pub mod api;
pub mod api_keys;
pub mod auth;
pub mod claude_cmd;
pub mod config;
pub mod sso;
pub mod team_lead;
pub mod tokens;
pub mod usage;
