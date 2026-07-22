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


def test_parse_returns_ordered_deduped_candidates():
    # recommended_models[0], decision_name, routing_decision all present + distinct
    # ⇒ ordered candidate list; a repeat is deduped.
    body = {"recommended_models": ["m-rec"], "decision_result": {"decision_name": "rule-x"},
            "routing_decision": "rule-x", "model": "m-legacy"}
    assert parse_eval_response(body).candidates == ("m-rec", "rule-x", "m-legacy")


def test_parse_caps_candidate_length():
    huge = "x" * 5000
    assert len(parse_eval_response({"recommended_models": [huge]}).candidates[0]) == 256


# ------------------------------------------------------------------ candidate fallback
def test_consult_tries_each_candidate_until_one_maps():
    # LIVE shape: recommended_models "sim-default" is UNmapped, but the rule name
    # "default-route" IS mapped. consult must fall through to the rule name — the
    # earlier "first candidate only" behaviour silently returned NO_DECISION here.
    eval_client.set_transport_hook(lambda m, u, t: {
        "recommended_models": ["sim-default"],
        "decision_result": {"decision_name": "default-route"},
        "routing_decision": "default-route"})
    dm = {"default-route": "claude-haiku-4-5"}
    d = eval_client.consult_eval(
        messages=[{"role": "user", "content": "hi"}],
        normalize=lambda n: dm.get(n))
    assert d.prefer_model == "claude-haiku-4-5"


def test_consult_body_size_cap_fails_open(monkeypatch):
    # a hostile oversized body must not be processed — fail open.
    def _huge(m, u, t):
        raise RuntimeError("eval body exceeds size cap")
    eval_client.set_transport_hook(_huge)
    d = eval_client.consult_eval(messages=[{"role": "user", "content": "hi"}],
                                 normalize=lambda n: "claude-haiku-4-5")
    assert d is NO_DECISION


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


# ------------------------------------------------------------------ prepare_eval_messages
def test_prepare_from_string():
    assert adapter.prepare_eval_messages("hi") == [{"role": "user", "content": "hi"}]


def test_prepare_drops_non_text_content():
    # tool_result / image blocks (list content) and non-dict items are dropped;
    # only {role, str-content} survive — no base64/tool payload reaches SR.
    raw = [
        {"role": "user", "content": "text ok"},
        {"role": "user", "content": [{"type": "image", "data": "AAAA"}]},  # dropped
        {"role": "tool", "content": {"toolResult": 1}},                      # dropped
        "not a dict",                                                          # dropped
    ]
    out = adapter.prepare_eval_messages(raw)
    assert out == [{"role": "user", "content": "text ok"}]


def test_prepare_caps_message_count_and_chars():
    raw = [{"role": "user", "content": f"m{i}"} for i in range(50)]
    out = adapter.prepare_eval_messages(raw)
    assert len(out) <= 12                    # last-N cap
    assert out[-1]["content"] == "m49"       # keeps the most recent


def test_prepare_char_budget_trims_from_front():
    raw = [{"role": "user", "content": "x" * 20_000},
           {"role": "user", "content": "recent"}]
    out = adapter.prepare_eval_messages(raw)
    # the huge older message is trimmed so the total is under budget; recent kept.
    assert {"role": "user", "content": "recent"} in out


def test_prepare_garbage_is_empty():
    assert adapter.prepare_eval_messages(12345) == []
    assert adapter.prepare_eval_messages(None) == []


def test_prepare_lone_huge_message_is_truncated_not_dropped():
    # a single user prompt over the char budget must be TRUNCATED (SR still sees it),
    # not dropped to [] (which would make SR silent on the most common case).
    out = adapter.prepare_eval_messages([{"role": "user", "content": "x" * 100_000}])
    assert len(out) == 1
    assert 0 < len(out[0]["content"]) <= 24_000


def test_prepare_drops_tool_role_even_with_string_content():
    # a tool/function message's content is a STRING (tool output) — role allowlist
    # must still drop it so tool results never reach SR (PII egress).
    out = adapter.prepare_eval_messages([
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": "SECRET TOOL OUTPUT", "tool_call_id": "c1"},
        {"role": "function", "content": "SECRET FN OUTPUT"},
    ])
    assert out == [{"role": "user", "content": "hi"}]


def test_prepare_extracts_text_parts():
    # Anthropic/OpenAI text-part lists are extracted; image/tool parts dropped.
    out = adapter.prepare_eval_messages([
        {"role": "user", "content": [
            {"type": "text", "text": "part one"},
            {"type": "image", "source": {"data": "BASE64"}},   # dropped
            {"type": "input_text", "text": "part two"},
        ]},
    ])
    assert out == [{"role": "user", "content": "part one\npart two"}]


# ------------------------------------------------------------------ hard deadline (real socket)
def test_hard_deadline_fires_on_header_slow_drip(monkeypatch):
    """A server that accepts the connection but never sends a response must NOT
    hang the caller past the deadline — the hard future timeout fails open.
    Uses a real listening socket that goes silent after accept (header slow-drip /
    stuck-before-headers is the case per-phase read timeouts cannot catch)."""
    import socket
    import threading as _t
    import time as _time

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop = _t.Event()

    def _accept_and_stall():
        try:
            conn, _ = srv.accept()
            # never send anything; just hold the connection until told to stop.
            while not stop.is_set():
                _time.sleep(0.05)
            conn.close()
        except OSError:
            pass

    th = _t.Thread(target=_accept_and_stall, daemon=True)
    th.start()
    monkeypatch.setenv("SEMANTIC_ROUTER_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SEMANTIC_ROUTER_EVAL_TIMEOUT_S", "0.3")
    eval_client.reset_for_test()   # ensure a real client (no transport hook)

    t0 = _time.monotonic()
    d = eval_client.consult_eval(messages=[{"role": "user", "content": "hi"}],
                                 normalize=lambda n: "claude-haiku-4-5")
    elapsed = _time.monotonic() - t0
    stop.set()
    srv.close()

    assert d is NO_DECISION                 # failed open
    assert elapsed < 2.0, f"deadline not enforced: took {elapsed:.2f}s"
