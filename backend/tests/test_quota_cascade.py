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


def _used(model, *, user=None):
    tbl = boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-model-quotas")
    pk = _quota._pk_user(TENANT, user) if user else _quota._pk_tenant(TENANT)
    resp = tbl.get_item(Key={"pk": pk, "sk": _quota._sk(model, current_period())})
    return int(resp.get("Item", {}).get("used", 0))


def _reserve(model="claude-sonnet-4-6", tokens=1000):
    return _pipeline.reserve_credit_for_model(
        _User(user_id=USER, org_id=TENANT),
        tokens,
        model_name=model,
        input_tokens_est=500,
        max_output_tokens=500,
    )


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
