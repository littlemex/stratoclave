//! Shared validation + carrier type for the `x-sc-*` attribution/pin
//! headers injected by the `claude` and `codex` wrapper subcommands.
//!
//! Backend contract (verified in code — keep in exact sync):
//!
//!   x-sc-group-id         \A[A-Za-z0-9._:-]{1,64}\Z    (empty ≡ absent)
//!   x-sc-workflow-run-id  \A[A-Za-z0-9._:-]{1,64}\Z    (empty ≡ absent)
//!   x-sc-model-pin        \A[A-Za-z0-9._:/-]{1,128}\Z  (empty ≡ absent)
//!
//! Present-but-malformed => HTTP 400 from the backend, so we mirror the
//! grammar here and fail *before* spawning the child: a bad value never
//! reaches the network, and — the security crux — a value containing
//! `\n`/`\r` can never be smuggled into ANTHROPIC_CUSTOM_HEADERS (header
//! splitting) or the generated codex config.toml (TOML injection). The
//! grammars are strict whitelists that exclude every control char, `"`,
//! `\`, and whitespace, so validated values are safe to emit verbatim
//! into both formats.
//!
//! CLI deviation from the backend, deliberate: the backend treats an
//! *empty* header as absent; we treat an explicitly-passed empty flag as
//! an ERROR, because `--group-id "$GROUP_ID"` with an unset shell variable
//! is the overwhelmingly likely cause and silently dropping the header
//! would corrupt attribution without anyone noticing.
//!
//! `ScHeaders` has private fields and exactly one validating constructor.
//! Downstream code (claude_cmd / codex_cmd) takes `&ScHeaders`, so an
//! unvalidated string cannot reach `.env()` or the temp config by
//! construction.
//!
//! NOTE: these headers are attribution IDs, not secrets. They are visible
//! in `/proc/<pid>/environ` and inherited by every tool subprocess the
//! child spawns — acceptable for IDs, but never route a secret through
//! this channel.

use anyhow::{bail, Result};

pub const H_GROUP_ID: &str = "x-sc-group-id";
pub const H_WORKFLOW_RUN_ID: &str = "x-sc-workflow-run-id";
pub const H_MODEL_PIN: &str = "x-sc-model-pin";

const ID_MAX: usize = 64;
const PIN_MAX: usize = 128;

const ID_GRAMMAR: &str = "[A-Za-z0-9._:-]{1,64}";
const PIN_GRAMMAR: &str = "[A-Za-z0-9._:/-]{1,128}";

// The grammars are pure ASCII, so byte-wise checks are exact: any
// multi-byte UTF-8 char has bytes >= 0x80, which fail the class check,
// and for accepted strings byte-length == char-count, so checking
// `value.len()` (bytes) against the max is equivalent to the regex's
// char-counted `{1,N}`.

#[inline]
fn is_id_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'-')
}

#[inline]
fn is_pin_byte(b: u8) -> bool {
    is_id_byte(b) || b == b'/'
}

fn validate(flag: &str, value: &str, max: usize, ok: fn(u8) -> bool, grammar: &str) -> Result<()> {
    if value.is_empty() {
        bail!(
            "--{flag} was passed an empty value. Omit the flag entirely if you \
             don't want the header. (An empty value usually means an unset \
             shell variable, e.g. --{flag} \"$SOME_VAR\".)"
        );
    }
    // Scan the character class FIRST so a non-ASCII value is diagnosed as a
    // disallowed-character error rather than a misleading "N bytes > max"
    // (a multi-byte char inflates the byte length; the real problem is the
    // char, not the count). Length is checked in chars() to match the
    // backend regex's char-counted {1,N} exactly.
    if let Some(bad) = value.bytes().find(|b| !ok(*b)) {
        bail!(
            "--{flag} contains disallowed character {}; allowed grammar: {grammar}",
            char::from(bad).escape_default()
        );
    }
    // Every byte passed the ASCII class check above, so bytes == chars here
    // and value.len() is the char count.
    if value.len() > max {
        bail!(
            "--{flag} is {} characters long; the backend grammar allows at most {max} ({grammar})",
            value.len()
        );
    }
    Ok(())
}

/// Validate against the shared id grammar `[A-Za-z0-9._:-]{1,64}`
/// (x-sc-group-id, x-sc-workflow-run-id).
pub fn validate_id(flag: &str, value: &str) -> Result<()> {
    validate(flag, value, ID_MAX, is_id_byte, ID_GRAMMAR)
}

