// Phase 2 (v2.1): only ui / pipe / chat / setup are referenced from main.rs.
// The old admin / auth / messages / sessions / teams modules were removed on
// 2026-04-25 and migrated under mvp/.
pub mod chat;
pub mod pipe;
pub mod setup;
pub mod ui;
