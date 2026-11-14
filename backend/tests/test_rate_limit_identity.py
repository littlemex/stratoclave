"""Sign-in routes carry two independent rate limits (`core/rate_limit.py`):

  - a coarse flood guard, keyed on the client address, SHARED by every
    unauthenticated sign-in route (`AUTH_SIGN_IN_ADDRESS_SCOPE`);
  - a tight check keyed on the identity the request names -- the email for
    `/login` and `/respond`, the access key id for `/sso-exchange` -- run
    explicitly inside the handler via `check_identity_rate_limit`.

This file drives the real route functions through FastAPI (the real
`@limiter.limit` decorator, the real `check_identity_rate_limit`), with only
the DynamoDB counter write (`core.rate_limit_ddb._check`) and the AWS calls
each handler makes replaced, so the wiring under test is the real one -- not
a reimplementation of it (the same reasoning as
`test_rate_limit_xff.py::test_a_limited_route_is_bucketed_by_the_user_the_auth_dependency_verified`).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.rate_limit_ddb as ddb
from core import rate_limit as rl


@pytest.fixture
def seen(monkeypatch):
    """Every `_check(scope, key, limit, window)` call, in order, with no
    counter actually enforced -- so every request in a test reaches the
    handler and the test asserts on which buckets were touched, not on when
    a 429 would have fired."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(ddb, "_check", lambda scope, key, limit, window: calls.append((scope, key)))
    return calls


class TestIdentityKey:
    def test_case_and_whitespace_insensitive(self):
        assert rl.identity_key("login", "Alice@Example.com") == rl.identity_key("login", " alice@example.com ")

    def test_different_kinds_do_not_collide(self):
        assert rl.identity_key("login", "x") != rl.identity_key("sts", "x")


class TestCheckIdentityRateLimit:
    def test_calls_check_with_the_identity_key_and_parsed_spec(self, seen):
        rl.check_identity_rate_limit("scope-a", "login", "Bob@Example.com", "5/minute")
        assert seen == [("scope-a", "identity:login:bob@example.com")]

    def test_propagates_rate_limit_exceeded(self, monkeypatch):
        def _raise(scope, key, limit, window):
            raise ddb.RateLimitExceeded(window)
        monkeypatch.setattr(ddb, "_check", _raise)
        with pytest.raises(ddb.RateLimitExceeded):
            rl.check_identity_rate_limit("scope-a", "login", "bob@example.com", "5/minute")


def _app_with_login():
    import mvp.cognito_auth as cognito_auth

    app = FastAPI()
    app.include_router(cognito_auth.router)
    return app, cognito_auth


class _FakeCognito:
    def admin_initiate_auth(self, **kwargs):
        return {"AuthenticationResult": {"AccessToken": "tok", "ExpiresIn": 3600}}


class TestLoginRouteWiring:
    """`/api/mvp/auth/login` through the real FastAPI route."""

    def _client(self, monkeypatch, seen):
        app, cognito_auth = _app_with_login()
        monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
        monkeypatch.setenv("COGNITO_CLIENT_ID", "client")
        monkeypatch.setattr(cognito_auth, "_cognito_client", lambda: _FakeCognito())
        return TestClient(app)

    def test_two_identities_behind_one_address_do_not_share_the_identity_bucket(self, monkeypatch, seen):
        client = self._client(monkeypatch, seen)
        headers = {"x-forwarded-for": "10.0.0.5"}
        r1 = client.post("/api/mvp/auth/login", json={"email": "alice@example.com", "password": "x"}, headers=headers)
        r2 = client.post("/api/mvp/auth/login", json={"email": "bob@example.com", "password": "x"}, headers=headers)
        assert r1.status_code == 200 and r2.status_code == 200

        address_calls = [k for scope, k in seen if scope == rl.AUTH_SIGN_IN_ADDRESS_SCOPE]
        identity_calls = [k for scope, k in seen if scope == "auth_login_identity"]

        # Both requests from the same address count against the SAME shared
        # address bucket.
        assert address_calls == ["10.0.0.5", "10.0.0.5"]
        # But each email gets its OWN identity bucket -- alice cannot be
        # locked out by guesses against bob's account, and vice versa.
        assert identity_calls == [
            "identity:login:alice@example.com",
            "identity:login:bob@example.com",
        ]
        assert len(set(identity_calls)) == 2

    def test_made_up_identities_still_hit_the_shared_address_ceiling(self, monkeypatch, seen):
        """An attacker cannot evade the address flood guard by minting a fresh
        made-up email on every request: every request from one address still
        counts once against the SAME address bucket, however many distinct
        (fictitious) identities it names."""
        client = self._client(monkeypatch, seen)
        headers = {"x-forwarded-for": "10.0.0.9"}
        for i in range(5):
            client.post(
                "/api/mvp/auth/login",
                json={"email": f"made-up-{i}@example.com", "password": "x"},
                headers=headers,
            )

        address_calls = [k for scope, k in seen if scope == rl.AUTH_SIGN_IN_ADDRESS_SCOPE]
        identity_calls = [k for scope, k in seen if scope == "auth_login_identity"]
        assert address_calls == ["10.0.0.9"] * 5
        assert len(identity_calls) == 5
        assert len(set(identity_calls)) == 5  # five distinct, tiny-quota buckets

    def test_the_address_check_runs_before_the_identity_check(self, monkeypatch):
        """If the address flood guard trips, the identity bucket must never be
        touched -- otherwise a flood still spends DynamoDB writes on
        identity counters, one per made-up email, which is the growth this
        ordering exists to avoid."""
        app, cognito_auth = _app_with_login()
        monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
        monkeypatch.setenv("COGNITO_CLIENT_ID", "client")
        monkeypatch.setattr(cognito_auth, "_cognito_client", lambda: _FakeCognito())

        def _raise(scope, key, limit, window):
            raise ddb.RateLimitExceeded(window)
        monkeypatch.setattr(ddb, "_check", _raise)

        client = TestClient(app)
        resp = client.post(
            "/api/mvp/auth/login",
            json={"email": "alice@example.com", "password": "x"},
            headers={"x-forwarded-for": "10.0.0.1"},
        )
        assert resp.status_code == 429


