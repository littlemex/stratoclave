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

    def test_fail_open_on_ddb_error(self, monkeypatch):
        # No dynamodb_mock → table doesn't exist → must NOT raise (fail open)
        import core.rate_limit_ddb as rl

        def boom():
            raise RuntimeError("table gone")
        monkeypatch.setattr(rl, "_table", boom)
        # Should not raise despite the backend being down
        _check("login", "10.0.0.99", limit=1, window_seconds=60)
        _check("login", "10.0.0.99", limit=1, window_seconds=60)


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