/// Validate against the model-pin grammar `[A-Za-z0-9._:/-]{1,128}`
/// (x-sc-model-pin; additionally allows `/`, e.g. inference profiles).
pub fn validate_model_pin(flag: &str, value: &str) -> Result<()> {
    validate(flag, value, PIN_MAX, is_pin_byte, PIN_GRAMMAR)
}

/// Validated carrier for the three optional headers. Fields are private
/// and the only constructor validates, so holding a `ScHeaders` is proof
/// that every contained value matches the backend grammar.
#[derive(Debug, Clone, Default)]
pub struct ScHeaders {
    group_id: Option<String>,
    workflow_run_id: Option<String>,
    model_pin: Option<String>,
}

impl ScHeaders {
    pub fn validated(
        group_id: Option<String>,
        workflow_run_id: Option<String>,
        model_pin: Option<String>,
    ) -> Result<Self> {
        if let Some(v) = &group_id {
            validate_id("group-id", v)?;
        }
        if let Some(v) = &workflow_run_id {
            validate_id("workflow-run-id", v)?;
        }
        if let Some(v) = &model_pin {
            validate_model_pin("model-pin", v)?;
        }
        Ok(Self {
            group_id,
            workflow_run_id,
            model_pin,
        })
    }

    /// All-absent instance (tests, callers with no flags).
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn none() -> Self {
        Self::default()
    }

    pub fn is_empty(&self) -> bool {
        self.group_id.is_none() && self.workflow_run_id.is_none() && self.model_pin.is_none()
    }

