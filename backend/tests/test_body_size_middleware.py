"""Z-3 regression (2026-04 third blind review).

The first-round ``MaxBodySizeMiddleware`` (sweep-1 C-H) only
inspected ``Content-Length`` and subclassed ``BaseHTTPMiddleware``.
Two problems:

  1. ``Transfer-Encoding: chunked`` requests carry no
     ``Content-Length``, so the cap did nothing for chunked uploads.
  2. ``BaseHTTPMiddleware`` buffers the entire body into memory
     BEFORE the inner handler runs, so the limit could only reject
     work that had already been received.

Z-3 moves the guard to a raw ASGI middleware that taps ``receive``
and tallies bytes per chunk. We test the two contracts:

  * ``Content-Length`` larger than the cap returns 413 immediately.
  * Chunked upload that cumulatively exceeds the cap returns 413,
    even when no ``Content-Length`` header is present.

The tests use Starlette's in-memory test harness so we exercise
the real ASGI path, not the wrapped ``BaseHTTPMiddleware`` surface.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

import pytest


def _build_app(monkeypatch, max_bytes: int = 1024):
    """Fresh FastAPI app wrapped in the ASGI body-size guard. We
    avoid importing ``main.app`` so the cap can be dialled down to a
    kilobyte without the large test-envvar surface."""
    monkeypatch.setenv("REQUEST_MAX_BODY_BYTES", str(max_bytes))
    from importlib import reload
    from fastapi import FastAPI
    import main as main_mod

    reload(main_mod)
    return main_mod.app


def _send_chunked(client, path: str, chunks: Iterable[bytes]):
    """Issue a POST with ``Transfer-Encoding: chunked`` via httpx's
    ``content`` iterator. No ``Content-Length`` header is sent when a
    generator is passed, which is exactly the shape our guard must
    catch."""
    def _gen():
        yield from chunks

    return client.post(path, content=_gen())


def _register_echo(app):
    """Inner handler that always 200s if the body is readable. We use
    it to prove that the ASGI guard does not leak a short-circuited
    413 onto routes that *should* succeed under the cap. We bypass
    FastAPI's Pydantic-based body parsing by grabbing the raw
    Starlette Request object — the guard operates at the ASGI layer,
    so FastAPI-level JSON validation is out of scope for this test.
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _echo(request: Request):  # pragma: no cover — trivial
        body = await request.body()
        return JSONResponse({"len": len(body)})

    # Use a Starlette-level Route so FastAPI's parameter dependency
    # machinery does not try to validate the body as JSON.
    app.router.routes.append(Route("/_test_echo", _echo, methods=["POST"]))


@pytest.fixture
def app_tiny_cap(monkeypatch):
    app = _build_app(monkeypatch, max_bytes=1024)
    _register_echo(app)
    yield app


class TestBodySizeContentLength:
    def test_accepts_body_under_cap(self, app_tiny_cap):
        from starlette.testclient import TestClient

        with TestClient(app_tiny_cap) as client:
            r = client.post("/_test_echo", content=b"x" * 512)
        assert r.status_code == 200
        assert r.json()["len"] == 512

    def test_rejects_content_length_above_cap(self, app_tiny_cap):
        from starlette.testclient import TestClient

        with TestClient(app_tiny_cap) as client:
            r = client.post("/_test_echo", content=b"x" * 4096)
        assert r.status_code == 413
        assert "too large" in r.text.lower()


class TestBodySizeChunked:
    """The whole point of Z-3: chunked uploads must be capped even
    though they carry no ``Content-Length`` header."""

    def test_chunked_under_cap_succeeds(self, app_tiny_cap):
        from starlette.testclient import TestClient

        with TestClient(app_tiny_cap) as client:
            r = _send_chunked(
                client,
                "/_test_echo",
                [b"a" * 200, b"b" * 200, b"c" * 200],  # 600 bytes total
            )
        assert r.status_code == 200

    def test_chunked_breach_returns_413(self, app_tiny_cap):
        """Four 400-byte chunks = 1600 bytes, cap=1024. The cumulative
        tally on the third chunk trips the cap and the inner handler
        sees an empty body — the guard then rewrites the outgoing
        response to 413."""
        from starlette.testclient import TestClient

        with TestClient(app_tiny_cap) as client:
            r = _send_chunked(
                client,
                "/_test_echo",
                [b"z" * 400, b"z" * 400, b"z" * 400, b"z" * 400],
            )
        assert r.status_code == 413


class TestBodySizeGetIsUnaffected:
    """GET requests never have a body we care about — the guard must
    not interfere."""

    def test_get_passes_through(self, app_tiny_cap):
        from starlette.testclient import TestClient

        with TestClient(app_tiny_cap) as client:
            r = client.get("/health")
        assert r.status_code == 200
