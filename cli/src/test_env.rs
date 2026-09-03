//! One lock for the process environment, shared by every test that swaps it.
//!
//! `HOME` and the `STRATOCLAVE_*` variables belong to the process, not to a test, so a test
//! that points `HOME` at a temporary directory changes it for every thread in the binary.
//! This used to be guarded by a mutex in each module that did it -- one in `mvp::tokens`, one
//! in `config` -- and two mutexes cannot serialise one variable. Under load a `config` test
//! would repoint `HOME` while a `tokens` test was mid-flight, the tokens assertion would fail
//! on a file that was no longer under `HOME`, and the panic would poison that module's mutex,
//! so the *next* tokens test failed with `PoisonError` rather than an assertion: two failures,
//! one of them a bystander, and only under load.
//!
//! The lock lives here rather than in either module because the defect was not the missing
//! lock, it was that a module could own one. A third test that swaps the environment would
//! otherwise introduce a third mutex just as quietly as the second one did.
//!
//! Poisoning is recovered from on purpose. A test that panicked while holding this has already
//! failed and said so; letting it take every later environment test down with it buries the
//! one failure that explains the rest.

use std::sync::{Mutex, MutexGuard};

static ENV_LOCK: Mutex<()> = Mutex::new(());

/// Serialise a test that mutates the process environment. Hold the guard for as long as the
/// environment is modified -- returning it alongside a restoring `Drop` guard is the pattern
/// the callers use, so the environment is restored before the lock is released.
pub fn env_lock() -> MutexGuard<'static, ()> {
    ENV_LOCK.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(test)]
mod tests {
    /// There were three of these before this module existed -- `mvp::tokens`, `mvp::config`
    /// and `mvp::codex_home` -- each added by someone who could not see the other two, and
    /// the third arrived exactly as quietly as the second. Moving the lock here does not stop
    /// a fourth; this does. It is a lint rather than a type because a bare `Mutex<()>` is what
    /// the mistake looks like, and nothing in the compiler can tell it from a legitimate one.
    #[test]
    fn the_process_environment_has_exactly_one_lock() {
        fn walk(dir: &std::path::Path, out: &mut Vec<(std::path::PathBuf, usize)>) {
            for entry in std::fs::read_dir(dir).expect("read src") {
                let path = entry.expect("dir entry").path();
                if path.is_dir() {
                    walk(&path, out);
                } else if path.extension().is_some_and(|e| e == "rs") {
                    let text = std::fs::read_to_string(&path).expect("read source");
                    let n = text.matches("Mutex<()>").count();
                    if n > 0 {
                        out.push((path, n));
                    }
                }
            }
        }
        let mut found = Vec::new();
        walk(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src").as_path(), &mut found);
        let offenders: Vec<_> = found
            .iter()
            .filter(|(p, _)| !p.ends_with("test_env.rs"))
            .map(|(p, n)| format!("{} ({n})", p.display()))
            .collect();
        assert!(
            offenders.is_empty(),
            "a second lock for the process environment defeats the first; \
             call crate::test_env::env_lock() instead. Found in: {}",
            offenders.join(", ")
        );
    }
}
