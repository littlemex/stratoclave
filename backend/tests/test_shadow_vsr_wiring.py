"""Integration tests for the shadow VSR request-path wiring (Fable shadow-wiring
review). Each of the three model routes computes a shadow decision and passes it
to the reserve chokepoint as `vsr_decision`. These tests SPY on that chokepoint:
we replace reserve_credit_for_model with a stub that captures the `vsr_decision`
kwarg and then raises a sentinel, so the handler is exercised up to (and
including) the reserve call WITHOUT needing a real user, DynamoDB, or Bedrock.

Proven per route:
  * flag OFF  -> vsr_decision is None AND shadow.propose is never called (dark).
  * flag ON, simple downgradable request -> vsr_decision is the shadow-advised
    dict (decision ∈ SHADOW_DECISIONS), i.e. potential-only, never a pin.
  * shadow NEVER sets vsr_hard_model (it cannot steer routing).
"""
from __future__ import annotations

import pytest

from mvp.deps import AuthenticatedUser


class _ReserveReached(Exception):
    """Sentinel: raised by the reserve stub to stop the handler right after the
    reserve call, carrying the captured kwargs."""

    def __init__(self, kwargs):
        self.kwargs = kwargs
        super().__init__("reserve reached")


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-1", email="u@example", org_id="test-org",
        roles=["user"], raw_claims={}, auth_kind="cognito",
    )


def _ctx():
    from mvp.observability.context import build_request_context
    return build_request_context(
        tenant_id="test-org", group_id_header=None, workflow_run_id_header=None)


def _dummy_request(headers: dict | None = None):
    from fastapi import Request
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": hdrs})


def _dummy_response():
    from fastapi import Response
    return Response()


def _spy_reserve(monkeypatch, module, symbol):
    """Patch `module.symbol` (the reserve chokepoint binding) with a stub that
    captures kwargs then raises _ReserveReached. Returns nothing; the test reads
    the captured kwargs off the raised exception."""
    def _stub(*a, **k):
        raise _ReserveReached(k)
    monkeypatch.setattr(module, symbol, _stub)


# --------------------------------------------------------------- chat.completions

def _call_chat(model="us.anthropic.claude-opus-4-7", content="hi", tools=None):
    from mvp.chat_completions import ChatCompletionsRequest, chat_completions
    body = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 64}
    if tools is not None:
        body["tools"] = tools
    return chat_completions(
        ChatCompletionsRequest.model_validate(body),
        _dummy_request(), _dummy_response(), user=_user(), ctx=_ctx())


