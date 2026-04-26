//! Authentication provider trait and common types.
//!
//! Phase 2 (v2.1) 以降は `mvp/*` が主経路で、本 trait は pipe / chat / ui 経由の legacy 用。
#![allow(dead_code)]

use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::future::Future;
use std::pin::Pin;

/// Authentication provider trait
pub trait AuthProvider: Send + Sync {
    /// Authenticate and return a token for Stratoclave backend
    fn authenticate<'a>(
        &'a self,
        config: &'a crate::config::AppConfig,
    ) -> Pin<Box<dyn Future<Output = Result<AuthToken>> + Send + 'a>>;

    /// Check if saved tokens are valid
    fn is_token_valid(&self, tokens: &SavedTokens) -> bool;

    /// Refresh tokens if possible (returns None if refresh not supported)
    fn refresh<'a>(
        &'a self,
        config: &'a crate::config::AppConfig,
        tokens: &'a SavedTokens,
    ) -> Pin<Box<dyn Future<Output = Result<Option<AuthToken>>> + Send + 'a>>;
}

/// Authentication method
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AuthMethod {
    /// Cognito User Pool (Browser/Device OIDC flow)
    Cognito,
    /// saml2aws CLI tool integration
    Saml2Aws,
    /// AWS Profile (future extension)
    AwsProfile,
}

impl Default for AuthMethod {
    fn default() -> Self {
        AuthMethod::Cognito
    }
}

/// Authentication token returned by providers
#[derive(Clone, Debug)]
pub struct AuthToken {
    /// Bearer token (JWT) to send to Stratoclave backend
    pub bearer_token: String,
    /// Token expiration timestamp (Unix seconds)
    pub expires_at: Option<u64>,
    /// Refresh token (if available)
    pub refresh_token: Option<String>,
    /// Authentication method used to obtain this token
    pub method: AuthMethod,
}

/// Saved tokens structure (matches existing tokens.json format)
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SavedTokens {
    pub access_token: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub id_token: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub refresh_token: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<u64>,
    #[serde(default)]
    pub method: AuthMethod,
}
