//! Ephemeral `sk-stratoclave-*` key minting / revoking, scope-parameterized.
//!
//! Both the `claude` and `codex` wrapper subcommands need the same
//! "spawn a child process holding a single-purpose API key" pattern, but
//! with different scopes (`messages:send` vs `responses:send`). The
//! per-scope minting was previously inlined in `claude_cmd.rs`; lifting
//! it here keeps the security-critical request shape (ephemeral=true,
//! 30-min TTL, scope subset) authored exactly once.
//!
//! Backend contract (see `backend/mvp/me_api_keys.py`):
//!
//!   POST /api/mvp/me/api-keys
//!     body  { name, scopes:[…], ephemeral:true, expires_in_minutes }
//!     auth  Bearer <Cognito access_token>
//!     200   { key_id, plaintext_key, scopes, expires_at }
//!
//!   GET /api/mvp/me/api-keys/by-key-id/{key_id}
//!     auth  Bearer <Cognito access_token>
//!     200   { key_id, …, last_used_at }  — null when the key never authenticated
//!     404   not the caller's key. A deployment without this route answers 405,
//!           because the same path already accepts DELETE.
//!
//!   DELETE /api/mvp/me/api-keys/by-key-id/{key_id}
//!     auth  Bearer <Cognito access_token>
//!     204   — also accepts 404 (the key already TTL'd or was revoked).
//!
//! The plaintext key is shown exactly once and only ever passed to the
//! child via env (`ANTHROPIC_API_KEY` or `STRATOCLAVE_OPENAI_KEY`); the
//! Cognito bearer is never exported into the child environment.

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};

#[derive(Serialize)]
struct CreateKeyRequest<'a> {
    name: &'a str,
    scopes: &'a [&'a str],
    ephemeral: bool,
    expires_in_minutes: u32,
}

#[derive(Deserialize)]
pub struct CreateKeyResponse {
    pub key_id: String,
    pub plaintext_key: String,
    #[allow(dead_code)]
    pub scopes: Vec<String>,
    #[allow(dead_code)]
    pub expires_at: Option<String>,
}

// Manual Debug that REDACTS the plaintext key (Fable security review M2): the
// derived Debug would print the live key on any stray `{:?}` in error/tracing
// paths. Everything else is safe to show.
impl std::fmt::Debug for CreateKeyResponse {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CreateKeyResponse")
            .field("key_id", &self.key_id)
            .field("plaintext_key", &"<redacted>")
            .field("scopes", &self.scopes)
            .field("expires_at", &self.expires_at)
            .finish()
    }
}

const DEFAULT_TTL_MINUTES: u32 = 30;

fn http_client() -> Result<reqwest::Client> {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()
        .context("Failed to build HTTP client")
}

/// Mint an ephemeral, scope-narrowed API key for a wrapper child process.
///
/// `name` must be unique-ish (e.g. "stratoclave-claude-wrapper") so the
/// audit log can attribute usage. `scopes` must be a subset of the
/// caller's role permissions; `_resolve_scopes` in the backend rejects
/// requests that escalate.
pub async fn mint_ephemeral_key_scoped(
    base_url: &str,
    bearer: &str,
    name: &str,
    scopes: &[&str],
) -> Result<CreateKeyResponse> {
    if scopes.is_empty() {
        return Err(anyhow!(
            "mint_ephemeral_key_scoped called with empty scopes; refusing"
        ));
    }
    let url = format!("{}/api/mvp/me/api-keys", base_url.trim_end_matches('/'));
    let body = CreateKeyRequest {
        name,
        scopes,
        ephemeral: true,
        expires_in_minutes: DEFAULT_TTL_MINUTES,
    };
    let resp = http_client()?
        .post(&url)
        .bearer_auth(bearer)
        .json(&body)
        .send()
        .await
        .context("Failed to POST /api/mvp/me/api-keys for wrapper key")?;
    if !resp.status().is_success() {
        let status = resp.status();
        let err_body = resp.text().await.unwrap_or_default();
        // The wrapper dies before the child starts, so this string is all the
        // user gets. 401 and 403 need different advice: re-authenticating fixes
        // an expired session, and does nothing at all for a missing permission —
        // sending a 403 to `auth login` just loops.
        let detail = summarize_error_body(&err_body);
        if status.as_u16() == 401 {
            return Err(anyhow!(
                "Your Stratoclave session is no longer valid (HTTP 401). Run \
                 `stratoclave auth login` (or `auth sso`) and retry. Server said: {detail}"
            ));
        }
        if status.as_u16() == 403 {
            // Do not assert where the 403 came from: a CDN, a WAF, or an IP
            // allowlist answers 403 without the request ever reaching the
            // application, and telling that user to ask for scopes sends them
            // after the wrong thing. `summarize_error_body` already distinguishes
            // an HTML page from the API's JSON, so name both possibilities and let
            // the body settle it.
            return Err(anyhow!(
                "Stratoclave refused to mint a wrapper key (HTTP 403). If the response \
                 below is the API's JSON, the session is authenticated but not allowed \
                 to create keys with the scopes {scopes:?} — ask an administrator to \
                 grant them (re-authenticating will not help). If it is an HTML page, \
                 the request was rejected before reaching the application. \
                 Server said: {detail}"
            ));
        }
        return Err(anyhow!(
            "Failed to mint ephemeral wrapper key (HTTP {status}): {detail}"
        ));
    }
    resp.json::<CreateKeyResponse>()
        .await
        .context("Failed to parse wrapper-key response")
}

