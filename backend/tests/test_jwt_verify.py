"""JWT verification contract for Cognito access tokens.

Guards the rules from deps.py:_decode_cognito_access_token:

  - Only access_token is accepted (id_token / refresh_token rejected).
  - `client_id` claim must equal OIDC_AUDIENCE / COGNITO_CLIENT_ID.
  - Issuer is verified.
  - alg must be RS256 (enforced by the `algorithms=["RS256"]` list).
  - Expired tokens are rejected.

We sign tokens with an in-memory RSA key and point PyJWKClient at a local
HTTP server serving a JWKS that lists the public key.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Iterator

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException


@dataclass
class JwksServer:
    url: str
    kid: str
    private_pem: bytes
    shutdown: callable


def _pick_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def jwks_server() -> Iterator[JwksServer]:
    """Spin up a minimal HTTP server that serves a JWKS document with
    one RSA key, and tear it down after the test."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = key.public_key().public_numbers()

    def b64url_uint(v: int) -> str:
        import base64

        n_bytes = v.to_bytes((v.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(n_bytes).rstrip(b"=").decode("ascii")

    kid = "test-key-1"
    jwks_doc = {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": b64url_uint(public_numbers.n),
                "e": b64url_uint(public_numbers.e),
            }
        ]
    }

    port = _pick_free_port()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):  # quiet the test output
            pass

        def do_GET(self):  # noqa: N802  (stdlib API)
            # PyJWKClient fetches `<issuer>/.well-known/jwks.json`; serve any
            # path so we do not have to stitch the issuer segment here.
            body = json.dumps(jwks_doc).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    yield JwksServer(
        url=f"http://127.0.0.1:{port}",
        kid=kid,
        private_pem=private_pem,
        shutdown=srv.shutdown,
    )

    srv.shutdown()
    thread.join(timeout=2)


def _sign(payload: dict, jwks: JwksServer, alg: str = "RS256") -> str:
    return pyjwt.encode(
        payload,
        jwks.private_pem,
        algorithm=alg,
        headers={"kid": jwks.kid},
    )


def _configure_env(monkeypatch, jwks: JwksServer):
    # The issuer must be reachable via HTTP because PyJWKClient fetches
    # `<issuer>/.well-known/jwks.json` at verify time. We point it at the
    # in-process test server so the decoder really runs end-to-end.
    monkeypatch.setenv("OIDC_ISSUER_URL", jwks.url)
    monkeypatch.setenv("OIDC_AUDIENCE", "client-id-XYZ")
    # Force a fresh PyJWKClient instance so it picks up the new URL.
    from mvp import deps

    deps._jwks_client.cache_clear()


def _call_decoder(token: str):
    from mvp.deps import _decode_cognito_access_token

    return _decode_cognito_access_token(token)


def test_valid_access_token_is_accepted(monkeypatch, jwks_server: JwksServer):
    _configure_env(monkeypatch, jwks_server)
    now = int(time.time())
    claims = _sign(
        {
            "sub": "user-abc",
            "iss": jwks_server.url,
            "client_id": "client-id-XYZ",
            "token_use": "access",
            "exp": now + 3600,
            "iat": now,
        },
        jwks_server,
    )
    decoded = _call_decoder(claims)
    assert decoded["sub"] == "user-abc"


def test_id_token_is_rejected(monkeypatch, jwks_server: JwksServer):
    _configure_env(monkeypatch, jwks_server)
    now = int(time.time())
    token = _sign(
        {
            "sub": "user-abc",
            "iss": jwks_server.url,
            "aud": "client-id-XYZ",
            "token_use": "id",
            "exp": now + 3600,
            "iat": now,
        },
        jwks_server,
    )
    with pytest.raises(HTTPException) as exc:
        _call_decoder(token)
    assert exc.value.status_code == 401
    assert "access_token" in str(exc.value.detail)


def test_wrong_client_id_is_rejected(monkeypatch, jwks_server: JwksServer):
    _configure_env(monkeypatch, jwks_server)
    now = int(time.time())
    token = _sign(
        {
            "sub": "user-abc",
            "iss": jwks_server.url,
            "client_id": "wrong-client",
            "token_use": "access",
            "exp": now + 3600,
            "iat": now,
        },
        jwks_server,
    )
    with pytest.raises(HTTPException) as exc:
        _call_decoder(token)
    assert exc.value.status_code == 401


def test_expired_token_is_rejected(monkeypatch, jwks_server: JwksServer):
    _configure_env(monkeypatch, jwks_server)
    now = int(time.time())
    token = _sign(
        {
            "sub": "user-abc",
            "iss": jwks_server.url,
            "client_id": "client-id-XYZ",
            "token_use": "access",
            "exp": now - 100,
            "iat": now - 3600,
        },
        jwks_server,
    )
    with pytest.raises(HTTPException) as exc:
        _call_decoder(token)
    assert exc.value.status_code == 401


def test_wrong_issuer_is_rejected(monkeypatch, jwks_server: JwksServer):
    _configure_env(monkeypatch, jwks_server)
    now = int(time.time())
    token = _sign(
        {
            "sub": "user-abc",
            "iss": "https://attacker.example.com/pool",
            "client_id": "client-id-XYZ",
            "token_use": "access",
            "exp": now + 3600,
            "iat": now,
        },
        jwks_server,
    )
    with pytest.raises(HTTPException) as exc:
        _call_decoder(token)
    assert exc.value.status_code == 401
