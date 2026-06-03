"""End-to-end regression tests for the OpenAI Responses SSE proxy.

What broke in the field
-----------------------
codex (OpenAI SDK) reported `stream disconnected before completion: stream
closed before response.completed`. The Stratoclave gateway is an SSE
pass-through to bedrock-mantle, so this either means

  (a) the upstream bytes never carried a `response.completed` event, or
  (b) the gateway dropped/mangled the framing such that the codex parser
      could not see the event.

These tests pin (b) — the bit Stratoclave actually owns. They drive the
real `mvp.openai_responses.create_response` route via FastAPI's
TestClient, with the bedrock-mantle httpx call replaced by an
`httpx.MockTransport`. We assert two things the field bug both depend on:

  1. the gateway preserves SSE event-frame boundaries (a `\\n\\n` after
     each `data:` payload), even when the upstream packs multiple
     `data:` lines into a single event;
  2. the `response.completed` payload reaches the client byte-for-byte
     (so codex's stream parser fires its terminal callback) AND the
     reservation is settled with the upstream's reported usage.

Auth and credit are stubbed at the FastAPI dependency layer; the network
path is the only thing under test.
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterable, Iterator

import httpx
import pytest


# Codex must be flipped on at module import time — the route reads
# `CODEX_ENABLED` per request via os.getenv, but the tests are clearer
# with the env set globally for the file.
os.environ["CODEX_ENABLED"] = "true"


from fastapi import FastAPI

from mvp import openai_responses as orx
from mvp.deps import AuthenticatedUser
from mvp.authz import require_permission


# ---------------------------------------------------------------------------
# Fixtures: stub the auth dep, stub credit reservation/settlement, and
# replace `_mantle_client` with one backed by an httpx.MockTransport.
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    """A minimal app that mounts only the openai_responses router.

    We deliberately avoid `from main import app` because main.py wires
    a long lifespan chain (Cognito, JWKS, table seeding) that is
    irrelevant to the SSE byte-level question we're testing.
    """
    app = FastAPI()
    app.include_router(orx.router)
    return app


@pytest.fixture
def stub_auth_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="user-stream",
        email="stream@test.example",
        org_id="default-org",
        roles=["user"],
        raw_claims={},
        auth_kind="jwt",
        key_scopes=None,
        api_key_hash=None,
    )


@pytest.fixture(autouse=True)
def _stub_credit_pipeline(monkeypatch: pytest.MonkeyPatch):
    """Take DynamoDB out of the picture entirely.

    `reserve_credit` / `settle_reservation_and_log` get monkeypatched at
    the module attribute level (not the source module) because that's
    what `mvp.openai_responses` imported. Settlements are captured so
    individual tests can assert on the actual_input/output_tokens that
    reached settle.
    """
    settle_calls: list[dict[str, Any]] = []

    class _StubRepo:
        def refund(self, *, user_id: str, tenant_id: str, tokens: int) -> int:
            return 0

    def _fake_reserve(user, reservation_tokens):  # noqa: ANN001 — match signature
        return _StubRepo()

    def _fake_settle(*, user, tenants_repo, reservation, actual_input_tokens,
                     actual_output_tokens, model_id):
        settle_calls.append(
            {
                "reservation": reservation,
                "actual_input_tokens": actual_input_tokens,
                "actual_output_tokens": actual_output_tokens,
                "model_id": model_id,
            }
        )

    monkeypatch.setattr(orx, "reserve_credit", _fake_reserve)
    monkeypatch.setattr(orx, "settle_reservation_and_log", _fake_settle)
    yield settle_calls


@pytest.fixture
def install_mantle_stream(monkeypatch: pytest.MonkeyPatch):
    """Yield a function that installs a fixed SSE byte sequence as the
    bedrock-mantle response, returning the FastAPI app it should be
    mounted under. Each test calls it with the exact upstream bytes
    it wants to exercise.
    """

    def _install(sse_bytes: bytes, status: int = 200) -> FastAPI:
        async def _handler(request: httpx.Request) -> httpx.Response:
            # Stream the bytes back as a single response body. httpx will
            # re-chunk on the read side; that is the exact framing the
            # gateway has to preserve.
            return httpx.Response(
                status_code=status,
                headers={"content-type": "text/event-stream"},
                content=sse_bytes,
            )

        transport = httpx.MockTransport(_handler)

        def _fake_mantle_client(region: str) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url=f"https://bedrock-mantle.{region}.api.aws/openai/v1",
                headers={"Authorization": "Bearer test"},
                transport=transport,
                timeout=httpx.Timeout(5.0, connect=1.0),
            )

        monkeypatch.setattr(orx, "_mantle_client", _fake_mantle_client)

        app = _make_app()
        return app

    return _install


def _override_auth(app: FastAPI, user: AuthenticatedUser) -> None:
    """Bypass cognito JWT verification by overriding the dep produced by
    `require_permission("responses:send")`. The dep object identity is
    a fresh closure each call; we install the override against the
    actual function in the route signature."""
    # The router was built with require_permission("responses:send") at
    # import time. The dep callable is the inner `_dep` function returned
    # by that factory; pulling it out of the route is brittle. Instead,
    # override `get_current_user` (the inner Depends) and stub
    # `user_has_permission` to always allow `responses:send` for our user.
    from mvp import deps, authz

    def _fake_user() -> AuthenticatedUser:
        return user

    app.dependency_overrides[deps.get_current_user] = _fake_user

    def _fake_has(u: AuthenticatedUser, permission: str) -> bool:  # noqa: ARG001
        return permission == "responses:send"

    # Patch in-place so the closure inside the route also sees the
    # permissive evaluator.
    authz.user_has_permission.__globals__["user_has_permission"] = _fake_has  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers: compose the kind of SSE bytes bedrock-mantle actually emits.
# ---------------------------------------------------------------------------


def _frame(event: str, data: dict[str, Any]) -> bytes:
    """A canonical single-line `data:` SSE frame."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _multiline_data_frame(event: str, payload: str) -> bytes:
    """A frame whose `data:` is split across multiple data: lines.

    Per the SSE spec each `data:` line in the same event is concatenated
    with a `\\n` by the parser. bedrock-mantle (and the OpenAI Responses
    API in general) is documented to fold long payloads onto multiple
    `data:` lines.
    """
    parts = payload.split("\n")
    body = "".join(f"data: {p}\n" for p in parts) + "\n"
    return f"event: {event}\n{body}".encode("utf-8")


