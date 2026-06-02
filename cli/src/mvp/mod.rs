//! Phase 2: Stratoclave CLI subcommands (clap derive).
//!
//!   stratoclave auth   { login | logout | whoami }
//!   stratoclave claude   -- [args]
//!   stratoclave codex    -- [args]
//!   stratoclave usage    show
//!   stratoclave admin    user|tenant|usage ...
//!   stratoclave team-lead tenant ...

pub mod admin;
pub mod admin_cmd;       // Legacy single-shot create; preserved but unused in Phase 2.
pub mod api;
pub mod api_keys;
pub mod auth;
pub mod child_launcher;  // Shared spawner used by claude_cmd and codex_cmd.
pub mod claude_cmd;
pub mod codex_cmd;
pub mod config;
pub mod ephemeral_key;   // Scope-parameterized ephemeral sk-stratoclave-* mint/revoke.
pub mod sso;
pub mod team_lead;
pub mod tokens;
pub mod usage;
