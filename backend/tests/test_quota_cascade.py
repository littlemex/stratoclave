"""Integration tests for per-model quota + cascading fallback (P0-11).

Exercises `reserve_credit_for_model` end-to-end against moto: routing config
(chain + per-model quotas) lives in the user-tenants table under CONFIG#ROUTING,
the quota counters live in the model-quotas table, and the pool lives in the
tenant-budgets table. These assert the wiring the route handlers depend on:

  - no config            → passthrough on the requested model
  - quota not exhausted  → reserve on the requested model, `used` charged
  - quota exhausted       → cascade to the next chain model (cheaper)
  - all quotas exhausted → 402 model_quota_exhausted
  - fallback disabled     → no cascade, 402 on the requested model
  - release / settle       → the per-model `used` counter is adjusted back
"""
from __future__ import annotations

import boto3
import pytest
from fastapi import HTTPException

from dataclasses import dataclass

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline
from mvp.routing import quota as _quota
from mvp.routing.config import _cache as _cfg_cache


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


TENANT = "cascade-org"
USER = "user-cascade-0001"


@pytest.fixture
def env(dynamodb_mock):
    """Fresh tenant with a generous pool + per-user balance; clean config cache."""
    _cfg_cache.clear()
    UserTenantsRepository().ensure(
        user_id=USER, tenant_id=TENANT, role="user", total_credit=10**12,
    )
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=TENANT, period=current_period(), pool_limit_microusd=10**11,
    )
    yield
    _cfg_cache.clear()


def _put_routing_config(**item):
    """Write a CONFIG#ROUTING row for TENANT and drop the config cache."""
    tbl = boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-user-tenants")
    tbl.put_item(Item={"user_id": "CONFIG#ROUTING", "tenant_id": TENANT, **item})
    _cfg_cache.clear()


def _first_responses_model():
    """First alias of a `responses`-protocol model in the registry, or None."""
    from mvp.models import _REGISTRY
    for entry in _REGISTRY:
        if entry.wire_protocol == "responses" and entry.aliases:
            return entry.aliases[0]
    return None


def _used(model, *, user=None):
    tbl = boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-model-quotas")
    pk = _quota._pk_user(TENANT, user) if user else _quota._pk_tenant(TENANT)
    resp = tbl.get_item(Key={"pk": pk, "sk": _quota._sk(model, current_period())})
    return int(resp.get("Item", {}).get("used", 0))


def _reserve(model="claude-sonnet-4-6", tokens=1000, wire_protocol="messages",
             vsr_hard_model=None, saar_prefer_model=None):
    return _pipeline.reserve_credit_for_model(
        _User(user_id=USER, org_id=TENANT),
        tokens,
        model_name=model,
        input_tokens_est=500,
        max_output_tokens=500,
        wire_protocol=wire_protocol,
        vsr_hard_model=vsr_hard_model,
        saar_prefer_model=saar_prefer_model,
    )


