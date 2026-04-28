//! Phase S: AWS SSO / STS 経由ログイン.
//!
//! フロー:
//!   1. AWS credentials provider chain (profile / env / EC2 metadata) から
//!      (access_key, secret_key, session_token) をロード
//!   2. sts:GetCallerIdentity を sigv4 署名した POST リクエストを組み立てる
//!   3. Backend `/api/mvp/auth/sso-exchange` に method/url/headers を転送
//!   4. Backend が STS を叩いて身元確認 → Cognito access_token を返却
//!   5. `~/.stratoclave/mvp_tokens.json` に保存

use anyhow::{anyhow, bail, Context, Result};
use aws_config::BehaviorVersion;
use aws_credential_types::provider::ProvideCredentials;
use aws_sigv4::http_request::{sign, SignableBody, SignableRequest, SigningSettings};
use aws_sigv4::sign::v4;
use http::Method;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use super::config::MvpConfig;
use super::tokens::{save, MvpTokens};

#[derive(Debug)]
pub struct SsoLoginOptions {
    pub profile: Option<String>,
    pub region: Option<String>,
}

#[derive(Debug, Serialize)]
struct SsoExchangeRequest {
    method: String,
    url: String,
    headers: HashMap<String, String>,
    body: String,
}

#[derive(Debug, Deserialize)]
struct SsoExchangeResponse {
    #[serde(default)]
    status: String,
    access_token: String,
    #[serde(default)]
    id_token: Option<String>,
    #[serde(default)]
    refresh_token: Option<String>,
    #[serde(default)]
    expires_in: Option<u64>,
    email: String,
    user_id: String,
    roles: Vec<String>,
    org_id: String,
    identity_type: String,
    new_user: bool,
}

pub async fn login(opts: SsoLoginOptions) -> Result<()> {
    let config = MvpConfig::load()?;

    // 1. AWS credentials
    let mut loader = aws_config::defaults(BehaviorVersion::latest());
    if let Some(p) = opts.profile.clone() {
        loader = loader.profile_name(p);
    }
    let region_str = opts
        .region
        .clone()
        .or_else(|| std::env::var("AWS_REGION").ok())
        .unwrap_or_else(|| "us-east-1".to_string());
    loader = loader.region(aws_config::Region::new(region_str.clone()));

    eprintln!("[INFO] Loading AWS credentials (profile={}, region={})...",
        opts.profile.as_deref().unwrap_or("default"),
        region_str,
    );
    let sdk_config = loader.load().await;
    let creds_provider = sdk_config
        .credentials_provider()
        .ok_or_else(|| anyhow!("No AWS credentials provider available"))?;
    let creds = creds_provider
        .provide_credentials()
        .await
        .context("Failed to resolve AWS credentials. Run `aws sso login` or set AWS_PROFILE.")?;

    // 2. sts:GetCallerIdentity の署名済みリクエストを生成
    //    (POST form body: Action=GetCallerIdentity&Version=2011-06-15)
    //    STS は us-east-1 グローバルを使用 (regional endpoint でも可、ここではシンプルに us-east-1)
    let sts_region = region_str.clone();
    let sts_host = format!("sts.{}.amazonaws.com", sts_region);
    let url = format!("https://{}/", sts_host);
    let body = "Action=GetCallerIdentity&Version=2011-06-15";

    let settings = SigningSettings::default();
    let identity = creds.into();
    let sign_params = v4::SigningParams::builder()
        .identity(&identity)
        .region(&sts_region)
        .name("sts")
        .time(SystemTime::now())
        .settings(settings)
        .build()?
        .into();

    let mut headers_map: HashMap<String, String> = HashMap::new();
    headers_map.insert(
        "Content-Type".to_string(),
        "application/x-www-form-urlencoded".to_string(),
    );
    headers_map.insert("Host".to_string(), sts_host.clone());

    let signable = SignableRequest::new(
        "POST",
        url.as_str(),
        headers_map.iter().map(|(k, v)| (k.as_str(), v.as_str())),
        SignableBody::Bytes(body.as_bytes()),
    )?;
    let (signing_instructions, _signature) =
        sign(signable, &sign_params)?.into_parts();

    // 署名済みヘッダーを取得
    let mut req = http::Request::builder()
        .method(Method::POST)
        .uri(url.clone())
        .body(body.to_string())?;
    for (k, v) in headers_map.iter() {
        req.headers_mut().insert(
            http::HeaderName::try_from(k.as_str())?,
            http::HeaderValue::from_str(v)?,
        );
    }
    signing_instructions.apply_to_request_http1x(&mut req);

    // リクエストから headers を抽出して HashMap 化 (Backend に送る形式)
    let mut signed_headers: HashMap<String, String> = HashMap::new();
    for (name, value) in req.headers() {
        signed_headers.insert(
            name.as_str().to_string(),
            value.to_str()?.to_string(),
        );
    }

    // 3. Backend に転送
    eprintln!("[INFO] Presenting identity to Stratoclave backend...");
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()?;
    let endpoint = format!("{}/api/mvp/auth/sso-exchange", config.api_endpoint);
    let resp = client
        .post(&endpoint)
        .json(&SsoExchangeRequest {
            method: "POST".to_string(),
            url,
            headers: signed_headers,
            body: body.to_string(),
        })
        .send()
        .await
        .context("POST /api/mvp/auth/sso-exchange failed")?;

    let status = resp.status();
    if !status.is_success() {
        let body: serde_json::Value = resp.json().await.unwrap_or_default();
        let detail = match body.get("detail") {
            Some(v) if v.is_string() => v.as_str().unwrap_or("unknown error").to_string(),
            Some(v) => serde_json::to_string(v).unwrap_or_else(|_| "unknown error".into()),
            None => "unknown error".into(),
        };
        bail!("SSO login failed ({}): {}", status, detail);
    }

    let login: SsoExchangeResponse = resp.json().await?;
    if login.status != "authenticated" {
        bail!("Unexpected status from backend: {}", login.status);
    }

    // 4. トークン保存
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)?
        .as_secs();
    let expires = login.expires_in.unwrap_or(3600);
    let tokens = MvpTokens {
        access_token: login.access_token,
        id_token: login.id_token,
        refresh_token: login.refresh_token,
        expires_at: now + expires,
        email: login.email.clone(),
    };
    save(&tokens)?;

    let marker = if login.new_user { " (new user provisioned)" } else { "" };
    eprintln!(
        "[OK] Signed in via {} as {}{}",
        login.identity_type, login.email, marker,
    );
    eprintln!("     org_id={} roles={:?}", login.org_id, login.roles);
    eprintln!("     user_id={}", login.user_id);
    eprintln!("     token saved to ~/.stratoclave/mvp_tokens.json");
    Ok(())
}
