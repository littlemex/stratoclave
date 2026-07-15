//! Backend API client for pipe / chat modes.
//!
//! From Phase 2 (v2.1) onward, the `admin` / `team-lead` / `usage` subcommands
//! use `mvp/api.rs`. This module is used only in pipe / chat mode and now talks
//! to the live inference surface: `POST /v1/messages` (Anthropic Messages wire,
//! SSE). The old REST/session/JSON-RPC client methods (list_sessions,
//! create_session, /api/acp, /api/settings, /api/teams, /api/tenants, ...) were
//! removed in this change: the backend never served those paths, so every call
//! 404'd — `echo hi | stratoclave` was dead-on-arrival before this fix.

use anyhow::{Context, Result};
use serde::Serialize;

use crate::config::AppConfig;
use crate::CliError;

/// Fallback model when config/env resolves nothing. Kept as a single named
/// constant so it is trivially greppable when the default needs bumping, and
/// validated against the live `/v1/messages` allowlist (see the CLI live E2E).
/// MUST be a real registry alias — the backend rejects unknown model ids with
/// HTTP 400, which would make the CLI dead-on-arrival for users without config.
const DEFAULT_MODEL: &str = "claude-sonnet-4-6";
/// Output cap for pipe/chat one-shot turns. The backend also bills input
/// tokens; this only bounds generation.
const DEFAULT_MAX_TOKENS: u32 = 4096;

/// One conversation turn on the Anthropic Messages wire. Chat mode accumulates
/// these so follow-up questions keep context (the Messages API is stateless —
/// the client owns history, unlike the removed session flow).
#[derive(Debug, Clone, Serialize)]
pub struct ChatTurn {
    pub role: String,
    pub content: String,
}

impl ChatTurn {
    pub fn user(content: impl Into<String>) -> Self {
        Self { role: "user".to_string(), content: content.into() }
    }
    pub fn assistant(content: impl Into<String>) -> Self {
        Self { role: "assistant".to_string(), content: content.into() }
    }
}

/// Result of one inference turn.
///
/// `complete` is true only when the stream ended cleanly with `message_stop`.
/// A non-empty `message` with `complete == false` is a PARTIAL response (the
/// stream errored or was cut mid-generation after some text arrived) —
/// `reason` explains why. Callers decide policy: pipe mode must NOT treat a
/// partial as trustworthy stdout, but interactive chat can show it with a
/// warning. An empty+incomplete stream is surfaced as `Err`, never here.
#[derive(Debug)]
pub struct ConverseResponse {
    pub message: String,
    pub complete: bool,
    pub reason: Option<String>,
}

/// How the SSE read loop terminated — drives the completeness check so a
/// truncated stream is never reported as a clean success.
enum StreamEnd {
    /// Saw the terminal `message_stop` frame — a complete response.
    Complete,
    /// Backend streamed an `error` frame mid-stream.
    Errored(String),
    /// Stream ended (EOF / read error / chunk timeout) before `message_stop`.
    Truncated(String),
}

/// Stratoclave API Client (pipe / chat inference).
pub struct ApiClient {
    config: AppConfig,
    /// One pooled streaming client, reused across chat turns so each turn does
    /// not pay a fresh TLS handshake. No overall timeout (streaming runs
    /// indefinitely); bounded per-chunk by the SSE read timeout and by an
    /// explicit time-to-first-byte guard on `.send()`.
    http: reqwest::Client,
    /// Authentication token sent as Bearer in the HTTP Authorization header.
    /// Holds the Cognito ID token when available, falling back to the access
    /// token; an `sk-stratoclave-*` API key also works on this path. The
    /// backend accepts both spellings under `Authorization: Bearer`.
    bearer_token: String,
    model_id: Option<String>,
}