/// Report whether the gateway ever authenticated a request with this key.
///
/// `Ok(true)` means the key carries a `last_used_at` stamp, i.e. at least one
/// request reached Stratoclave. `Ok(false)` means the key exists and was never
/// used. Ambiguity (key missing from the listing, listing failed) is an `Err` so
/// callers can stay silent instead of making a false accusation.
///
/// `ChildLauncher` uses this to catch the silent-bypass case: a child that
/// answers normally while routing around the gateway produces no records, no
/// attribution, and no budget enforcement — and nothing in the session looks
/// wrong, which is exactly what makes it dangerous.
pub async fn key_was_used(base_url: &str, bearer: &str, key_id: &str) -> Result<bool> {
    let base = base_url.trim_end_matches('/');
    // Ask for the one key. Reading the whole listing made the answer depend on
    // this key being on the first page, so the check went quiet for anyone
    // holding many keys — and quietly, which is the worst property a detector can
    // have. The listing below stays as the fallback for a deployment that predates
    // this route: it answers 405, not 404, because the same path already accepts
    // DELETE (measured against a live deployment — treating only 404 as "no such
    // route" made the check fail silently there).
    let by_id = format!(
        "{}/api/mvp/me/api-keys/by-key-id/{}",
        base,
        urlencoding::encode(key_id)
    );
    let client = usage_check_client()?;
    let resp = client
        .get(&by_id)
        .bearer_auth(bearer)
        .send()
        .await
        .context("Failed to GET /api/mvp/me/api-keys/by-key-id for the gateway-usage check")?;
    if resp.status().is_success() {
        let key: serde_json::Value = resp
            .json()
            .await
            .context("Failed to parse the api-key response")?;
        return last_used_of(&key, key_id);
    }
    // A deployment that predates this route answers 405, not 404: the same path
    // already accepts DELETE, so the path exists and only the method does not.
    // Measured against a live deployment — treating 404 alone as "no such route"
    // left the check silently disabled there.
    let route_missing = matches!(resp.status().as_u16(), 404 | 405);
    if !route_missing {
        return Err(anyhow!("api-key lookup returned HTTP {}", resp.status()));
    }
    let url = format!("{}/api/mvp/me/api-keys", base);
    let resp = client
        .get(&url)
        .bearer_auth(bearer)
        .send()
        .await
        .context("Failed to GET /api/mvp/me/api-keys for the gateway-usage check")?;
    if !resp.status().is_success() {
        return Err(anyhow!("api-key listing returned HTTP {}", resp.status()));
    }
    let body: serde_json::Value = resp
        .json()
        .await
        .context("Failed to parse api-key listing")?;
    key_used_from_listing(&body, key_id)
}

/// Seconds allowed for the usage check. Bounded tightly: it sits between child
/// exit and key revocation.
const USAGE_CHECK_TIMEOUT_SECS: u64 = 3;

/// Client for the usage check: it must not stall shutdown or stretch the
/// plaintext key's lifetime, so its timeout is far below the 15 s default.
fn usage_check_client() -> Result<reqwest::Client> {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(USAGE_CHECK_TIMEOUT_SECS))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()
        .context("Failed to build HTTP client for the gateway-usage check")
}

