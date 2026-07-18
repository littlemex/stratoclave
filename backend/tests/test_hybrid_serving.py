"""Hybrid serving (self-hosted vLLM) seam — P0.

Proves the seam is correct AND that it is inert with the flag off:

  * flag OFF  => `_attempt_invoke` never touches the vLLM branch and calls the
    Bedrock client, byte-for-byte the old path (differential guard);
  * catalog   => a vLLM entry expands to exactly ONE self-hosted target (no
    cross-region fan-out);
  * servability => a vLLM entry is unservable unless the flag is on AND its
    endpoint_key is in the operator allowlist (SSRF guard);
  * SSE translation => an OpenAI chat.completions stream becomes the SAME
    Bedrock event dicts `normalized_events` consumes, incl. the 4-key usage
    metadata with cache fields hard-zero;
  * exception taxonomy => connect/5xx/timeout -> FAILOVER-classed exceptions;
    4xx -> FATAL-classed; a clean 200 with no usage yields no metadata event;
  * cleanup => the HTTP response is closed on abandonment.

No GPU and no real vLLM: the SSE is fed from in-memory fakes.
"""
from __future__ import annotations

import json

import pytest

from mvp.routing import chains
from mvp.routing.classify import classify
from mvp.routing.types import Disposition, Target
from mvp.serving import vllm


# --------------------------------------------------------------------------
# Endpoint allowlist / servability (SSRF guard)
# --------------------------------------------------------------------------

def test_endpoint_unservable_when_flag_off(monkeypatch):
    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "false")
    monkeypatch.setenv("VLLM_ENDPOINTS", json.dumps({"vllm-primary": "http://vsr:8000"}))
    vllm.reset_for_test()
    assert vllm.hybrid_serving_enabled() is False
    assert vllm.endpoint_is_servable("vllm-primary") is False


def test_endpoint_servable_only_with_flag_and_allowlisted_key(monkeypatch):
    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "true")
    monkeypatch.setenv("VLLM_ENDPOINTS", json.dumps({"vllm-primary": "http://vsr:8000"}))
    vllm.reset_for_test()
    assert vllm.endpoint_is_servable("vllm-primary") is True
    # A key NOT in the allowlist is never servable — no URL can be invented.
    assert vllm.endpoint_is_servable("evil-key") is False
    assert vllm.endpoint_is_servable(None) is False


def test_endpoints_rejects_non_http_and_malformed(monkeypatch):
    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "true")
    # file:// scheme is dropped (SSRF); a non-JSON value yields an empty map.
    monkeypatch.setenv("VLLM_ENDPOINTS", json.dumps({"bad": "file:///etc/passwd", "ok": "http://h:8000"}))
    vllm.reset_for_test()
    assert vllm.endpoints() == {"ok": "http://h:8000"}
    monkeypatch.setenv("VLLM_ENDPOINTS", "not json")
    vllm.reset_for_test()
    assert vllm.endpoints() == {}


# --------------------------------------------------------------------------
# Catalog gate: vLLM entry -> exactly one self-hosted target
# --------------------------------------------------------------------------

def _vllm_registry():
    from mvp.models import ModelEntry

    return (
        ModelEntry(
            provider="openai", bedrock_model_id="vllm-llama-3", bedrock_region="",
            aliases=("vllm-llama-3",), wire_protocol="messages",
            pricing_key="vllm", served_by="vllm", endpoint_key="vllm-primary",
        ),
    )


def test_vllm_entry_expands_to_single_self_hosted_target(monkeypatch):
    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "true")
    monkeypatch.setenv("VLLM_ENDPOINTS", json.dumps({"vllm-primary": "http://vsr:8000"}))
    monkeypatch.setattr("mvp.models._REGISTRY", _vllm_registry())
    vllm.reset_for_test()
    chains.reset_catalog()
    cat = chains.get_catalog()
    targets = cat["vllm-llama-3"]
    assert len(targets) == 1  # NO cross-region fan-out
    t = targets[0]
    assert t.served_by == "vllm"
    assert t.region == "self-hosted"
    assert t.endpoint_key == "vllm-primary"
    chains.reset_catalog()


def test_vllm_registry_requires_endpoint_key():
    from mvp.models import ModelEntry, _validate_registry

    bad = (
        ModelEntry(
            provider="openai", bedrock_model_id="vllm-x", bedrock_region="",
            aliases=("vllm-x",), wire_protocol="messages",
            served_by="vllm", endpoint_key=None,
        ),
    )
    with pytest.raises(ValueError, match="endpoint_key"):
        _validate_registry(bad)