def test_chat_flag_off_passes_none_and_never_calls_propose(monkeypatch):
    from mvp import chat_completions as cc
    from mvp.vsr import shadow
    monkeypatch.delenv("STRATOCLAVE_SHADOW_VSR", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(shadow, "propose", lambda **kw: called.__setitem__("n", called["n"] + 1))
    _spy_reserve(monkeypatch, cc, "reserve_credit_for_model")
    with pytest.raises(_ReserveReached) as e:
        _call_chat()
    assert e.value.kwargs.get("vsr_decision") is None
    assert called["n"] == 0                        # dark: judge never ran


def test_chat_flag_on_attaches_shadow_advised(monkeypatch):
    from mvp import chat_completions as cc
    from mvp.vsr.client import SHADOW_DECISIONS
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")
    _spy_reserve(monkeypatch, cc, "reserve_credit_for_model")
    with pytest.raises(_ReserveReached) as e:
        _call_chat()                               # simple opus request -> downgrade
    vd = e.value.kwargs.get("vsr_decision")
    assert vd is not None and vd["decision"] in SHADOW_DECISIONS
    assert vd["mode"] == "shadow" and vd["rule_id"] == "R2-simple-downgrade"
    # shadow must NEVER set a routing pin.
    assert e.value.kwargs.get("vsr_hard_model") in (None, "")


def test_chat_flag_on_tools_request_is_not_advised(monkeypatch):
    from mvp import chat_completions as cc
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")
    _spy_reserve(monkeypatch, cc, "reserve_credit_for_model")
    with pytest.raises(_ReserveReached) as e:
        _call_chat(tools=[{"type": "function",
                           "function": {"name": "f", "description": "d", "parameters": {}}}])
    # a tool-bearing request is never downgraded -> no shadow advice.
    assert e.value.kwargs.get("vsr_decision") is None


def test_chat_flag_on_pinned_request_is_not_advised(monkeypatch):
    """A deliberate model pin decides routing; shadow must NOT advise on it
    (Fable review-2 (a)/(d)) — else potential is attributed to pinned traffic."""
    from mvp import chat_completions as cc
    from mvp.chat_completions import ChatCompletionsRequest, chat_completions
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")
    _spy_reserve(monkeypatch, cc, "reserve_credit_for_model")
    body = ChatCompletionsRequest.model_validate(
        {"model": "us.anthropic.claude-opus-4-7",
         "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64})
    with pytest.raises(_ReserveReached) as e:
        chat_completions(body, _dummy_request({"x-sc-model-pin": "us.anthropic.claude-opus-4-7"}),
                         _dummy_response(), user=_user(), ctx=_ctx())
    assert e.value.kwargs.get("vsr_decision") is None


def test_chat_flag_on_never_changes_selected_model(monkeypatch):
    """Served-model identity (Fable review-2 (e)): the model handed to reserve is
    the requested one whether the flag is on or off — shadow only annotates the
    decision record, it never reorders/selects a candidate."""
    from mvp import chat_completions as cc
    seen = {}

    def _stub(*a, **k):
        seen["model_name"] = k.get("model_name")
        seen["vsr"] = k.get("vsr_decision")
        raise _ReserveReached(k)
    monkeypatch.setattr(cc, "reserve_credit_for_model", _stub)
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "false")
    with pytest.raises(_ReserveReached):
        _call_chat()
    off_model, off_vsr = seen["model_name"], seen["vsr"]
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")
    with pytest.raises(_ReserveReached):
        _call_chat()
    on_model, on_vsr = seen["model_name"], seen["vsr"]
    assert off_model == on_model               # served model unchanged by the flag
    assert off_vsr is None and on_vsr is not None   # only the annotation differs


# --------------------------------------------------------------- /v1/messages

def _call_anthropic(model="us.anthropic.claude-opus-4-7"):
    from mvp.anthropic import AnthropicMessagesRequest, messages
    body = AnthropicMessagesRequest.model_validate(
        {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64})
    return messages(
        body, _dummy_request(), _dummy_response(), user=_user(), ctx=_ctx())


def test_anthropic_flag_off_passes_none(monkeypatch):
    from mvp import anthropic as an
    from mvp.vsr import shadow
    monkeypatch.delenv("STRATOCLAVE_SHADOW_VSR", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(shadow, "propose", lambda **kw: called.__setitem__("n", called["n"] + 1))
    _spy_reserve(monkeypatch, an, "_reserve_credit_for_model")
    with pytest.raises(_ReserveReached) as e:
        _call_anthropic()
    assert e.value.kwargs.get("vsr_decision") is None
    assert called["n"] == 0


def test_anthropic_flag_on_attaches_shadow_advised(monkeypatch):
    from mvp import anthropic as an
    from mvp.vsr.client import SHADOW_DECISIONS
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR", "true")
    _spy_reserve(monkeypatch, an, "_reserve_credit_for_model")
    with pytest.raises(_ReserveReached) as e:
        _call_anthropic()
    vd = e.value.kwargs.get("vsr_decision")
    assert vd is not None and vd["decision"] in SHADOW_DECISIONS
    # shadow never becomes the enforced pin.
    assert e.value.kwargs.get("vsr_hard_model") in (None, "")
