//! MVP API client: authorization header injection + 401 handling.
//!
//! すべての Phase 2 サブコマンド (admin / team-lead / usage) はこのヘルパーを経由する。
//! 401 を検知したら「`stratoclave auth login` で再ログインしてください」を促す。

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

    /// DELETE は通常 204 body 無しなので bool 的に返す。
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
