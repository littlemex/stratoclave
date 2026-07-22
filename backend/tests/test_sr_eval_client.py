"""Unit tests for the A' decide path: eval client + decision map + adapter.decide.

All hermetic — the eval HTTP transport is stubbed via set_transport_hook, so no
real router is needed. Proves: response-shape parsing (3 forms), fail-open on
every failure mode, decision→registry mapping (explicit + identity + unmapped),
and that decide() returns a SOFT prefer_model that can never remove a model.
"""
from __future__ import annotations

import pytest

from mvp.sr import adapter, eval_client
from mvp.sr.decision_map import normalize_decision, validate_map_against_registry
from mvp.sr.eval_client import EvalOutcome, parse_eval_response
from mvp.sr.port import NO_DECISION


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    eval_client.reset_for_test()
    monkeypatch.setenv("SEMANTIC_ROUTER_BASE_URL", "http://sr:8080")
    yield
    eval_client.reset_for_test()


# ------------------------------------------------------------------ parse
def test_parse_current_decision_result():
    body = {"decision_result": {"decision_name": "reasoning", "used_signals": {}},
            "signal_confidences": {"complexity": 0.9}}
    assert parse_eval_response(body).decision_name == "reasoning"


def test_parse_routing_decision_string():
    assert parse_eval_response({"routing_decision": "cheap-chat"}).decision_name == "cheap-chat"


def test_parse_routing_decision_dict_model():
    assert parse_eval_response({"routing_decision": {"model": "claude-haiku-4-5"}}).decision_name \
        == "claude-haiku-4-5"


def test_parse_legacy_chat_completion_model():
    body = {"object": "chat.completion", "model": "claude-opus-4-7"}
    assert parse_eval_response(body).decision_name == "claude-opus-4-7"


def test_parse_no_decision_present():
    assert parse_eval_response({"signal_confidences": {}}).decision_name is None
    assert parse_eval_response({}).decision_name is None
    assert parse_eval_response("garbage").decision_name is None  # type: ignore[arg-type]


def test_parse_decision_result_wins_over_routing():
    body = {"decision_result": {"decision_name": "A"}, "routing_decision": "B"}
    assert parse_eval_response(body).decision_name == "A"


def test_parse_live_v03_response_prefers_recommended_models():
    # The EXACT shape a live vLLM SR v0.3 router returned from /api/v1/eval:
    # recommended_models is the concrete recommendation and wins over the rule name.
    live = {
        "original_text": "What is the derivative of x^2?",
        "decision_result": {"decision_name": "default-route", "used_signals": {},
                            "matched_signals": {}, "unmatched_signals": {}},
        "recommended_models": ["sim-default"],
        "routing_decision": "default-route",
        "metrics": {"complexity": {"execution_time_ms": 0, "confidence": 0}},
    }
    assert parse_eval_response(live).decision_name == "sim-default"


def test_parse_falls_back_to_decision_name_without_recommended():
    body = {"decision_result": {"decision_name": "reasoning"}, "recommended_models": []}
    assert parse_eval_response(body).decision_name == "reasoning"


# ------------------------------------------------------------------ decision_map
def _known(name):
    return name in {"claude-haiku-4-5", "claude-opus-4-7"}


def test_normalize_explicit_map_wins():
    assert normalize_decision("cheap", decision_map={"cheap": "claude-haiku-4-5"},
                              is_known_model=_known) == "claude-haiku-4-5"


def test_normalize_identity_when_already_a_model():
    assert normalize_decision("claude-opus-4-7", decision_map={},
                              is_known_model=_known) == "claude-opus-4-7"


def test_normalize_unmapped_returns_none():
    assert normalize_decision("mystery-decision", decision_map={},
                              is_known_model=_known) is None


def test_normalize_map_over_identity():
    # even if the name is a known model, an explicit entry redirects it.
    assert normalize_decision("claude-opus-4-7",
                              decision_map={"claude-opus-4-7": "claude-haiku-4-5"},
                              is_known_model=_known) == "claude-haiku-4-5"


def test_validate_map_flags_unpriced_values():
    m = {"a": "claude-haiku-4-5", "b": "not-a-real-model"}
    bad = validate_map_against_registry(m, is_priced_enabled=_known)
    assert bad == ["not-a-real-model"]


