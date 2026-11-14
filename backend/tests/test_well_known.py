"""Well-known discovery endpoint contract (docs-driven).

ARCHITECTURE.md documents the `/.well-known/stratoclave-config` shape.
These tests lock the contract so CLI consumers can rely on it:

  - schema_version is "1".
  - required fields are present.
  - no field value leaks secrets (password / aws_secret_access_key /
    private keys etc.).
  - Cache-Control is no-store: the document says where a CLI sends credentials.
  - Endpoint is reachable without Authorization.
"""
from __future__ import annotations

import os
import re

from fastapi.testclient import TestClient


def _make_client(monkeypatch):
    """Build a minimal FastAPI app around the well-known router with all
    required environment variables set."""
    from fastapi import FastAPI

    # Populate the env the router needs to render a successful response.
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_ABCDEFGHI")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "1234567890abcdefghijklmnop")
    monkeypatch.setenv(
        "COGNITO_DOMAIN",
        "https://stratoclave-auth-testing.auth.us-east-1.amazoncognito.com",
    )
    monkeypatch.setenv("COGNITO_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv(
        "STRATOCLAVE_API_ENDPOINT", "https://example-deployment.cloudfront.net"
    )

    from mvp.well_known import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_well_known_returns_expected_shape(monkeypatch):
    client = _make_client(monkeypatch)
    resp = client.get("/.well-known/stratoclave-config")
    assert resp.status_code == 200
    body = resp.json()

    # schema_version must be exactly "1" per docs.
    assert body["schema_version"] == "1"

    # Required top-level fields.
    assert body["api_endpoint"].startswith(("http://", "https://"))
    assert "cognito" in body
    assert "cli" in body

    # Cognito info: all four fields present (documented contract).
    cognito = body["cognito"]
    for key in ("user_pool_id", "client_id", "domain", "region"):
        assert key in cognito, f"cognito.{key} missing"
        assert isinstance(cognito[key], str) and cognito[key]

    # CLI defaults: at minimum default_model and callback_port.
    assert "default_model" in body["cli"]
    assert "callback_port" in body["cli"]


def test_well_known_is_unauthenticated(monkeypatch):
    """The endpoint must be reachable without any Authorization header."""
    client = _make_client(monkeypatch)
    resp = client.get("/.well-known/stratoclave-config")
    assert resp.status_code == 200


def test_well_known_sets_cache_control(monkeypatch):
    client = _make_client(monkeypatch)
    resp = client.get("/.well-known/stratoclave-config")
    cc = resp.headers.get("cache-control", "").lower()
    assert cc == "no-store"


def test_outside_a_local_environment_the_endpoint_is_never_taken_from_request_headers(monkeypatch):
    """Without STRATOCLAVE_API_ENDPOINT, a deployment refuses (503, as for any
    missing setting) rather than hand a CLI a URL named by X-Forwarded-Host."""
    client = _make_client(monkeypatch)
    monkeypatch.delenv("STRATOCLAVE_API_ENDPOINT", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    resp = client.get("/.well-known/stratoclave-config",
                      headers={"X-Forwarded-Host": "attacker.example", "X-Forwarded-Proto": "https"})
    assert resp.status_code == 503
    assert "STRATOCLAVE_API_ENDPOINT" in resp.text
    assert "attacker.example" not in resp.text


def test_in_a_local_environment_the_endpoint_may_come_from_request_headers(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.delenv("STRATOCLAVE_API_ENDPOINT", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "development")
    resp = client.get("/.well-known/stratoclave-config",
                      headers={"X-Forwarded-Host": "gw.local", "X-Forwarded-Proto": "http"})
    assert resp.status_code == 200 and resp.json()["api_endpoint"] == "http://gw.local"


_SECRET_RE = re.compile(
    r"(password|private_key|aws_secret_access_key|-----BEGIN)",
    re.IGNORECASE,
)


def test_well_known_does_not_leak_secrets(monkeypatch):
    """The full response body must never contain obvious secret-shaped
    strings. This is a regression fence, not a cryptographic proof.
    """
    client = _make_client(monkeypatch)
    resp = client.get("/.well-known/stratoclave-config")
    assert not _SECRET_RE.search(resp.text), (
        "Well-known response leaked a secret-shaped substring; "
        "check well_known.py for accidental inclusion."
    )


def test_well_known_returns_503_when_misconfigured(monkeypatch):
    """If a required env var is missing the endpoint must fail closed
    with 503, not silently return partial data.
    """
    # Clear all required env so _require_env fails.
    for key in (
        "COGNITO_USER_POOL_ID",
        "COGNITO_CLIENT_ID",
        "COGNITO_DOMAIN",
    ):
        monkeypatch.delenv(key, raising=False)
    # Keep region available so derive_api_endpoint does not blow up first.
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("COGNITO_REGION", "us-east-1")

    from fastapi import FastAPI

    from mvp.well_known import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.get("/.well-known/stratoclave-config")
    assert resp.status_code == 503
