"""Sign-in routes carry two independent rate limits (`core/rate_limit.py`):

  - a per-route flood guard, keyed on the client address alone, at the SAME
    numbers this module always used (10/minute login, 10/minute respond,
    20/minute sso-exchange/ui-ticket-consume);
  - a tight check keyed on BOTH the identity the request names AND the
    client's own address -- the email for `/login` and `/respond`, the
    verified account for `/sso-exchange` (charged only AFTER STS has proven
    the caller holds the credential, never before) -- run explicitly inside
    the handler via `check_identity_rate_limit`.

The address half of the identity key is the fix for a real attack: an
identity-ONLY bucket lets anyone who merely knows (never needs to hold) a
victim's email lock that victim out, by sending bad guesses from as many
addresses as they like -- each address gets its own share of the identity
bucket's quota, and the sum keeps landing on the SAME victim-keyed row no
matter which address it came from. Binding the address means every attacking
address only ever spends its OWN (identity, that address) row.

This file drives the real route functions through FastAPI (the real
`@limiter.limit` decorator, the real `check_identity_rate_limit`), with only
the DynamoDB counter write (`core.rate_limit_ddb._check`) and the AWS calls
each handler makes replaced, so the wiring under test is the real one -- not
a reimplementation of it.
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
        assert rl.identity_key("login", "Alice@Example.com", "1.2.3.4") == rl.identity_key(
            "login", " alice@example.com ", "1.2.3.4"
        )

    def test_different_kinds_do_not_collide(self):
        assert rl.identity_key("login", "x", "1.2.3.4") != rl.identity_key("sts", "x", "1.2.3.4")

    def test_different_addresses_do_not_collide(self):
        """The whole point of binding the address: the SAME identity from TWO
        different addresses must land in two different buckets."""
        a = rl.identity_key("login", "alice@example.com", "1.2.3.4")
        b = rl.identity_key("login", "alice@example.com", "5.6.7.8")
        assert a != b

    def test_the_raw_identity_is_not_in_the_key(self):
        """The key is a sha256 digest, not the email itself -- the table must
        not carry unbounded-length PII."""
        key = rl.identity_key("login", "alice@example.com", "1.2.3.4")
        assert "alice" not in key
        assert "example.com" not in key

    def test_empty_identity_is_refused(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            rl.identity_key("login", "   ", "1.2.3.4")
        assert exc.value.status_code == 400

    def test_oversize_identity_is_refused(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            rl.identity_key("login", "a" * (rl.MAX_IDENTITY_LENGTH + 1), "1.2.3.4")
        assert exc.value.status_code == 400


class TestCheckIdentityRateLimit:
    def test_calls_check_with_the_identity_and_address_key_and_parsed_spec(self, seen):
        rl.check_identity_rate_limit("scope-a", "login", "Bob@Example.com", "1.2.3.4", "5/minute")
        assert seen == [("scope-a", rl.identity_key("login", "bob@example.com", "1.2.3.4"))]

    def test_propagates_rate_limit_exceeded(self, monkeypatch):
        def _raise(scope, key, limit, window):
            raise ddb.RateLimitExceeded(window)

        monkeypatch.setattr(ddb, "_check", _raise)
        with pytest.raises(ddb.RateLimitExceeded):
            rl.check_identity_rate_limit("scope-a", "login", "bob@example.com", "1.2.3.4", "5/minute")


def _app_with_login():
    import mvp.cognito_auth as cognito_auth

    app = FastAPI()
    app.include_router(cognito_auth.router)
    return app, cognito_auth


class _FakeCognito:
    def admin_initiate_auth(self, **kwargs):
        return {"AuthenticationResult": {"AccessToken": "tok", "ExpiresIn": 3600}}

    def admin_respond_to_auth_challenge(self, **kwargs):
        return {"AuthenticationResult": {"AccessToken": "tok", "ExpiresIn": 3600}}


class TestLoginRouteWiring:
    """`/api/mvp/auth/login` through the real FastAPI route."""

    def _client(self, monkeypatch):
        app, cognito_auth = _app_with_login()
        monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
        monkeypatch.setenv("COGNITO_CLIENT_ID", "client")
        monkeypatch.setattr(cognito_auth, "_cognito_client", lambda: _FakeCognito())
        return TestClient(app)

    def test_eleven_bad_posts_for_alice_from_one_address_then_alice_from_another_succeeds(
        self, monkeypatch, seen
    ):
        """The blocker this file exists to pin: an attacker who does not
        control Alice's own address cannot spend Alice's sign-in quota.
        Eleven requests naming Alice from address A touch only the
        (alice, A) identity bucket and A's own address bucket; a twelfth
        request naming Alice from a DIFFERENT address B still reaches the
        handler (200), because B's (alice, B) bucket and B's own address
        bucket are untouched by anything A did."""
        client = self._client(monkeypatch)
        for _ in range(11):
            resp = client.post(
                "/api/mvp/auth/login",
                json={"email": "alice@example.com", "password": "guess"},
                headers={"x-forwarded-for": "10.0.0.1"},
            )
            assert resp.status_code == 200  # `_check` never enforces in this fixture
        resp = client.post(
            "/api/mvp/auth/login",
            json={"email": "alice@example.com", "password": "x"},
            headers={"x-forwarded-for": "10.0.0.2"},
        )
        assert resp.status_code == 200

        identity_calls = [k for scope, k in seen if scope == "auth_login_identity"]
        alice_from_a = rl.identity_key("login", "alice@example.com", "10.0.0.1")
        alice_from_b = rl.identity_key("login", "alice@example.com", "10.0.0.2")
        assert identity_calls.count(alice_from_a) == 11
        assert identity_calls.count(alice_from_b) == 1
        # The two buckets are DIFFERENT keys -- eleven hits on one never
        # appear against the other.
        assert alice_from_a != alice_from_b

    def test_two_identities_behind_one_address_do_not_share_the_identity_bucket(self, monkeypatch, seen):
        client = self._client(monkeypatch)
        headers = {"x-forwarded-for": "10.0.0.5"}
        r1 = client.post("/api/mvp/auth/login", json={"email": "alice@example.com", "password": "x"}, headers=headers)
        r2 = client.post("/api/mvp/auth/login", json={"email": "bob@example.com", "password": "x"}, headers=headers)
        assert r1.status_code == 200 and r2.status_code == 200

        address_calls = [k for scope, k in seen if scope == "login"]
        identity_calls = [k for scope, k in seen if scope == "auth_login_identity"]

        # Both requests from the same address count against the SAME
        # address-only flood guard.
        assert address_calls == ["10.0.0.5", "10.0.0.5"]
        # But each email gets its OWN identity bucket.
        assert len(set(identity_calls)) == 2

    def test_made_up_identities_still_hit_the_address_only_flood_guard(self, monkeypatch, seen):
        """An attacker cannot evade the per-address flood guard by minting a
        fresh made-up email on every request: every request from one address
        still counts once against that route's own address-only bucket,
        however many distinct (fictitious) identities it names."""
        client = self._client(monkeypatch)
        headers = {"x-forwarded-for": "10.0.0.9"}
        for i in range(5):
            client.post(
                "/api/mvp/auth/login",
                json={"email": f"made-up-{i}@example.com", "password": "x"},
                headers=headers,
            )

        address_calls = [k for scope, k in seen if scope == "login"]
        identity_calls = [k for scope, k in seen if scope == "auth_login_identity"]
        assert address_calls == ["10.0.0.9"] * 5
        assert len(set(identity_calls)) == 5

    def test_the_address_guard_runs_before_the_identity_check_and_the_reverse_order_would_fail_this(
        self, monkeypatch
    ):
        """Pins the ORDER, with a condition that actually distinguishes it:
        raising ONLY for the address-only scope ("login") must mean the
        identity scope ("auth_login_identity") is NEVER reached. A version of
        this test that raised on every `_check` call (any scope) would pass
        identically whichever order the handler calls them in, which is why
        it is written this way instead: swap the handler's two calls and
        THIS test starts failing, because the identity-scope call would then
        run (and get recorded) before the address-scope call ever raises.
        """
        app, cognito_auth = _app_with_login()
        monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
        monkeypatch.setenv("COGNITO_CLIENT_ID", "client")
        monkeypatch.setattr(cognito_auth, "_cognito_client", lambda: _FakeCognito())

        calls: list[tuple[str, str]] = []

        def _check(scope, key, limit, window):
            calls.append((scope, key))
            if scope == "login":
                raise ddb.RateLimitExceeded(window)

        monkeypatch.setattr(ddb, "_check", _check)

        client = TestClient(app)
        resp = client.post(
            "/api/mvp/auth/login",
            json={"email": "alice@example.com", "password": "x"},
            headers={"x-forwarded-for": "10.0.0.1"},
        )
        assert resp.status_code == 429
        assert calls == [("login", "10.0.0.1")]
        assert not any(scope == "auth_login_identity" for scope, _ in calls)

    def test_an_identity_only_trip_through_the_real_route_returns_429_not_500(self, monkeypatch):
        """`check_identity_rate_limit` raises `RateLimitExceeded` directly
        inside the handler body, not through the `@limiter.limit` decorator's
        own wrapper -- confirm FastAPI still converts it to 429 rather than
        an unhandled-exception 500, since `RateLimitExceeded` is an
        `HTTPException` subclass and FastAPI's conversion happens at the
        exception-handler layer around the whole request, not specifically
        inside the decorator."""
        app, cognito_auth = _app_with_login()
        monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
        monkeypatch.setenv("COGNITO_CLIENT_ID", "client")
        monkeypatch.setattr(cognito_auth, "_cognito_client", lambda: _FakeCognito())

        def _check(scope, key, limit, window):
            if scope == "auth_login_identity":
                raise ddb.RateLimitExceeded(window)

        monkeypatch.setattr(ddb, "_check", _check)

        client = TestClient(app)
        resp = client.post(
            "/api/mvp/auth/login",
            json={"email": "alice@example.com", "password": "x"},
            headers={"x-forwarded-for": "10.0.0.1"},
        )
        assert resp.status_code == 429

    def test_a_body_that_fails_field_validation_never_reaches_either_check(self, monkeypatch, seen):
        """Narrows the claim "the address bucket is touched once per request
        regardless": FastAPI resolves `body: LoginRequest` (and
        `Depends(...)`, same as `mvp.deps.get_current_user`) BEFORE calling
        the decorated handler at all, so a request whose body fails Pydantic
        field validation never reaches the `@limiter.limit` wrapper either --
        a 422, not a 429, and NEITHER `_check` call happens. The oversized-
        body case is a separate, already-existing control
        (`main.py`'s `MaxBodySizeASGIMiddleware`, ahead of FastAPI/Pydantic
        entirely, not this file's concern)."""
        client = self._client(monkeypatch)
        resp = client.post(
            "/api/mvp/auth/login",
            json={"password": "x"},  # missing required "email"
            headers={"x-forwarded-for": "10.0.0.1"},
        )
        assert resp.status_code == 422
        assert seen == []


class TestRespondRouteWiring:
    """`/api/mvp/auth/respond`: same two-limit shape as `/login`, pinned
    separately since it is a distinct route with its own scopes."""

    def _client(self, monkeypatch):
        app, cognito_auth = _app_with_login()
        monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
        monkeypatch.setenv("COGNITO_CLIENT_ID", "client")
        monkeypatch.setattr(cognito_auth, "_cognito_client", lambda: _FakeCognito())
        return TestClient(app)

    def test_two_identities_behind_one_address_do_not_share_the_identity_bucket(self, monkeypatch, seen):
        client = self._client(monkeypatch)
        headers = {"x-forwarded-for": "10.0.1.5"}
        body = lambda email: {"email": email, "new_password": "NewPassw0rd!", "session": "sess"}
        r1 = client.post("/api/mvp/auth/respond", json=body("alice@example.com"), headers=headers)
        r2 = client.post("/api/mvp/auth/respond", json=body("bob@example.com"), headers=headers)
        assert r1.status_code == 200 and r2.status_code == 200

        address_calls = [k for scope, k in seen if scope == "respond_challenge"]
        identity_calls = [k for scope, k in seen if scope == "auth_respond_identity"]
        assert address_calls == ["10.0.1.5", "10.0.1.5"]
        assert len(set(identity_calls)) == 2

    def test_a_different_address_naming_the_same_victim_gets_its_own_identity_bucket(self, monkeypatch, seen):
        client = self._client(monkeypatch)
        body = {"email": "alice@example.com", "new_password": "NewPassw0rd!", "session": "sess"}
        client.post("/api/mvp/auth/respond", json=body, headers={"x-forwarded-for": "10.0.1.1"})
        client.post("/api/mvp/auth/respond", json=body, headers={"x-forwarded-for": "10.0.1.2"})
        identity_calls = [k for scope, k in seen if scope == "auth_respond_identity"]
        assert len(set(identity_calls)) == 2


def _app_with_ui_ticket():
    import mvp.ui_ticket as ui_ticket

    app = FastAPI()
    app.include_router(ui_ticket.router)
    return app, ui_ticket


class TestUiTicketConsumeRouteWiring:
    """`/api/mvp/auth/ui-ticket/consume`: address-only flood guard, no
    identity-keyed check at all (the ticket itself is single-use and the
    only identity a request could name) -- confirmed by wiring, not merely
    asserted in a docstring."""

    def test_only_the_address_only_scope_is_touched(self, monkeypatch, seen):
        import dynamo

        app, ui_ticket = _app_with_ui_ticket()

        class _FakeRepo:
            def consume(self, ticket):
                raise dynamo.TicketNotFoundError("not found")

        monkeypatch.setattr(ui_ticket, "UiTicketsRepository", _FakeRepo)
        client = TestClient(app)
        resp = client.post(
            "/api/mvp/auth/ui-ticket/consume",
            json={"ticket": "stt_" + "a" * 32},
            headers={"x-forwarded-for": "10.0.2.1"},
        )
        assert resp.status_code == 404  # the fake repo's TicketNotFoundError
        assert seen == [("consume_ui_ticket", "10.0.2.1")]


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
    """`/api/mvp/auth/sso-exchange`: no identity-keyed charge before STS has
    verified the signature; the verified account is charged only after
    `verify_and_call_sts` succeeds."""

    def test_forged_sigv4_naming_a_victim_key_id_never_charges_the_victim(self, monkeypatch, seen):
        """A request that merely NAMES a victim's access key id in an
        unsigned/forged `Authorization` string -- which is exactly what an
        attacker who only knows (does not hold) the key id can produce --
        must never touch that victim's identity bucket at all, because this
        route no longer reads an identity out of the request before STS has
        verified it. `verify_and_call_sts` is mocked to fail exactly as the
        real one would for a signature STS cannot verify."""
        app, sso_exchange = _sso_exchange_app()
        from fastapi import HTTPException

        def _reject(**kwargs):
            raise HTTPException(status_code=401, detail="STS rejected the presigned request")

        monkeypatch.setattr(sso_exchange, "verify_and_call_sts", _reject)
        client = TestClient(app)
        resp = client.post(
            "/api/mvp/auth/sso-exchange",
            json=_sso_body("AKIDVICTIM"),
            headers={"x-forwarded-for": "10.1.0.1"},
        )
        assert resp.status_code == 401
        assert not [k for scope, k in seen if scope == "auth_sso_exchange_identity"]
        # The route's own address-only flood guard still saw the request.
        assert [k for scope, k in seen if scope == "sso_exchange"] == ["10.1.0.1"]

    def test_a_verified_exchange_charges_the_account_and_principal_not_the_session_name(
        self, monkeypatch, seen
    ):
        """After `verify_and_call_sts` succeeds, the charge is keyed on the
        account id plus the AWS-assigned prefix of the UserId
        (`principal_id`) -- NOT the ARN's RoleSessionName tail, which
        whoever calls AssumeRole picks and could rotate per request to evade
        a per-ARN bucket the same way an unverified access key id could."""
        from mvp.sso_sts import classify_arn

        app, sso_exchange = _sso_exchange_app()
        identity = classify_arn(
            arn="arn:aws:sts::123456789012:assumed-role/MyRole/session-one",
            user_id="AROAEXAMPLE:session-one",
            account_id="123456789012",
        )

        def _verified(**kwargs):
            return identity

        monkeypatch.setattr(sso_exchange, "verify_and_call_sts", _verified)
        monkeypatch.setattr(
            sso_exchange, "validate_sso_identity",
            lambda sts_identity: (_ for _ in ()).throw(
                __import__("fastapi").HTTPException(status_code=403, detail="not trusted")
            ),
        )
        client = TestClient(app)
        resp = client.post(
            "/api/mvp/auth/sso-exchange",
            json=_sso_body("AKIDHOLDER"),
            headers={"x-forwarded-for": "10.1.0.2"},
        )
        assert resp.status_code == 403  # the gate denial, reached only after the charge above it

        identity_calls = [k for scope, k in seen if scope == "auth_sso_exchange_identity"]
        assert identity_calls == [rl.identity_key("sts", "123456789012:AROAEXAMPLE", "10.1.0.2")]

    def test_two_different_verified_sessions_of_one_role_share_one_bucket(self, monkeypatch, seen):
        """Two different RoleSessionNames for the SAME role/principal must
        land in the SAME bucket -- proving the charge really is keyed on the
        AWS-assigned principal, not on the attacker-chosen session name."""
        from mvp.sso_sts import classify_arn

        app, sso_exchange = _sso_exchange_app()

        def _make_verified(session_name):
            def _verified(**kwargs):
                return classify_arn(
                    arn=f"arn:aws:sts::123456789012:assumed-role/MyRole/{session_name}",
                    user_id="AROAEXAMPLE:" + session_name,
                    account_id="123456789012",
                )
            return _verified

        monkeypatch.setattr(
            sso_exchange, "validate_sso_identity",
            lambda sts_identity: (_ for _ in ()).throw(
                __import__("fastapi").HTTPException(status_code=403, detail="not trusted")
            ),
        )
        client = TestClient(app)
        for session_name in ("session-one", "session-two"):
            monkeypatch.setattr(sso_exchange, "verify_and_call_sts", _make_verified(session_name))
            client.post(
                "/api/mvp/auth/sso-exchange",
                json=_sso_body("AKID"),
                headers={"x-forwarded-for": "10.1.0.3"},
            )

        identity_calls = [k for scope, k in seen if scope == "auth_sso_exchange_identity"]
        assert len(identity_calls) == 2
        assert len(set(identity_calls)) == 1  # same bucket both times

    def test_a_malformed_authorization_header_is_not_rate_limited_as_an_identity_at_all(
        self, monkeypatch, seen
    ):
        """No access-key-id extraction happens pre-STS any more at all, so a
        malformed header reaches `verify_and_call_sts` (mocked here to fail
        the same way the real STS validation would) without this route
        touching the identity scope -- there is nothing left in this route
        for a malformed header to confuse."""
        app, sso_exchange = _sso_exchange_app()
        from fastapi import HTTPException

        monkeypatch.setattr(
            sso_exchange, "verify_and_call_sts",
            lambda **kwargs: (_ for _ in ()).throw(
                HTTPException(status_code=400, detail="malformed SigV4 Authorization header")
            ),
        )
        client = TestClient(app)
        body = _sso_body("AKID")
        body["headers"]["Authorization"] = "not sigv4"
        resp = client.post(
            "/api/mvp/auth/sso-exchange", json=body, headers={"x-forwarded-for": "10.1.0.9"}
        )
        assert resp.status_code == 400
        assert not [k for scope, k in seen if scope == "auth_sso_exchange_identity"]
        assert [k for scope, k in seen if scope == "sso_exchange"] == ["10.1.0.9"]
