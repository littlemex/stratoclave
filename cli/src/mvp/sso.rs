//! Phase S: Login via AWS SSO / STS.
//!
//! Flow:
//!   1. Load (access_key, secret_key, session_token) from the AWS credentials
//!      provider chain (profile / env / EC2 metadata)
//!   2. Build a SigV4-signed POST request for sts:GetCallerIdentity
//!   3. Forward method/url/headers to the backend at `/api/mvp/auth/sso-exchange`
//!   4. The backend calls STS to verify identity and returns a Cognito access_token
//!   5. Save the token to `~/.stratoclave/mvp_tokens.json`

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

/// The host of the gateway URL this CLI sends the exchange to: the audience the
/// signed GetCallerIdentity is bound to. The invariant that matters is that the
/// audience and the URL the request is POSTed to are derived from the SAME
/// value (`config.api_endpoint`, bound once in `login` below and passed to
/// both this function and the exchange URL) -- not that a request is somehow
/// scoped to one service or one path, which this header has no mechanism to
/// express at all: binding is to an ALLOWED HOST (this gateway's own, or any
/// of `STRATOCLAVE_STS_AUDIENCES`), nothing narrower.
fn audience_host(api_endpoint: &str) -> Result<String> {
    let url = reqwest::Url::parse(api_endpoint)
        .with_context(|| format!("api_endpoint is not a URL: {api_endpoint}"))?;
    let host = url
        .host_str()
        .ok_or_else(|| anyhow!("api_endpoint has no host: {api_endpoint}"))?
        .trim_end_matches('.')
        .to_ascii_lowercase();
    Ok(match url.port() {
        Some(port) => format!("{host}:{port}"),
        None => host,
    })
}

pub async fn login(opts: SsoLoginOptions) -> Result<()> {
    let config = MvpConfig::load()?;
    // Bound once, used for both the audience header below and the exchange
    // URL further down: the invariant `audience_host`'s own doc comment
    // states (audience and POST target come from the same value) only holds
    // if there is one place in this function that reads `config.api_endpoint`
    // at all, not two call sites that happen to agree today.
    let endpoint = &config.api_endpoint;

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

    // 2. Build the signed request for sts:GetCallerIdentity
    //    (POST form body: Action=GetCallerIdentity&Version=2011-06-15)
    //    Using the us-east-1 global STS endpoint (a regional endpoint also works;
    //    us-east-1 is used here for simplicity)
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
    // Bind the signed request to the host it is sent to: the header is signed, so
    // STS refuses any change to it, and a gateway that requires the binding
    // refuses a request naming a host that is not its own, which stops a
    // GetCallerIdentity signed for another service (or another gateway) from
    // being replayed there.
    headers_map.insert(
        "X-Stratoclave-Audience".to_string(),
        audience_host(endpoint)?,
    );

    let signable = SignableRequest::new(
        "POST",
        url.as_str(),
        headers_map.iter().map(|(k, v)| (k.as_str(), v.as_str())),
        SignableBody::Bytes(body.as_bytes()),
    )?;
    let (signing_instructions, _signature) =
        sign(signable, &sign_params)?.into_parts();

    // Retrieve the signed headers
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

    // Extract headers from the request into a HashMap (the format the backend expects)
    let mut signed_headers: HashMap<String, String> = HashMap::new();
    for (name, value) in req.headers() {
        signed_headers.insert(
            name.as_str().to_string(),
            value.to_str()?.to_string(),
        );
    }

    // 3. Forward to backend
    eprintln!("[INFO] Presenting identity to Stratoclave backend...");
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .user_agent(concat!("stratoclave-cli/", env!("CARGO_PKG_VERSION")))
        .build()?;
    let exchange_url = format!("{}/api/mvp/auth/sso-exchange", endpoint);
    let resp = client
        .post(&exchange_url)
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

    // 4. Save tokens
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

#[cfg(test)]
mod audience_tests {
    use super::audience_host;

    #[test]
    fn the_audience_is_the_lowercased_host_of_the_configured_endpoint() {
        assert_eq!(audience_host("https://D1.CloudFront.net").unwrap(), "d1.cloudfront.net");
        assert_eq!(audience_host("https://gw.internal.example.com/").unwrap(), "gw.internal.example.com");
        assert_eq!(audience_host("http://127.0.0.1:8080").unwrap(), "127.0.0.1:8080");
        assert!(audience_host("not a url").is_err());
    }

    /// The audience header and the exchange URL (`login`'s `exchange_url`)
    /// are both derived from one bound `endpoint`, not from two call sites
    /// that merely happen to read the same config field today. This proves
    /// the invariant holds structurally -- the SAME string, run through
    /// `audience_host` and through the exchange URL's own host -- rather
    /// than reading `login`'s source by eye, which cannot run `login` itself
    /// (it needs live AWS credentials and network access).
    #[test]
    fn the_audience_and_the_exchange_url_name_the_same_host() {
        for endpoint in [
            "https://d1.cloudfront.net",
            "https://stratoclave.internal.example.com/",
            "http://127.0.0.1:8080",
        ] {
            let audience = audience_host(endpoint).unwrap();
            let exchange_url = format!("{endpoint}/api/mvp/auth/sso-exchange");
            let exchange_host = audience_host(&exchange_url).unwrap();
            assert_eq!(
                audience, exchange_host,
                "audience_host({endpoint:?}) and the exchange URL's own host disagree"
            );
        }
    }

    /// Host normalisation shared with `backend/core/deployment.py::host_of`,
    /// read from the one vectors file both sides check against
    /// (`contracts/host_normalization_vectors.json`) so the two
    /// implementations cannot silently drift apart case by case.
    #[test]
    fn matches_the_shared_host_normalization_vectors() {
        #[derive(serde::Deserialize)]
        struct Case { url: String, host: String }
        #[derive(serde::Deserialize)]
        struct Vectors { cases: Vec<Case> }

        let raw = include_str!("../../../contracts/host_normalization_vectors.json");
        let vectors: Vectors = serde_json::from_str(raw).expect("vectors file must parse");
        assert!(!vectors.cases.is_empty());
        for case in vectors.cases {
            assert_eq!(
                audience_host(&case.url).unwrap(),
                case.host,
                "audience_host({:?}) did not match the shared vector",
                case.url,
            );
        }
    }
}