# --------------------------------------------------------------------------
# _attempt_invoke differential: flag OFF => Bedrock path, vLLM branch dead
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_attempt_invoke_flag_off_uses_bedrock(monkeypatch):
    # Low-level defense-in-depth: even if a vLLM target somehow reached
    # _attempt_invoke with the flag off, it must NOT enter the vLLM branch. (At
    # the SYSTEM level a vLLM entry never even gets catalogued when the flag is
    # off — see test_vllm_entry_not_catalogued_when_flag_off — so this target is
    # unreachable in practice; this pins the guard anyway.)
    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "false")
    vllm.reset_for_test()
    from mvp.routing import infrarouter

    called = {"bedrock": False, "vllm": False}

    class _FakeClient:
        def converse_stream(self, **kw):
            called["bedrock"] = True
            return {"stream": iter([])}

    monkeypatch.setattr(infrarouter, "bedrock_client", lambda region: _FakeClient())
    monkeypatch.setattr(vllm, "vllm_invoke", lambda *a, **k: called.__setitem__("vllm", True))

    target = Target(model_id="vllm-x", region="self-hosted", served_by="vllm",
                    endpoint_key="vllm-primary")
    await infrarouter._attempt_invoke(target, {"messages": []})
    assert called["bedrock"] is True
    assert called["vllm"] is False


def test_vllm_entry_not_catalogued_when_flag_off(monkeypatch):
    # SYSTEM-level flag-off inertness: a vLLM registry entry must NOT appear in
    # the routing catalog at all when hybrid serving is off, so a client naming
    # it cannot route a bogus "self-hosted" region into the Bedrock client — it
    # simply resolves as an unknown model.
    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "false")
    monkeypatch.setattr("mvp.models._REGISTRY", _vllm_registry())
    vllm.reset_for_test()
    chains.reset_catalog()
    cat = chains.get_catalog()
    assert "vllm-llama-3" not in cat
    chains.reset_catalog()


def test_vllm_entry_not_catalogued_when_key_not_allowlisted(monkeypatch):
    # Flag on but the endpoint_key is NOT in VLLM_ENDPOINTS => still unservable
    # (SSRF guard: no invented URL), so still absent from the catalog.
    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "true")
    monkeypatch.setenv("VLLM_ENDPOINTS", json.dumps({"other-key": "http://h:8000"}))
    monkeypatch.setattr("mvp.models._REGISTRY", _vllm_registry())
    vllm.reset_for_test()
    chains.reset_catalog()
    cat = chains.get_catalog()
    assert "vllm-llama-3" not in cat
    chains.reset_catalog()


def test_vllm_pin_rejected_400_when_flag_off(monkeypatch):
    # A client x-sc-model-pin naming a vLLM model must be rejected (400
    # unservable) pre-reserve when the flag is off — never routed with a bogus
    # region. _validate_model_pin is the pre-reserve gate.
    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "false")
    monkeypatch.setattr("mvp.models._REGISTRY", _vllm_registry())
    vllm.reset_for_test()
    from fastapi import HTTPException

    from mvp import _pipeline
    from mvp.routing.model_resolver import RoutingConfig

    with pytest.raises(HTTPException) as ei:
        _pipeline._validate_model_pin("vllm-llama-3", RoutingConfig(), "messages")
    assert ei.value.status_code == 400
    assert ei.value.detail["reason"] == "invalid_model_pin"


@pytest.mark.asyncio
async def test_attempt_invoke_flag_on_uses_vllm_for_vllm_target(monkeypatch):
    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "true")
    monkeypatch.setenv("VLLM_ENDPOINTS", json.dumps({"vllm-primary": "http://vsr:8000"}))
    vllm.reset_for_test()
    from mvp.routing import infrarouter

    called = {"bedrock": False, "vllm": False}
    monkeypatch.setattr(infrarouter, "bedrock_client",
                        lambda region: (_ for _ in ()).throw(AssertionError("bedrock hit")))
    monkeypatch.setattr(vllm, "vllm_invoke",
                        lambda t, p: called.__setitem__("vllm", True) or {"stream": iter([])})

    target = Target(model_id="vllm-x", region="self-hosted", served_by="vllm",
                    endpoint_key="vllm-primary")
    await infrarouter._attempt_invoke(target, {"messages": []})
    assert called["vllm"] is True

    # A bedrock target with the flag on still goes to Bedrock.
    monkeypatch.setattr(infrarouter, "bedrock_client",
                        lambda region: type("C", (), {"converse_stream": lambda self, **k: called.__setitem__("bedrock", True) or {"stream": iter([])}})())
    await infrarouter._attempt_invoke(Target(model_id="m", region="us-east-1"), {"messages": []})
    assert called["bedrock"] is True


# --------------------------------------------------------------------------
# Converse -> OpenAI payload translation
# --------------------------------------------------------------------------

def test_converse_to_openai_maps_system_and_inference():
    payload = {
        "system": [{"text": "be terse"}],
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        "inferenceConfig": {"maxTokens": 64, "temperature": 0.2, "topP": 0.9,
                            "stopSequences": ["X"]},
    }
    out = vllm._converse_to_openai("vllm-llama-3", payload)
    assert out["model"] == "vllm-llama-3"
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}
    assert out["messages"][0] == {"role": "system", "content": "be terse"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}
    assert out["max_tokens"] == 64
    assert out["temperature"] == 0.2
    assert out["top_p"] == 0.9
    assert out["stop"] == ["X"]


# --------------------------------------------------------------------------
# SSE translation -> Bedrock event dicts
# --------------------------------------------------------------------------

