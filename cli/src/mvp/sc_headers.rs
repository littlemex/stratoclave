//! Shared validation + carrier type for the `x-sc-*` attribution/pin
//! headers injected by the `claude` and `codex` wrapper subcommands.
//!
//! Backend contract (verified in code — keep in exact sync):
//!
//!   x-sc-group-id         \A[A-Za-z0-9._:-]{1,64}\Z    (empty ≡ absent)
//!   x-sc-workflow-run-id  \A[A-Za-z0-9._:-]{1,64}\Z    (empty ≡ absent)
//!   x-sc-model-pin        \A[A-Za-z0-9._:/-]{1,128}\Z  (empty ≡ absent)
//!   x-sc-task-tag         \A[A-Za-z0-9._:-]{1,64}\Z    (empty ≡ absent)
//!
//! We mirror the grammars here and fail *before* spawning the child: a bad
//! value never reaches the network, and — the security crux — a value
//! containing `\n`/`\r` can never be smuggled into ANTHROPIC_CUSTOM_HEADERS
//! (header splitting) or the generated codex config.toml (TOML injection).
//! The grammars are strict whitelists that exclude every control char, `"`,
//! `\`, and whitespace, so validated values are safe to emit verbatim
//! into both formats.
//!
//! What the BACKEND does with a malformed value differs by header, and this
//! module is stricter than all of them on purpose. The first three draw an
//! HTTP 400. `x-sc-task-tag` never draws one: the gateway records the
//! reserved sentinel and reports the drop on a response header. Refusing it
//! here anyway is what keeps the injection argument above true of every
//! header rather than of three of them — and a person who mistyped a label
//! is better served by a refusal than by billable work filed as unlabelled.
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
pub const H_TASK_TAG: &str = "x-sc-task-tag";
/// RESPONSE header: set by the gateway only when it discarded the tag we sent, carrying
/// `"reserved"` or `"grammar"`. Declared beside the request header it answers, so the pair
/// cannot drift apart in two files.
pub const H_TASK_TAG_DROPPED: &str = "x-sc-task-tag-dropped";

// Env-var fallback for the pipe/chat inference paths, which have no flag
// surface. Same values, same grammar, same validation as the wrapper flags.
//
// STRATOCLAVE_TASK_TAG is additionally read on the WRAPPER path, where the
// other three are flags-only — see `resolve_task_tag_for_wrapper` for why the
// tag gets that and they deliberately do not.
pub const ENV_GROUP_ID: &str = "STRATOCLAVE_GROUP_ID";
pub const ENV_WORKFLOW_RUN_ID: &str = "STRATOCLAVE_WORKFLOW_RUN_ID";
pub const ENV_MODEL_PIN: &str = "STRATOCLAVE_MODEL_PIN";
pub const ENV_TASK_TAG: &str = "STRATOCLAVE_TASK_TAG";

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