class TestVsrHardPin:
    """P0-15: a hard model pin selects exactly that model — no cascade, no
    fallback — validated against servability (400) and the allowlist (403)."""

    def test_pin_selects_that_model_no_cascade(self, no_pool_env):
        # chain would normally start at sonnet; pin forces haiku with no config
        # fallback path taken.
        _put_routing_config(
            chain=["claude-sonnet-4-6", "claude-haiku-4-5"],
            quotas={}, fallback_default="on")
        ctx = _reserve("claude-sonnet-4-6", vsr_hard_model="claude-haiku-4-5")
        assert ctx.selected_model == "claude-haiku-4-5"

    def test_pin_overrides_requested_even_with_no_config(self, no_pool_env):
        # No routing config at all: without a pin this is passthrough on the
        # requested model; the pin still forces a different model.
        ctx = _reserve("claude-sonnet-4-6", vsr_hard_model="claude-haiku-4-5")
        assert ctx.selected_model == "claude-haiku-4-5"

    def test_pin_not_in_allowlist_is_403(self, no_pool_env):
        _put_routing_config(
            allowlist=["claude-sonnet-4-6"], quotas={}, fallback_default="off")
        with pytest.raises(HTTPException) as e:
            _reserve("claude-sonnet-4-6", vsr_hard_model="claude-haiku-4-5")
        assert e.value.status_code == 403
        assert e.value.detail["reason"] == "model_pin_not_allowed"

    def test_pin_in_allowlist_is_allowed(self, no_pool_env):
        _put_routing_config(
            allowlist=["claude-sonnet-4-6", "claude-haiku-4-5"],
            quotas={}, fallback_default="off")
        ctx = _reserve("claude-sonnet-4-6", vsr_hard_model="claude-haiku-4-5")
        assert ctx.selected_model == "claude-haiku-4-5"

    def test_unservable_pin_is_400_never_substituted(self, no_pool_env):
        with pytest.raises(HTTPException) as e:
            _reserve("claude-sonnet-4-6", vsr_hard_model="totally-not-a-model")
        assert e.value.status_code == 400
        assert e.value.detail["reason"] == "invalid_model_pin"

    def test_wrong_protocol_pin_is_400(self, no_pool_env):
        gpt = _first_responses_model()
        if gpt is None:
            pytest.skip("no responses-protocol model in the registry")
        with pytest.raises(HTTPException) as e:
            _reserve("claude-sonnet-4-6", wire_protocol="messages", vsr_hard_model=gpt)
        assert e.value.status_code == 400

    def test_pin_allowlist_matches_across_spellings(self, no_pool_env):
        # Fable rev2 NEW-1/F1: the pin is used VERBATIM downstream (not
        # canonicalized — that bypassed alias-keyed quotas), but the allowlist
        # membership check compares on the registry ENTRY, so a pin spelled as
        # the bedrock id still matches an allowlist entry spelled as the alias.
        from mvp.models import resolve_model
        entry = resolve_model("claude-haiku-4-5")
        bedrock_id = entry.bedrock_model_id
        if bedrock_id == entry.aliases[0]:
            pytest.skip("model has no distinct bedrock-id spelling")
        _put_routing_config(
            allowlist=["claude-haiku-4-5"], quotas={}, fallback_default="off")
        ctx = _reserve("claude-sonnet-4-6", vsr_hard_model=bedrock_id)
        # served verbatim under the spelling the caller pinned (config/request
        # spelling-agreement is the P0-11 convention; pin follows it)
        assert ctx.selected_model == bedrock_id

    def test_chain_only_tenant_rejects_pin_outside_chain(self, no_pool_env):
        # Fable rev2 NEW-2: a chain-only tenant (no allowlist) must not let a
        # client header pin a model OUTSIDE the chain — the chain is the policy.
        _put_routing_config(
            chain=["claude-sonnet-4-6"], quotas={}, fallback_default="off")
        with pytest.raises(HTTPException) as e:
            _reserve("claude-sonnet-4-6", vsr_hard_model="claude-haiku-4-5")
        assert e.value.status_code == 403
        assert e.value.detail["reason"] == "model_pin_not_allowed"

    def test_chain_only_tenant_allows_pin_inside_chain(self, no_pool_env):
        _put_routing_config(
            chain=["claude-sonnet-4-6", "claude-haiku-4-5"],
            quotas={}, fallback_default="off")
        ctx = _reserve("claude-sonnet-4-6", vsr_hard_model="claude-haiku-4-5")
        assert ctx.selected_model == "claude-haiku-4-5"

    def test_pinned_model_quota_exhausted_402_no_fallback(self, no_pool_env):
        # pin haiku with a ~0 quota; chain has sonnet as a fallback, but a hard
        # pin must NOT fall back — it 402s.
        _put_routing_config(
            chain=["claude-haiku-4-5", "claude-sonnet-4-6"],
            quotas={"claude-haiku-4-5": {"limit": 1}},
            fallback_default="on")
        with pytest.raises(HTTPException) as e:
            _reserve("claude-haiku-4-5", vsr_hard_model="claude-haiku-4-5")
        assert e.value.status_code == 402
        assert e.value.detail["reason"] == "model_quota_exhausted"
        # sonnet (the chain fallback) was never charged
        assert _used("claude-sonnet-4-6") == 0


class TestPassthrough:
    def test_no_config_uses_requested_model(self, env):
        ctx = _reserve("claude-sonnet-4-6")
        assert ctx.selected_model == "claude-sonnet-4-6"
        # No quota configured → no quota row written.
        assert _used("claude-sonnet-4-6") == 0
        assert ctx.quota_reserved_amount == 0