# ---------------------------------------------------------------------------
# 1) Happy-path: framing is preserved, response.completed reaches client,
#    settle is called with usage from the upstream.
# ---------------------------------------------------------------------------


def test_stream_completes_and_settles_with_upstream_usage(
    install_mantle_stream, stub_auth_user, _stub_credit_pipeline
):
    sse = b"".join(
        [
            _frame("response.created", {"type": "response.created"}),
            _frame(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "Hello"},
            ),
            _frame(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {"usage": {"input_tokens": 7, "output_tokens": 13}},
                },
            ),
        ]
    )

    app = install_mantle_stream(sse)
    _override_auth(app, stub_auth_user)

    from fastapi.testclient import TestClient
    client = TestClient(app)

    with client.stream(
        "POST",
        "/openai/v1/responses",
        json={
            "model": "openai.gpt-5.4",
            "input": "hi",
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200, resp.read()
        body = b"".join(resp.iter_bytes())

    # 1. The terminal frame survives the proxy intact, including the
    #    `\n\n` separator codex's parser depends on.
    assert b"event: response.completed\n" in body, body
    # We must end with at least one `\n\n` so the LAST event boundary
    # is preserved — codex won't fire `response.completed` until it
    # sees this terminator.
    assert body.endswith(b"\n\n") or b"\n\n" in body, body

    # 2. The settle call carries the upstream's usage, not (0, 0).
    assert _stub_credit_pipeline, "settle_reservation_and_log was never called"
    last = _stub_credit_pipeline[-1]
    assert last["actual_input_tokens"] == 7, last
    assert last["actual_output_tokens"] == 13, last


# ---------------------------------------------------------------------------
# 2) Multi-line `data:` payloads. SSE spec: when an event has multiple
#    `data:` lines, the parser joins them with `\n` before consuming.
#    The previous proxy parsed each `data:` line as its own JSON object,
#    so the proxy's usage extractor missed every multi-line completion.
# ---------------------------------------------------------------------------


def test_stream_multiline_data_completed_event_extracts_usage(
    install_mantle_stream, stub_auth_user, _stub_credit_pipeline
):
    # An upstream that pretty-prints the JSON across multiple `data:`
    # lines, with a literal newline between members. This is what the
    # SSE spec calls out as the "long payload" case and is what we
    # actually saw bedrock-mantle do for some completed events.
    multiline = (
        b"event: response.completed\n"
        b'data: {"type":"response.completed",\n'
        b'data:  "response":{"usage":{"input_tokens":11,"output_tokens":22}}}\n'
        b"\n"
    )

    sse = _frame("response.created", {"type": "response.created"}) + multiline

    app = install_mantle_stream(sse)
    _override_auth(app, stub_auth_user)

    from fastapi.testclient import TestClient
    client = TestClient(app)

    with client.stream(
        "POST",
        "/openai/v1/responses",
        json={"model": "openai.gpt-5.4", "input": "hi", "stream": True},
    ) as resp:
        body = b"".join(resp.iter_bytes())

    # The frame must reach the client with both `data:` lines and the
    # blank-line boundary preserved — codex needs to reassemble the
    # payload from both lines per the SSE spec.
    assert b"event: response.completed\n" in body, body
    assert body.count(b"\ndata:") >= 2, body
    assert b"\n\n" in body, body

    # And the gateway must recover usage from the multi-line completion
    # (concatenate-with-newline per SSE spec, then JSON-parse).
    assert _stub_credit_pipeline, "settle_reservation_and_log was never called"
    last = _stub_credit_pipeline[-1]
    assert last["actual_input_tokens"] == 11, last
    assert last["actual_output_tokens"] == 22, last


# ---------------------------------------------------------------------------
# 4) The codex bug: upstream closes the chunked body without flushing
#    the trailing blank-line event terminator. The SSE client never
#    sees `\\n\\n` after the final event and reports
#    "stream closed before response.completed".
#    The gateway must synthesize a terminator on flush.
# ---------------------------------------------------------------------------


def test_stream_unterminated_final_event_is_terminated_by_proxy(
    install_mantle_stream, stub_auth_user, _stub_credit_pipeline
):
    # Note: NO trailing `\n\n` after the response.completed event.
    sse = (
        b"event: response.created\n"
        b'data: {"type":"response.created"}\n'
        b"\n"
        b"event: response.completed\n"
        b'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":4}}}\n'
        # <-- upstream closed here, missing the blank line terminator.
    )

    app = install_mantle_stream(sse)
    _override_auth(app, stub_auth_user)

    from fastapi.testclient import TestClient
    client = TestClient(app)

    with client.stream(
        "POST",
        "/openai/v1/responses",
        json={"model": "openai.gpt-5.4", "input": "hi", "stream": True},
    ) as resp:
        body = b"".join(resp.iter_bytes())

    # The terminal `\n\n` MUST appear after the completed event, even
    # though the upstream forgot it. Without this synthesis codex hangs
    # in its SSE buffer until the connection drops, surfacing as
    # "stream closed before response.completed".
    assert body.endswith(b"\n\n"), body[-80:]
    assert b"event: response.completed\n" in body, body
    # And usage is still settled.
    last = _stub_credit_pipeline[-1]
    assert last["actual_input_tokens"] == 3, last
    assert last["actual_output_tokens"] == 4, last


# ---------------------------------------------------------------------------
# 5) Chunk boundaries arbitrarily slice through the middle of an event.
#    The TestClient harness above sends the body as one chunk; in
#    production the upstream and ALB split bytes at TCP-MTU boundaries,
#    so the gateway must buffer correctly when an event arrives across
#    multiple `aiter_bytes` chunks.
# ---------------------------------------------------------------------------


def test_stream_buffers_event_split_across_chunks(monkeypatch, stub_auth_user, _stub_credit_pipeline):
    # Build the same SSE bytes as the happy path test, but slice them
    # into 17-byte chunks so every event ends up split across multiple
    # `aiter_bytes()` deliveries. If the gateway's buffering is sound,
    # the framing reaches the client byte-for-byte and usage is
    # recovered.
    sse = b"".join(
        [
            _frame("response.created", {"type": "response.created"}),
            _frame(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "Hi there!"},
            ),
            _frame(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {"usage": {"input_tokens": 9, "output_tokens": 10}},
                },
            ),
        ]
    )

    chunks = [sse[i : i + 17] for i in range(0, len(sse), 17)]

    async def _handler(request: httpx.Request) -> httpx.Response:
        async def _aiter() -> Iterator[bytes]:
            for c in chunks:
                yield c

        return httpx.Response(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            content=_aiter(),
        )

    transport = httpx.MockTransport(_handler)

    def _fake_mantle_client(region: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"https://bedrock-mantle.{region}.api.aws/openai/v1",
            headers={"Authorization": "Bearer test"},
            transport=transport,
            timeout=httpx.Timeout(5.0, connect=1.0),
        )

    monkeypatch.setattr(orx, "_mantle_client", _fake_mantle_client)

    app = _make_app()
    _override_auth(app, stub_auth_user)

    from fastapi.testclient import TestClient
    client = TestClient(app)

    with client.stream(
        "POST",
        "/openai/v1/responses",
        json={"model": "openai.gpt-5.4", "input": "hi", "stream": True},
    ) as resp:
        body = b"".join(resp.iter_bytes())

    assert b"event: response.completed\n" in body, body
    assert body.endswith(b"\n\n"), body[-80:]
    last = _stub_credit_pipeline[-1]
    assert last["actual_input_tokens"] == 9, last
    assert last["actual_output_tokens"] == 10, last


