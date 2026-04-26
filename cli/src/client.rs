//! Backend API client module (legacy, pipe / chat 用).
//!
//! Phase 2 (v2.1) 以降、`admin` / `team-lead` / `usage` サブコマンドは `mvp/api.rs` を使う。
//! 本モジュールは pipe / chat モード (旧 converse エンドポイント) でのみ使用され、
//! 旧 REST API クライアントメソッド (list_sessions / list_users 等) はビルド互換のため残存。
#![allow(dead_code)]

use anyhow::{Context, Result};
use serde::Serialize;

use crate::config::AppConfig;
use crate::CliError;

/// Bedrock Converse API request (legacy pipe mode)
#[derive(Debug, Serialize)]
pub struct ConverseRequest {
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub system: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f32>,
}

/// Converse response (pipe mode)
#[derive(Debug)]
pub struct ConverseResponse {
    pub message: String,
}

/// Stratoclave API Client
pub struct ApiClient {
    http_client: reqwest::Client,
    config: AppConfig,
    /// Authentication token sent as Bearer in HTTP Authorization header.
    /// Holds the Cognito ID token when available, falling back to the access token.
    /// The backend validates the `aud` claim which only exists in ID tokens.
    bearer_token: String,
    model_id: Option<String>,
}

impl ApiClient {
    pub fn new(config: AppConfig, bearer_token: String) -> Self {
        let http_client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(config.timeouts.http_total_secs()))
            .connect_timeout(std::time::Duration::from_secs(config.timeouts.connection_secs()))
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());

        let model_id = config.resolve_model();

        Self {
            http_client,
            config,
            bearer_token,
            model_id,
        }
    }

    /// Create a client with longer timeout for streaming operations (no overall timeout, uses connect timeout)
    fn streaming_client(&self) -> reqwest::Client {
        reqwest::Client::builder()
            .connect_timeout(std::time::Duration::from_secs(self.config.timeouts.connection_secs()))
            // No overall timeout - streaming responses need to run indefinitely
            .build()
            .unwrap_or_else(|_| reqwest::Client::new())
    }

    /// Build a request with auth header
    fn request(&self, method: reqwest::Method, path: &str) -> reqwest::RequestBuilder {
        let base_url = self.config.resolve_base_url();
        self.http_client
            .request(method, format!("{}{}", base_url, path))
            .header("Authorization", format!("Bearer {}", self.bearer_token))
            .header("Content-Type", "application/json")
    }

    /// Map HTTP response status to CliError
    fn map_status_error(status: reqwest::StatusCode, body: &str) -> CliError {
        match status.as_u16() {
            401 => CliError::AuthExpired(format!(
                "Authentication failed (HTTP 401: {}). \
                 This may indicate that an access token was used instead of an ID token. \
                 The backend requires an ID token with an `aud` claim for OIDC validation. \
                 Run `stratoclave auth login` to re-authenticate and obtain a valid ID token.",
                body
            )),
            403 => CliError::PermissionDenied(format!(
                "Permission denied. You do not have access to this resource. (HTTP 403: {})",
                body
            )),
            404 => CliError::NotFound(format!(
                "Resource not found. (HTTP 404: {})",
                body
            )),
            422 => CliError::NotFound(format!(
                "Validation error or resource not found. (HTTP 422: {})",
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

    /// Send request and handle errors
    async fn send_request(
        &self,
        request: reqwest::RequestBuilder,
    ) -> Result<reqwest::Response, CliError> {
        let response = request.send().await.map_err(|e| {
            if e.is_connect() || e.is_timeout() {
                CliError::NetworkError(format!(
                    "Network error: connection failed or timed out: {}",
                    e
                ))
            } else {
                CliError::NetworkError(format!("Network error: {}", e))
            }
        })?;

        let status = response.status();
        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            return Err(Self::map_status_error(status, &body));
        }

        Ok(response)
    }

    // ---- Pipe mode (Converse) ----
    // Creates a temp session, sends message, and returns response text
    pub async fn converse(&self, message: &str) -> Result<ConverseResponse, CliError> {
        // 1. Create a temp session
        let session_data = self.create_session(Some("bedrock"), None).await?;
        let session_id = session_data["session_id"]
            .as_str()
            .ok_or_else(|| CliError::General("No session_id in response".to_string()))?
            .to_string();

        // 2. Send message via /api/sessions/send (returns SSE)
        let body = serde_json::json!({
            "session_id": session_id,
            "content": message,
            "provider": "bedrock",
        });

        let base_url = self.config.resolve_base_url();
        let streaming = self.streaming_client();
        let mut response = streaming
            .post(format!("{}/api/sessions/send", base_url))
            .header("Authorization", format!("Bearer {}", self.bearer_token))
            .header("Content-Type", "application/json")
            .json(&body)
            .send()
            .await
            .map_err(|e| {
                if e.is_connect() || e.is_timeout() {
                    CliError::NetworkError(format!(
                        "Network error: connection failed or timed out: {}",
                        e
                    ))
                } else {
                    CliError::NetworkError(format!("Network error: {}", e))
                }
            })?;

        let status = response.status();
        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            return Err(Self::map_status_error(status, &body));
        }

        // Read SSE stream chunk by chunk with timeout
        let mut content = String::new();
        let mut buffer = String::new();
        let mut stream_done = false;
        let sse_timeout_secs = self.config.timeouts.sse_chunk_secs();

        use tokio::time::{timeout, Duration};

        while !stream_done {
            match timeout(Duration::from_secs(sse_timeout_secs), response.chunk()).await {
                Ok(Ok(Some(chunk))) => {
                    let chunk_str = String::from_utf8_lossy(&chunk);
                    buffer.push_str(&chunk_str);

                    // Process complete lines
                    while let Some(newline_pos) = buffer.find('\n') {
                        let line = buffer[..newline_pos].trim_end().to_string();
                        buffer = buffer[newline_pos + 1..].to_string();

                        Self::process_sse_line(&line, &mut content, &mut stream_done);
                    }
                }
                Ok(Ok(None)) => {
                    // Stream ended
                    if !buffer.is_empty() {
                        Self::process_sse_line(&buffer, &mut content, &mut stream_done);
                    }
                    stream_done = true;
                }
                Ok(Err(e)) => {
                    // Read error - but if we have some content, return it
                    if content.is_empty() {
                        return Err(CliError::General(format!("Failed to read response: {}", e)));
                    }
                    stream_done = true;
                }
                Err(_) => {
                    // Timeout waiting for next chunk - return what we have
                    if content.is_empty() {
                        return Err(CliError::NetworkError("Timeout waiting for response from backend".to_string()));
                    }
                    stream_done = true;
                }
            }
        }

        if content.is_empty() {
            content = "[No response]".to_string();
        }

        Ok(ConverseResponse { message: content })
    }

    /// Process a single SSE line and extract content
    fn process_sse_line(line: &str, content: &mut String, done: &mut bool) {
        if let Some(data) = line.strip_prefix("data: ") {
            if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(data) {
                // Extract text from various possible formats
                if let Some(text) = parsed.get("text").and_then(|t| t.as_str()) {
                    content.push_str(text);
                } else if let Some(delta) = parsed.get("delta") {
                    if let Some(text) = delta.get("text").and_then(|t| t.as_str()) {
                        content.push_str(text);
                    }
                } else if let Some(msg) = parsed.get("message").and_then(|m| m.as_str()) {
                    content.push_str(msg);
                } else if let Some(c) = parsed.get("content").and_then(|c| c.as_str()) {
                    // Backend sends cumulative content, so replace instead of append
                    content.clear();
                    content.push_str(c);
                }
                // Check for error
                if let Some(err) = parsed.get("error").and_then(|e| e.as_str()) {
                    if content.is_empty() {
                        content.push_str(&format!("[ERROR] {}", err));
                    }
                    *done = true;
                }
                // Check for completion
                if parsed.get("status").and_then(|s| s.as_str()) == Some("complete") {
                    *done = true;
                }
                if parsed.get("type").and_then(|t| t.as_str()) == Some("message_stop") {
                    *done = true;
                }
            }
        } else if line.starts_with("event: ") {
            let event_type = line.strip_prefix("event: ").unwrap_or("");
            if event_type == "done" || event_type == "complete" || event_type == "error" {
                *done = true;
            }
        }
    }

    // ---- Sessions ----

    pub async fn list_sessions(&self) -> Result<serde_json::Value, CliError> {
        // Backend uses JSON-RPC 2.0 at POST /api/acp
        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "method": "session/list",
            "params": {},
            "id": 1
        });

        let req = self
            .request(reqwest::Method::POST, "/api/acp")
            .json(&body);
        let response = self.send_request(req).await?;
        let json_rpc: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;

        // Extract result from JSON-RPC response
        if let Some(result) = json_rpc.get("result") {
            Ok(result.clone())
        } else if let Some(error) = json_rpc.get("error") {
            Err(CliError::General(format!("API error: {}", error)))
        } else {
            Ok(json_rpc)
        }
    }

    pub async fn create_session(
        &self,
        provider: Option<&str>,
        cwd: Option<&str>,
    ) -> Result<serde_json::Value, CliError> {
        let mut body = serde_json::json!({
            "provider": provider.unwrap_or("bedrock"),
        });
        if let Some(c) = cwd {
            body["cwd"] = serde_json::Value::String(c.to_string());
        }
        // Include model_id in options if available
        if let Some(ref model) = self.model_id {
            body["options"] = serde_json::json!({
                "model": {
                    "model_id": model
                }
            });
        }

        let req = self
            .request(reqwest::Method::POST, "/api/sessions/new")
            .json(&body);

        let response = self.send_request(req).await?;
        let result: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;
        Ok(result)
    }

    pub async fn delete_session(&self, id: &str) -> Result<(), CliError> {
        // Try JSON-RPC method for session deletion
        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "method": "session/delete",
            "params": {"session_id": id},
            "id": 1
        });

        let req = self
            .request(reqwest::Method::POST, "/api/acp")
            .json(&body);

        let response = self.send_request(req).await?;
        let json_rpc: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;

        if let Some(error) = json_rpc.get("error") {
            let code = error.get("code").and_then(|c| c.as_i64()).unwrap_or(0);
            let msg = error
                .get("message")
                .and_then(|m| m.as_str())
                .unwrap_or("Unknown error");

            // -32601 = Method not found (JSON-RPC spec)
            // Treat as success since backend may not implement delete yet
            if code == -32601 {
                eprintln!("[WARNING] Session delete not supported by backend, session {} marked for deletion", id);
                return Ok(());
            }

            if msg.contains("not found") {
                return Err(CliError::NotFound(format!("Session {} not found", id)));
            }
            return Err(CliError::General(format!("Delete error: {}", msg)));
        }

        Ok(())
    }

    // ---- Messages ----

    pub async fn send_message(
        &self,
        session_id: &str,
        text: &str,
        provider: Option<&str>,
    ) -> Result<serde_json::Value, CliError> {
        let body = serde_json::json!({
            "session_id": session_id,
            "content": text,
            "provider": provider.unwrap_or("bedrock"),
        });

        let base_url = self.config.resolve_base_url();
        let streaming = self.streaming_client();
        let mut response = streaming
            .post(format!("{}/api/sessions/send", base_url))
            .header("Authorization", format!("Bearer {}", self.bearer_token))
            .header("Content-Type", "application/json")
            .json(&body)
            .send()
            .await
            .map_err(|e| {
                if e.is_connect() || e.is_timeout() {
                    CliError::NetworkError(format!(
                        "Network error: connection failed or timed out: {}",
                        e
                    ))
                } else {
                    CliError::NetworkError(format!("Network error: {}", e))
                }
            })?;

        let status = response.status();
        if !status.is_success() {
            let body_text = response.text().await.unwrap_or_default();
            return Err(Self::map_status_error(status, &body_text));
        }

        // Read SSE stream chunk by chunk with timeout
        let mut content = String::new();
        let mut buffer = String::new();
        let mut stream_done = false;
        let sse_timeout_secs = self.config.timeouts.sse_chunk_secs();

        use tokio::time::{timeout, Duration};

        while !stream_done {
            match timeout(Duration::from_secs(sse_timeout_secs), response.chunk()).await {
                Ok(Ok(Some(chunk))) => {
                    let chunk_str = String::from_utf8_lossy(&chunk);
                    buffer.push_str(&chunk_str);

                    while let Some(newline_pos) = buffer.find('\n') {
                        let line = buffer[..newline_pos].trim_end().to_string();
                        buffer = buffer[newline_pos + 1..].to_string();
                        Self::process_sse_line(&line, &mut content, &mut stream_done);
                    }
                }
                Ok(Ok(None)) => {
                    if !buffer.is_empty() {
                        Self::process_sse_line(&buffer, &mut content, &mut stream_done);
                    }
                    stream_done = true;
                }
                Ok(Err(e)) => {
                    if content.is_empty() {
                        return Err(CliError::General(format!("Failed to read response: {}", e)));
                    }
                    stream_done = true;
                }
                Err(_) => {
                    if content.is_empty() {
                        return Err(CliError::NetworkError(
                            "Timeout waiting for response".to_string(),
                        ));
                    }
                    stream_done = true;
                }
            }
        }

        if content.is_empty() {
            content = "[No response]".to_string();
        }

        Ok(serde_json::json!({
            "message": content,
            "role": "assistant"
        }))
    }

    pub async fn stream_events(&self, session_id: &str) -> Result<String, CliError> {
        let base_url = self.config.resolve_base_url();
        let url = format!("{}/api/sessions/{}/stream", base_url, session_id);

        let response = self
            .http_client
            .get(&url)
            .header("Authorization", format!("Bearer {}", self.bearer_token))
            .header("Accept", "text/event-stream")
            .send()
            .await
            .map_err(|e| {
                if e.is_connect() || e.is_timeout() {
                    CliError::NetworkError(format!("Network error: {}", e))
                } else {
                    CliError::NetworkError(format!("Network error: {}", e))
                }
            })?;

        let status = response.status();
        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            return Err(Self::map_status_error(status, &body));
        }

        let body = response
            .text()
            .await
            .map_err(|e| CliError::General(format!("Failed to read SSE stream: {}", e)))?;
        Ok(body)
    }

    // ---- Admin: Users ----

    pub async fn list_users(
        &self,
        limit: Option<u32>,
        offset: Option<u32>,
    ) -> Result<serde_json::Value, CliError> {
        let mut path = "/api/admin/users".to_string();
        let mut params = Vec::new();
        if let Some(l) = limit {
            params.push(format!("limit={}", l));
        }
        if let Some(o) = offset {
            params.push(format!("offset={}", o));
        }
        if !params.is_empty() {
            path = format!("{}?{}", path, params.join("&"));
        }

        let req = self.request(reqwest::Method::GET, &path);
        let response = self.send_request(req).await?;
        let body: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;
        Ok(body)
    }

    pub async fn create_user(
        &self,
        email: &str,
        provider: Option<&str>,
        temp_password: Option<&str>,
        no_email: bool,
    ) -> Result<serde_json::Value, CliError> {
        let mut body = serde_json::json!({
            "email": email,
            "auth_provider": provider.unwrap_or("cognito"),
        });
        if let Some(pw) = temp_password {
            body["temp_password"] = serde_json::Value::String(pw.to_string());
        }
        if no_email {
            body["suppress_invitation"] = serde_json::Value::Bool(true);
        }

        let req = self
            .request(reqwest::Method::POST, "/api/admin/users")
            .json(&body);

        let response = self.send_request(req).await?;
        let result: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;
        Ok(result)
    }

    pub async fn delete_user(&self, id: &str) -> Result<(), CliError> {
        let req = self.request(
            reqwest::Method::DELETE,
            &format!("/api/admin/users/{}", id),
        );
        self.send_request(req).await?;
        Ok(())
    }

    // ---- Admin: Tenants ----

    pub async fn list_tenants(&self) -> Result<serde_json::Value, CliError> {
        let req = self.request(reqwest::Method::GET, "/api/tenants");
        let response = self.send_request(req).await?;
        let body: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;
        Ok(body)
    }

    // ---- Admin: Settings ----

    pub async fn get_settings(&self) -> Result<serde_json::Value, CliError> {
        let req = self.request(reqwest::Method::GET, "/api/settings");
        let response = self.send_request(req).await?;
        let body: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;
        Ok(body)
    }

    pub async fn update_setting(
        &self,
        key: &str,
        value: &str,
    ) -> Result<serde_json::Value, CliError> {
        // Convert CLI key format (aws-region) to backend field name (aws_region)
        let backend_key = key.replace('-', "_");
        let body = serde_json::json!({
            backend_key: value,
        });

        let req = self
            .request(reqwest::Method::PUT, "/api/settings")
            .json(&body);

        let response = self.send_request(req).await?;
        let result: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;
        Ok(result)
    }

    // ---- Admin: Usage Logs ----

    pub async fn list_usage_logs(
        &self,
        user_email: Option<&str>,
        tenant: Option<&str>,
        model: Option<&str>,
        limit: Option<u32>,
        offset: Option<u32>,
    ) -> Result<serde_json::Value, CliError> {
        let mut path = "/api/admin/usage-logs".to_string();
        let mut params = Vec::new();
        if let Some(email) = user_email {
            params.push(format!(
                "user={}",
                url::form_urlencoded::byte_serialize(email.as_bytes()).collect::<String>()
            ));
        }
        if let Some(t) = tenant {
            params.push(format!("tenant={}", t));
        }
        if let Some(m) = model {
            params.push(format!("model={}", m));
        }
        if let Some(l) = limit {
            params.push(format!("limit={}", l));
        }
        if let Some(o) = offset {
            params.push(format!("offset={}", o));
        }
        if !params.is_empty() {
            path = format!("{}?{}", path, params.join("&"));
        }

        let req = self.request(reqwest::Method::GET, &path);
        let response = self.send_request(req).await?;
        let body: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;
        Ok(body)
    }

    // ---- Admin: User-Tenant management ----

    pub async fn add_tenant_to_user(
        &self,
        user_id: &str,
        tenant_id: &str,
        role: Option<&str>,
    ) -> Result<serde_json::Value, CliError> {
        let mut body = serde_json::json!({
            "tenant_id": tenant_id,
        });
        if let Some(r) = role {
            body["role"] = serde_json::Value::String(r.to_string());
        }

        let req = self
            .request(
                reqwest::Method::POST,
                &format!("/api/admin/users/{}/tenants", user_id),
            )
            .json(&body);

        let response = self.send_request(req).await?;
        let result: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;
        Ok(result)
    }

    pub async fn remove_tenant_from_user(
        &self,
        user_id: &str,
        tenant_id: &str,
    ) -> Result<(), CliError> {
        let req = self.request(
            reqwest::Method::DELETE,
            &format!("/api/admin/users/{}/tenants/{}", user_id, tenant_id),
        );
        self.send_request(req).await?;
        Ok(())
    }

    // ---- Teams ----

    pub async fn list_team_members(
        &self,
        org_id: &str,
        limit: Option<u32>,
        offset: Option<u32>,
    ) -> Result<serde_json::Value, CliError> {
        let mut path = format!("/api/teams/{}/members", org_id);
        let mut params = Vec::new();
        if let Some(l) = limit {
            params.push(format!("limit={}", l));
        }
        if let Some(o) = offset {
            params.push(format!("offset={}", o));
        }
        if !params.is_empty() {
            path = format!("{}?{}", path, params.join("&"));
        }

        let req = self.request(reqwest::Method::GET, &path);
        let response = self.send_request(req).await?;
        let body: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;
        Ok(body)
    }

    pub async fn get_team_usage(
        &self,
        org_id: &str,
        start: Option<&str>,
        end: Option<&str>,
    ) -> Result<serde_json::Value, CliError> {
        let mut path = format!("/api/teams/{}/usage", org_id);
        let mut params = Vec::new();
        if let Some(s) = start {
            params.push(format!("start={}", s));
        }
        if let Some(e) = end {
            params.push(format!("end={}", e));
        }
        if !params.is_empty() {
            path = format!("{}?{}", path, params.join("&"));
        }

        let req = self.request(reqwest::Method::GET, &path);
        let response = self.send_request(req).await?;
        let body: serde_json::Value = response
            .json()
            .await
            .map_err(|e| CliError::General(format!("Failed to parse response: {}", e)))?;
        Ok(body)
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
    unsafe { isatty(fd) != 0 }
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- Test: ApiClient construction ---

    #[test]
    fn test_api_client_new() {
        let config = AppConfig {
            client_id: "test".to_string(),
            cognito_domain: "https://auth.example.com".to_string(),
            redirect_port: 18080,
            redirect_host: "127.0.0.1".to_string(),
            redirect_uri: "http://127.0.0.1:18080/callback".to_string(),
            api_endpoint: "http://localhost:8000".to_string(),
            default_model: None,
            config_dir: std::path::PathBuf::from("/tmp/test"),
            timeouts: crate::config::Timeouts::default(),
        };

        let client = ApiClient::new(config, "test-token".to_string());
        // Just verify construction succeeds without panic
        let _ = client;
    }

    // --- Test: map_status_error ---

    #[test]
    fn test_map_status_error_401() {
        let err = ApiClient::map_status_error(
            reqwest::StatusCode::UNAUTHORIZED,
            "unauthorized",
        );
        match err {
            CliError::AuthExpired(msg) => assert!(msg.contains("401")),
            other => panic!("Expected AuthExpired, got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_403() {
        let err = ApiClient::map_status_error(
            reqwest::StatusCode::FORBIDDEN,
            "forbidden",
        );
        match err {
            CliError::PermissionDenied(msg) => assert!(msg.contains("403")),
            other => panic!("Expected PermissionDenied, got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_404() {
        let err = ApiClient::map_status_error(
            reqwest::StatusCode::NOT_FOUND,
            "not found",
        );
        match err {
            CliError::NotFound(msg) => assert!(msg.contains("404")),
            other => panic!("Expected NotFound, got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_429() {
        let err = ApiClient::map_status_error(
            reqwest::StatusCode::TOO_MANY_REQUESTS,
            "rate limited",
        );
        match err {
            CliError::RateLimited(msg) => assert!(msg.contains("429")),
            other => panic!("Expected RateLimited, got: {:?}", other),
        }
    }

    #[test]
    fn test_map_status_error_500() {
        let err = ApiClient::map_status_error(
            reqwest::StatusCode::INTERNAL_SERVER_ERROR,
            "internal error",
        );
        match err {
            CliError::ServerError(msg) => assert!(msg.contains("500")),
            other => panic!("Expected ServerError, got: {:?}", other),
        }
    }

    // --- Test: process_sse_line ---

    #[test]
    fn test_process_sse_line_text_field() {
        let mut content = String::new();
        let mut done = false;
        ApiClient::process_sse_line(
            r#"data: {"text":"hello world"}"#,
            &mut content,
            &mut done,
        );
        assert_eq!(content, "hello world");
        assert!(!done);
    }

    #[test]
    fn test_process_sse_line_delta_text() {
        let mut content = String::new();
        let mut done = false;
        ApiClient::process_sse_line(
            r#"data: {"delta":{"text":"chunk"}}"#,
            &mut content,
            &mut done,
        );
        assert_eq!(content, "chunk");
    }

    #[test]
    fn test_process_sse_line_message_stop() {
        let mut content = String::new();
        let mut done = false;
        ApiClient::process_sse_line(
            r#"data: {"type":"message_stop"}"#,
            &mut content,
            &mut done,
        );
        assert!(done);
    }

    #[test]
    fn test_process_sse_line_event_done() {
        let mut content = String::new();
        let mut done = false;
        ApiClient::process_sse_line("event: done", &mut content, &mut done);
        assert!(done);
    }

    #[test]
    fn test_process_sse_line_error() {
        let mut content = String::new();
        let mut done = false;
        ApiClient::process_sse_line(
            r#"data: {"error":"something went wrong"}"#,
            &mut content,
            &mut done,
        );
        assert!(content.contains("something went wrong"));
        assert!(done);
    }

    // --- Test: add_tenant_to_user and remove_tenant_from_user methods exist ---
    // (Integration tests require a running backend, but we verify compilation)

    #[test]
    fn test_api_client_has_tenant_methods() {
        // Verify that add_tenant_to_user and remove_tenant_from_user compile
        let config = AppConfig {
            client_id: "test".to_string(),
            cognito_domain: "https://auth.example.com".to_string(),
            redirect_port: 18080,
            redirect_host: "127.0.0.1".to_string(),
            redirect_uri: "http://127.0.0.1:18080/callback".to_string(),
            api_endpoint: "http://localhost:8000".to_string(),
            default_model: None,
            config_dir: std::path::PathBuf::from("/tmp/test"),
            timeouts: crate::config::Timeouts::default(),
        };

        let client = ApiClient::new(config, "test-token".to_string());

        // These function pointers verify the methods exist with correct signatures
        let _add_fn = ApiClient::add_tenant_to_user;
        let _remove_fn = ApiClient::remove_tenant_from_user;
        let _ = client;
    }
}
