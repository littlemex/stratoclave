"""Integration: an SR eval decision actually steers the reserve (architecture A').

Proves the full A' decide→reserve chain against moto, exactly as the handlers wire
it: adapter.decide() consults a FAKE /api/v1/eval, maps the decision to a registry
model, and its prefer_model is passed as `saar_prefer_model` to
reserve_credit_for_model — which reorders the servable candidate chain so the
SR-chosen model is the one reserved+billed. Also proves the money boundary
(prefer can never remove a servable model / expand the allowlist) and flag-off
inertness (sr_mode=off ⇒ no consult ⇒ identical selection).
"""
from __future__ import annotations

from dataclasses import dataclass

import boto3
import pytest

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline
from mvp.routing.config import _cache as _cfg_cache
from mvp.sr import adapter, eval_client


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


TENANT = "sr-int-org"
USER = "user-sr-int-0001"


@pytest.fixture
def env(dynamodb_mock, monkeypatch):
    _cfg_cache.clear()
    eval_client.reset_for_test()
    UserTenantsRepository().ensure(
        user_id=USER, tenant_id=TENANT, role="user", total_credit=10**12)
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=TENANT, period=current_period(), pool_limit_microusd=10**11)
    monkeypatch.setenv("SEMANTIC_ROUTER_BASE_URL", "http://sr:8080")
    monkeypatch.delenv("STRATOCLAVE_SR_FORCE_OFF", raising=False)
    yield
    _cfg_cache.clear()
    eval_client.reset_for_test()


def _put_routing_config(**item):
    tbl = boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-user-tenants")
    tbl.put_item(Item={"user_id": "CONFIG#ROUTING", "tenant_id": TENANT, **item})
    _cfg_cache.clear()


def _decide_then_reserve(requested="claude-opus-4-7", tokens=2000):
    """Mirror the handler: consult SR, feed prefer/hard into the reserve."""
    d = adapter.decide(
        tenant_id=TENANT, session_key="conv-1", requested_model=requested,
        has_tool_result=False, messages=[{"role": "user", "content": "2+2?"}])
    return _pipeline.reserve_credit_for_model(
        _User(user_id=USER, org_id=TENANT), tokens,
        model_name=requested, input_tokens_est=1000, max_output_tokens=1000,
        wire_protocol="messages",
        vsr_hard_model=d.hard_model, saar_prefer_model=d.prefer_model,
    )


def test_sr_decision_steers_reserve_to_chosen_model(env, monkeypatch):
    # sr_mode active + a chain [opus, haiku]; SR decides "cheap" → haiku. The
    # reserve must select haiku (prefer heads the chain), proving SR steered it.
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "active")
    monkeypatch.setenv("SEMANTIC_ROUTER_DECISION_MAP", '{"cheap": "claude-haiku-4-5"}')
    _put_routing_config(
        chain=["claude-opus-4-7", "claude-haiku-4-5"],
        quotas={}, fallback_default="on")
    eval_client.set_transport_hook(
        lambda m, u, t: {"decision_result": {"decision_name": "cheap"}})

    ctx = _decide_then_reserve()
    assert ctx.selected_model == "claude-haiku-4-5"


def test_sr_off_is_inert_identical_selection(env, monkeypatch):
    # sr_mode off: even though the fake eval WOULD say "cheap", no consult happens,
    # so selection is the requested model (head of chain) — byte-identical to today.
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "off")
    monkeypatch.setenv("SEMANTIC_ROUTER_DECISION_MAP", '{"cheap": "claude-haiku-4-5"}')
    _put_routing_config(
        chain=["claude-opus-4-7", "claude-haiku-4-5"],
        quotas={}, fallback_default="on")
    eval_client.set_transport_hook(
        lambda m, u, t: {"decision_result": {"decision_name": "cheap"}})

    ctx = _decide_then_reserve()
    assert ctx.selected_model == "claude-opus-4-7"   # requested, unchanged


def test_sr_prefer_cannot_expand_allowlist(env, monkeypatch):
    # SR decides a model NOT in the tenant allowlist. A soft prefer can only
    # reorder the servable set — it can never add a model. The reserve must fall
    # to an allowlisted model, never the SR-suggested-but-forbidden one.
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "active")
    monkeypatch.setenv("SEMANTIC_ROUTER_DECISION_MAP", '{"x": "claude-opus-4-7"}')
    _put_routing_config(
        allowlist=["claude-haiku-4-5"],            # opus is NOT allowed
        chain=["claude-haiku-4-5"],
        quotas={}, fallback_default="on")
    eval_client.set_transport_hook(
        lambda m, u, t: {"decision_result": {"decision_name": "x"}})

    ctx = _decide_then_reserve(requested="claude-haiku-4-5")
    assert ctx.selected_model == "claude-haiku-4-5"   # SR could not force opus in


def test_sr_transport_failure_fails_open(env, monkeypatch):
    # a router error must NOT break the request: decide() returns NO_DECISION and
    # the reserve proceeds on the requested model.
    monkeypatch.setenv("STRATOCLAVE_SR_MODE_DEFAULT", "active")

    def _boom(m, u, t):
        raise RuntimeError("router down")
    eval_client.set_transport_hook(_boom)
    _put_routing_config(
        chain=["claude-opus-4-7", "claude-haiku-4-5"],
        quotas={}, fallback_default="on")

    ctx = _decide_then_reserve()
    assert ctx.selected_model == "claude-opus-4-7"   # requested, unaffected
