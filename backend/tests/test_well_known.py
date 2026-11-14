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
    # The 503 itself is never cached, same as the 200 it would have been --
    # a shared cache serving a stale 503 to a different client is as wrong as
    # serving a stale endpoint.
    assert resp.headers.get("cache-control", "").lower() == "no-store"


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


def test_well_known_both_mode_selects_by_forwarded_port(monkeypatch):
    """STRATOCLAVE_ENTRY=both (iac/bin/iac.ts) sets both
    STRATOCLAVE_API_ENDPOINT (CloudFront) and STRATOCLAVE_API_ENDPOINT_PRIVATE
    (PrivateLink) on the shared ALB, plus STRATOCLAVE_BOTH_CLOUDFRONT_PORT —
    the CloudFront listener port `core.forwarded_port.is_cloudfront_leg`
    positively matches against (set here to mirror
    `lib/_common.ts`'s `BOTH_MODE_CLOUDFRONT_LISTENER_PORT`). Only a
    request POSITIVELY identified as the CloudFront leg (`X-Forwarded-Port`
    carrying exactly one value equal to this port) gets the CloudFront URL;
    every other case — the confirmed PrivateLink port, AND round-1
    implementation review's fail-closed default for anything ambiguous,
    including no header at all — gets the PrivateLink URL instead. This
    reverses what an earlier version of this test asserted for the
    no-header case (it used to default to the CloudFront URL): the
    coordinator's decision was that the safe default when the leg is
    ambiguous is the PRIVATE url, because a CloudFront client wrongly
    handed it simply cannot resolve or reach it and fails loudly, which is
    the safer direction to be wrong in than a PrivateLink client being
    handed the public CloudFront URL.
    """
    client = _make_client(monkeypatch)
    monkeypatch.setenv("STRATOCLAVE_BOTH_CLOUDFRONT_PORT", "80")
    monkeypatch.setenv(
        "STRATOCLAVE_API_ENDPOINT_PRIVATE",
        "https://stratoclave.internal.example.com",
    )

    resp_private_leg = client.get(
        "/.well-known/stratoclave-config", headers={"x-forwarded-port": "443"}
    )
    assert (
        resp_private_leg.json()["api_endpoint"]
        == "https://stratoclave.internal.example.com"
    )

    resp_cloudfront_leg = client.get(
        "/.well-known/stratoclave-config", headers={"x-forwarded-port": "80"}
    )
    assert (
        resp_cloudfront_leg.json()["api_endpoint"]
        == "https://example-deployment.cloudfront.net"
    )

    # No header at all is AMBIGUOUS, not "clearly CloudFront" — the safe
    # default is the PrivateLink URL (see the docstring above).
    resp_no_port_header = client.get("/.well-known/stratoclave-config")
    assert (
        resp_no_port_header.json()["api_endpoint"]
        == "https://stratoclave.internal.example.com"
    )

    # A malformed/unrecognised port value is likewise ambiguous, not
    # positively CloudFront.
    resp_malformed_port = client.get(
        "/.well-known/stratoclave-config", headers={"x-forwarded-port": "31337"}
    )
    assert (
        resp_malformed_port.json()["api_endpoint"]
        == "https://stratoclave.internal.example.com"
    )


def test_well_known_both_mode_with_no_configured_cloudfront_port_keeps_old_images_cloudfront_default(
    monkeypatch,
):
    """`STRATOCLAVE_BOTH_CLOUDFRONT_PORT` is unset on every entry mode but
    `both` (`bin/iac.ts`) — and, within `both`, on a backend image old
    enough to predate it. `core.forwarded_port.is_cloudfront_leg` degrades
    to "every request is the CloudFront leg" in that case, which is the
    exact behaviour `STRATOCLAVE_API_ENDPOINT_PRIVATE`-unset deployments
    already relied on before this feature existed — pinned here with
    `STRATOCLAVE_API_ENDPOINT_PRIVATE` SET, to isolate this one variable's
    effect from that unset-private-endpoint case
    (`test_well_known_single_endpoint_mode_ignores_forwarded_port` below)."""
    client = _make_client(monkeypatch)
    monkeypatch.delenv("STRATOCLAVE_BOTH_CLOUDFRONT_PORT", raising=False)
    monkeypatch.setenv(
        "STRATOCLAVE_API_ENDPOINT_PRIVATE",
        "https://stratoclave.internal.example.com",
    )
    resp = client.get(
        "/.well-known/stratoclave-config", headers={"x-forwarded-port": "443"}
    )
    assert resp.json()["api_endpoint"] == "https://example-deployment.cloudfront.net"


def test_well_known_single_endpoint_mode_ignores_forwarded_port(monkeypatch):
    """Outside STRATOCLAVE_ENTRY=both, STRATOCLAVE_API_ENDPOINT_PRIVATE is
    unset, so the port-based selector never engages — same behaviour as
    before this feature, regardless of what X-Forwarded-Port says."""
    client = _make_client(monkeypatch)
    resp = client.get(
        "/.well-known/stratoclave-config", headers={"x-forwarded-port": "443"}
    )
    assert resp.json()["api_endpoint"] == "https://example-deployment.cloudfront.net"


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
    assert resp.headers.get("cache-control", "").lower() == "no-store"
