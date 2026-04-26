// Phase 2 (v2.1): main.rs から参照されるのは ui / pipe / chat / setup のみ。
// 旧 admin / auth / messages / sessions / teams は 2026-04-25 撤去済み (mvp/ 配下に移行)。
pub mod chat;
pub mod pipe;
pub mod setup;
pub mod ui;