/// Read `last_used_at` off a single api-key object.
///
/// Only an explicit `null` counts as "never used". A missing field is reported as
/// an error, so a backend that stops serializing nulls cannot turn every run into
/// an accusation.
pub(crate) fn last_used_of(key: &serde_json::Value, key_id: &str) -> Result<bool> {
    match key.get("last_used_at") {
        Some(serde_json::Value::Null) => Ok(false),
        Some(_) => Ok(true),
        None => Err(anyhow!(
            "api-key response carried no `last_used_at` for {key_id}; cannot tell \
             whether the gateway saw this session"
        )),
    }
}

/// Decide "was this key used" from an api-key listing.
///
/// Split out from the HTTP call so the ambiguity rules are testable, because they
/// are the whole safety property: only an explicit `last_used_at: null` counts as
/// "never used". A missing `keys` array, a key that is not in the listing, or a
/// response with no `last_used_at` field at all are all reported as errors, so a
/// backend that stops sending the field cannot turn every run into an accusation.
pub(crate) fn key_used_from_listing(body: &serde_json::Value, key_id: &str) -> Result<bool> {
    let keys = body
        .get("keys")
        .and_then(|k| k.as_array())
        .ok_or_else(|| anyhow!("api-key listing had no `keys` array"))?;
    if let Some(key) = keys
        .iter()
        .find(|k| k.get("key_id").and_then(|v| v.as_str()) == Some(key_id))
    {
        return last_used_of(key, key_id);
    }
    // The endpoint returns every key of the caller today. If it ever grows a
    // cursor, a key on a later page would look "never used" unless this case is
    // named — say which of the two it is, because the fixes differ.
    let paged = ["next_cursor", "cursor", "next_token", "next"]
        .iter()
        .any(|k| body.get(*k).map(|v| !v.is_null()).unwrap_or(false));
    if paged {
        return Err(anyhow!(
            "wrapper key {key_id} was not on the first page of the api-key listing, \
             which is now paged; the gateway-usage check needs a by-key-id lookup"
        ));
    }
    Err(anyhow!(
        "wrapper key {key_id} was not in the api-key listing (already revoked or expired?)"
    ))
}

/// Condense a server error body for a one-line CLI message.
///
/// Error bodies are not always the JSON the API documents: a CDN or WAF that
/// rejects the request upstream answers with an HTML page, and pasting that into
/// the terminal buries the actual message.
fn summarize_error_body(body: &str) -> String {
    let trimmed = body.trim();
    if trimmed.is_empty() {
        return "(empty response body)".to_string();
    }
    let looks_like_html = {
        let head = trimmed.get(..64.min(trimmed.len())).unwrap_or(trimmed);
        let lowered = head.to_ascii_lowercase();
        lowered.starts_with("<!doctype html") || lowered.starts_with("<html")
    };
    if looks_like_html {
        return "an HTML error page, not the API's JSON — the request was most likely \
                rejected upstream of the application (CDN or WAF)"
            .to_string();
    }
    let one_line: String = trimmed.split_whitespace().collect::<Vec<_>>().join(" ");
    if one_line.chars().count() > 300 {
        let short: String = one_line.chars().take(300).collect();
        format!("{short}… (truncated)")
    } else {
        one_line
    }
}

