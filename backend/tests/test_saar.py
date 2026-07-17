"""Session-Aware Agentic Routing (SAAR) — P0 tests.

Covers the five P0 pieces:
  P0-1  feature flag + session-key resolution (+ tenant separation)
  P0-2  SAARMEM store round-trip over moto (fail-open read, monotonic write)
  P0-3  the pure decision state machine (cold / sticky / idle-reset / tool-loop
        hard lock / decision drift)
  P0-4  the stay/switch checkout-delta pricing that gates a switch on budget
  P0-5  the saar_switch_eval decision-log claim + replay headers

The invariants under test (Fable SAAR design §7): hard-lock inviolability,
tenant separation of the memory partition, money-neutrality of the memory/claim
writes, and degenerate-safety (flag off / memory miss ⇒ pre-SAAR behaviour).
"""
from __future__ import annotations

import os

import pytest

from mvp.routing import saar
from mvp.routing.saar import Phase, SaarDecision, SessionMemory


# --------------------------------------------------------------------------- P0-1


def test_saar_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("SAAR_ENABLED", raising=False)
    assert saar.saar_enabled() is False


@pytest.mark.parametrize("val,expected", [("true", True), ("TRUE", True), ("True", True),
                                          ("false", False), ("1", False), ("yes", False), ("", False)])
def test_saar_flag_parse(monkeypatch, val, expected):
    monkeypatch.setenv("SAAR_ENABLED", val)
    assert saar.saar_enabled() is expected


def test_session_partition_is_tenant_only():
    # The partition is derived ONLY from the (server-supplied) tenant id — a
    # client session id can never appear in it (tenant-separation invariant).
    assert saar.session_partition("acme") == "SAARMEM#acme"
    assert saar.session_partition("acme") != saar.session_partition("evil")


def test_session_sort_key_variants():
    assert saar.session_sort_key("s1") == "SESSION#s1"
    assert saar.session_sort_key("s1", user_scoped=True, user_id="u1") == "SESSION#u1#s1"
    # user_scoped without a user id degrades to the shared key (never crashes).
    assert saar.session_sort_key("s1", user_scoped=True, user_id=None) == "SESSION#s1"


def test_session_key_precedence():
    from mvp.observability.context import build_request_context

    # explicit > workflow_run > group
    c = build_request_context(tenant_id="t", group_id_header="g", workflow_run_id_header="wr",
                              session_id_header="sess", request_id="r")
    assert c.session_key() == "sess"
    c = build_request_context(tenant_id="t", group_id_header="g", workflow_run_id_header="wr",
                              session_id_header=None, request_id="r")
    assert c.session_key() == "wr"
    # no session, no run header ⇒ server-minted wr_ (stable, non-empty)
    c = build_request_context(tenant_id="t", group_id_header=None, workflow_run_id_header=None,
                              session_id_header=None)
    assert c.session_key().startswith("wr_")


def test_session_id_malformed_is_rejected():
    from mvp.observability.context import InvalidCorrelationHeader, build_request_context

    with pytest.raises(InvalidCorrelationHeader):
        build_request_context(tenant_id="t", group_id_header=None, workflow_run_id_header=None,
                              session_id_header="has a space")
    with pytest.raises(InvalidCorrelationHeader):
        build_request_context(tenant_id="t", group_id_header=None, workflow_run_id_header=None,
                              session_id_header="inject#key")


# --------------------------------------------------------------------------- P0-2 store


def test_store_roundtrip(dynamodb_mock):
    mem = SessionMemory(
        last_physical_model="us.anthropic.claude-opus-4-7", phase=Phase.TOOL_LOOP,
        matched_decision="code", switch_count=2, turn_count=5, last_turn_at=1_000_000,
        warm_prefix_tokens=1234, rating_version="v1", replay_id="rp1",
    )
    saar.save_session_memory(tenant_id="acme", session_key="s1", mem=mem)
    _drain_saar_writes()
    got = saar.load_session_memory(tenant_id="acme", session_key="s1")
    assert got is not None
    assert got.last_physical_model == mem.last_physical_model
    assert got.phase == Phase.TOOL_LOOP
    assert got.matched_decision == "code"
    assert got.turn_count == 5
    assert got.warm_prefix_tokens == 1234
    assert got.rating_version == "v1"


def test_store_miss_returns_none(dynamodb_mock):
    assert saar.load_session_memory(tenant_id="acme", session_key="nope") is None