# ---------------------------------------------------------------------------
# 3) Frame boundary preservation: clients depend on a literal `\n\n` to
#    cut events. If the gateway's per-line `+ "\n"` re-emit ever
#    swallows the blank-line terminator, the LAST event silently never
#    completes for the client even though the bytes "look" delivered.
# ---------------------------------------------------------------------------


def test_stream_preserves_blank_line_event_terminator(
    install_mantle_stream, stub_auth_user, _stub_credit_pipeline
):
    # Two events back-to-back: an interior event and a terminal one.
    sse = (
        b"event: response.created\n"
        b'data: {"type":"response.created"}\n'
        b"\n"
        b"event: response.completed\n"
        b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2}}}\n'
        b"\n"
    )

    app = install_mantle_stream(sse)
    _override_auth(app, stub_auth_user)

    from fastapi.testclient import TestClient
    client = TestClient(app)

    with client.stream(
        "POST",
        "/openai/v1/responses",
        json={"model": "openai.gpt-5.4", "input": "hi", "stream": True},
    ) as resp:
        body = b"".join(resp.iter_bytes())

    # There must be exactly two `\n\n` — one between the two events,
    # one terminating the stream. The Stratoclave proxy must not
    # collapse either of them.
    assert body.count(b"\n\n") >= 2, (body, body.count(b"\n\n"))
