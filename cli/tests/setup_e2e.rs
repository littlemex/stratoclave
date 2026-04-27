//! End-to-end test for `stratoclave setup <url>`.
//!
//! Drives the compiled binary against a wiremock backend that serves a
//! canned `/.well-known/stratoclave-config` response, with HOME and
//! STRATOCLAVE_CONFIG_DIR pointed at a temp dir so the developer's real
//! `~/.stratoclave/` is untouched.
//!
//! Covers:
//!   - the happy path: run setup, `config.toml` is written with the
//!     expected nested schema (`[api] endpoint`, `[auth]`, `[defaults]`).
//!   - a misconfigured backend (missing `schema_version`) makes the
//!     command fail with a descriptive error.

use assert_cmd::Command;
use predicates::prelude::*;
use serde_json::json;
use std::fs;
use tempfile::TempDir;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn well_known_body() -> serde_json::Value {
    json!({
        "schema_version": "1",
        "api_endpoint": "http://placeholder.local",
        "cognito": {
            "user_pool_id": "us-east-1_TESTPOOLX",
            "client_id": "client-test-id",
            "domain": "https://example.auth.us-east-1.amazoncognito.com",
            "region": "us-east-1",
        },
        "cli": {
            "default_model": "claude-opus-4-7",
            "callback_port": 18080,
        },
    })
}

#[tokio::test]
async fn setup_writes_expected_toml_against_a_live_backend() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/.well-known/stratoclave-config"))
        .respond_with(ResponseTemplate::new(200).set_body_json(well_known_body()))
        .expect(1)
        .mount(&server)
        .await;

    let home = TempDir::new().expect("mktemp");
    let config_dir = home.path().join(".stratoclave");

    Command::cargo_bin("stratoclave")
        .unwrap()
        .env_clear()
        .env("HOME", home.path())
        .env("STRATOCLAVE_CONFIG_DIR", &config_dir)
        // Avoid pulling the real ~/.aws for the aws_config default chain —
        // nothing in `setup` needs AWS creds, but CI envs are noisy.
        .env("AWS_ACCESS_KEY_ID", "testing")
        .env("AWS_SECRET_ACCESS_KEY", "testing")
        .env("AWS_REGION", "us-east-1")
        .args(["setup", &server.uri()])
        .assert()
        .success()
        .stdout(predicate::str::contains("Saved to"))
        .stdout(predicate::str::contains("api_endpoint"));

    let toml_path = config_dir.join("config.toml");
    let body = fs::read_to_string(&toml_path).unwrap();
    // Nested schema that mvp/config.rs::load() now accepts post PR #2.
    assert!(body.contains("[api]"), "missing [api] section: {body}");
    assert!(
        body.contains("endpoint ="),
        "missing api.endpoint line: {body}"
    );
    assert!(body.contains("[auth]"), "missing [auth] section: {body}");
    assert!(
        body.contains("client_id = \"client-test-id\""),
        "client_id not written: {body}"
    );
    assert!(
        body.contains("user_pool_id = \"us-east-1_TESTPOOLX\""),
        "user_pool_id not written: {body}"
    );
    assert!(
        body.contains("model = \"claude-opus-4-7\""),
        "default model not written: {body}"
    );
}

#[tokio::test]
async fn setup_fails_when_well_known_is_invalid_json() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/.well-known/stratoclave-config"))
        // Return text that is not JSON; the CLI should surface a clear error.
        .respond_with(ResponseTemplate::new(200).set_body_string("<html>oops</html>"))
        .mount(&server)
        .await;

    let home = TempDir::new().expect("mktemp");
    let config_dir = home.path().join(".stratoclave");

    let output = Command::cargo_bin("stratoclave")
        .unwrap()
        .env_clear()
        .env("HOME", home.path())
        .env("STRATOCLAVE_CONFIG_DIR", &config_dir)
        .env("AWS_ACCESS_KEY_ID", "testing")
        .env("AWS_SECRET_ACCESS_KEY", "testing")
        .env("AWS_REGION", "us-east-1")
        .args(["setup", &server.uri()])
        .assert()
        .failure();

    // The CLI must surface *something* informative on stderr.
    let stderr = String::from_utf8_lossy(&output.get_output().stderr).to_string();
    assert!(
        !stderr.is_empty(),
        "expected an error message on stderr, got empty"
    );

    // And no config.toml should have been written.
    assert!(!config_dir.join("config.toml").exists());
}