def test_validate_map_all_good_is_empty():
    assert validate_map_against_registry({"a": "claude-haiku-4-5"},
                                         is_priced_enabled=_known) == []


# ------------------------------------------------------------------ consult_eval
def test_consult_maps_and_returns_soft_prefer():
    eval_client.set_transport_hook(lambda m, u, t: {"decision_result": {"decision_name": "cheap"}})
    d = eval_client.consult_eval(messages=[{"role": "user", "content": "hi"}],
                                 normalize=lambda n: "claude-haiku-4-5" if n == "cheap" else None)
    assert d.prefer_model == "claude-haiku-4-5"
    assert d.hard_model is None          # SOFT — never removes a servable model
    assert d.origin == "semantic-router"


def test_consult_hard_policy_yields_hard_pin():
    eval_client.set_transport_hook(lambda m, u, t: {"decision_result": {"decision_name": "x"}})
    d = eval_client.consult_eval(messages=[{"role": "user", "content": "hi"}],
                                 normalize=lambda n: "claude-opus-4-7", hard=True)
    assert d.hard_model == "claude-opus-4-7"
    assert d.prefer_model is None


def test_consult_unmapped_decision_fails_open():
    eval_client.set_transport_hook(lambda m, u, t: {"decision_result": {"decision_name": "unknown"}})
    d = eval_client.consult_eval(messages=[{"role": "user", "content": "hi"}],
                                 normalize=lambda n: None)
    assert d is NO_DECISION


def test_consult_transport_error_fails_open():
    def _boom(m, u, t):
        raise RuntimeError("router down")
    eval_client.set_transport_hook(_boom)
    d = eval_client.consult_eval(messages=[{"role": "user", "content": "hi"}],
                                 normalize=lambda n: "claude-haiku-4-5")
    assert d is NO_DECISION


def test_consult_no_base_url_is_noop(monkeypatch):
    monkeypatch.delenv("SEMANTIC_ROUTER_BASE_URL", raising=False)
    eval_client.set_transport_hook(lambda m, u, t: {"decision_result": {"decision_name": "x"}})
    d = eval_client.consult_eval(messages=[{"role": "user", "content": "hi"}],
                                 normalize=lambda n: "claude-haiku-4-5")
    assert d is NO_DECISION


def test_consult_empty_messages_is_noop():
    eval_client.set_transport_hook(lambda m, u, t: {"decision_result": {"decision_name": "x"}})
    assert eval_client.consult_eval(messages=[], normalize=lambda n: "claude-haiku-4-5") is NO_DECISION


# ------------------------------------------------------------------ adapter.decide
def test_decide_off_tenant_is_noop(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SR_FORCE_OFF", "true")
    eval_client.set_transport_hook(lambda m, u, t: {"decision_result": {"decision_name": "x"}})
    d = adapter.decide(tenant_id="acme", session_key=None, requested_model="claude-opus-4-7",
                       has_tool_result=False, messages=[{"role": "user", "content": "hi"}],
                       is_known_model=lambda n: True)
    assert d is NO_DECISION


def test_decide_active_tenant_consults_and_maps(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "active")
    monkeypatch.delenv("STRATOCLAVE_SR_FORCE_OFF", raising=False)
    monkeypatch.setenv("SEMANTIC_ROUTER_DECISION_MAP", '{"cheap": "claude-haiku-4-5"}')
    eval_client.set_transport_hook(lambda m, u, t: {"decision_result": {"decision_name": "cheap"}})
    d = adapter.decide(tenant_id="acme", session_key=None, requested_model="claude-opus-4-7",
                       has_tool_result=False, messages=[{"role": "user", "content": "hi"}],
                       is_known_model=lambda n: n == "claude-haiku-4-5")
    assert d.prefer_model == "claude-haiku-4-5"
    assert d.origin == "semantic-router"


def test_decide_no_messages_is_noop(monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "active")
    monkeypatch.delenv("STRATOCLAVE_SR_FORCE_OFF", raising=False)
    d = adapter.decide(tenant_id="acme", session_key=None, requested_model="claude-opus-4-7",
                       has_tool_result=False)
    assert d is NO_DECISION