class _FakeResp:
    """Minimal stand-in for httpx.Response.iter_lines() + close()."""

    def __init__(self, lines, status=200):
        self._lines = lines
        self.status_code = status
        self.closed = False

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def _sse(*objs):
    return [f"data: {json.dumps(o)}" for o in objs] + ["data: [DONE]"]


def test_translate_sse_happy_path_with_usage():
    resp = _FakeResp(_sse(
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 5}},
    ))
    events = list(vllm._translate_sse(resp))
    assert events[0] == {"messageStart": {"role": "assistant"}}
    # text deltas present
    texts = [e["contentBlockDelta"]["delta"]["text"] for e in events
             if "contentBlockDelta" in e]
    assert texts == ["Hel", "lo"]
    stop = next(e for e in events if "messageStop" in e)
    assert stop["messageStop"]["stopReason"] == "end_turn"
    meta = next(e for e in events if "metadata" in e)
    usage = meta["metadata"]["usage"]
    assert usage == {"inputTokens": 12, "outputTokens": 5,
                     "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
    assert resp.closed is True


def test_translate_sse_missing_usage_emits_no_metadata():
    resp = _FakeResp(_sse(
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ))
    events = list(vllm._translate_sse(resp))
    assert not any("metadata" in e for e in events)  # finalizer settles at reserve
    stop = next(e for e in events if "messageStop" in e)
    assert stop["messageStop"]["stopReason"] == "max_tokens"
    assert resp.closed is True


def test_translate_sse_empty_stream_raises_failover():
    resp = _FakeResp(["data: [DONE]"])
    with pytest.raises(RuntimeError, match="empty stream"):
        list(vllm._translate_sse(resp))
    # empty-stream RuntimeError is classified FAILOVER by the existing branch
    assert classify(RuntimeError("empty stream: vllm"),
                    Target(model_id="m", region="self-hosted")) == Disposition.FAILOVER
    assert resp.closed is True


def test_translate_sse_malformed_frame_raises_connectionerror():
    resp = _FakeResp(["data: {not json"])
    with pytest.raises(ConnectionError):
        list(vllm._translate_sse(resp))
    assert resp.closed is True


def test_translate_sse_malformed_usage_numeric_fails_over_not_fatal():
    # A non-numeric usage field must NOT raise a raw ValueError/TypeError (which
    # classify() would map to FATAL=500); it must become a FAILOVER-classed
    # ConnectionError so a misbehaving self-hosted endpoint fails over cleanly.
    resp = _FakeResp(_sse(
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": "n/a", "completion_tokens": 5}},
    ))
    with pytest.raises(ConnectionError):
        list(vllm._translate_sse(resp))
    assert classify(ConnectionError("vllm malformed usage chunk: x"),
                    Target(model_id="m", region="self-hosted")) == Disposition.FAILOVER
    assert resp.closed is True


# --------------------------------------------------------------------------
# Exception taxonomy: vLLM boundary errors classify to the right disposition
# --------------------------------------------------------------------------

@pytest.mark.parametrize("exc,expected", [
    (ConnectionError("vllm 503"), Disposition.FAILOVER),
    (TimeoutError("vllm read timeout"), Disposition.FAILOVER),
    (OSError("connreset"), Disposition.FAILOVER),
    (vllm.VllmClientError("vllm 400"), Disposition.FATAL),
])
def test_vllm_exceptions_classify_correctly(exc, expected):
    assert classify(exc, Target(model_id="m", region="self-hosted")) == expected


# --------------------------------------------------------------------------
# End-to-end (router): a single-target vLLM whose endpoint is down exhausts the
# chain and RAISES (no silent success, no fan-out to nonexistent regions).
# --------------------------------------------------------------------------

def test_vllm_dead_endpoint_exhausts_chain_and_raises(monkeypatch):
    import asyncio

    monkeypatch.setenv("HYBRID_SERVING_ENABLED", "true")
    monkeypatch.setenv("VLLM_ENDPOINTS", json.dumps({"vllm-primary": "http://vsr:8000"}))
    monkeypatch.setattr("mvp.models._REGISTRY", _vllm_registry())
    vllm.reset_for_test()
    chains.reset_catalog()

    from mvp.routing.infrarouter import route_stream
    from mvp.routing.types import RouteRequest

    # The vLLM endpoint is down: vllm_invoke raises a FAILOVER-classed error.
    def _dead(target, payload):
        raise ConnectionError("vllm connect failed: refused")

    monkeypatch.setattr(vllm, "vllm_invoke", _dead)
    # Guard: Bedrock must NEVER be hit for a vLLM target.
    monkeypatch.setattr("mvp.routing.infrarouter.bedrock_client",
                        lambda region: (_ for _ in ()).throw(AssertionError("bedrock hit for vllm")))

    req = RouteRequest(
        alias="vllm-llama-3",
        payload={"messages": [], "inferenceConfig": {"maxTokens": 16}},
        tenant_id="t", request_id="rv1",
    )
    # Single self-hosted target, no cross-region alternates => chain exhausts
    # and the FAILOVER exception is re-raised (a clean request failure, never a
    # silent success or a settle against a different model).
    with pytest.raises(ConnectionError):
        asyncio.run(route_stream(req))

    chains.reset_catalog()
