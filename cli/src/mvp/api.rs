//! MVP API client: authorization header injection + 401 handling.
//!
//! All Phase 2 subcommands (admin / team-lead / usage) go through this helper.
//! On a 401 response, users are instructed to re-authenticate with
//! `stratoclave auth login`.

use anyhow::{anyhow, Context, Result};
use reqwest::{Client, RequestBuilder, Response, StatusCode};
use serde::de::DeserializeOwned;
use serde_json::Value;

use super::config::MvpConfig;
use super::tokens::{load as load_tokens, MvpTokens};

pub struct ApiClient {
    pub config: MvpConfig,
    pub tokens: MvpTokens,
    http: Client,
}

impl ApiClient {
    pub fn new() -> Result<Self> {
        let config = MvpConfig::load()?;
        let tokens = load_tokens().map_err(|e| {
            anyhow!(
                "Not logged in: {e}. Run `stratoclave auth login` first."
            )
        })?;
        // WAF (AWSManagedRulesCommonRuleSet) blocks requests with an empty
        // User-Agent. reqwest does not auto-populate one, so every CLI
        // subcommand must pass an explicit identifier.
        let http = Client::builder()
            .timeout(std::time::Duration::from_secs(60))
            .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
            .build()?;
        Ok(Self {
            config,
            tokens,
            http,
        })
    }

    pub fn api_url(&self, path: &str) -> String {
        self.config.api(path)
    }

    async fn send(&self, req: RequestBuilder) -> Result<Response> {
        let req = req.bearer_auth(&self.tokens.access_token);
        let resp = req.send().await.context("HTTP request failed")?;
        Ok(resp)
    }

    pub async fn get_json<T: DeserializeOwned>(&self, path: &str) -> Result<T> {
        let resp = self.send(self.http.get(self.api_url(path))).await?;
        handle_response(resp).await
    }

    pub async fn post_json<T: DeserializeOwned>(
        &self,
        path: &str,
        body: &Value,
    ) -> Result<T> {
        let resp = self
            .send(self.http.post(self.api_url(path)).json(body))
            .await?;
        handle_response(resp).await
    }

    /// POST with extra request headers (e.g. `Idempotency-Key` for external
    /// authorize). Same auth injection + response handling as `post_json`.
    pub async fn post_json_with_headers<T: DeserializeOwned>(
        &self,
        path: &str,
        body: &Value,
        headers: &[(&str, &str)],
    ) -> Result<T> {
        let mut req = self.http.post(self.api_url(path)).json(body);
        for (k, v) in headers {
            req = req.header(*k, *v);
        }
        let resp = self.send(req).await?;
        handle_response(resp).await
    }

    pub async fn put_json<T: DeserializeOwned>(&self, path: &str, body: &Value) -> Result<T> {
        let resp = self
            .send(self.http.put(self.api_url(path)).json(body))
            .await?;
        handle_response(resp).await
    }

    pub async fn patch_json<T: DeserializeOwned>(
        &self,
        path: &str,
        body: &Value,
    ) -> Result<T> {
        let resp = self
            .send(self.http.patch(self.api_url(path)).json(body))
            .await?;
        handle_response(resp).await
    }

    /// DELETE typically returns 204 with no body; returns () on success.
    pub async fn delete(&self, path: &str) -> Result<()> {
        let resp = self.send(self.http.delete(self.api_url(path))).await?;
        let status = resp.status();
        if status.is_success() {
            return Ok(());
        }
        let body = resp.text().await.unwrap_or_default();
        raise_http(status, &body)
    }
}

async fn handle_response<T: DeserializeOwned>(resp: Response) -> Result<T> {
    let status = resp.status();
    let text = resp.text().await.context("Read HTTP response body")?;
    if status.is_success() {
        serde_json::from_str::<T>(&text).with_context(|| {
            format!(
                "Failed to parse JSON response (status={}, body={})",
                status,
                truncate(&text, 400)
            )
        })
    } else {
        raise_http(status, &text)
    }
}

fn raise_http<T>(status: StatusCode, body: &str) -> Result<T> {
    let friendly_reason = parse_detail(body).unwrap_or_else(|| truncate(body, 400));
    let msg = match status {
        StatusCode::UNAUTHORIZED => format!(
            "401 Unauthorized: {friendly_reason}. \n\
             Your session may have expired or the tenant was switched. \n\
             Please run `stratoclave auth login` again."
        ),
        StatusCode::FORBIDDEN => {
            format!("403 Forbidden: {friendly_reason}")
        }
        StatusCode::NOT_FOUND => format!("404 Not Found: {friendly_reason}"),
        StatusCode::TOO_MANY_REQUESTS => {
            format!("429 Too Many Requests: {friendly_reason}")
        }
        s if s.is_server_error() => format!("{} Server error: {}", s.as_u16(), friendly_reason),
        s => format!("{} {}", s.as_u16(), friendly_reason),
    };
    Err(anyhow!(msg))
}

