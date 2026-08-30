"""A routing input the gateway could not read is not a routing input.

Three places substituted a value the gateway could not know, and each
substitution loosened a control:

  - `routing/config.py` answered a failed DynamoDB read with an empty
    `RoutingConfig()` and cached it for the 60s TTL. Everything that table
    carries only RESTRICTS a request (the model allowlist, the fallback chain,
    the per-model spend caps), so the empty answer did not degrade routing, it
    removed the restrictions — for a minute, per process.
  - the per-model quota counter was keyed on the spelling the client sent, while
    the admin write path stores quota keys canonicalised. Any other spelling of
    the same model missed the quota dict, so no quota line joined the reserve
    transaction and the request ran unmetered.
  - the uncatalogued-alias fallback in `routing/chains.py` stamped
    `price_key="sonnet"` and `cost_tier=2` on whatever model it had just
    resolved: settle would charge the Sonnet rate for a model priced above Opus,
    and the invented tier passed a breaker DOWNGRADE cap whose whole job is to
    stop a "cheaper" fallback from being the expensive one.

These tests pin each substitution as a defect rather than a default.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import boto3
import pytest
from fastapi import HTTPException

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline
from mvp.models import canonical_model_id
from mvp.routing import chains as _chains
from mvp.routing import config as _config
from mvp.routing import quota as _quota


TENANT = "routing-inputs-org"
USER = "user-routing-inputs-0001"

# Two names for ONE registry entry: the primary alias (what the admin write path
# stores) and the dated alias (a spelling a client may plausibly send).
CANONICAL = "claude-opus-4-5"
DATED_ALIAS = "claude-opus-4-5-20251101"
BEDROCK_ID = "us.anthropic.claude-opus-4-5-20251101-v1:0"


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


@pytest.fixture
def env(dynamodb_mock):
    """Tenant with a generous pool and per-user balance; no config memory."""
    _config.reset_cache()
    UserTenantsRepository().ensure(
        user_id=USER, tenant_id=TENANT, role="user", total_credit=10**12,
    )
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=TENANT, period=current_period(), pool_limit_microusd=10**11,
    )
    yield
    _config.reset_cache()


def _put_routing_config(**item):
    tbl = boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-user-tenants")
    tbl.put_item(Item={"user_id": "CONFIG#ROUTING", "tenant_id": TENANT, **item})
    _config.reset_cache()


def _used(model):
    tbl = boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-model-quotas")
    resp = tbl.get_item(Key={"pk": _quota._pk_tenant(TENANT),
                             "sk": _quota._sk(model, current_period())})
    return int(resp.get("Item", {}).get("used", 0))


def _reserve(model, tokens=1000):
    return _pipeline.reserve_credit_for_model(
        _User(user_id=USER, org_id=TENANT),
        tokens,
        model_name=model,
        input_tokens_est=500,
        max_output_tokens=500,
        wire_protocol="messages",
    )


class _RaisingTable:
    def get_item(self, **_kwargs):
        raise RuntimeError("DynamoDB is having a moment")


class _RaisingResource:
    def Table(self, _name):  # noqa: N802 — mirrors the boto3 resource API.
        return _RaisingTable()


class TestTheQuotaCounterIsKeyedOnTheModelNotItsSpelling:

    def test_every_spelling_of_one_model_shares_one_counter_key(self):
        period = "2026-08"
        keys = {_quota._sk(name, period)
                for name in (CANONICAL, DATED_ALIAS, BEDROCK_ID)}
        assert keys == {f"MQ#{CANONICAL}#{period}"}, (
            "one model must have one counter; a per-spelling counter is a cap "
            "that is avoided by respelling its subject")

    def test_different_models_still_have_different_keys(self):
        period = "2026-08"
        assert _quota._sk("claude-haiku-4-5", period) != _quota._sk(CANONICAL, period)

    def test_a_name_outside_the_registry_keeps_its_own_spelling(self):
        # Canonicalisation must not turn an unknown name into the default model's
        # counter — that would charge one model's quota for another.
        assert _quota._sk("no-such-model", "2026-08") == "MQ#no-such-model#2026-08"

    def test_an_empty_name_is_not_the_default_model(self):
        assert canonical_model_id("") == ""
        assert _quota._sk("", "2026-08") == "MQ##2026-08"

    def test_quota_binds_when_the_request_spells_the_model_differently(self, env):
        # The config is written the way the admin API writes it: canonical key.
        # The client asks for the SAME model under its dated alias.
        _put_routing_config(quotas={CANONICAL: {"unit": "usd_micro", "limit": 40_000}})

        ctx = _reserve(DATED_ALIAS)
        assert ctx.selected_model == DATED_ALIAS
        assert ctx.quota_reserved_amount > 0, (
            "a request that names a quota-capped model under another spelling "
            "must still reserve against that model's quota")
        assert _used(CANONICAL) == ctx.quota_reserved_amount

    def test_the_cap_is_reached_through_the_other_spelling(self, env):
        _put_routing_config(quotas={CANONICAL: {"unit": "usd_micro", "limit": 40_000}})
        first = _reserve(DATED_ALIAS)
        assert first.quota_reserved_amount > 0
        # Exhaust: reserve until the limit refuses. With no chain configured there
        # is nothing to cascade to, so exhaustion is a 402 rather than a fallback.
        with pytest.raises(HTTPException) as e:
            for _ in range(40):
                _reserve(DATED_ALIAS)
        assert e.value.status_code == 402
        assert e.value.detail["reason"] == "model_quota_exhausted"


class TestAFailedConfigReadIsNotAnEmptyConfig:

    def test_read_fault_with_nothing_remembered_raises(self, env, monkeypatch):
        monkeypatch.setattr(_config, "get_dynamodb_resource", _RaisingResource)
        with pytest.raises(_config.RoutingConfigUnavailable):
            _config.get_tenant_routing_config(TENANT)

    def test_read_fault_serves_the_last_config_actually_read(self, env, monkeypatch):
        _put_routing_config(
            allowlist=[CANONICAL],
            quotas={CANONICAL: {"unit": "usd_micro", "limit": 40_000}},
        )
        good = _config.get_tenant_routing_config(TENANT)
        assert good.quotas[CANONICAL].limit == 40_000

        # Expire the TTL entry, then fault the read.
        _config._cache.clear()
        monkeypatch.setattr(_config, "get_dynamodb_resource", _RaisingResource)
        stale = _config.get_tenant_routing_config(TENANT)
        assert stale.quotas[CANONICAL].limit == 40_000
        assert stale.allowlist == (CANONICAL,), (
            "a stale restriction is safe; an absent one is not")

    def test_read_fault_is_not_cached_as_a_config(self, env, monkeypatch):
        """The stale answer must not be remembered AS a successful read: once the
        table recovers, the next request past the short retry window sees the
        real config, not the value the fault was served from."""
        _put_routing_config(quotas={CANONICAL: {"unit": "usd_micro", "limit": 40_000}})
        _config.get_tenant_routing_config(TENANT)
        _config._cache.clear()
        monkeypatch.setattr(_config, "get_dynamodb_resource", _RaisingResource)
        _config.get_tenant_routing_config(TENANT)

        monkeypatch.undo()
        _put_routing_config(quotas={CANONICAL: {"unit": "usd_micro", "limit": 7}})
        assert _config.get_tenant_routing_config(TENANT).quotas[CANONICAL].limit == 7

    def test_an_absent_config_row_is_still_an_empty_config(self, env):
        cfg = _config.get_tenant_routing_config("tenant-with-no-config-row")
        assert cfg.quotas == {}
        assert cfg.allowlist == ()

    def test_reserve_fails_closed_when_the_tenant_config_cannot_be_read(
            self, env, monkeypatch):
        def _raise(_tenant_id):
            raise _config.RoutingConfigUnavailable("read failed")

        monkeypatch.setattr(_config, "get_tenant_routing_config", _raise)
        with pytest.raises(HTTPException) as e:
            _reserve(CANONICAL)
        assert e.value.status_code == 503
        assert e.value.detail["reason"] == "routing_config_unavailable"

    def test_reserve_fails_closed_when_the_user_config_cannot_be_read(
            self, env, monkeypatch):
        _put_routing_config(chain=[CANONICAL, "claude-haiku-4-5"], quotas={})

        def _raise(_tenant_id, _user_id):
            raise _config.RoutingConfigUnavailable("read failed")

        monkeypatch.setattr(_config, "get_user_routing_config", _raise)
        with pytest.raises(HTTPException) as e:
            _reserve(CANONICAL)
        assert e.value.status_code == 503
        assert e.value.detail["reason"] == "routing_config_unavailable"


class TestAnUncataloguedModelIsPricedFromTheRegistry:

    def test_the_fallback_reads_the_price_it_does_not_invent_one(self):
        targets = _chains._uncatalogued_targets(CANONICAL)
        assert targets, "an Anthropic-family model must still resolve to a target"
        for t in targets:
            assert t.price_key == "opus", (
                "settle charges `price_key`; a fabricated 'sonnet' bills an "
                "Opus-priced model at the Sonnet rate")
            assert t.cost_tier == _chains._tier_for("opus")
            assert t.cost_tier != 2 or _chains._tier_for("opus") == 2

    def test_the_tier_states_the_models_price_class_not_a_constant(self):
        """`cost_tier` is what the breaker's DOWNGRADE stage filters on and what
        the decision record reports as the chosen model's class. A constant 2
        described an Opus-priced target as a Sonnet-class one: it satisfied a
        tier-2 cap, and the audit trail agreed with it."""
        targets = _chains._uncatalogued_targets(CANONICAL)
        assert _chains._tier_for("opus") > 2, "fixture assumes Opus above tier 2"
        assert {t.cost_tier for t in targets} == {_chains._tier_for("opus")}
        assert all(t.cost_tier > 2 for t in targets)

    def test_an_unservable_vllm_entry_yields_no_targets(self, monkeypatch):
        """`_build_catalog` states that a vLLM entry with hybrid serving off
        "resolves exactly like a model that does not exist". That held only for a
        non-Anthropic provider: an `anthropic` entry resolved through
        `resolve_bedrock_model` and was routed into a Bedrock region."""
        from mvp import models as _models
        from mvp.serving import vllm as _vllm

        entry = SimpleNamespace(
            aliases=("local-opus",), bedrock_model_id="local/opus",
            pricing_key="opus", served_by="vllm", endpoint_key="not-allowlisted",
        )
        monkeypatch.setattr(_models, "resolve_model", lambda _n: entry)
        monkeypatch.setattr(_models, "resolve_bedrock_model", lambda _n: "local/opus")
        monkeypatch.setattr(_vllm, "endpoint_is_servable", lambda _k: False)

        assert _chains._uncatalogued_targets("local-opus") == []
        with pytest.raises(ValueError, match="not servable"):
            _chains.resolve_chain("local-opus")

    def test_a_servable_vllm_entry_still_resolves(self, monkeypatch):
        from mvp import models as _models
        from mvp.serving import vllm as _vllm

        entry = SimpleNamespace(
            aliases=("local-opus",), bedrock_model_id="local/opus",
            pricing_key="opus", served_by="vllm", endpoint_key="allowlisted",
        )
        monkeypatch.setattr(_models, "resolve_model", lambda _n: entry)
        monkeypatch.setattr(_models, "resolve_bedrock_model", lambda _n: "local/opus")
        monkeypatch.setattr(_vllm, "endpoint_is_servable", lambda _k: True)

        targets = _chains._uncatalogued_targets("local-opus")
        assert targets and all(t.price_key == "opus" for t in targets)
