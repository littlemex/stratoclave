"""Regression guard for sweep-4 C-Critical: ui_ticket mint must verify
the supplied access_token against the caller's identity.

Squash events in PR #57 and PR #59 twice deleted the "body access_token
must JWKS-verify and sub must equal the authenticated caller" check,
creating a session-fixation primitive where Alice could mint a UI ticket
holding Bob's access_token. This test locks the guard in place so any
future squash that reintroduces the regression fails CI.

We keep the test deliberately narrow and at the handler level so it does
not depend on Cognito / moto / DynamoDB. The JWKS verification path is
already separately exercised by test_jwt_verify.py — here we only need
to prove that mint_ui_ticket rejects a caller-mismatched body.
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

    kid = "ui-ticket-kid"
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
        def log_message(self, *args, **kwargs):
            pass

        def do_GET(self):  # noqa: N802
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


def _sign(payload: dict, jwks: JwksServer) -> str:
    return pyjwt.encode(
        payload, jwks.private_pem, algorithm="RS256", headers={"kid": jwks.kid}
    )


@pytest.fixture
def _configured(monkeypatch, jwks_server: JwksServer):
    monkeypatch.setenv("OIDC_ISSUER_URL", jwks_server.url)
    monkeypatch.setenv("OIDC_AUDIENCE", "client-id-XYZ")
    from mvp import deps

    deps._jwks_client.cache_clear()
    return jwks_server


def _make_user(user_id: str, email: str):
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id=user_id,
        email=email,
        org_id="default-org",
        roles=["user"],
        raw_claims={},
    )


def _valid_token(user_id: str, jwks: JwksServer) -> str:
    now = int(time.time())
    return _sign(
        {
            "sub": user_id,
            "iss": jwks.url,
            "client_id": "client-id-XYZ",
            "token_use": "access",
            "exp": now + 3600,
            "iat": now,
        },
        jwks,
    )


class _StubRepo:
    def mint(self, *, user_id, access_token, id_token, refresh_token, expires_in, token_type):
        return ("stt_plaintext", int(time.time()) + 30)


def test_mint_rejects_body_access_token_belonging_to_another_user(
    monkeypatch, _configured: JwksServer
):
    """Session-fixation guard: Alice cannot mint a UI ticket whose body
    carries Bob's access_token. sub(body) must equal caller.user_id."""
    from mvp import ui_ticket

    monkeypatch.setattr(ui_ticket, "UiTicketsRepository", _StubRepo)
    monkeypatch.setattr(ui_ticket, "log_audit_event", lambda **_: None)

    alice = _make_user("alice-sub-1111", "alice@example.com")
    bob_token = _valid_token("bob-sub-2222", _configured)

    body = ui_ticket.MintUiTicketRequest(access_token=bob_token)
    with pytest.raises(HTTPException) as exc:
        ui_ticket.mint_ui_ticket(body=body, user=alice)
    assert exc.value.status_code in (401, 403)
    # Error detail must not leak either user's sub — just complain about
    # mismatch.
    detail = str(exc.value.detail).lower()
    assert "mismatch" in detail or "not permitted" in detail or "forbidden" in detail


def test_mint_rejects_unverifiable_access_token(
    monkeypatch, _configured: JwksServer
):
    """A syntactically valid but signature-invalid access_token must be
    rejected at mint time. Previously the body was trusted verbatim."""
    from mvp import ui_ticket

    monkeypatch.setattr(ui_ticket, "UiTicketsRepository", _StubRepo)
    monkeypatch.setattr(ui_ticket, "log_audit_event", lambda **_: None)

    alice = _make_user("alice-sub-1111", "alice@example.com")
    # header + payload + bogus signature
    tampered = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhbGljZS1zdWItMTExMSJ9.NOT_A_SIG"

    body = ui_ticket.MintUiTicketRequest(access_token=tampered)
    with pytest.raises(HTTPException) as exc:
        ui_ticket.mint_ui_ticket(body=body, user=alice)
    assert exc.value.status_code == 401


def test_mint_accepts_matching_access_token(monkeypatch, _configured: JwksServer):
    """Happy path: sub(body.access_token) == caller.user_id -> ticket minted."""
    from mvp import ui_ticket

    monkeypatch.setattr(ui_ticket, "UiTicketsRepository", _StubRepo)
    monkeypatch.setattr(ui_ticket, "log_audit_event", lambda **_: None)

    alice = _make_user("alice-sub-1111", "alice@example.com")
    alice_token = _valid_token("alice-sub-1111", _configured)

    body = ui_ticket.MintUiTicketRequest(access_token=alice_token)
    resp = ui_ticket.mint_ui_ticket(body=body, user=alice)
    assert resp.ticket == "stt_plaintext"
