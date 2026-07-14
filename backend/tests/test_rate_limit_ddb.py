"""Tests for the DynamoDB-backed fixed-window rate limiter.

Verifies the limiter enforces per-IP caps using a shared DynamoDB counter
(so multi-task deployments share state), fails open on DynamoDB errors,
and preserves the slowapi-compatible decorator interface.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from core.rate_limit_ddb import DynamoRateLimiter, _parse_spec, _check


def _key(request: Request) -> str:
    # Deterministic key for tests: use a header if present, else peer.
    return request.headers.get("x-test-ip", "1.2.3.4")


class TestParseSpec:
    def test_minute(self):
        assert _parse_spec("10/minute") == (10, 60)

    def test_second(self):
        assert _parse_spec("5/second") == (5, 1)

    def test_hour(self):
        assert _parse_spec("100/hour") == (100, 3600)

    def test_plural(self):
        assert _parse_spec("3/minutes") == (3, 60)

    def test_missing_period_raises(self):
        # A spec with no '/period' must fail loudly (import-time), not silently
        # default to 60s — that would be a 60x loosening of a security control.
        with pytest.raises(ValueError):
            _parse_spec("10")

    def test_unknown_period_raises(self):
        with pytest.raises(ValueError):
            _parse_spec("100/hr")  # typo for 'hour'

    def test_non_integer_limit_raises(self):
        with pytest.raises(ValueError):
            _parse_spec("ten/minute")


class TestCheckEnforcement:
    def test_under_limit_passes(self, dynamodb_mock):
        # 3 hits under a limit of 5 → no raise
        for _ in range(3):
            _check("login", "10.0.0.1", limit=5, window_seconds=60)

    def test_over_limit_raises_429(self, dynamodb_mock):
        # limit=2: hits 1,2 pass; hit 3 raises
        _check("login", "10.0.0.2", limit=2, window_seconds=60)
        _check("login", "10.0.0.2", limit=2, window_seconds=60)
        with pytest.raises(HTTPException) as exc:
            _check("login", "10.0.0.2", limit=2, window_seconds=60)
        assert exc.value.status_code == 429
        assert "Retry-After" in exc.value.headers

    def test_different_ips_independent(self, dynamodb_mock):
        # Each IP gets its own bucket
        _check("login", "10.0.0.10", limit=1, window_seconds=60)
        _check("login", "10.0.0.11", limit=1, window_seconds=60)  # different IP, fresh bucket
        with pytest.raises(HTTPException):
            _check("login", "10.0.0.10", limit=1, window_seconds=60)  # first IP over

    def test_different_scopes_independent(self, dynamodb_mock):
        _check("login", "10.0.0.20", limit=1, window_seconds=60)
        _check("respond", "10.0.0.20", limit=1, window_seconds=60)  # different scope, fresh

    def test_fail_open_on_connection_error(self, monkeypatch):
        # A genuine outage (connectivity) must NOT lock users out of auth:
        # fail open (allow), but the limiter logs it (alarmable elsewhere).
        import core.rate_limit_ddb as rl
        from botocore.exceptions import EndpointConnectionError

        def boom():
            raise EndpointConnectionError(endpoint_url="https://dynamodb")
        monkeypatch.setattr(rl, "_table", boom)
        # Should not raise despite the backend being unreachable.
        _check("login", "10.0.0.99", limit=1, window_seconds=60)
        _check("login", "10.0.0.99", limit=1, window_seconds=60)

    def test_fail_closed_on_throttle(self, monkeypatch):
        # Throttling of the counter item is evidence the IP is hammering one
        # hot key — that IS the breach the limiter exists to stop. Fail CLOSED.
        import core.rate_limit_ddb as rl
        from botocore.exceptions import ClientError

        def throttled():
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "UpdateItem",
            )

        class _T:
            def update_item(self, **kw):
                throttled()

        monkeypatch.setattr(rl, "_table", lambda: _T())
        with pytest.raises(HTTPException) as exc:
            _check("login", "10.0.0.98", limit=1, window_seconds=60)
        assert exc.value.status_code == 429

    def test_programming_error_propagates(self, monkeypatch):
        # A bug in the limiter (e.g. TypeError) must surface as a 500, never be
        # swallowed into silent unlimited auth.
        import core.rate_limit_ddb as rl

        class _T:
            def update_item(self, **kw):
                raise TypeError("bug in limiter")

        monkeypatch.setattr(rl, "_table", lambda: _T())
        with pytest.raises(TypeError):
            _check("login", "10.0.0.97", limit=1, window_seconds=60)


class TestDecoratorInterface:
    def test_sync_route_enforced(self, dynamodb_mock):
        app = FastAPI()
        limiter = DynamoRateLimiter(client_key_func=_key)

        @app.post("/login")
        @limiter.limit("2/minute")
        def login(request: Request):
            return {"ok": True}

        client = TestClient(app)
        assert client.post("/login").status_code == 200
        assert client.post("/login").status_code == 200
        assert client.post("/login").status_code == 429

    def test_distinct_ips_not_shared(self, dynamodb_mock):
        app = FastAPI()
        limiter = DynamoRateLimiter(client_key_func=_key)

        @app.post("/login")
        @limiter.limit("1/minute")
        def login(request: Request):
            return {"ok": True}

        client = TestClient(app)
        assert client.post("/login", headers={"x-test-ip": "a"}).status_code == 200
        assert client.post("/login", headers={"x-test-ip": "b"}).status_code == 200
        assert client.post("/login", headers={"x-test-ip": "a"}).status_code == 429

    def test_async_route_enforced_off_event_loop(self, dynamodb_mock):
        # The async path must offload the blocking check (to_thread) and still
        # enforce the cap end to end.
        app = FastAPI()
        limiter = DynamoRateLimiter(client_key_func=_key)

        @app.post("/respond")
        @limiter.limit("2/minute")
        async def respond(request: Request):
            return {"ok": True}

        client = TestClient(app)
        assert client.post("/respond").status_code == 200
        assert client.post("/respond").status_code == 200
        assert client.post("/respond").status_code == 429

    def test_missing_request_param_raises_at_decoration(self):
        # A handler without a `request` param must fail loudly at import, not
        # silently run unlimited (SEV-2: a refactor dropping `request` would
        # otherwise disable the cap with no signal).
        limiter = DynamoRateLimiter(client_key_func=_key)
        with pytest.raises(RuntimeError, match="request"):
            @limiter.limit("2/minute")
            def handler(body: dict):  # no request param
                return body
