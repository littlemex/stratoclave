"""C13.6: every provider call carries the correlation marker, on every transport.

The marker names the tenant, the attempt's money hold and the gateway's own user
id (`provider_outcome.MARKER_KEYS`). Bedrock echoes it into model invocation log
records, which is what lets an operator attribute a logged call to a tenant and a
user without the gateway holding anything (`docs/design/request-bodies.md`).

How it travels, per transport:

| Transport | Where the marker goes |
|---|---|
| Bedrock Converse, streaming or not | `requestMetadata` |
| The OpenAI-compatible surface on `bedrock-runtime` | the request's `metadata` field |
| A self-hosted endpoint (vLLM) | HTTP headers `provider_outcome.MARKER_HEADERS` |

For the self-hosted row the responsibility splits: the gateway sends the headers
on every inference call; whether they are recorded is decided by whoever runs the endpoint
(a reverse proxy's access log is enough). The gateway stores nothing and reads
nothing back.

The streaming paths used to send no marker at all: they built their own payload
and the marker was attached only on the non-streaming paths. The marker is now
handed to every `invoke_stream` by `_budget_flow.run_stream`, from the hold, so a
streaming route cannot omit it; the AST check below fails if one does not use it.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib

import httpx
import pytest

from mvp import provider_outcome as po

BACKEND = pathlib.Path(__file__).resolve().parents[1]


# --- the marker itself -------------------------------------------------------

def test_the_marker_names_the_attempt_the_tenant_and_the_user():
    assert po.attempt_request_metadata("h-1", "t-1", "u-1") == {
        "sc_attempt_id": "h-1", "sc_tenant": "t-1", "sc_user": "u-1"}
    assert set(po.MARKER_KEYS) == set(po.MARKER_HEADERS)


def test_an_absent_part_is_left_out_rather_than_sent_empty():
    assert po.attempt_request_metadata(None, "t-1", "u-1") == {"sc_tenant": "t-1", "sc_user": "u-1"}
    assert po.attempt_request_metadata(None, None, None) == {}


@pytest.mark.parametrize("bad", ["a|b", "ユーザー", " u", "u ", "a\tb", "a\r\nInjected: 1", "x" * 257, ""])
def test_a_value_bedrock_would_refuse_is_left_out_and_the_rest_still_sent(bad):
    """Bedrock refuses the whole call when one `requestMetadata` value is outside
    its character set (checked on real Bedrock with "a|b" and non-ASCII). A tenant
    id is chosen by an admin, so this happens; the bad part is dropped, never
    altered, and the call goes out with the rest."""
    assert po.attempt_request_metadata("h-1", bad, "u-1") == {"sc_attempt_id": "h-1", "sc_user": "u-1"}


@pytest.mark.parametrize("good", ["default-org", "u@x.com/1+2=3,#$:_-.", "team a", "x" * 256,
                                  "3f2b8c1e-0d4a-4e7b-9c1f-2a6b5d8e9f01"])
def test_ids_inside_the_bedrock_set_are_sent_unchanged(good):
    assert po.attempt_request_metadata(None, good, None) == {"sc_tenant": good}


def test_the_headers_carry_only_the_gateways_keys_and_only_header_safe_values():
    md = {"sc_attempt_id": "h-1", "sc_tenant": "t-1", "sc_user": "u-1", "other": "x"}
    assert po.marker_headers(md) == {
        "X-Stratoclave-Attempt-Id": "h-1", "X-Stratoclave-Tenant": "t-1", "X-Stratoclave-User": "u-1"}
    assert po.marker_headers(None) == {}


@pytest.mark.parametrize("value", ["a|b", "ユーザー", " u ", "a\r\nInjected: 1", "team a", "u@x.com"])
def test_the_headers_and_the_bedrock_marker_follow_one_value_rule(value):
    """A self-hosted endpoint sees exactly the values a Bedrock log record would."""
    sent_to_bedrock = "sc_user" in po.attempt_request_metadata(None, None, value)
    sent_as_header = "X-Stratoclave-User" in po.marker_headers({"sc_user": value})
    assert sent_to_bedrock == sent_as_header


# --- the OpenAI-compatible surface -------------------------------------------

def test_the_openai_metadata_carries_the_user_too():
    payload: dict = {}
    assert po.stamp_openai_metadata_marker(payload, "t-1", "u-1") is True
    assert payload["metadata"]["sc_user"] == "u-1"
    assert payload["metadata"]["sc_tenant"] == "t-1"


def test_the_openai_key_ceiling_now_leaves_room_for_three_keys():
    """Bedrock accepts 16 `metadata` keys. With three marker keys, a caller using
    14 or more of its own gets the request sent without the marker, rather than
    refused (the whole marker, never part of it)."""
    thirteen = {f"k{i}": "v" for i in range(13)}
    fourteen = {f"k{i}": "v" for i in range(14)}
    p13: dict = {"metadata": dict(thirteen)}
    p14: dict = {"metadata": dict(fourteen)}
    assert po.stamp_openai_metadata_marker(p13, "t-1", "u-1") is True
    assert po.stamp_openai_metadata_marker(p14, "t-1", "u-1") is False
    assert not set(po.MARKER_KEYS) & set(p14["metadata"])


def test_a_caller_cannot_forge_the_user_key():
    payload = {"metadata": {"sc_user": "someone-else", " SC_USER ": "x"}}
    po.stamp_openai_metadata_marker(payload, "t-1", "u-1")
    assert payload["metadata"]["sc_user"] == "u-1"
    assert " SC_USER " not in payload["metadata"]


# --- streaming: the hand-off through run_stream ------------------------------

def test_run_stream_hands_the_holds_marker_to_invoke_stream():
    from mvp import _budget_flow

    seen = {}

    class _Hold:
        def provider_call_starting(self):
            pass

        def request_metadata(self):
            return {"sc_tenant": "t-1", "sc_user": "u-1"}

        def claim_unobserved(self, **kw):
            return None

        def claim_stream_interrupted(self, **kw):
            return None

        def claim_settle(self, acc):
            return None

    def invoke(*, body, model_id, request_metadata):
        seen["md"] = request_metadata
        raise RuntimeError("stop here")

    class _Adapter:
        def prologue(self):
            return []

        def error_event(self, message):
            return []

    async def drive():
        try:
            async for _ in _budget_flow.run_stream(body=None, model_id="m", hold=_Hold(),
                                                   invoke_stream=invoke, adapter=_Adapter()):
                pass
        except Exception:
            pass

    asyncio.run(drive())
    assert seen["md"] == {"sc_tenant": "t-1", "sc_user": "u-1"}


STREAMING_MODULES = {"mvp/anthropic.py", "mvp/chat_completions.py"}


def _callee_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")


def _invoke_stream_functions():
    """Every function passed as `invoke_stream=` to `run_stream` under mvp/,
    resolved inside the function that makes the call (where `_invoke` is defined)
    so two same-named helpers in one module cannot stand in for each other."""
    for path in sorted((BACKEND / "mvp").rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for scope in [tree, *scopes]:
            body_nodes = [n for stmt in scope.body for n in ast.walk(stmt)]
            for call in body_nodes:
                if not (isinstance(call, ast.Call) and _callee_name(call) == "run_stream"):
                    continue
                for kw in call.keywords:
                    if kw.arg != "invoke_stream":
                        continue
                    assert isinstance(kw.value, ast.Name), (
                        f"{rel}: invoke_stream= must be a named function this check can read")
                    local = [n for n in body_nodes
                             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and n.name == kw.value.id]
                    assert len(local) == 1, f"{rel}: cannot resolve invoke_stream={kw.value.id}"
                    yield rel, local[0]


def test_every_streaming_route_attaches_the_marker_it_is_handed():
    found = list(_invoke_stream_functions())
    assert {rel for rel, _ in found} == STREAMING_MODULES, found
    for rel, fn in found:
        params = [a.arg for a in fn.args.kwonlyargs + fn.args.args]
        assert "request_metadata" in params, f"{rel}:{fn.name} does not take request_metadata"
        used = any(isinstance(n, ast.Name) and n.id == "request_metadata" for n in ast.walk(fn))
        assert used, f"{rel}:{fn.name} takes request_metadata and drops it"


def test_the_streaming_messages_route_sends_the_marker_to_converse_stream(monkeypatch, dynamodb_mock):
    """Through the real `/v1/messages` streaming code and the real router, against
    a fake Bedrock client."""
    from mvp import anthropic as anth
    from mvp.routing import infrarouter

    seen = {}

    class _Stream:
        def __iter__(self):
            return iter([{"messageStart": {"role": "assistant"}},
                         {"messageStop": {"stopReason": "end_turn"}},
                         {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}}])

    class _Bedrock:
        def converse_stream(self, **kwargs):
            seen["kwargs"] = kwargs
            return {"stream": _Stream()}

    monkeypatch.setattr(infrarouter, "bedrock_client", lambda region: _Bedrock())
    monkeypatch.setattr(anth, "_settle_reservation_and_log", lambda **kw: None)

    class _Repo:
        hold_id = "hold-MARK"

        def refund(self, **kwargs):
            pass

    class _User:
        user_id = "u-1"
        org_id = "default-org"

    body = anth.AnthropicMessagesRequest.model_validate({
        "model": "us.anthropic.claude-opus-4-7", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16, "stream": True})

    async def drive():
        async for _ in anth._stream_messages(body=body, model_id="us.anthropic.claude-opus-4-7",
                                             user=_User(), tenants_repo=_Repo(), reservation=100):
            pass

    asyncio.run(drive())
    md = seen["kwargs"].get("requestMetadata")
    assert md == {"sc_tenant": "default-org", "sc_user": "u-1", "sc_attempt_id": "hold-MARK"}, seen["kwargs"]


def test_the_streaming_chat_route_sends_the_marker_to_converse_stream(monkeypatch):
    """The twin for streaming `/v1/chat/completions`, which calls Converse
    directly rather than through the router."""
    from mvp import chat_completions as cc

    seen = {}

    class _Bedrock:
        def converse_stream(self, **kwargs):
            seen["kwargs"] = kwargs
            return {"stream": iter([{"messageStart": {"role": "assistant"}},
                                    {"messageStop": {"stopReason": "end_turn"}},
                                    {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}}])}

    monkeypatch.setattr(cc, "deployment_client", lambda *a, **k: _Bedrock())
    monkeypatch.setattr(cc, "_settle_reservation_and_log", lambda **kw: None)
    monkeypatch.setattr(cc, "_release_pool", lambda *a, **k: None, raising=False)

    class _Repo:
        hold_id = "hold-MARK"

        def refund(self, **kwargs):
            pass

    class _User:
        user_id = "u-1"
        org_id = "default-org"

    body = cc.ChatCompletionsRequest.model_validate({
        "model": "us.anthropic.claude-opus-4-7", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16, "stream": True})
    kwargs = {"messages": [{"role": "user", "content": [{"text": "hi"}]}],
              "inferenceConfig": {"maxTokens": 16}}

    async def drive():
        async for _ in cc._stream_chat(body, "us.anthropic.claude-opus-4-7", _User(), _Repo(), 100,
                                       kwargs, entry=None):
            pass

    asyncio.run(drive())
    md = seen["kwargs"].get("requestMetadata")
    assert md == {"sc_tenant": "default-org", "sc_user": "u-1", "sc_attempt_id": "hold-MARK"}, seen["kwargs"]


# --- the OpenAI-compatible surface: every payload build passes the user ------

_OPENAI_BUILDERS = {"_build_openai_chat_payload": 3, "_build_openai_responses_payload": None}


def test_every_openai_payload_build_passes_the_user():
    """The builders default `user_id` to None so a test may call them bare; this
    holds every call in mvp/ to passing one. The behaviour of the value itself is
    pinned through the routes in `test_openai_request_marker.py`."""
    calls = 0
    for path in sorted((BACKEND / "mvp").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and _callee_name(call) in _OPENAI_BUILDERS):
                continue
            calls += 1
            position = _OPENAI_BUILDERS[_callee_name(call)]
            arg = next((kw.value for kw in call.keywords if kw.arg == "user_id"), None)
            if arg is None and position is not None and len(call.args) > position:
                arg = call.args[position]
            where = f"{path.relative_to(BACKEND)}:{call.lineno}"
            assert arg is not None, f"{where} builds an OpenAI payload without the user"
            assert not (isinstance(arg, ast.Constant) and arg.value is None), where
    assert calls >= 4, calls


# --- self-hosted: the headers -------------------------------------------------

def test_a_self_hosted_call_carries_the_marker_as_headers(monkeypatch):
    from mvp.serving import vllm

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content.decode()
        sse = (b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],'
               b'"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    client = httpx.Client(base_url="http://selfhosted.test", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(vllm, "_client_for", lambda key: (client, "http://selfhosted.test"))

    class _Target:
        model_id = "local-model"
        endpoint_key = "local"
        served_by = "vllm"
        region = "self-hosted"

    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "true")
    marker = {"sc_tenant": "tenant-MARK", "sc_attempt_id": "hold-MARK", "sc_user": "user-MARK"}
    payload = {"messages": [{"role": "user", "content": [{"text": "hi"}]}],
               "inferenceConfig": {"maxTokens": 8}, "requestMetadata": marker}

    # Through the router's dispatch, the one way a self-hosted target is reached.
    from mvp.routing import infrarouter

    async def drive():
        out = await infrarouter._attempt_invoke(_Target(), payload)
        for _ in out["stream"]:
            pass

    asyncio.run(drive())
    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers["x-stratoclave-tenant"] == "tenant-MARK"
    assert headers["x-stratoclave-attempt-id"] == "hold-MARK"
    assert headers["x-stratoclave-user"] == "user-MARK"
    body = json.loads(seen["body"])
    assert not set(po.MARKER_KEYS) & set(body) and "requestMetadata" not in body
    assert not any(v in seen["body"] for v in marker.values()), (
        "the marker must not reach the self-hosted model's input")


@pytest.mark.parametrize("module", ["mvp/serving/vllm.py"])
def test_the_self_hosted_transport_takes_its_header_names_from_the_one_table(module):
    """The header names are spelled once, in `provider_outcome.MARKER_HEADERS`."""
    text = (BACKEND / module).read_text(encoding="utf-8")
    assert "X-Stratoclave-" not in text
    tree = ast.parse(text)
    requests = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
                and getattr(c.func, "attr", "") in {"stream", "post", "request", "send"}]
    assert requests, "no request site found"
    for call in requests:
        headers = next((kw.value for kw in call.keywords if kw.arg == "headers"), None)
        carries = headers is not None and any(
            isinstance(n, ast.Call) and _callee_name(n) == "marker_headers" for n in ast.walk(headers))
        assert carries, f"{module}:{call.lineno} sends a request without the marker headers"