def test_store_tenant_isolation(dynamodb_mock):
    m = SessionMemory(last_physical_model="opus", turn_count=1, last_turn_at=1)
    saar.save_session_memory(tenant_id="tenant-a", session_key="shared", mem=m)
    _drain_saar_writes()
    # Same session key, different tenant → different partition → miss.
    assert saar.load_session_memory(tenant_id="tenant-b", session_key="shared") is None
    assert saar.load_session_memory(tenant_id="tenant-a", session_key="shared") is not None


def test_store_monotonic_write_drops_stale_turn(dynamodb_mock):
    newer = SessionMemory(last_physical_model="opus", turn_count=10, last_turn_at=2000)
    saar.save_session_memory(tenant_id="acme", session_key="s", mem=newer)
    _drain_saar_writes()
    # A stale (lower turn_count) write must not overwrite the newer state.
    older = SessionMemory(last_physical_model="haiku", turn_count=3, last_turn_at=1000)
    saar.save_session_memory(tenant_id="acme", session_key="s", mem=older)
    _drain_saar_writes()
    got = saar.load_session_memory(tenant_id="acme", session_key="s")
    assert got.turn_count == 10 and got.last_physical_model == "opus", (
        "stale-turn write overwrote newer state — monotonic guard failed"
    )


def test_store_read_failopen(monkeypatch):
    # No moto here: the resource call will fail. load must return None, not raise.
    assert saar.load_session_memory(tenant_id="acme", session_key="s") is None


def _drain_saar_writes():
    """SAAR writes are fire-and-forget on the shared telemetry executor; block
    until queued writes complete so the assertions see them."""
    import time
    from mvp.learning import signals

    for _ in range(200):
        if signals._slots._value >= (signals._MAX_WORKERS + signals._MAX_QUEUED):
            return
        time.sleep(0.01)


# --------------------------------------------------------------------------- P0-3 decision SM


def test_decide_cold_no_memory():
    d = saar.decide(mem=None, now_epoch=1000, request_has_tool_result=False)
    assert d.hard_model is None and d.reason == "cold"


def test_decide_sticky_default():
    m = SessionMemory(last_physical_model="opus", phase=Phase.NORMAL, last_turn_at=1000,
                      warm_prefix_tokens=500)
    d = saar.decide(mem=m, now_epoch=1010, request_has_tool_result=False)
    assert d.prefer_model == "opus" and d.hard_model is None and d.reason == "sticky" and d.warm_prefix_tokens == 500
    assert d.switched is False


def test_decide_idle_reset():
    m = SessionMemory(last_physical_model="opus", phase=Phase.NORMAL, last_turn_at=1000)
    d = saar.decide(mem=m, now_epoch=1000 + 301, request_has_tool_result=False)
    assert d.hard_model is None and d.reason == "reset" and d.stale is True


def test_decide_tool_loop_hard_lock():
    m = SessionMemory(last_physical_model="sonnet", phase=Phase.TOOL_LOOP, last_turn_at=1000)
    # tool result present in a tool-loop phase ⇒ forced back to the same model
    d = saar.decide(mem=m, now_epoch=1010, request_has_tool_result=True)
    assert d.hard_model == "sonnet" and d.reason == "tool-loop-lock"


def test_decide_tool_loop_lock_only_with_tool_result():
    # In tool-loop phase but WITHOUT a tool result, the loop is closing → not a
    # hard lock (falls through to sticky, still same model but not locked).
    m = SessionMemory(last_physical_model="sonnet", phase=Phase.TOOL_LOOP, last_turn_at=1000)
    d = saar.decide(mem=m, now_epoch=1010, request_has_tool_result=False)
    assert d.reason == "sticky"


def test_decide_decision_drift():
    m = SessionMemory(last_physical_model="opus", phase=Phase.NORMAL, last_turn_at=1000,
                      matched_decision="code")
    d = saar.decide(mem=m, now_epoch=1010, request_has_tool_result=False, matched_decision="synthesis")
    assert d.hard_model is None and d.reason == "drift"
    # same decision → sticky
    d = saar.decide(mem=m, now_epoch=1010, request_has_tool_result=False, matched_decision="code")
    assert d.reason == "sticky"


def test_idle_reset_beats_tool_loop_lock():
    # An idle session drops its lock even in tool-loop phase (a stale lock must
    # not strand a session forever — the reset boundary is the escape hatch).
    m = SessionMemory(last_physical_model="sonnet", phase=Phase.TOOL_LOOP, last_turn_at=1000)
    d = saar.decide(mem=m, now_epoch=1000 + 999, request_has_tool_result=True)
    assert d.reason == "reset" and d.hard_model is None