/// Revoke an ephemeral key by its `key_id`. Treats HTTP 404 as success
/// because the backend's TTL TTLs the key out independently of this call,
/// and a revoke that races with TTL expiry is the expected steady state.
pub async fn revoke_ephemeral_key(base_url: &str, bearer: &str, key_id: &str) -> Result<()> {
    let url = format!(
        "{}/api/mvp/me/api-keys/by-key-id/{}",
        base_url.trim_end_matches('/'),
        urlencoding::encode(key_id)
    );
    let resp = http_client()?
        .delete(&url)
        .bearer_auth(bearer)
        .send()
        .await
        .context("Failed to DELETE /api/mvp/me/api-keys/by-key-id")?;
    let status = resp.status();
    if !status.is_success() && status.as_u16() != 404 {
        let err_body = resp.text().await.unwrap_or_default();
        return Err(anyhow!(
            "wrapper key revoke returned HTTP {}: {}",
            status,
            err_body
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // The safety property of the detector: only an explicit null means "never
    // used". Everything else the backend might do — drop the field, page the key
    // out of the listing, rename the array — must read as "cannot tell", or a
    // schema change turns every run into a false accusation.
    #[test]
    fn only_explicit_null_last_used_counts_as_unused() {
        let body = json!({"keys": [{"key_id": "k1", "last_used_at": null}]});
        assert_eq!(key_used_from_listing(&body, "k1").unwrap(), false);

        let body = json!({"keys": [{"key_id": "k1", "last_used_at": "2026-08-23T00:00:00Z"}]});
        assert_eq!(key_used_from_listing(&body, "k1").unwrap(), true);
    }

    // A key that has fallen off the first page must not read as "never used". The
    // message has to say which of the two it is, because the remedies differ: a
    // paged listing needs a by-key-id lookup, a missing key is simply gone.
    // The primary path reads one key, so the same ambiguity rules have to hold for
    // a single object, not just for a listing.
    // 404 and 405 both mean "this deployment has no such route": the path already
    // accepts DELETE, so an older backend rejects the method rather than the path.
    // Anything else is a real failure and must not be mistaken for a missing route.
    #[test]
    fn only_404_and_405_mean_the_route_is_absent() {
        for code in [404u16, 405] {
            assert!(matches!(code, 404 | 405), "{code} must fall back to the listing");
        }
        for code in [401u16, 403, 500, 503] {
            assert!(!matches!(code, 404 | 405), "{code} must surface as an error");
        }
    }

    #[test]
    fn a_single_key_response_follows_the_same_rules() {
        assert_eq!(
            last_used_of(&json!({"key_id": "k1", "last_used_at": null}), "k1").unwrap(),
            false
        );
        assert_eq!(
            last_used_of(
                &json!({"key_id": "k1", "last_used_at": "2026-08-24T00:00:00Z"}),
                "k1"
            )
            .unwrap(),
            true
        );
        assert!(last_used_of(&json!({"key_id": "k1"}), "k1").is_err());
    }

    #[test]
    fn a_paged_listing_is_reported_as_paged() {
        let body =
            json!({"keys": [{"key_id": "other", "last_used_at": null}], "next_cursor": "abc"});
        let err = key_used_from_listing(&body, "k1").expect_err("must not answer");
        assert!(format!("{err:#}").contains("paged"), "got: {err:#}");

        let body =
            json!({"keys": [{"key_id": "other", "last_used_at": null}], "next_cursor": null});
        let err = key_used_from_listing(&body, "k1").expect_err("must not answer");
        assert!(!format!("{err:#}").contains("paged"), "got: {err:#}");
    }

    #[test]
    fn ambiguous_listings_are_errors_not_accusations() {
        // Field omitted entirely (a backend that skips nulls when serializing).
        let body = json!({"keys": [{"key_id": "k1"}]});
        assert!(key_used_from_listing(&body, "k1").is_err());
        // Key absent from the page.
        let body = json!({"keys": [{"key_id": "other", "last_used_at": null}]});
        assert!(key_used_from_listing(&body, "k1").is_err());
        // Response shape changed.
        let body = json!({"items": []});
        assert!(key_used_from_listing(&body, "k1").is_err());
    }

    #[test]
    fn error_bodies_are_condensed_for_a_one_line_message() {
        assert_eq!(summarize_error_body("  "), "(empty response body)");
        assert_eq!(
            summarize_error_body("{\"detail\":\n \"Invalid JWT\"}"),
            "{\"detail\": \"Invalid JWT\"}"
        );
        // A CDN/WAF rejection answers with a page; say that instead of pasting it.
        let html = "<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01//EN\">\n<HTML><HEAD>\
                    <TITLE>ERROR: The request could not be satisfied</TITLE>";
        let summary = summarize_error_body(html);
        assert!(summary.contains("HTML error page"), "got: {summary}");
        assert!(!summary.contains("<HTML>"), "got: {summary}");
        // Long JSON is truncated rather than flooding the terminal.
        let long = format!("{{\"detail\":\"{}\"}}", "x".repeat(2000));
        let summary = summarize_error_body(&long);
        assert!(
            summary.ends_with("… (truncated)"),
            "got tail: {}",
            &summary[summary.len() - 20..]
        );
        assert!(summary.chars().count() < 330);
    }
}