fn parse_detail(body: &str) -> Option<String> {
    let v: Value = serde_json::from_str(body).ok()?;
    v.get("detail").and_then(|d| match d {
        Value::String(s) => Some(s.clone()),
        Value::Array(items) => Some(
            items
                .iter()
                .map(|it| {
                    it.get("msg")
                        .and_then(|m| m.as_str())
                        .map(String::from)
                        .unwrap_or_else(|| it.to_string())
                })
                .collect::<Vec<_>>()
                .join("; "),
        ),
        other => Some(other.to_string()),
    })
}

fn truncate(s: &str, n: usize) -> String {
    if s.len() <= n {
        s.to_string()
    } else {
        format!("{}…", &s[..n])
    }
}

#[cfg(test)]
mod tests {
    //! Unit tests for the HTTP error mapping (`raise_http` / `parse_detail`)
    //! that every MVP subcommand funnels through on a non-2xx response.
    //!
    //! Scope note (PENDING protocol, docs/design/pending-protocol.md): the
    //! backend can now return 402 `credit_exhausted` (reason
    //! `tenant_pool_exhausted`), 410 (expired hold on capture), 503
    //! `budget_unavailable` (reason `pool_reservation_ambiguous` /
    //! `pool_reservation_in_flight` / `pool_reservation_contended`), and 409
    //! (capture-vs-void race / `authorization_inconsistent`). `raise_http` has
    //! NO status-specific branch for 402/410/409 today — those fall through the
    //! generic `s => format!("{} {}", s.as_u16(), friendly_reason)` arm, and 503
    //! falls through the generic `s.is_server_error()` arm. These tests pin
    //! down that catch-all behavior AS IMPLEMENTED: `friendly_reason` comes from
    //! `parse_detail`, which — for a JSON *object* `detail` (all of the shapes
    //! above) — stringifies the whole object (alphabetical key order, no
    //! feature to cherry-pick just `reason`/`type`). We assert the status code
    //! and the object's key/value substrings appear in the final message,
    //! rather than assuming any dedicated extraction that does not exist in the
    //! implementation. If a future change adds a specific reason-extraction
    //! branch, tighten these assertions rather than loosen them.

    use super::*;

    fn err_msg(status: StatusCode, body: &str) -> String {
        // `raise_http` is unconditional (it always returns Err; callers only
        // invoke it on the non-success path), so calling it directly on a
        // canned body is a faithful unit test of the mapping logic without
        // needing a real HTTP round-trip.
        raise_http::<()>(status, body).unwrap_err().to_string()
    }

    // ---- parse_detail ----

    #[test]
    fn parse_detail_extracts_plain_string_detail() {
        // e.g. the 429 rate-limit body: {"detail": "Rate limit exceeded. Try again later."}
        let body = r#"{"detail": "Rate limit exceeded. Try again later."}"#;
        assert_eq!(
            parse_detail(body),
            Some("Rate limit exceeded. Try again later.".to_string())
        );
    }

    #[test]
    fn parse_detail_stringifies_object_detail() {
        // The 402 credit_exhausted shape (mvp/_pipeline.py::_err_402): an OBJECT
        // detail, not a string. parse_detail's only matches are String/Array;
        // an object falls to the catch-all `other => Some(other.to_string())`,
        // which yields the object's compact JSON form. We assert the
        // machine-readable fields are still present as substrings (so a human
        // reading the CLI error can still see `reason`/`type`), without
        // pretending the code extracts them structurally.
        let body = r#"{"detail": {"type": "credit_exhausted", "reason": "tenant_pool_exhausted", "message": "Insufficient budget for this request. Contact your admin."}}"#;
        let parsed = parse_detail(body).expect("object detail must parse to Some");
        assert!(parsed.contains("credit_exhausted"));
        assert!(parsed.contains("tenant_pool_exhausted"));
    }

    #[test]
    fn parse_detail_stringifies_budget_unavailable_object() {
        // The 503 budget_unavailable shape (mvp/_pipeline.py), covering both
        // PENDING-protocol reasons the CLI can now see.
        for reason in ["pool_reservation_ambiguous", "pool_reservation_in_flight"] {
            let body = format!(
                r#"{{"detail": {{"type": "budget_unavailable", "reason": "{reason}", "message": "Retry shortly."}}}}"#
            );
            let parsed = parse_detail(&body).expect("object detail must parse to Some");
            assert!(parsed.contains("budget_unavailable"));
            assert!(parsed.contains(reason));
        }
    }