def test_next_phase_after_turn():
    assert saar.next_phase_after_turn(response_had_tool_use=True, request_had_tool_result=False) == Phase.TOOL_LOOP
    assert saar.next_phase_after_turn(response_had_tool_use=False, request_had_tool_result=True) == Phase.NORMAL


# --------------------------------------------------------------------------- P0-4 checkout delta pricing


def test_checkout_delta_is_input_minus_cacheread(dynamodb_mock, seed_active_tenant):
    from mvp import pricing
    from mvp.pricing import PricingConfigRepository

    # Seed a known rate: input 3.00/Mtok, cache_read 0.30/Mtok → delta rate 2.70.
    repo = PricingConfigRepository()
    _seed_rate(repo, "opus", input_mtok=3_000_000, cache_read_mtok=300_000, output_mtok=15_000_000)
    # 1000 warm prefix tokens × (3.00 − 0.30)/Mtok = 2700 microusd.
    delta = pricing.saar_checkout_delta_microusd(pricing_key="opus", warm_prefix_tokens=1000, repo=repo)
    assert delta == 2700, delta


def test_checkout_delta_never_negative(dynamodb_mock):
    from mvp import pricing
    from mvp.pricing import PricingConfigRepository

    repo = PricingConfigRepository()
    # Misconfigured: cache_read ABOVE input. Delta must clamp to 0, never invent a saving.
    _seed_rate(repo, "weird", input_mtok=1_000_000, cache_read_mtok=5_000_000, output_mtok=1_000_000)
    assert pricing.saar_checkout_delta_microusd(pricing_key="weird", warm_prefix_tokens=1000, repo=repo) == 0


def test_checkout_delta_zero_when_no_warm_prefix(dynamodb_mock):
    from mvp import pricing
    from mvp.pricing import PricingConfigRepository

    repo = PricingConfigRepository()
    _seed_rate(repo, "opus", input_mtok=3_000_000, cache_read_mtok=300_000, output_mtok=15_000_000)
    assert pricing.saar_checkout_delta_microusd(pricing_key="opus", warm_prefix_tokens=0, repo=repo) == 0


def _seed_rate(repo, key, *, input_mtok, cache_read_mtok, output_mtok):
    from mvp import pricing

    repo.set_rates(version=f"saar-test-{key}", rates={key: pricing.Rate(
        input_per_mtok_microusd=input_mtok,
        output_per_mtok_microusd=output_mtok,
        cache_read_per_mtok_microusd=cache_read_mtok,
        cache_write_per_mtok_microusd=input_mtok,
    )})
    pricing.reset_cache()


# --------------------------------------------------------------------------- P0-5 claim + headers


def test_saar_eval_item_hashes_session_key():
    from mvp.learning import decision_log as dl

    item = dl.build_saar_eval_item(
        tenant_id="acme", run_id="wr1", span_id="sp1", session_key="secret-session-name",
        replay_id="rp1", reason="sticky", phase=Phase.NORMAL, prev_model="opus",
        chosen_model="opus", switched=False, warm_prefix_tokens_claimed=500,
        checkout_delta_microusd=2700, rating_version="v1", created_at_ms=1_700_000_000_000,
    )
    assert item["record_type"] == "saar_switch_eval"
    # The raw session name must NOT appear anywhere in the audit record.
    assert "secret-session-name" not in str(item)
    assert item["checkout_delta_microusd"] == 2700
    assert item["rating_version"] == "v1"


def test_replay_headers_shape():
    d = SaarDecision(hard_model=None, prefer_model="opus", phase=Phase.NORMAL, reason="sticky",
                     switched=False, prev_model="opus", warm_prefix_tokens=500)
    h = saar.replay_headers(replay_id="rp1", decision=d, chosen_model="opus", checkout_delta_microusd=2700)
    assert h["x-sc-saar-replay-id"] == "rp1"
    assert h["x-sc-saar-model"] == "opus"
    assert h["x-sc-saar-switch"] == "stayed:sticky"
    assert h["x-sc-saar-cache-tokens"] == "500"
    assert h["x-sc-saar-delta-microusd"] == "2700"


def test_replay_headers_locked_and_switched():
    locked = SaarDecision(hard_model="s", prefer_model=None, phase=Phase.TOOL_LOOP, reason="tool-loop-lock", switched=False, prev_model="s")
    assert saar.replay_headers(replay_id="r", decision=locked, chosen_model="s")["x-sc-saar-switch"] == "locked:tool-loop-lock"
    # prev_model="g" but committed "h" ⇒ the header must report a real switch,
    # computed from committed-vs-prev (not a stale flag) — Fable review-1 M2.
    switched = SaarDecision(hard_model=None, prefer_model="h", phase=Phase.NORMAL, reason="sticky", switched=False, prev_model="g")
    assert saar.replay_headers(replay_id="r", decision=switched, chosen_model="h")["x-sc-saar-switch"] == "switched:sticky"