/// Read an env var, distinguishing absent from set-but-non-UTF-8.
///
/// `std::env::var(..).ok()` collapses those two into `None`, which is the silent drop the
/// fail-loud contract forbids: a set-but-invalid-UTF-8 value would vanish and the request
/// would be attributed to nobody. Hoisted out of `from_env` because the wrapper path now
/// reads one of these vars too, and the rule has to be the same in both places rather than
/// implemented twice.
fn env_utf8(key: &str) -> Result<Option<String>> {
    match std::env::var_os(key) {
        None => Ok(None),
        Some(os) => os
            .into_string()
            .map(Some)
            .map_err(|_| anyhow::anyhow!("env var {}: value is not valid UTF-8", key)),
    }
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

/// Validate against the shared id grammar, for `x-sc-task-tag`.
///
/// The tag's grammar IS the id grammar, so this is `validate_id` under a name that says
/// which header it is checking. It is a separate function on purpose: the tag is the one
/// value here a human types, and a future reader looking for "where is the tag checked"
/// should find something rather than an id validator called with a different flag name.
///
/// **This validates the GRAMMAR and nothing else.** The gateway owns canonicalisation
/// (NFKC then case-fold) and owns the reserved word: `Migration-42` is sent verbatim and
/// recorded as `migration-42`, and `UNLABELLED` passes here and is dropped there as
/// reserved. A second canonicaliser, or a second copy of the reserved list, would be two
/// places deciding what a tag means.
///
/// **Why this is STRICTER than the gateway, deliberately.** The gateway never refuses a
/// malformed tag — it records a sentinel and says so in a response header. This fails
/// before the child is spawned, for the reason in the module docstring: a validated value
/// is emitted verbatim into `ANTHROPIC_CUSTOM_HEADERS` and into a generated `config.toml`,
/// so a value carrying `\n` or `"` is an injection vector here rather than a mislabelled
/// usage row. The gateway can afford to drop one because by then the bytes are contained;
/// this cannot, because it is the thing doing the emitting. A malformed tag is also failed
/// user intent — running an untagged billable request instead is worse than refusing.
pub fn validate_task_tag(flag: &str, value: &str) -> Result<()> {
    validate(flag, value, ID_MAX, is_id_byte, ID_GRAMMAR)
}

/// Warn on stderr when the gateway discarded the tag this invocation asked for.
///
/// Only `pipe` and `chat` can call this: they make the request themselves and can read the
/// response. The `claude`/`codex` wrappers hand the connection to a child process and never
/// see a response at all, so **a tag dropped under a wrapper is silent by construction** —
/// which is why the wrapper's `--help` says `UNLABELLED` is reserved and dropped rather than
/// relying on a runtime message that cannot arrive.
///
/// From this client the only reachable reason is `reserved`, and it is **deterministic**: a
/// tag whose canonical form is the sentinel is dropped on every request, forever, so the
/// first warning is the whole story and the work will be recorded as unlabelled until the
/// caller picks another name. Said plainly, because the alternative reading — a transient
/// gateway hiccup worth retrying — is wrong and expensive.
pub fn warn_if_task_tag_dropped(headers: &ScHeaders, dropped: Option<&str>) {
    let Some(reason) = dropped else { return };
    // `sent` is what we asked for; the gateway does not echo a tag it discarded, so there is
    // nothing to compare against and no need to print the gateway's own copy.
    let sent = headers.task_tag().unwrap_or("<none>");
    match reason {
        "reserved" => eprintln!(
            "[WARN] x-sc-task-tag={sent} was DROPPED: it canonicalises onto the reserved \
             sentinel, so this request is recorded as unlabelled and will not appear under \
             that tag in usage aggregation. Every request using this tag is dropped the same \
             way — choose a different name rather than retrying."
        ),
        other => eprintln!(
            "[WARN] x-sc-task-tag={sent} was DROPPED by the gateway (reason: {other}). This \
             request is recorded as unlabelled. A grammar reason here is unexpected — this \
             client validates the same grammar before sending — and means the CLI and the \
             gateway disagree about what a tag may contain."
        ),
    }
}

/// Resolve the task tag for the wrapper subcommands from the flag and `STRATOCLAVE_TASK_TAG`.
///
/// **Why the tag gets an env fallback on this path when the other three do not.** The other
/// three are flags-only for `claude`/`codex` and env-only for `pipe`/`chat`, and this does not
/// change that. The tag is different in use: a wrapper session is long-lived and interactive,
/// and the tag names the piece of work the whole session belongs to, so it wants to be set
/// once for a shell rather than retyped per invocation. Extending the same fallback to
/// `--group-id` would be a **behaviour change for existing users** — a variable the wrapper
/// ignores today would start being sent, silently relabelling requests that are currently
/// unlabelled. A brand-new header can carry the ergonomics because there is no existing
/// expectation to break. Deliberately not generalised.
///
/// **Disagreement is refused, agreement is accepted.** If both are set and differ, neither
/// precedence is defensible: silently preferring the flag means an exported variable a person
/// forgot is quietly ignored, and preferring the env means the flag they just typed is. Both
/// end with billable work filed under a label nobody chose. Equal values are accepted rather
/// than refused, because `--task-tag "$STRATOCLAVE_TASK_TAG"` in a script is not a mistake.
///
/// The error names the two sources and does **not** print either value: this runs in CI, whose
/// logs capture stderr, and the tag is documented as a non-secret operational label rather
/// than guaranteed to be one. Naming the sources is what makes it actionable — the fix is to
/// unset one, and that needs no value to carry out.
pub fn resolve_task_tag_for_wrapper(flag: Option<String>) -> Result<Option<String>> {
    reconcile_task_tag(flag, env_utf8(ENV_TASK_TAG)?)
}

/// The agreement rule itself, pure. Split out from `resolve_task_tag_for_wrapper` so it can be
/// tested without mutating process-global env, which is order-dependent under a threaded test
/// runner and would make this rule's coverage depend on which test ran first.
fn reconcile_task_tag(flag: Option<String>, from_env: Option<String>) -> Result<Option<String>> {
    match (flag, from_env) {
        (Some(f), Some(e)) if f == e => Ok(Some(f)),
        (Some(_), Some(_)) => bail!(
            "--task-tag and {ENV_TASK_TAG} are both set and disagree. Unset one: \
             the request would otherwise be billed under a label neither source chose. \
             (Values are not printed — this often runs in CI, and the tag is an \
             operational label rather than a guaranteed-public string.)"
        ),
        (Some(f), None) | (None, Some(f)) => Ok(Some(f)),
        (None, None) => Ok(None),
    }
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
    task_tag: Option<String>,
}

impl ScHeaders {
    pub fn validated(
        group_id: Option<String>,
        workflow_run_id: Option<String>,
        model_pin: Option<String>,
        task_tag: Option<String>,
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
        if let Some(v) = &task_tag {
            validate_task_tag("task-tag", v)?;
        }
        Ok(Self {
            group_id,
            workflow_run_id,
            model_pin,
            task_tag,
        })
    }

    /// Read the STRATOCLAVE_* attribution env vars through the SAME validating
    /// constructor the `claude`/`codex` wrapper flags use, so pipe/chat and the
    /// wrappers can't disagree on grammar. Used by pipe/chat, which have no
    /// argv surface for flags (bare `echo | stratoclave`). Unset = absent;
    /// set-but-empty = error (mirrors the empty-flag deviation: an unset shell
    /// variable, e.g. STRATOCLAVE_GROUP_ID="$UNSET", must fail loudly rather
    /// than silently drop attribution).
    pub fn from_env() -> Result<Self> {
        // `env_utf8` carries the reason a set-but-non-UTF-8 value must fail rather
        // than read as absent.
        // Resolve each var to Option<String> (propagating a non-UTF-8 error),
        // then hand to the shared validating constructor.
        let get = env_utf8;
        Self::validated(
            get(ENV_GROUP_ID)?,
            get(ENV_WORKFLOW_RUN_ID)?,
            get(ENV_MODEL_PIN)?,
            get(ENV_TASK_TAG)?,
        )
    }

    /// The wrapper subcommands' constructor: flags for three headers, flag-or-env for the
    /// task tag, then the same validation as every other path.
    ///
    /// Exists so `claude` and `codex` hold no resolution logic of their own. Two copies of
    /// "resolve the tag, then validate" is two places for the order to drift, and resolving
    /// after validation would validate a value that is then replaced by the env's.
    pub fn for_wrapper(
        group_id: Option<String>,
        workflow_run_id: Option<String>,
        model_pin: Option<String>,
        task_tag_flag: Option<String>,
    ) -> Result<Self> {
        Self::validated(
            group_id,
            workflow_run_id,
            model_pin,
            resolve_task_tag_for_wrapper(task_tag_flag)?,
        )
    }

    /// `from_env` with an injectable lookup — pure and unit-testable without
    /// mutating process-global env. (Tests use this; the real env path is
    /// `from_env`, which additionally rejects a set-but-non-UTF-8 value.)
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Self> {
        // validated() already rejects Some("") — no extra empty handling here.
        Self::validated(
            get(ENV_GROUP_ID),
            get(ENV_WORKFLOW_RUN_ID),
            get(ENV_MODEL_PIN),
            get(ENV_TASK_TAG),
        )
    }

    /// All-absent instance (tests, callers with no flags).
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn none() -> Self {
        Self::default()
    }

    pub fn is_empty(&self) -> bool {
        self.group_id.is_none()
            && self.workflow_run_id.is_none()
            && self.model_pin.is_none()
            && self.task_tag.is_none()
    }

    /// The validated task tag, if set.
    pub fn task_tag(&self) -> Option<&str> {
        self.task_tag.as_deref()
    }

    /// The validated model pin, if set (for the pipe/chat INFO line noting a
    /// server-side pin overrides the configured model).
    pub fn model_pin(&self) -> Option<&str> {
        self.model_pin.as_deref()
    }

    /// `(header-name, validated-value)` pairs for the present headers, in a
    /// fixed order. Single source of truth for both emitters.
    pub fn iter(&self) -> impl Iterator<Item = (&'static str, &str)> {
        [
            (H_GROUP_ID, self.group_id.as_deref()),
            (H_WORKFLOW_RUN_ID, self.workflow_run_id.as_deref()),
            (H_MODEL_PIN, self.model_pin.as_deref()),
            (H_TASK_TAG, self.task_tag.as_deref()),
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
        assert!(ScHeaders::validated(Some(String::new()), None, None, None).is_err());
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
        let h = ScHeaders::validated(None, None, None, None).unwrap();
        assert!(h.is_empty());
        assert_eq!(h.iter().count(), 0);
    }

    #[test]
    fn unit_iter_order_and_presence() {
        let h = ScHeaders::validated(Some("g".into()), None, Some("p".into()), None).unwrap();
        let got: Vec<_> = h.iter().collect();
        assert_eq!(got, vec![(H_GROUP_ID, "g"), (H_MODEL_PIN, "p")]);
    }

    /// A tag is diagnosed by CHARACTER before LENGTH. A 65-char tag made of multi-byte
    /// characters is wrong for both reasons, and the length message would be actively
    /// misleading — it would quote a byte count the user cannot see in their own string.
    #[test]
    fn task_tag_diagnoses_character_before_length() {
        let e = validate_task_tag("task-tag", &"あ".repeat(65))
            .expect_err("must reject")
            .to_string();
        assert!(e.contains("disallowed character"), "{e}");
        assert!(
            !e.contains("characters long"),
            "length must not be the diagnosis: {e}"
        );
    }

    /// The tag shares the id grammar's 64-char bound, checked at the boundary from both
    /// sides so an off-by-one in either direction fails.
    #[test]
    fn task_tag_length_boundary() {
        assert!(validate_task_tag("task-tag", &"a".repeat(64)).is_ok());
        let e = validate_task_tag("task-tag", &"a".repeat(65))
            .expect_err("65 must be rejected")
            .to_string();
        assert!(e.contains("at most 64"), "{e}");
    }

    /// Reserved and mixed-case values pass the CLI. The gateway owns both meanings; a second
    /// copy of either rule here is the defect the module docstring names.
    #[test]
    fn task_tag_validation_is_grammar_only() {
        for v in ["UNLABELLED", "unlabelled", "Migration-42", "a", "A.B_c:d-e"] {
            assert!(
                validate_task_tag("task-tag", v).is_ok(),
                "must accept {v:?}"
            );
        }
    }

    /// The resolver's whole contract, without touching process-global env: equal is accepted,
    /// disagreement is refused, either alone wins, neither is absent.
    ///
    /// Driven through a pure helper rather than `resolve_task_tag_for_wrapper` because that
    /// reads the real environment, and a test that mutates process env is order-dependent
    /// under a threaded runner. The helper IS the rule; the wrapper only supplies the env
    /// side of it.
    #[test]
    fn task_tag_flag_env_agreement_rule() {
        let r = |f: Option<&str>, e: Option<&str>| {
            reconcile_task_tag(f.map(str::to_owned), e.map(str::to_owned))
        };
        assert_eq!(r(None, None).unwrap(), None);
        assert_eq!(r(Some("a"), None).unwrap(), Some("a".to_string()));
        assert_eq!(r(None, Some("a")).unwrap(), Some("a".to_string()));
        assert_eq!(r(Some("a"), Some("a")).unwrap(), Some("a".to_string()));

        let e = r(Some("a"), Some("b"))
            .expect_err("disagreement must refuse")
            .to_string();
        assert!(e.contains("disagree"), "{e}");
        // The values must not appear: stderr is captured by CI logs.
        assert!(
            !e.contains("\"a\"") && !e.contains("\"b\""),
            "values leaked: {e}"
        );
    }

    #[test]
    fn from_lookup_env_mapping() {
        // Unset -> absent.
        assert!(ScHeaders::from_lookup(|_| None).unwrap().is_empty());
        // Set-but-empty -> error (an unset shell var must fail loudly).
        assert!(ScHeaders::from_lookup(|k| (k == ENV_GROUP_ID).then(String::new)).is_err());
        // Malformed -> error (fails the shared grammar, before any network).
        assert!(
            ScHeaders::from_lookup(|k| (k == ENV_GROUP_ID).then(|| "has space".to_string()))
                .is_err()
        );
        // A full valid set maps to the right headers via the env-var names.
        let h = ScHeaders::from_lookup(|k| match k {
            ENV_GROUP_ID => Some("team-a".to_string()),
            ENV_WORKFLOW_RUN_ID => Some("wr-1".to_string()),
            ENV_MODEL_PIN => Some("claude-sonnet-4-6".to_string()),
            _ => None,
        })
        .unwrap();
        assert_eq!(
            h.iter().collect::<Vec<_>>(),
            vec![
                (H_GROUP_ID, "team-a"),
                (H_WORKFLOW_RUN_ID, "wr-1"),
                (H_MODEL_PIN, "claude-sonnet-4-6"),
            ]
        );
        assert_eq!(h.model_pin(), Some("claude-sonnet-4-6"));
    }
}