    /// `(header-name, validated-value)` pairs for the present headers, in a
    /// fixed order. Single source of truth for both emitters.
    pub fn iter(&self) -> impl Iterator<Item = (&'static str, &str)> {
        [
            (H_GROUP_ID, self.group_id.as_deref()),
            (H_WORKFLOW_RUN_ID, self.workflow_run_id.as_deref()),
            (H_MODEL_PIN, self.model_pin.as_deref()),
        ]
        .into_iter()
        .filter_map(|(k, v)| v.map(|v| (k, v)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Tiny deterministic xorshift64* PRNG so the property tests are
    // dependency-free and reproducible (no reliance on the `rand` version's
    // range-method spelling). Seeded per-test for stable failures.
    struct Rng(u64);
    impl Rng {
        fn new(seed: u64) -> Self {
            // Avoid the zero state (xorshift fixed point).
            Rng(seed | 1)
        }
        fn next_u64(&mut self) -> u64 {
            let mut x = self.0;
            x ^= x >> 12;
            x ^= x << 25;
            x ^= x >> 27;
            self.0 = x;
            x.wrapping_mul(0x2545_F491_4F6C_DD1D)
        }
        /// Uniform-ish index in `0..n` (n > 0). Modulo bias is irrelevant for
        /// test-input generation.
        fn below(&mut self, n: usize) -> usize {
            (self.next_u64() % n as u64) as usize
        }
        /// Inclusive range `0..=max`.
        fn upto(&mut self, max: usize) -> usize {
            self.below(max + 1)
        }
    }

    // ---------------------------------------------------------------
    // Independent oracles: literal transcriptions of the backend
    // regexes, written from the spec (explicit set string + char
    // count), NOT sharing code with the validators.
    // ---------------------------------------------------------------
    const ID_SET: &str =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-";
    const PIN_SET: &str =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/-";

    fn oracle_id(v: &str) -> bool {
        let n = v.chars().count();
        (1..=64).contains(&n) && v.chars().all(|c| ID_SET.contains(c))
    }
    fn oracle_pin(v: &str) -> bool {
        let n = v.chars().count();
        (1..=128).contains(&n) && v.chars().all(|c| PIN_SET.contains(c))
    }

    // Hostile alphabet: allowed chars + every escape vector we care about.
    const HOSTILE: &[char] = &[
        'a', 'Z', '0', '.', '_', ':', '-', '/', // boundary-legal
        '\n', '\r', '\0', '\t', '\x1b', '\x7f', // control / CRLF
        '"', '\\', ' ', ',', ';', '=', '{', '}', '#', // TOML / format chars
        'é', '\u{2028}', '\u{FF0F}', // non-ASCII incl. line-sep, fullwidth /
    ];

    fn gen_string(rng: &mut Rng, max_len: usize) -> String {
        let len = rng.upto(max_len);
        (0..len).map(|_| HOSTILE[rng.below(HOSTILE.len())]).collect()
    }

    // P1: accepts iff backend grammar (randomized).
    #[test]
    fn prop_grammar_equivalence_randomized() {
        let mut rng = Rng::new(0x5c_11ead5);
        for _ in 0..20_000 {
            let s = gen_string(&mut rng, 140);
            assert_eq!(
                validate_id("t", &s).is_ok(),
                oracle_id(&s),
                "id validator/oracle disagree on {s:?}"
            );
            assert_eq!(
                validate_model_pin("t", &s).is_ok(),
                oracle_pin(&s),
                "pin validator/oracle disagree on {s:?}"
            );
        }
    }

    // P1 (exhaustive): every 1-byte and 2-byte ASCII-superset input. The
    // grammar is a Kleene closure of a char class, so this is effectively
    // a proof of the class logic.
    #[test]
    fn prop_grammar_equivalence_exhaustive_short() {
        for b in 0u8..=255 {
            if let Ok(s) = std::str::from_utf8(&[b]).map(str::to_owned) {
                assert_eq!(validate_id("t", &s).is_ok(), oracle_id(&s), "byte {b:#04x}");
                assert_eq!(
                    validate_model_pin("t", &s).is_ok(),
                    oracle_pin(&s),
                    "byte {b:#04x}"
                );
            }
        }
        for b1 in 0u8..=255 {
            for b2 in 0u8..=255 {
                if let Ok(s) = std::str::from_utf8(&[b1, b2]).map(str::to_owned) {
                    assert_eq!(validate_id("t", &s).is_ok(), oracle_id(&s), "{s:?}");
                    assert_eq!(validate_model_pin("t", &s).is_ok(), oracle_pin(&s), "{s:?}");
                }
            }
        }
    }

    // P2: independent of P1 — nothing accepted may contain a byte that
    // could split a header line, escape a TOML basic string, or embed a
    // control char. Trips even if validator AND oracle share a bug.
    #[test]
    fn prop_accepted_values_contain_no_dangerous_bytes() {
        let mut rng = Rng::new(0xdead_beef);
        for _ in 0..20_000 {
            let s = gen_string(&mut rng, 140);
            if validate_model_pin("t", &s).is_ok() || validate_id("t", &s).is_ok() {
                for b in s.bytes() {
                    assert!(
                        b >= 0x20 && b != 0x7f && b != b'"' && b != b'\\' && b != b' ',
                        "dangerous byte {b:#04x} in accepted value {s:?}"
                    );
                }
            }
        }
    }

    // P5 + concrete regressions.
    #[test]
    fn unit_empty_rejected() {
        assert!(validate_id("group-id", "").is_err());
        assert!(validate_model_pin("model-pin", "").is_err());
        assert!(ScHeaders::validated(Some(String::new()), None, None).is_err());
    }

    #[test]
    fn unit_crlf_and_header_splitting_rejected() {
        assert!(validate_id("t", "ok\r\nx-evil: 1").is_err());
        assert!(validate_id("t", "ok\nx-evil: 1").is_err());
        assert!(validate_model_pin("t", "m\r\nx-sc-group-id: hijack").is_err());
        assert!(validate_id("t", "with space").is_err());
    }

    #[test]
    fn unit_valid_accepted() {
        assert!(validate_id("t", "team-alpha_v2.prod:eu").is_ok());
        assert!(validate_id("t", &"a".repeat(64)).is_ok());
        assert!(validate_model_pin("t", &"a".repeat(128)).is_ok());
    }

    #[test]
    fn unit_model_pin_allows_slash_id_does_not() {
        let pin = "arn-ish:inference-profile/anthropic.claude-sonnet-4-5:1";
        assert!(validate_model_pin("t", pin).is_ok());
        assert!(validate_id("t", "a/b").is_err());
    }

    #[test]
    fn unit_over_length_rejected() {
        assert!(validate_id("t", &"a".repeat(65)).is_err());
        assert!(validate_model_pin("t", &"a".repeat(129)).is_err());
    }

    #[test]
    fn unit_none_is_absent() {
        let h = ScHeaders::validated(None, None, None).unwrap();
        assert!(h.is_empty());
        assert_eq!(h.iter().count(), 0);
    }

    #[test]
    fn unit_iter_order_and_presence() {
        let h = ScHeaders::validated(Some("g".into()), None, Some("p".into())).unwrap();
        let got: Vec<_> = h.iter().collect();
        assert_eq!(got, vec![(H_GROUP_ID, "g"), (H_MODEL_PIN, "p")]);
    }
}
