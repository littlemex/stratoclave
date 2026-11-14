"""Issue #247 — the OpenAI-compatible routes must stamp the same per-hold
correlation marker Converse already does, in the OpenAI-standard `metadata`
field, with the caller's own `metadata` preserved.

These tests drive the real route code (not just the pure helpers in
`test_provider_outcome.py`) to pin two things neither unit test alone
proves: that the route actually wires `stamp_openai_metadata_marker` /
`stamp_attempt_id` into the payload it sends, and that the REAL hold id —
not the placeholder — is what reaches the wire once reserve has run.

Without this change, `_build_openai_chat_payload` / `_build_openai_responses_payload`
forward the caller's body plus `model`/`stream` and nothing else, so every
test below that asserts a `metadata` key is present on the sent payload is
red against that baseline — see the PR for the stash-based before/after run.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.chat_completions import ChatCompletionsRequest, _openai_chat_completion
from mvp.models import ModelEntry
from mvp.provider_outcome import ATTEMPT_ID_PLACEHOLDER

REAL_HOLD_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ---------------------------------------------------------------------------
# /v1/chat/completions -> the OpenAI-compatible transport leg
# (`_openai_chat_completion`, same direct-call style as
# test_chat_completions_multiprovider.py)
# ---------------------------------------------------------------------------


def _entry(region="us-east-2", model_id="openai.gpt-5.6-sol"):
    return ModelEntry(
        provider="openai", bedrock_model_id=model_id, bedrock_region=region,
        aliases=("gpt-5.6-sol",), wire_protocol="responses", pricing_key="openai",
    )


def _user(org_id="org-chat-1"):
    return SimpleNamespace(user_id="u1", org_id=org_id)


def _body(*, extra_body: dict[str, Any] | None = None, **kw) -> ChatCompletionsRequest:
    """`ChatCompletionsRequest` allows extras (`extra="allow"`); a caller's
    own top-level `metadata` key arrives exactly this way, not as a declared
    field, so `extra_body` is merged into the raw dict rather than passed as
    a kwarg `ChatCompletionsRequest` would reject."""
    kw.setdefault("model", "gpt-5.6-sol")
    kw.setdefault("messages", [{"role": "user", "content": "hi"}])
    return ChatCompletionsRequest.model_validate({**kw, **(extra_body or {})})


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


_OK = {
    "id": "chatcmpl-x", "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    "model": "openai.gpt-5.6-sol",
}


def _call_chat_completion(body, *, org_id="org-chat-1", hold_id=REAL_HOLD_ID):
    tenants_repo = MagicMock()
    tenants_repo.hold_id = hold_id
    client = MagicMock()
    client.post.return_value = _FakeResponse(200, _OK)
    with patch("mvp._openai_transport.mint_bearer_token", return_value="tok"), \
         patch("mvp._openai_transport.sync_client", return_value=client), \
         patch("mvp.chat_completions._settle_reservation_and_log"), \
         patch("mvp.chat_completions._release_pool"):
        _openai_chat_completion(
            body=body, entry=_entry(), user=_user(org_id), tenants_repo=tenants_repo,
            reservation=1024, corr={}, request_id="req-1",
        )
    return client.post.call_args.kwargs["json"]


class TestChatCompletionsStampsTheMarker:
    def test_sent_payload_carries_the_real_hold_id_and_tenant(self):
        sent = _call_chat_completion(_body())
        assert sent["metadata"]["sc_attempt_id"] == REAL_HOLD_ID, (
            "the placeholder must have been swapped for the real hold id "
            "before the request left the gateway"
        )
        assert sent["metadata"]["sc_tenant"] == "org-chat-1"
        assert sent["metadata"]["sc_user"] == "u1"
        # The placeholder must never actually reach the wire.
        assert ATTEMPT_ID_PLACEHOLDER not in json.dumps(sent)

    def test_callers_own_metadata_is_preserved_beside_the_marker(self):
        sent = _call_chat_completion(_body(extra_body={"metadata": {"order_id": "o-42"}}))
        assert sent["metadata"]["order_id"] == "o-42"
        assert sent["metadata"]["sc_attempt_id"] == REAL_HOLD_ID
        assert sent["metadata"]["sc_tenant"] == "org-chat-1"

    def test_a_caller_metadata_key_matching_ours_is_overwritten_by_the_gateway(self):
        sent = _call_chat_completion(
            _body(extra_body={"metadata": {"sc_attempt_id": "caller-supplied-nonsense"}})
        )
        assert sent["metadata"]["sc_attempt_id"] == REAL_HOLD_ID

    def test_unpooled_tenant_sends_sc_tenant_alone_no_placeholder_no_attempt_id(self):
        # Found on a real machine (an unpooled "accounting" tenant has no
        # `hold_id` at all -- `mvp._pipeline.ReservationContext` only sets it
        # on the dollar-pool branch, equally true for Converse), then pinned
        # here. The wire body must carry `sc_tenant` alone: no
        # `sc_attempt_id`, and definitely not the placeholder.
        sent = _call_chat_completion(_body(), hold_id=None)
        assert "sc_attempt_id" not in sent["metadata"]
        assert ATTEMPT_ID_PLACEHOLDER not in json.dumps(sent)
        assert sent["metadata"]["sc_tenant"] == "org-chat-1"

    def test_forgery_is_blocked_at_the_wire_on_the_ceiling_drop_path(self):
        # The pure-function forgery tests in test_provider_outcome.py prove
        # `merge_openai_metadata` itself is safe; this proves the full
        # builder -> reserve -> send pipeline is too, with a real (mocked
        # transport) send at the end. 15 filler keys + a forged `sc_tenant`
        # pushes the caller to 16 keys; after the forged key is stripped and
        # the gateway's 2 keys are counted (15 + 2 = 17 > 16), the whole
        # marker is dropped and the caller's filler survives, but the forged
        # value must not.
        filler = {f"k{i}": "v" for i in range(15)}
        filler["sc_tenant"] = "forged-victim-tenant"
        sent = _call_chat_completion(_body(extra_body={"metadata": filler}))
        assert sent["metadata"].get("sc_tenant") != "forged-victim-tenant"
        assert "sc_tenant" not in sent["metadata"]
        assert "sc_attempt_id" not in sent["metadata"]
        assert all(sent["metadata"][f"k{i}"] == "v" for i in range(15))


# ---------------------------------------------------------------------------
# The Converse leg of `/v1/chat/completions` and `/v1/messages` never passes
# `payload=` to `_open_hold`, so the consolidation that made `_open_hold`
# call `stamp_attempt_id` must be a complete no-op for it (round-3 review:
# "add one test asserting that").
# ---------------------------------------------------------------------------


def test_open_hold_without_payload_never_touches_provider_outcome():
    from mvp.chat_completions import _open_hold

    tenants_repo = MagicMock()
    tenants_repo.hold_id = REAL_HOLD_ID
    with patch("mvp.chat_completions._settle_reservation_and_log"), \
         patch("mvp.chat_completions._release_pool"), \
         patch("mvp.provider_outcome.stamp_attempt_id") as stamp:
        _open_hold(
            task_tag=None,
            user=_user(), tenants_repo=tenants_repo, reservation=1024,
            model_id="us.anthropic.claude-haiku-4-5", route="chat_completions",
        )
    stamp.assert_not_called()


# ---------------------------------------------------------------------------
# Streaming leg of `/v1/chat/completions` -- same hold, same payload object,
# the other branch of `_openai_chat_completion`. A mutation that deletes the
# `stamp_attempt_id` call or that only wires it into the non-streaming branch
# must fail here (round-1 review finding: streaming was unexercised).
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    def __init__(self, status_code, lines):
        self.status_code = status_code
        self._lines = lines
        self.text = ""

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    def __init__(self, resp, capture):
        self._resp, self.capture = resp, capture

    def stream(self, method, url, json=None, headers=None):
        self.capture["payload"] = json
        return _FakeStreamCtx(self._resp)

    async def aclose(self):
        pass


def test_chat_completions_streaming_also_carries_the_real_hold_id():
    import asyncio

    capture: dict[str, Any] = {}
    lines = ["data: " + json.dumps(
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
    ), "", "data: [DONE]", ""]
    client = _FakeAsyncClient(_FakeStreamResponse(200, lines), capture)
    tenants_repo = MagicMock()
    tenants_repo.hold_id = REAL_HOLD_ID
    with patch("mvp._openai_transport.mint_bearer_token", return_value="tok"), \
         patch("mvp._openai_transport.async_client", return_value=client), \
         patch("mvp.chat_completions._settle_reservation_and_log"), \
         patch("mvp.chat_completions._release_pool"):
        out = _openai_chat_completion(
            body=_body(stream=True), entry=_entry(), user=_user("org-chat-stream"),
            tenants_repo=tenants_repo, reservation=1024, corr={}, request_id="req-1",
        )

        async def drain():
            return [f async for f in out.body_iterator]

        asyncio.run(drain())

    assert capture["payload"]["metadata"]["sc_attempt_id"] == REAL_HOLD_ID
    assert capture["payload"]["metadata"]["sc_tenant"] == "org-chat-stream"
    assert capture["payload"]["metadata"]["sc_user"] == "u1"
    assert ATTEMPT_ID_PLACEHOLDER not in json.dumps(capture["payload"])


# ---------------------------------------------------------------------------
# The byte-invariance claim itself, through the REAL builder and the REAL
# survey function (no mocks): `_build_openai_chat_payload` stamps the
# placeholder, `survey_and_hash_openai_chat_payload` surveys it, and the
# payload is then re-stamped with a real-length hold id by `stamp_attempt_id`
# exactly as the route does. A mutation that moves the stamp call to AFTER
# the survey (or turns the swap into an insert) changes the payload's
# serialised length between the two measurements below.
# ---------------------------------------------------------------------------


def test_surveyed_bytes_equal_sent_bytes_through_the_real_pipeline():
    from mvp.chat_completions import _build_openai_chat_payload
    from mvp.provider_outcome import stamp_attempt_id
    from mvp.reservation_bound import envelope_bytes, survey_and_hash_openai_chat_payload

    entry = _entry()
    body = _body(extra_body={"metadata": {"caller_key": "x"}})
    payload, _ = _build_openai_chat_payload(body, entry, "org-bytes", user_id=None, task_tag=None)
    assert payload["metadata"]["sc_attempt_id"] == ATTEMPT_ID_PLACEHOLDER, (
        "the placeholder must already be in the payload the builder hands "
        "to the caller, i.e. BEFORE the survey below ever runs"
    )

    _survey, surveyed_bytes, _hash = survey_and_hash_openai_chat_payload(payload)
    assert surveyed_bytes == envelope_bytes(payload)

    stamp_attempt_id(payload, REAL_HOLD_ID)
    assert payload["metadata"]["sc_attempt_id"] == REAL_HOLD_ID

    sent_bytes = envelope_bytes(payload)
    assert sent_bytes == surveyed_bytes, (
        "the hard-ceiling bound was computed from `surveyed_bytes`; the "
        "payload actually sent must serialise to exactly that many bytes, "
        "not more, not fewer"
    )


# ---------------------------------------------------------------------------
# /openai/v1/responses (full route, via FastAPI TestClient — mirrors
# test_openai_responses_stream_e2e.py's fixtures for the non-streaming leg)
# ---------------------------------------------------------------------------

import os  # noqa: E402

from mvp import openai_responses as orx  # noqa: E402
from mvp.deps import AuthenticatedUser  # noqa: E402

os.environ["STRATOCLAVE_CODEX_ENABLED"] = "true"


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(orx.router)
    return app


def _override_auth(app: FastAPI, user: AuthenticatedUser, monkeypatch: pytest.MonkeyPatch) -> None:
    from mvp import deps, authz

    app.dependency_overrides[deps.get_current_user] = lambda: user
    monkeypatch.setattr(
        authz, "user_has_permission", lambda u, permission: permission == "responses:send")


@pytest.fixture
def stub_auth_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="user-marker",
        email="marker@test.example",
        org_id="org-responses-1",
        roles=["user"],
        raw_claims={},
        auth_kind="jwt",
        key_scopes=None,
        api_key_hash=None,
    )


@pytest.fixture
def _stub_credit_pipeline(monkeypatch: pytest.MonkeyPatch):
    class _StubRepo:
        hold_id = REAL_HOLD_ID

        def refund(self, *, user_id: str, tenant_id: str, tokens: int) -> int:
            return 0

    def _fake_reserve(user, reservation_tokens, **kwargs):  # noqa: ANN001
        return _StubRepo()

    def _fake_settle(*, user, tenants_repo, reservation, actual_input_tokens,
                     actual_output_tokens, model_id, **kwargs):
        return None

    monkeypatch.setattr(orx, "reserve_credit_for_model", _fake_reserve)
    monkeypatch.setattr(orx, "settle_reservation_and_log", _fake_settle)


@pytest.fixture
def capture_openai_request(monkeypatch: pytest.MonkeyPatch):
    """Install a mock transport that records every outgoing request body and
    answers with a fixed, valid Responses-API JSON body."""
    captured: list[dict[str, Any]] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            status_code=200,
            json={
                "id": "resp_1", "object": "response", "status": "completed",
                "output": [], "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )

    transport = httpx.MockTransport(_handler)

    def _fake_openai_client(region: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1",
            transport=transport,
        )

    async def _fake_auth(region: str) -> dict[str, str]:
        return {"Authorization": "Bearer test"}

    monkeypatch.setattr(orx, "_openai_client", _fake_openai_client)
    monkeypatch.setattr(orx, "_openai_auth", _fake_auth)
    return captured


def _post_response(client: TestClient, **extra) -> httpx.Response:
    body = {"model": "openai.gpt-5.6-sol", "input": "hi"}
    body.update(extra)
    return client.post("/openai/v1/responses", json=body)


class TestOpenAIResponsesStampsTheMarker:
    def test_sent_payload_carries_the_real_hold_id_and_tenant(
        self, monkeypatch, stub_auth_user, _stub_credit_pipeline, capture_openai_request
    ):
        app = _make_app()
        _override_auth(app, stub_auth_user, monkeypatch)
        client = TestClient(app)

        resp = _post_response(client)
        assert resp.status_code == 200, resp.text

        assert len(capture_openai_request) == 1
        sent = capture_openai_request[0]
        assert sent["metadata"]["sc_attempt_id"] == REAL_HOLD_ID
        assert sent["metadata"]["sc_tenant"] == "org-responses-1"
        assert sent["metadata"]["sc_user"] == "user-marker"
        assert ATTEMPT_ID_PLACEHOLDER not in json.dumps(sent)

    def test_callers_own_metadata_is_preserved_beside_the_marker(
        self, monkeypatch, stub_auth_user, _stub_credit_pipeline, capture_openai_request
    ):
        app = _make_app()
        _override_auth(app, stub_auth_user, monkeypatch)
        client = TestClient(app)

        resp = _post_response(client, metadata={"customer_ref": "c-1"})
        assert resp.status_code == 200, resp.text

        sent = capture_openai_request[0]
        assert sent["metadata"]["customer_ref"] == "c-1"
        assert sent["metadata"]["sc_attempt_id"] == REAL_HOLD_ID
        assert sent["metadata"]["sc_tenant"] == "org-responses-1"

    def test_a_caller_metadata_key_matching_ours_is_overwritten_by_the_gateway(
        self, monkeypatch, stub_auth_user, _stub_credit_pipeline, capture_openai_request
    ):
        app = _make_app()
        _override_auth(app, stub_auth_user, monkeypatch)
        client = TestClient(app)

        resp = _post_response(client, metadata={"sc_tenant": "whatever-the-caller-sent"})
        assert resp.status_code == 200, resp.text

        sent = capture_openai_request[0]
        assert sent["metadata"]["sc_tenant"] == "org-responses-1"


# ---------------------------------------------------------------------------
# Streaming leg of /openai/v1/responses (`_stream_response`) — a separate
# hold-opening code path from the non-streaming one above; the
# `stamp_attempt_id` call there is wired independently (round-1 review
# finding: streaming was unexercised).
# ---------------------------------------------------------------------------


@pytest.fixture
def capture_openai_stream_request(monkeypatch: pytest.MonkeyPatch):
    captured: list[dict[str, Any]] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        sse = (
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":'
            b'{"usage":{"input_tokens":5,"output_tokens":3}}}\n\n'
        )
        return httpx.Response(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            content=sse,
        )

    transport = httpx.MockTransport(_handler)

    def _fake_openai_client(region: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1",
            transport=transport,
        )

    async def _fake_auth(region: str) -> dict[str, str]:
        return {"Authorization": "Bearer test"}

    monkeypatch.setattr(orx, "_openai_client", _fake_openai_client)
    monkeypatch.setattr(orx, "_openai_auth", _fake_auth)
    return captured


def test_openai_responses_carries_the_callers_task_tag(
    monkeypatch, stub_auth_user, _stub_credit_pipeline, capture_openai_request
):
    app = _make_app()
    _override_auth(app, stub_auth_user, monkeypatch)
    client = TestClient(app)
    resp = client.post("/openai/v1/responses", headers={"x-sc-task-tag": "nightly-eval"},
                       json={"model": "openai.gpt-5.6-sol", "input": "hi"})
    assert resp.status_code == 200, resp.text
    assert capture_openai_request[0]["metadata"]["sc_task_tag"] == "nightly-eval"


def test_openai_responses_streaming_also_carries_the_real_hold_id(
    monkeypatch, stub_auth_user, _stub_credit_pipeline, capture_openai_stream_request
):
    app = _make_app()
    _override_auth(app, stub_auth_user, monkeypatch)
    client = TestClient(app)

    with client.stream(
        "POST", "/openai/v1/responses",
        json={"model": "openai.gpt-5.6-sol", "input": "hi", "stream": True},
    ) as resp:
        assert resp.status_code == 200, resp.read()
        list(resp.iter_bytes())  # drain so the generator runs to completion

    assert len(capture_openai_stream_request) == 1
    sent = capture_openai_stream_request[0]
    assert sent["metadata"]["sc_attempt_id"] == REAL_HOLD_ID
    assert sent["metadata"]["sc_tenant"] == "org-responses-1"
    assert sent["metadata"]["sc_user"] == "user-marker"
    assert ATTEMPT_ID_PLACEHOLDER not in json.dumps(sent)


def test_surveyed_bytes_equal_sent_bytes_for_responses_payload_too():
    from mvp.openai_responses import OpenAIResponsesRequest, _build_openai_responses_payload
    from mvp.provider_outcome import stamp_attempt_id
    from mvp.reservation_bound import envelope_bytes, survey_and_hash_openai_responses_payload

    body = OpenAIResponsesRequest.model_validate(
        {"model": "gpt-5.6-sol", "input": "hi", "metadata": {"caller_key": "x"}}
    )
    payload = _build_openai_responses_payload(
        body, _entry(), stream=False, tenant_id="org-bytes-resp", user_id=None, task_tag=None
    )
    assert payload["metadata"]["sc_attempt_id"] == ATTEMPT_ID_PLACEHOLDER

    _survey, surveyed_bytes, _hash = survey_and_hash_openai_responses_payload(payload)
    assert surveyed_bytes == envelope_bytes(payload)

    stamp_attempt_id(payload, REAL_HOLD_ID)
    assert envelope_bytes(payload) == surveyed_bytes