impl ApiClient {
    pub fn new(config: AppConfig, bearer_token: String) -> Result<Self, CliError> {
        let model_id = config.resolve_model();
        let http = reqwest::Client::builder()
            .connect_timeout(std::time::Duration::from_secs(config.timeouts.connection_secs()))
            .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|e| {
                CliError::General(format!("Failed to build HTTP client: {}", e))
            })?;
        Ok(Self { config, http, bearer_token, model_id })
    }

    /// Extract the backend error code (`detail.type` / `detail.reason` /
    /// `detail.code`) from a JSON error body, if present. The backend returns
    /// `{"detail": {"type": "...", "reason": "...", "message": "..."}}` on the
    /// inference path; this lets the CLI branch on the specific failure instead
    /// of a generic HTTP-status message (Fable contract audit B4).
    fn error_code(body: &str) -> Option<String> {
        let v: serde_json::Value = serde_json::from_str(body).ok()?;
        let detail = v.get("detail").unwrap_or(&v);
        for key in ["type", "reason", "code"] {
            if let Some(s) = detail.get(key).and_then(|x| x.as_str()) {
                return Some(s.to_string());
            }
        }
        None
    }

    /// Map an initial HTTP response status to a CliError.
    fn map_status_error(status: reqwest::StatusCode, body: &str) -> CliError {
        let code = Self::error_code(body);
        match status.as_u16() {
            401 => CliError::AuthExpired(format!(
                "Authentication failed (HTTP 401: {}). \
                 This may indicate that an access token was used instead of an ID token. \
                 The backend requires an ID token with an `aud` claim for OIDC validation. \
                 Run `stratoclave auth login` to re-authenticate and obtain a valid ID token.",
                body
            )),
            // 402: budget / pool / per-model quota exhausted (Fable audit B3) —
            // a distinct, actionable class, not a random failure.
            402 => CliError::BudgetExceeded(format!(
                "Budget exhausted (HTTP 402{}). Check `stratoclave usage` or ask your \
                 tenant owner to raise the limit.",
                code.as_deref().map(|c| format!(", {c}")).unwrap_or_default()
            )),
            // 403: distinguish a VSR model-pin rejection from a real
            // access-denied (Fable audit B4) — the user's access is fine, their
            // --model-pin isn't allowed for the tenant.
            403 if code.as_deref() == Some("model_pin_not_allowed") => {
                CliError::PermissionDenied(format!(
                    "The pinned model is not allowed for your tenant (HTTP 403: \
                     model_pin_not_allowed). Remove --model-pin or ask an admin to \
                     allowlist it. ({})",
                    body
                ))
            }
            403 => CliError::PermissionDenied(format!(
                "Permission denied. You do not have access to this resource. (HTTP 403: {})",
                body
            )),
            404 => CliError::NotFound(format!(
                "Resource not found. (HTTP 404: {})",
                body
            )),
            // 400 invalid_model: an unknown model alias, not a generic bad
            // request — point the user at the model list (Fable audit B4).
            400 if code.as_deref() == Some("invalid_model") => CliError::General(format!(
                "Unknown model (HTTP 400: invalid_model). Check the alias against the \
                 gateway's supported models. ({})",
                body
            )),
            // 422 = request schema violation on /v1/messages (unprocessable),
            // NOT a missing resource — keep it distinct so the message is honest.
            422 => CliError::General(format!(
                "Request rejected as invalid (HTTP 422: {})",
                body
            )),
            429 => CliError::RateLimited(format!(
                "Rate limited. Too many requests. Please retry later. (HTTP 429: {})",
                body
            )),
            500..=599 => CliError::ServerError(format!(
                "Server error (HTTP {}): {}",
                status.as_u16(),
                body
            )),
            _ => CliError::General(format!(
                "API error (HTTP {}): {}",
                status.as_u16(),
                body
            )),
        }
    }

    // ---- Pipe / chat mode (Anthropic Messages, SSE) ----
    //
    // POST /v1/messages with a streaming body, accumulate text deltas, and
    // return the assembled assistant message. This is the ONLY live inference
    // path the CLI drives.

    /// Single-turn convenience: send one user message with no prior history.
    /// Used by pipe mode (`echo ... | stratoclave`).
    pub async fn converse(&self, message: &str) -> Result<ConverseResponse, CliError> {
        self.send_turns(&[ChatTurn::user(message)]).await
    }

    /// Send a full conversation (multi-turn) and stream back the assistant
    /// reply. Chat mode passes the accumulated history so context is preserved.
    pub async fn send_turns(&self, turns: &[ChatTurn]) -> Result<ConverseResponse, CliError> {
        // Always send an explicit model. Relying on a backend default would
        // couple us to unverified behavior; an explicit id is self-documenting
        // and the request schema requires model (min_length=1) anyway.
        let model = self.model_id.as_deref().unwrap_or(DEFAULT_MODEL);

        let body = serde_json::json!({
            "model": model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "messages": turns,
            "stream": true,
        });

        let base_url = self.config.resolve_base_url();
        let sse_timeout_secs = self.config.timeouts.sse_chunk_secs();

        use tokio::time::{timeout, Duration};

        // Guard the header phase: connect_timeout only covers TCP/TLS, and there
        // is no overall client timeout, so a server that accepts the connection
        // but never sends headers would otherwise hang forever. Bound
        // time-to-first-byte with the same per-chunk budget.
        let send_fut = self
            .http
            .post(format!("{}/v1/messages", base_url))
            .header("Authorization", format!("Bearer {}", self.bearer_token))
            .header("Content-Type", "application/json")
            .json(&body)
            .send();
        let mut response = match timeout(Duration::from_secs(sse_timeout_secs), send_fut).await {
            Ok(Ok(resp)) => resp,
            Ok(Err(e)) => {
                return Err(if e.is_connect() || e.is_timeout() {
                    CliError::NetworkError(format!(
                        "Network error: connection failed or timed out: {}",
                        e
                    ))
                } else {
                    CliError::NetworkError(format!("Network error: {}", e))
                });
            }
            Err(_) => {
                return Err(CliError::NetworkError(
                    "Timeout waiting for response headers from backend".to_string(),
                ));
            }
        };

        let status = response.status();
        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            return Err(Self::map_status_error(status, &body));
        }

        // Read the SSE stream chunk by chunk with a per-chunk timeout. We buffer
        // RAW BYTES (not lossy-decoded strings): a multi-byte UTF-8 codepoint
        // split across TCP chunk boundaries must not be corrupted into U+FFFD.
        // Only complete lines (server frames are well-formed UTF-8) are decoded.
        let mut content = String::new();
        let mut buffer: Vec<u8> = Vec::new();
        let mut stream_error: Option<String> = None;
        let mut stop_reason: Option<String> = None;
        let mut saw_message_stop = false;
        let end: StreamEnd;

        loop {
            match timeout(Duration::from_secs(sse_timeout_secs), response.chunk()).await {
                Ok(Ok(Some(chunk))) => {
                    buffer.extend_from_slice(&chunk);

                    while let Some(nl) = buffer.iter().position(|&b| b == b'\n') {
                        let line_bytes: Vec<u8> = buffer.drain(..=nl).collect();
                        let line = String::from_utf8_lossy(&line_bytes);
                        let line = line.trim_end_matches(['\r', '\n']);
                        Self::process_anthropic_sse_line(
                            line,
                            &mut content,
                            &mut stream_error,
                            &mut stop_reason,
                            &mut saw_message_stop,
                        );
                    }
                    if stream_error.is_some() {
                        end = StreamEnd::Errored(stream_error.take().unwrap());
                        break;
                    }
                    if saw_message_stop {
                        end = StreamEnd::Complete;
                        break;
                    }
                }
                Ok(Ok(None)) => {
                    // EOF. Flush any trailing line missing its newline.
                    if !buffer.is_empty() {
                        let line = String::from_utf8_lossy(&buffer);
                        let line = line.trim_end_matches(['\r', '\n']);
                        Self::process_anthropic_sse_line(
                            line,
                            &mut content,
                            &mut stream_error,
                            &mut stop_reason,
                            &mut saw_message_stop,
                        );
                    }
                    end = if let Some(err) = stream_error.take() {
                        StreamEnd::Errored(err)
                    } else if saw_message_stop {
                        StreamEnd::Complete
                    } else {
                        StreamEnd::Truncated("stream closed before completion".to_string())
                    };
                    break;
                }
                Ok(Err(e)) => {
                    end = StreamEnd::Truncated(format!("read error: {}", e));
                    break;
                }
                Err(_) => {
                    end = StreamEnd::Truncated("timeout waiting for next chunk".to_string());
                    break;
                }
            }
        }

        // Completeness handling. A clean stream returns complete=true. An
        // error/truncation with NO text is a hard failure (Err). An
        // error/truncation WITH text returns the partial content and
        // complete=false, so the caller decides: pipe mode rejects it (stdout
        // must be trustworthy), interactive chat can show it with a warning.
        match end {
            StreamEnd::Complete => {
                // A clean `message_stop` still isn't a COMPLETE answer if the
                // generation was cut by the token cap (stop_reason=max_tokens)
                // or any terminal reason other than end_turn/stop_sequence
                // (Fable review Finding 2). Pipe stdout must be trustworthy, so
                // flag it incomplete and let pipe mode reject / chat mode warn.
                let clean = matches!(
                    stop_reason.as_deref(),
                    None | Some("end_turn") | Some("stop_sequence")
                );
                Ok(ConverseResponse {
                    message: content,
                    complete: clean,
                    reason: if clean {
                        None
                    } else {
                        Some(format!(
                            "stopped early: {}",
                            stop_reason.as_deref().unwrap_or("unknown")
                        ))
                    },
                })
            }
            StreamEnd::Errored(msg) => {
                if content.is_empty() {
                    Err(CliError::ServerError(format!(
                        "Backend streamed an error: {}",
                        msg
                    )))
                } else {
                    Ok(ConverseResponse {
                        message: content,
                        complete: false,
                        reason: Some(format!("backend error: {}", msg)),
                    })
                }
            }
            StreamEnd::Truncated(why) => {
                if content.is_empty() {
                    Err(CliError::NetworkError(format!(
                        "No response from backend ({})",
                        why
                    )))
                } else {
                    Ok(ConverseResponse {
                        message: content,
                        complete: false,
                        reason: Some(format!("truncated: {}", why)),
                    })
                }
            }
        }
    }

    /// Parse one Anthropic Messages SSE line. Accumulates text from
    /// `content_block_delta` `text_delta` events; sets `saw_stop` on
    /// `message_stop`; records the message of an `error` frame. Ignores
    /// `event:` lines, blank lines, comments, malformed frames, and non-text
    /// deltas (tool_use / thinking) — the CLI sends no tools, so those never
    /// carry visible text.
    fn process_anthropic_sse_line(
        line: &str,
        content: &mut String,
        error: &mut Option<String>,
        stop_reason: &mut Option<String>,
        saw_stop: &mut bool,
    ) {
        let Some(data) = line.strip_prefix("data:") else {
            return; // `event: ...` lines, blanks, `:` comments
        };
        let data = data.trim();
        if data.is_empty() {
            return;
        }
        let Ok(json) = serde_json::from_str::<serde_json::Value>(data) else {
            return; // tolerate a malformed frame rather than aborting mid-stream
        };
        match json.get("type").and_then(|t| t.as_str()) {
            Some("content_block_delta") => {
                let delta = &json["delta"];
                if delta.get("type").and_then(|t| t.as_str()) == Some("text_delta") {
                    if let Some(text) = delta.get("text").and_then(|t| t.as_str()) {
                        content.push_str(text);
                    }
                }
            }
            Some("message_delta") => {
                // The final `message_delta` carries the terminal stop_reason
                // (end_turn / stop_sequence / max_tokens / tool_use). We capture
                // it so the completeness check can flag a cap-truncated answer
                // (max_tokens) as NOT a clean completion, even though a valid
                // message_stop follows (Fable review Finding 2).
                if let Some(sr) = json
                    .get("delta")
                    .and_then(|d| d.get("stop_reason"))
                    .and_then(|s| s.as_str())
                {
                    *stop_reason = Some(sr.to_string());
                }
            }
            Some("error") => {
                // Anthropic wire: {"type":"error","error":{"type":..,"message":..}}
                let msg = json
                    .get("error")
                    .and_then(|e| e.get("message"))
                    .and_then(|m| m.as_str())
                    .unwrap_or("unknown streaming error");
                *error = Some(msg.to_string());
            }
            Some("message_stop") => *saw_stop = true,
            _ => {} // message_start, content_block_start/stop, ping
        }
    }
}