    #[test]
    fn parse_detail_none_on_unparsable_body() {
        assert_eq!(parse_detail("not json"), None);
        assert_eq!(parse_detail(""), None);
    }

    #[test]
    fn parse_detail_none_when_no_detail_key() {
        assert_eq!(parse_detail(r#"{"message": "oops"}"#), None);
    }

    // ---- raise_http status mapping ----

    #[test]
    fn raise_http_402_credit_exhausted_tenant_pool() {
        // reserve_external_authorization's genuine-exhaustion path
        // (mvp/_pipeline.py:1937 / :2103) — 402, reason tenant_pool_exhausted.
        let body = r#"{"detail": {"type": "credit_exhausted", "reason": "tenant_pool_exhausted", "message": "Insufficient budget for this request. Contact your admin."}}"#;
        let msg = err_msg(StatusCode::PAYMENT_REQUIRED, body);
        // No dedicated "402 Payment Required: ..." branch exists today; the
        // catch-all prints the bare code, so pin that shape rather than a
        // friendlier wording the code does not produce.
        assert!(msg.starts_with("402 "), "unexpected message: {msg}");
        assert!(msg.contains("credit_exhausted"));
        assert!(msg.contains("tenant_pool_exhausted"));
    }

    #[test]
    fn raise_http_410_expired_hold_on_capture() {
        // billing_authorize.py: `raise HTTPException(status_code=410, detail="authorization expired")`.
        let body = r#"{"detail": "authorization expired"}"#;
        let msg = err_msg(StatusCode::GONE, body);
        assert!(msg.starts_with("410 "), "unexpected message: {msg}");
        assert!(msg.contains("authorization expired"));
    }

    #[test]
    fn raise_http_503_pool_reservation_ambiguous() {
        // _reserve_external_pending's ambiguous-commit path (mvp/_pipeline.py:2094).
        let body = r#"{"detail": {"type": "budget_unavailable", "reason": "pool_reservation_ambiguous", "message": "Budget reservation could not be confirmed. Retry with the same Idempotency-Key."}}"#;
        let msg = err_msg(StatusCode::SERVICE_UNAVAILABLE, body);
        // 503 IS a server error, so it takes the explicit
        // "{code} Server error: {reason}" arm.
        assert!(
            msg.starts_with("503 Server error:"),
            "unexpected message: {msg}"
        );
        assert!(msg.contains("budget_unavailable"));
        assert!(msg.contains("pool_reservation_ambiguous"));
    }

    #[test]
    fn raise_http_503_pool_reservation_in_flight() {
        // _pending_replay_result's in-flight replay path (mvp/_pipeline.py:2008).
        let body = r#"{"detail": {"type": "budget_unavailable", "reason": "pool_reservation_in_flight", "message": "A reservation for this Idempotency-Key is in flight. Retry shortly."}}"#;
        let msg = err_msg(StatusCode::SERVICE_UNAVAILABLE, body);
        assert!(
            msg.starts_with("503 Server error:"),
            "unexpected message: {msg}"
        );
        assert!(msg.contains("pool_reservation_in_flight"));
    }

    #[test]
    fn raise_http_429_too_many_requests() {
        let body = r#"{"detail": "Rate limit exceeded. Try again later."}"#;
        let msg = err_msg(StatusCode::TOO_MANY_REQUESTS, body);
        assert_eq!(
            msg,
            "429 Too Many Requests: Rate limit exceeded. Try again later."
        );
    }

    #[test]
    fn raise_http_409_capture_vs_void_race() {
        // _capture_terminal_response / _void_terminal_response race outcomes
        // (billing_authorize.py) — e.g. already_voided / already_captured /
        // authorization_inconsistent. No dedicated 409 branch exists; pin the
        // catch-all shape.
        let body = r#"{"detail": {"type": "already_voided"}}"#;
        let msg = err_msg(StatusCode::CONFLICT, body);
        assert!(msg.starts_with("409 "), "unexpected message: {msg}");
        assert!(msg.contains("already_voided"));
    }

    #[test]
    fn raise_http_401_unauthorized_still_has_dedicated_copy() {
        // Sanity check that adding PENDING-protocol coverage above did not
        // regress the one status the CLI DOES special-case today.
        let body = r#"{"detail": "invalid token"}"#;
        let msg = err_msg(StatusCode::UNAUTHORIZED, body);
        assert!(msg.starts_with("401 Unauthorized: invalid token"));
        assert!(msg.contains("stratoclave auth login"));
    }

    #[test]
    fn raise_http_falls_back_to_truncated_body_on_unparsable_detail() {
        let msg = err_msg(StatusCode::INTERNAL_SERVER_ERROR, "plain text failure, no json");
        assert!(msg.contains("plain text failure, no json"));
    }
}