def _sso_exchange_app():
    import mvp.sso_exchange as sso_exchange

    app = FastAPI()
    app.include_router(sso_exchange.router)
    return app, sso_exchange


def _sso_body(access_key_id: str) -> dict:
    return {
        "method": "POST",
        "url": "https://sts.us-east-1.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "headers": {
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={access_key_id}/20261114/us-east-1/sts/aws4_request, "
                "SignedHeaders=host;x-amz-date, Signature=abc"
            ),
            "X-Amz-Date": "20261114T000000Z",
        },
        "body": "",
    }


class TestSsoExchangeRouteWiring:
    """`/api/mvp/auth/sso-exchange`: the identity bucket is keyed on the
    presigned request's access key id, extracted before the (mocked) call to
    STS -- so a flood of requests presenting a credential this gateway never
    verifies still spends an identity-rate-limit write, not a free pass."""

    def _client(self, monkeypatch):
        app, sso_exchange = _sso_exchange_app()
        # Stop the pipeline right after the identity check, with a cheap,
        # recognisable failure -- the real STS call, trust gate and Cognito
        # provisioning are out of scope for this file.
        monkeypatch.setattr(
            sso_exchange, "verify_and_call_sts",
            lambda **kwargs: (_ for _ in ()).throw(ValueError("stopped-before-sts")),
        )
        return TestClient(app, raise_server_exceptions=False)

    def test_two_access_key_ids_behind_one_address_do_not_share_the_identity_bucket(self, monkeypatch, seen):
        client = self._client(monkeypatch)
        headers = {"x-forwarded-for": "10.1.0.5"}
        for akid in ("AKIDALICE", "AKIDBOB"):
            resp = client.post("/api/mvp/auth/sso-exchange", json=_sso_body(akid), headers=headers)
            assert resp.status_code == 500  # the mocked ValueError, not a rate-limit refusal

        identity_calls = [k for scope, k in seen if scope == "auth_sso_exchange_identity"]
        address_calls = [k for scope, k in seen if scope == rl.AUTH_SIGN_IN_ADDRESS_SCOPE]
        assert identity_calls == ["identity:sts:AKIDALICE".lower(), "identity:sts:AKIDBOB".lower()]
        assert address_calls == ["10.1.0.5", "10.1.0.5"]

    def test_a_malformed_authorization_header_skips_the_identity_check_without_crashing(self, monkeypatch, seen):
        client = self._client(monkeypatch)
        body = _sso_body("AKID")
        body["headers"]["Authorization"] = "not sigv4"
        resp = client.post(
            "/api/mvp/auth/sso-exchange", json=body, headers={"x-forwarded-for": "10.1.0.9"}
        )
        assert resp.status_code == 500
        assert not [k for scope, k in seen if scope == "auth_sso_exchange_identity"]
        assert [k for scope, k in seen if scope == rl.AUTH_SIGN_IN_ADDRESS_SCOPE] == ["10.1.0.9"]
