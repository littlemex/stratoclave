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

    def _patch_client_error(self, monkeypatch, code):
        import core.rate_limit_ddb as rl
        from botocore.exceptions import ClientError

        class _C:
            def update_item(self, **kw):
                raise ClientError({"Error": {"Code": code}}, "UpdateItem")

        monkeypatch.setattr(rl, "_client", lambda: _C())

    def _reset_local(self):
        import core.rate_limit_ddb as rl
        rl._local_fallback = rl._LocalWindows()

    def test_outage_degrades_to_local_limiter(self, monkeypatch):
        # A genuine outage (connectivity) must NOT lock users out, but must NOT
        # fail fully open either: degrade to the in-process limiter. Under the
        # limit → allowed; over it → 429 from the local fallback.
        import core.rate_limit_ddb as rl
        from botocore.exceptions import EndpointConnectionError
        self._reset_local()

        class _C:
            def update_item(self, **kw):
                raise EndpointConnectionError(endpoint_url="https://dynamodb")
        monkeypatch.setattr(rl, "_client", lambda: _C())
        # limit=2: first two allowed, third trips the LOCAL fallback.
        _check("login", "10.0.0.99", limit=2, window_seconds=60)
        _check("login", "10.0.0.99", limit=2, window_seconds=60)
        with pytest.raises(HTTPException) as exc:
            _check("login", "10.0.0.99", limit=2, window_seconds=60)
        assert exc.value.status_code == 429

    def test_fail_closed_on_partition_throttle(self, monkeypatch):
        # Per-partition throttle of the counter item is evidence the IP is
        # hammering one hot key — the breach the limiter exists to stop.
        self._patch_client_error(monkeypatch, "ProvisionedThroughputExceededException")
        with pytest.raises(HTTPException) as exc:
            _check("login", "10.0.0.98", limit=1, window_seconds=60)
        assert exc.value.status_code == 429

    def test_account_scoped_throttle_degrades_to_local(self, monkeypatch):
        # RequestLimitExceeded is account/table-scoped: a noisy neighbour can
        # trip it, so failing closed would 429 every auth user. Degrade to the
        # local limiter instead (bounded bypass, not a lockout).
        self._reset_local()
        self._patch_client_error(monkeypatch, "RequestLimitExceeded")
        # Under the local limit → allowed.
        _check("login", "10.0.0.96", limit=5, window_seconds=60)

    def test_fail_closed_on_misconfig(self, monkeypatch):
        # Wrong table / missing IAM / key-schema mismatch is a broken control,
        # not a transient outage — must NOT run auth unlimited.
        for code in ("ResourceNotFoundException", "AccessDeniedException", "ValidationException"):
            self._patch_client_error(monkeypatch, code)
            with pytest.raises(HTTPException) as exc:
                _check("login", "10.0.0.95", limit=1, window_seconds=60)
            assert exc.value.status_code == 429, code

    def test_programming_error_propagates(self, monkeypatch):
        # A bug in the limiter (e.g. TypeError) must surface as a 500, never be
        # swallowed into silent unlimited auth.
        import core.rate_limit_ddb as rl

        class _C:
            def update_item(self, **kw):
                raise TypeError("bug in limiter")

        monkeypatch.setattr(rl, "_client", lambda: _C())
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
        # A handler without a Request param must fail loudly at import, not
        # silently run unlimited (SEV-2: a refactor dropping it would otherwise
        # disable the cap with no signal).
        limiter = DynamoRateLimiter(client_key_func=_key)
        with pytest.raises(RuntimeError, match="Request"):
            @limiter.limit("2/minute")
            def handler(body: dict):  # no Request param
                return body

    def test_request_param_under_nonstandard_name_is_enforced(self, dynamodb_mock):
        # N2 regression: FastAPI passes the Request under the handler's own
        # param name. A handler declared `req: Request` (not literally
        # "request") must STILL be rate-limited, not silently skipped.
        app = FastAPI()
        limiter = DynamoRateLimiter(client_key_func=_key)

        @app.post("/login")
        @limiter.limit("2/minute")
        def login(req: Request):  # non-standard param name
            return {"ok": True}

        client = TestClient(app)
        assert client.post("/login").status_code == 200
        assert client.post("/login").status_code == 200
        assert client.post("/login").status_code == 429  # would be 200 if skipped