# --------------------------------------------------------------------------- tool-block detection


def test_request_has_tool_result():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]
    assert saar.request_has_tool_result(msgs) is True
    assert saar.request_has_tool_result([{"role": "user", "content": "plain"}]) is False
    assert saar.request_has_tool_result("not a list") is False
    assert saar.request_has_tool_result(None) is False


def test_response_has_tool_use():
    assert saar.response_has_tool_use([{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "t"}]) is True
    assert saar.response_has_tool_use([{"type": "text", "text": "hi"}]) is False
    assert saar.response_has_tool_use("nope") is False


def test_tool_result_only_last_user_message_counts():
    # H2 (Fable review-1): a HISTORICAL tool_result must NOT trigger the lock —
    # only the current turn's (last user message) does. Converse re-sends history
    # each turn, so scanning all messages would wrongly keep a moved-on session
    # locked.
    history_with_old_tool = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        {"role": "user", "content": "now a plain question"},   # current turn: NOT a tool return
    ]
    assert saar.request_has_tool_result(history_with_old_tool) is False
    # But when the LAST user message is the tool result, it counts.
    current_is_tool = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
    ]
    assert saar.request_has_tool_result(current_is_tool) is True


def test_sticky_is_soft_preference_not_hard_pin():
    # C2 (Fable review-1): sticky must be a SOFT preference (prefer_model), never
    # a hard pin (hard_model) — so it can never disable the cascade and turn a
    # servable request into a 402/403.
    m = SessionMemory(last_physical_model="opus", phase=Phase.NORMAL, last_turn_at=1000)
    d = saar.decide(mem=m, now_epoch=1010, request_has_tool_result=False)
    assert d.hard_model is None, "sticky must not set a hard pin"
    assert d.prefer_model == "opus", "sticky must set a soft preference"
    # Only tool-loop lock is a hard pin.
    mt = SessionMemory(last_physical_model="sonnet", phase=Phase.TOOL_LOOP, last_turn_at=1000)
    dt = saar.decide(mem=mt, now_epoch=1010, request_has_tool_result=True)
    assert dt.hard_model == "sonnet" and dt.prefer_model is None


# --------------------------------------------------------------------------- adapters (flag gating + degenerate safety)


class _Ctx:
    def __init__(self, tenant_id="acme", session_id="s1", wr="wr1"):
        self.tenant_id = tenant_id
        self._sid = session_id
        self.workflow_run_id = wr

    def session_key(self):
        return self._sid


class _Cfg:
    saar_user_scoped = False


def test_pre_reserve_returns_none_when_flag_off(dynamodb_mock, monkeypatch):
    monkeypatch.setenv("SAAR_ENABLED", "false")
    out = saar.saar_pre_reserve(ctx=_Ctx(), org_id="acme", user_id="u1", request_messages=[])
    assert out is None  # degenerate-safe: caller uses the normal cascade, no memory touched


def test_pre_reserve_cold_then_sticky_roundtrip(dynamodb_mock, monkeypatch):
    monkeypatch.setenv("SAAR_ENABLED", "true")
    ctx = _Ctx(session_id="sess-42", wr="wr-42")

    # Turn 1: cold (no memory) → no pin (cascade decides), decision reason 'cold'.
    s1 = saar.saar_pre_reserve(ctx=ctx, org_id="acme", user_id="u1", request_messages=[])
    assert s1 is not None and s1.decision.hard_model is None and s1.decision.reason == "cold"
    # Persist the turn: committed to opus, no tool use.
    saar.saar_post_settle(
        sctx=s1, committed_model="opus", response_had_tool_use=False,
        request_had_tool_result=False, now_epoch=1000,
    )
    _drain_saar_writes()

    # Turn 2: memory now exists → sticky to opus.
    s2 = saar.saar_pre_reserve(ctx=ctx, org_id="acme", user_id="u1",
                               request_messages=[], now_epoch=1010)
    assert s2 is not None and s2.decision.prefer_model == "opus" and s2.decision.reason == "sticky"