/// Read stdin message (for pipe mode)
pub fn read_stdin_message() -> Result<Option<String>> {
    use std::io::Read;

    if atty_check() {
        return Ok(None);
    }

    let mut input = String::new();
    std::io::stdin()
        .read_to_string(&mut input)
        .context("Failed to read stdin")?;

    let trimmed = input.trim().to_string();
    if trimmed.is_empty() {
        Ok(None)
    } else {
        Ok(Some(trimmed))
    }
}

fn atty_check() -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::io::AsRawFd;
        libc_isatty(std::io::stdin().as_raw_fd())
    }
    #[cfg(not(unix))]
    {
        false
    }
}

/// Check if stdin is a TTY (public interface for main.rs pipe detection)
pub fn is_stdin_tty() -> bool {
    atty_check()
}

#[cfg(unix)]
fn libc_isatty(fd: i32) -> bool {
    extern "C" {
        fn isatty(fd: i32) -> i32;
    }
    // SAFETY: isatty(3) takes an int fd and returns int; no pointers involved.
    // fd comes from STDOUT/STDIN_FILENO style constants upstream and is always valid.
    #[allow(unsafe_code)]
    unsafe {
        isatty(fd) != 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_config() -> AppConfig {
        AppConfig {
            client_id: "test".to_string(),
            cognito_domain: "https://auth.example.com".to_string(),
            redirect_port: 18080,
            redirect_host: "127.0.0.1".to_string(),
            redirect_uri: "http://127.0.0.1:18080/callback".to_string(),
            api_endpoint: "http://localhost:8000".to_string(),
            admin_ui_url: None,
            default_model: None,
            config_dir: std::path::PathBuf::from("/tmp/test"),
            timeouts: crate::config::Timeouts::default(),
            auth_method: crate::auth::AuthMethod::default(),
            saml2aws: None,
        }
    }

    // --- Test: ApiClient construction ---

    #[test]
    fn test_api_client_new() {
        let client = ApiClient::new(test_config(), "test-token".to_string());
        assert!(client.is_ok());
    }

    // --- Test: ChatTurn constructors ---

    #[test]
    fn chat_turn_roles() {
        assert_eq!(ChatTurn::user("hi").role, "user");
        assert_eq!(ChatTurn::assistant("yo").role, "assistant");
        // Serializes to the Anthropic message shape.
        let v = serde_json::to_value(ChatTurn::user("x")).unwrap();
        assert_eq!(v, serde_json::json!({"role": "user", "content": "x"}));
    }

    // --- Test: map_status_error ---

    #[test]
    fn test_map_status_error_401() {
        let err = ApiClient::map_status_error(reqwest::StatusCode::UNAUTHORIZED, "unauthorized");
        match err {
            CliError::AuthExpired(msg) => assert!(msg.contains("401")),
            other => panic!("Expected AuthExpired, got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_403() {
        let err = ApiClient::map_status_error(reqwest::StatusCode::FORBIDDEN, "forbidden");
        match err {
            CliError::PermissionDenied(msg) => assert!(msg.contains("403")),
            other => panic!("Expected PermissionDenied, got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_404() {
        let err = ApiClient::map_status_error(reqwest::StatusCode::NOT_FOUND, "not found");
        match err {
            CliError::NotFound(msg) => assert!(msg.contains("404")),
            other => panic!("Expected NotFound, got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_422_is_invalid_not_notfound() {
        let err = ApiClient::map_status_error(
            reqwest::StatusCode::UNPROCESSABLE_ENTITY,
            "bad model",
        );
        match err {
            CliError::General(msg) => assert!(msg.contains("422")),
            other => panic!("Expected General(invalid), got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_429() {
        let err = ApiClient::map_status_error(reqwest::StatusCode::TOO_MANY_REQUESTS, "rate limited");
        match err {
            CliError::RateLimited(msg) => assert!(msg.contains("429")),
            other => panic!("Expected RateLimited, got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_500() {
        let err =
            ApiClient::map_status_error(reqwest::StatusCode::INTERNAL_SERVER_ERROR, "internal error");
        match err {
            CliError::ServerError(msg) => assert!(msg.contains("500")),
            other => panic!("Expected ServerError, got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_402_budget() {
        let err = ApiClient::map_status_error(
            reqwest::StatusCode::PAYMENT_REQUIRED,
            r#"{"detail":{"type":"tenant_pool_exhausted"}}"#,
        );
        match err {
            CliError::BudgetExceeded(msg) => {
                assert!(msg.contains("402"));
                assert!(msg.contains("tenant_pool_exhausted"));
            }
            other => panic!("Expected BudgetExceeded, got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_403_model_pin_vs_generic() {
        // A model-pin rejection must NOT read as generic permission-denied.
        let pin = ApiClient::map_status_error(
            reqwest::StatusCode::FORBIDDEN,
            r#"{"detail":{"reason":"model_pin_not_allowed"}}"#,
        );
        match pin {
            CliError::PermissionDenied(msg) => assert!(msg.contains("pinned model")),
            other => panic!("Expected pin-specific PermissionDenied, got: {:?}", other),
        }
        // A real access-denied still gets the generic message.
        let denied = ApiClient::map_status_error(reqwest::StatusCode::FORBIDDEN, "nope");
        match denied {
            CliError::PermissionDenied(msg) => assert!(msg.contains("do not have access")),
            other => panic!("Expected generic PermissionDenied, got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_400_invalid_model() {
        let err = ApiClient::map_status_error(
            reqwest::StatusCode::BAD_REQUEST,
            r#"{"detail":{"type":"invalid_model","message":"unknown"}}"#,
        );
        match err {
            CliError::General(msg) => assert!(msg.contains("Unknown model")),
            other => panic!("Expected General(invalid_model), got: {:?}", other),
        }
    }

    #[test]
    fn error_code_extracts_nested_and_flat() {
        assert_eq!(
            ApiClient::error_code(r#"{"detail":{"type":"x"}}"#).as_deref(),
            Some("x")
        );
        assert_eq!(
            ApiClient::error_code(r#"{"reason":"y"}"#).as_deref(),
            Some("y")
        );
        assert_eq!(ApiClient::error_code("not json").as_deref(), None);
    }

    // --- Test: Anthropic SSE parser ---

    /// Feed lines through the parser and return (content, error, saw_stop).
    fn feed(lines: &[&str]) -> (String, Option<String>, bool) {
        let (c, e, _sr, s) = feed_full(lines);
        (c, e, s)
    }

    /// Full parser output incl. the captured stop_reason.
    fn feed_full(lines: &[&str]) -> (String, Option<String>, Option<String>, bool) {
        let mut content = String::new();
        let mut error = None;
        let mut stop_reason = None;
        let mut saw_stop = false;
        for line in lines {
            ApiClient::process_anthropic_sse_line(
                line,
                &mut content,
                &mut error,
                &mut stop_reason,
                &mut saw_stop,
            );
        }
        (content, error, stop_reason, saw_stop)
    }

    #[test]
    fn captures_max_tokens_stop_reason() {
        // A cap-truncated generation: text deltas, then message_delta with
        // stop_reason=max_tokens, then a clean message_stop. The parser must
        // surface the stop_reason so the caller flags it incomplete.
        let (content, error, stop_reason, saw_stop) = feed_full(&[
            r#"data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}"#,
            r#"data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":4096}}"#,
            r#"data: {"type":"message_stop"}"#,
        ]);
        assert_eq!(content, "partial");
        assert!(error.is_none());
        assert_eq!(stop_reason.as_deref(), Some("max_tokens"));
        assert!(saw_stop);
    }

    #[test]
    fn captures_end_turn_stop_reason() {
        let (_c, _e, stop_reason, saw_stop) = feed_full(&[
            r#"data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}"#,
            r#"data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}"#,
            r#"data: {"type":"message_stop"}"#,
        ]);
        assert_eq!(stop_reason.as_deref(), Some("end_turn"));
        assert!(saw_stop);
    }

    #[test]
    fn concatenates_multiple_text_deltas() {
        let (content, error, saw_stop) = feed(&[
            "event: message_start",
            r#"data: {"type":"message_start","message":{"id":"msg_1"}}"#,
            "event: content_block_delta",
            r#"data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello, "}}"#,
            r#"data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"world"}}"#,
            r#"data: {"type":"content_block_stop","index":0}"#,
        ]);
        assert_eq!(content, "Hello, world");
        assert!(error.is_none());
        assert!(!saw_stop);
    }

    #[test]
    fn stops_on_message_stop_and_ignores_event_lines() {
        let (content, error, saw_stop) = feed(&[
            "event: content_block_delta",
            r#"data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}"#,
            "event: message_stop",
            r#"data: {"type":"message_stop"}"#,
        ]);
        assert_eq!(content, "hi");
        assert!(error.is_none());
        assert!(saw_stop);
    }

    #[test]
    fn ignores_non_text_deltas_and_garbage() {
        let (content, error, saw_stop) = feed(&[
            r#"data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"x\":1}"}}"#,
            "data: not-json",
            "",
            ": comment line",
        ]);
        assert_eq!(content, "");
        assert!(error.is_none());
        assert!(!saw_stop);
    }

    #[test]
    fn records_error_frame() {
        let (content, error, saw_stop) = feed(&[
            "event: error",
            r#"data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}"#,
        ]);
        assert_eq!(content, "");
        assert_eq!(error.as_deref(), Some("Overloaded"));
        assert!(!saw_stop);
    }

    #[test]
    fn error_frame_without_message_falls_back() {
        let (_content, error, _saw_stop) = feed(&[r#"data: {"type":"error","error":{}}"#]);
        assert_eq!(error.as_deref(), Some("unknown streaming error"));
    }

    #[test]
    fn handles_multibyte_utf8_intact_when_line_whole() {
        // A complete line carrying Japanese text decodes cleanly.
        let (content, _e, _s) = feed(&[
            r#"data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"こんにちは"}}"#,
        ]);
        assert_eq!(content, "こんにちは");
    }
}
