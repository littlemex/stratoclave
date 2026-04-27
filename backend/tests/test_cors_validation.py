"""Regression tests for CORS_ORIGINS startup validation (P1-9).

Guards against four accidents that are both common and dangerous:
  - Forgetting to set CORS_ORIGINS at all.
  - Setting it to `*` (wildcard) — incompatible with allow_credentials.
  - Copy-pasting a bare host without a scheme.
  - Shipping to production with an `http://` origin (plaintext).
"""
from __future__ import annotations

import pytest


def _call(value: str, env: str = "development") -> list[str]:
    import main

    previous = main.environment
    main.environment = env
    try:
        return main._validate_cors_origins(value)
    finally:
        main.environment = previous


def test_accepts_single_https_origin():
    assert _call("https://example.cloudfront.net") == [
        "https://example.cloudfront.net"
    ]


def test_accepts_multiple_origins_comma_separated():
    got = _call("https://a.example.com, https://b.example.com")
    assert got == ["https://a.example.com", "https://b.example.com"]


def test_rejects_empty_value():
    with pytest.raises(EnvironmentError):
        _call("")


def test_rejects_wildcard():
    with pytest.raises(EnvironmentError) as exc:
        _call("*")
    assert "wildcard" in str(exc.value).lower()


def test_rejects_missing_scheme():
    with pytest.raises(EnvironmentError):
        _call("example.com")


def test_rejects_http_in_production():
    with pytest.raises(EnvironmentError):
        _call("http://example.com", env="production")


def test_allows_http_localhost_even_in_production():
    # Local development hooks sometimes run in a "production-like" env;
    # keeping localhost allowed avoids false-positive lockouts.
    got = _call("http://localhost:3003", env="production")
    assert got == ["http://localhost:3003"]