def test_pre_reserve_tool_loop_lock_e2e(dynamodb_mock, monkeypatch):
    monkeypatch.setenv("SAAR_ENABLED", "true")
    ctx = _Ctx(session_id="sess-tl", wr="wr-tl")

    # Turn 1: model emits tool_use → phase persists as tool-loop.
    s1 = saar.saar_pre_reserve(ctx=ctx, org_id="acme", user_id="u1", request_messages=[])
    saar.saar_post_settle(
        sctx=s1, committed_model="sonnet", response_had_tool_use=True,
        request_had_tool_result=False, now_epoch=2000,
    )
    _drain_saar_writes()

    # Turn 2: request carries a tool_result AND phase is tool-loop → HARD lock to sonnet.
    tool_msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]}]
    s2 = saar.saar_pre_reserve(ctx=ctx, org_id="acme", user_id="u1",
                               request_messages=tool_msgs, now_epoch=2010)
    assert s2.decision.hard_model == "sonnet" and s2.decision.reason == "tool-loop-lock"


def test_post_settle_none_is_noop():
    # A None sctx (SAAR was silent) must be a harmless no-op.
    saar.saar_post_settle(sctx=None, committed_model="x", response_had_tool_use=False,
                          request_had_tool_result=False)


# --------------------------------------------------------------------------- HTTP e2e (handler wiring)

from dataclasses import dataclass as _dc
from unittest.mock import patch as _patch

from fastapi import FastAPI as _FastAPI
from fastapi.testclient import TestClient as _TestClient


@_dc
class _HUser:
    user_id: str = "user-saar-1"
    org_id: str = "saar-org"
    email: str = "t@example.com"
    roles: list = None
    auth_kind: str = "jwt"
    key_scopes: list = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["user"]


def _mock_converse(**kwargs):
    return {"output": {"message": {"content": [{"text": "hi"}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 3, "outputTokens": 2}}


@pytest.fixture
def saar_api(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz
    from dynamo.user_tenants import UserTenantsRepository
    from mvp.anthropic import router as anthropic_router
    from mvp.deps import get_current_user

    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: True)
    UserTenantsRepository().ensure(user_id=_HUser().user_id, tenant_id=_HUser().org_id,
                                   role="user", total_credit=10**9)
    app = _FastAPI()
    app.include_router(anthropic_router)
    app.dependency_overrides[get_current_user] = lambda: _HUser()
    with _patch("mvp.anthropic._bedrock_client") as mb:
        mb.return_value.converse.side_effect = _mock_converse
        yield _TestClient(app)


def _msg(client, headers=None):
    return client.post("/v1/messages", headers=headers or {}, json={
        "model": "us.anthropic.claude-opus-4-7",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 40, "stream": False,
    })


def test_http_flag_off_no_saar_headers(saar_api, monkeypatch):
    # Degenerate-safety: flag off ⇒ NO x-sc-saar-* headers, behaviour unchanged.
    monkeypatch.setenv("SAAR_ENABLED", "false")
    r = _msg(saar_api, headers={"x-sc-session-id": "sess-1"})
    assert r.status_code == 200
    assert not any(k.lower().startswith("x-sc-saar-") for k in r.headers)


def test_http_flag_on_emits_replay_headers_and_sticks(saar_api, monkeypatch):
    monkeypatch.setenv("SAAR_ENABLED", "true")
    # Turn 1: cold — SAAR acts (headers present) but defers model choice to cascade.
    r1 = _msg(saar_api, headers={"x-sc-session-id": "sess-sticky"})
    assert r1.status_code == 200
    assert r1.headers.get("x-sc-saar-replay-id", "").startswith("rp_")
    assert r1.headers.get("x-sc-saar-switch") == "none:cold"
    _drain_saar_writes()

    # Turn 2: same session ⇒ sticky to turn 1's committed model.
    r2 = _msg(saar_api, headers={"x-sc-session-id": "sess-sticky"})
    assert r2.status_code == 200
    assert r2.headers.get("x-sc-saar-switch") == "stayed:sticky"
    assert r2.headers.get("x-sc-saar-model")  # the stuck model id is echoed


def test_http_explicit_pin_beats_saar(saar_api, monkeypatch):
    # An explicit x-sc-model-pin always wins: SAAR must not run (no headers).
    monkeypatch.setenv("SAAR_ENABLED", "true")
    r = _msg(saar_api, headers={"x-sc-session-id": "s", "x-sc-model-pin": "claude-haiku-4-5"})
    assert r.status_code == 200
    assert "x-sc-saar-replay-id" not in r.headers


def test_http_distinct_sessions_do_not_stick_together(saar_api, monkeypatch):
    monkeypatch.setenv("SAAR_ENABLED", "true")
    _msg(saar_api, headers={"x-sc-session-id": "sess-A"})
    _drain_saar_writes()
    # A different session id starts cold (no cross-session stickiness).
    r = _msg(saar_api, headers={"x-sc-session-id": "sess-B"})
    assert r.headers.get("x-sc-saar-switch") == "none:cold"