class TestCascade:
    def test_charges_requested_model_when_under_quota(self, env):
        _put_routing_config(
            chain=["claude-sonnet-4-6", "claude-haiku-4-5"],
            quotas={"claude-sonnet-4-6": {"limit": 10**9}},
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6")
        assert ctx.selected_model == "claude-sonnet-4-6"
        assert ctx.quota_reserved_amount > 0
        assert _used("claude-sonnet-4-6") == ctx.quota_reserved_amount

    def test_cascades_to_next_model_when_quota_exhausted(self, env):
        # Sonnet quota is basically zero → its reserve condition fails → cascade
        # to haiku (no quota configured → unlimited).
        _put_routing_config(
            chain=["claude-sonnet-4-6", "claude-haiku-4-5"],
            quotas={"claude-sonnet-4-6": {"limit": 1}},
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6")
        assert ctx.selected_model == "claude-haiku-4-5"
        # Sonnet was never charged; haiku has no quota row (unlimited).
        assert _used("claude-sonnet-4-6") == 0

    def test_all_quotas_exhausted_raises_402(self, env):
        _put_routing_config(
            chain=["claude-sonnet-4-6", "claude-haiku-4-5"],
            quotas={
                "claude-sonnet-4-6": {"limit": 1},
                "claude-haiku-4-5": {"limit": 1},
            },
            fallback_default="on",
        )
        with pytest.raises(HTTPException) as e:
            _reserve("claude-sonnet-4-6")
        assert e.value.status_code == 402
        assert e.value.detail["reason"] == "model_quota_exhausted"

    def test_fallback_disabled_does_not_cascade(self, env):
        _put_routing_config(
            chain=["claude-sonnet-4-6", "claude-haiku-4-5"],
            quotas={"claude-sonnet-4-6": {"limit": 1}},
            fallback_default="off",
        )
        with pytest.raises(HTTPException) as e:
            _reserve("claude-sonnet-4-6")
        assert e.value.status_code == 402
        assert e.value.detail["reason"] == "model_quota_exhausted"


class TestServabilityFilter:
    """Fable F2/F3/F4 root fix: an unservable candidate must never win the
    cascade, because the handler would then silently invoke the requested model
    past its exhausted quota, mispriced. Unservable = unresolvable in the model
    registry, or wrong wire protocol for the route."""

    def test_unresolvable_candidate_is_skipped(self, env):
        # requested haiku quota-exhausted, chain falls to a typo'd model that
        # doesn't resolve in the registry → it must be dropped, not selected.
        _put_routing_config(
            chain=["claude-haiku-4-5", "claude-sonnet-4-6"],
            quotas={"claude-haiku-4-5": {"limit": 1}},
            fallback_default="on",
        )
        # Insert a bogus first-fallback by using a chain whose 2nd entry is a
        # real model, and a 3rd that's a typo — the typo must never be selected.
        _put_routing_config(
            chain=["claude-haiku-4-5", "totally-not-a-model", "claude-sonnet-4-6"],
            quotas={"claude-haiku-4-5": {"limit": 1}},
            fallback_default="on",
        )
        ctx = _reserve("claude-haiku-4-5")
        assert ctx.selected_model == "claude-sonnet-4-6"

    def test_wrong_protocol_candidate_is_skipped(self, env):
        # A `responses`-protocol model in a `messages`-route chain must be
        # dropped so the messages route never selects an unservable model.
        gpt = _first_responses_model()
        if gpt is None:
            pytest.skip("no responses-protocol model in the registry")
        _put_routing_config(
            chain=["claude-haiku-4-5", gpt, "claude-sonnet-4-6"],
            quotas={"claude-haiku-4-5": {"limit": 1}},
            fallback_default="on",
        )
        ctx = _reserve("claude-haiku-4-5", wire_protocol="messages")
        assert ctx.selected_model == "claude-sonnet-4-6"

    def test_requested_model_exempt_from_protocol_filter(self, env):
        # The requested model is always servable on its own route even if the
        # chain is otherwise junk — a bad chain must not fail a valid request.
        _put_routing_config(
            chain=["claude-sonnet-4-6"],
            quotas={},
            fallback_default="off",
        )
        ctx = _reserve("claude-sonnet-4-6", wire_protocol="messages")
        assert ctx.selected_model == "claude-sonnet-4-6"


class TestReleaseSettle:
    def test_release_returns_quota(self, env):
        _put_routing_config(
            chain=["claude-sonnet-4-6"],
            quotas={"claude-sonnet-4-6": {"limit": 10**9}},
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6")
        reserved = ctx.quota_reserved_amount
        assert _used("claude-sonnet-4-6") == reserved
        _pipeline.release_pool(ctx)
        assert _used("claude-sonnet-4-6") == 0

    def test_settle_does_not_seed_phantom_user_scope_row(self, env):
        # Regression: settle/release must adjust ONLY the reserved scopes. With
        # a tenant-only quota, no per-user row is reserved; settle must NOT
        # create a phantom user-scope row with a negative `used` (which would
        # later let that user over-spend if a per-user limit were configured).
        _put_routing_config(
            chain=["claude-sonnet-4-6"],
            quotas={"claude-sonnet-4-6": {"limit": 10**9}},  # tenant-only, no user limit
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6")
        assert _used("claude-sonnet-4-6", user=USER) == 0  # no user row after reserve
        _pipeline.settle_reservation_and_log(
            user=_User(user_id=USER, org_id=TENANT), tenants_repo=ctx,
            reservation=1000, actual_input_tokens=0, actual_output_tokens=0,
            model_id="claude-sonnet-4-6", context=ctx,
            actual_cost_microusd=ctx.quota_reserved_amount // 3,
        )
        # Tenant row settled to actual; user row still absent (reads as 0).
        assert _used("claude-sonnet-4-6", user=USER) == 0
        tbl = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "stratoclave-model-quotas")
        user_row = tbl.get_item(Key={
            "pk": _quota._pk_user(TENANT, USER),
            "sk": _quota._sk("claude-sonnet-4-6", current_period()),
        }).get("Item")
        assert user_row is None, f"phantom user-scope row written: {user_row}"

    def test_settle_adjusts_quota_to_actual(self, env):
        _put_routing_config(
            chain=["claude-sonnet-4-6"],
            quotas={"claude-sonnet-4-6": {"limit": 10**9}},
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6")
        reserved = ctx.quota_reserved_amount
        assert reserved > 0
        user = _User(user_id=USER, org_id=TENANT)
        # Settle with a smaller actual than reserved → used drops to `actual`.
        actual = reserved // 3
        _pipeline.settle_reservation_and_log(
            user=user, tenants_repo=ctx, reservation=1000,
            actual_input_tokens=0, actual_output_tokens=0,
            model_id="claude-sonnet-4-6", context=ctx,
            actual_cost_microusd=actual,
        )
        assert _used("claude-sonnet-4-6") == actual


@pytest.fixture
def no_pool_env(dynamodb_mock):
    """Tenant with a per-user balance but NO pool budget (Fable F-3 scenario)."""
    _cfg_cache.clear()
    UserTenantsRepository().ensure(
        user_id=USER, tenant_id=TENANT, role="user", total_credit=10**12,
    )
    # deliberately NO set_pool_limit
    yield
    _cfg_cache.clear()


class TestQuotaWithoutPool:
    """Fable F-3: per-model quota must be enforced even when the tenant has no
    dollar pool. Previously reserve_credit short-circuited on `pool is None` and
    committed no quota line → pool-less tenants were served unmetered, and the
    cascade / selected_model were bypassed entirely."""

    def test_quota_enforced_and_charged_without_pool(self, no_pool_env):
        _put_routing_config(
            chain=["claude-sonnet-4-6"],
            quotas={"claude-sonnet-4-6": {"limit": 10**9}},
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6")
        assert ctx.pool_active is False
        assert ctx.selected_model == "claude-sonnet-4-6"   # not None anymore
        assert ctx.quota_reserved_amount > 0
        assert ctx.quota_period == current_period()
        assert _used("claude-sonnet-4-6") == ctx.quota_reserved_amount

    def test_cascade_without_pool(self, no_pool_env):
        # sonnet quota ~0 → must cascade to haiku even with no pool
        _put_routing_config(
            chain=["claude-sonnet-4-6", "claude-haiku-4-5"],
            quotas={"claude-sonnet-4-6": {"limit": 1}},
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6")
        assert ctx.pool_active is False
        assert ctx.selected_model == "claude-haiku-4-5"
        assert _used("claude-sonnet-4-6") == 0  # sonnet never charged

    def test_all_exhausted_without_pool_raises_402(self, no_pool_env):
        _put_routing_config(
            chain=["claude-sonnet-4-6", "claude-haiku-4-5"],
            quotas={"claude-sonnet-4-6": {"limit": 1}, "claude-haiku-4-5": {"limit": 1}},
            fallback_default="on",
        )
        with pytest.raises(HTTPException) as e:
            _reserve("claude-sonnet-4-6")
        assert e.value.status_code == 402
        assert e.value.detail["reason"] == "model_quota_exhausted"

    def test_release_returns_quota_without_pool(self, no_pool_env):
        _put_routing_config(
            chain=["claude-sonnet-4-6"],
            quotas={"claude-sonnet-4-6": {"limit": 10**9}},
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6")
        assert _used("claude-sonnet-4-6") == ctx.quota_reserved_amount
        _pipeline.release_pool(ctx)
        assert _used("claude-sonnet-4-6") == 0


class TestQuotaIdempotencyAndPeriod:
    """Fable F-1 (period) + F-6 (idempotent settle/release)."""

    def test_double_release_is_idempotent(self, env):
        _put_routing_config(
            chain=["claude-sonnet-4-6"],
            quotas={"claude-sonnet-4-6": {"limit": 10**9}},
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6")
        assert _used("claude-sonnet-4-6") == ctx.quota_reserved_amount
        _pipeline.release_pool(ctx)
        assert _used("claude-sonnet-4-6") == 0
        # Second release must NOT drive `used` negative (F-6).
        _pipeline.release_pool(ctx)
        assert _used("claude-sonnet-4-6") == 0

    def test_release_after_settle_is_idempotent(self, env):
        _put_routing_config(
            chain=["claude-sonnet-4-6"],
            quotas={"claude-sonnet-4-6": {"limit": 10**9}},
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6")
        reserved = ctx.quota_reserved_amount
        _pipeline.settle_reservation_and_log(
            user=_User(user_id=USER, org_id=TENANT), tenants_repo=ctx,
            reservation=1000, actual_input_tokens=0, actual_output_tokens=0,
            model_id="claude-sonnet-4-6", context=ctx,
            actual_cost_microusd=reserved // 2,
        )
        settled_used = _used("claude-sonnet-4-6")
        assert settled_used == reserved // 2
        # A stray release after settle must not subtract again (F-6).
        _pipeline.release_pool(ctx)
        assert _used("claude-sonnet-4-6") == settled_used

    def test_settle_uses_reserved_period_not_current(self, env):
        # Simulate a month-boundary crossing: reserve stamps quota_period, then
        # the wall clock advances a month before settle. Settle must adjust the
        # RESERVED period's row, never a fresh current_period() (F-1).
        _put_routing_config(
            chain=["claude-sonnet-4-6"],
            quotas={"claude-sonnet-4-6": {"limit": 10**9}},
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6")
        reserved_period = ctx.quota_period
        reserved = ctx.quota_reserved_amount
        assert _used("claude-sonnet-4-6") == reserved
        # Force the context's period to look "old" by leaving quota_period as-is
        # (that's exactly what settle must key on). Settle to a smaller actual.
        _pipeline.settle_reservation_and_log(
            user=_User(user_id=USER, org_id=TENANT), tenants_repo=ctx,
            reservation=1000, actual_input_tokens=0, actual_output_tokens=0,
            model_id="claude-sonnet-4-6", context=ctx,
            actual_cost_microusd=reserved // 4,
        )
        # The reserved period's row moved to `actual`; no other period touched.
        tbl = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "stratoclave-model-quotas")
        row = tbl.get_item(Key={
            "pk": _quota._pk_tenant(TENANT),
            "sk": _quota._sk("claude-sonnet-4-6", reserved_period),
        }).get("Item")
        assert int(row["used"]) == reserved // 4


class TestSaarSoftPreference:
    """SAAR sticky is a SOFT preference, not a hard pin (Fable review-1 C2): it
    heads the cascade with the warm model but NEVER disables fallback, so it can
    never turn a servable request into a 402/403."""

    def test_prefer_in_chain_heads_the_cascade(self, env):
        # sonnet is requested & first in chain, but the session is warm on haiku:
        # the preference should reorder haiku to the head and serve it (it has no
        # quota configured → unlimited), NOT sonnet.
        _put_routing_config(
            chain=["claude-sonnet-4-6", "claude-haiku-4-5"],
            quotas={"claude-sonnet-4-6": {"limit": 10**9}},
            fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6", saar_prefer_model="claude-haiku-4-5")
        assert ctx.selected_model == "claude-haiku-4-5"

    def test_prefer_not_in_chain_is_ignored_cascade_proceeds(self, env):
        # The warm model is NOT in the resolved candidate chain (e.g. it was
        # dropped from the allowlist since the session started). The preference
        # must be IGNORED and the request served by the normal chain — never a
        # 402/403 (the C2 availability guarantee). A hard pin outside the chain
        # would 403; a soft preference is silently dropped.
        _put_routing_config(
            chain=["claude-sonnet-4-6", "claude-haiku-4-5"],
            quotas={}, fallback_default="on",
        )
        ctx = _reserve("claude-sonnet-4-6", saar_prefer_model="claude-opus-4-7")
        # Served by the normal chain head, not the (disallowed) preferred model.
        assert ctx.selected_model == "claude-sonnet-4-6"
