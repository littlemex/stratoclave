"""Contract 1.6 / 1.2 / 1.4 — the model policy set, and the unit a cap is in.

  C1.6 A request is served only by a model inside the tenant's configured policy
       set. An empty admissible set is a refusal, never a widening.
  C1.4 The identity of a limit's subject is the subject, not the spelling the
       caller used for it.
  C1.2 Every limit CONFIGURED for the request participates in the admission
       write. A cap enforced in a different denomination than it was written in is
       not the configured cap.

Candidate resolution treated "the admissible list came out empty" as a
loss of availability to recover from, and recovered it by widening: after the
allowlist filter it fell back to the top of the chain, and after the servability
filter it fell back to the model the client asked for — which the route validated
for protocol and nobody validated for policy. Both fallbacks are the same mistake,
which is why the contract clause is about the empty set rather than about either
site.
"""
from __future__ import annotations

from dataclasses import dataclass

import boto3
import pytest
from fastapi import HTTPException

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline
from mvp.routing import config as _routing_config


TENANT = "model-policy-org"
USER = "user-model-policy-0001"

ALLOWED = "claude-haiku-4-5"
ALLOWED_DATED_ALIAS = "claude-haiku-4-5-20251001"
NOT_ALLOWED = "claude-opus-4-5"


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


@pytest.fixture
def env(dynamodb_mock):
    _routing_config.reset_cache()
    UserTenantsRepository().ensure(
        user_id=USER, tenant_id=TENANT, role="user", total_credit=10**12,
    )
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=TENANT, period=current_period(), manual_limit_microusd=10**11,
    )
    yield
    _routing_config.reset_cache()


def _put_routing_config(**item):
    boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-user-tenants"
    ).put_item(Item={"user_id": "CONFIG#ROUTING", "tenant_id": TENANT, **item})
    _routing_config.reset_cache()


def _reserve(model, *, wire_protocol="messages"):
    return _pipeline.reserve_credit_for_model(
        _User(user_id=USER, org_id=TENANT),
        1000,
        model_name=model,
        input_tokens_est=500,
        max_output_tokens=500,
        wire_protocol=wire_protocol,
    )


class TestTheAllowlistIsThePolicySet:

    def test_a_model_outside_the_allowlist_is_refused_not_substituted(self, env):
        """Serving a different model than the one asked for is the substitution the
        pin path already refuses to do; the allowlist path did it silently."""
        _put_routing_config(allowlist=[ALLOWED], quotas={}, fallback_default="off")
        with pytest.raises(HTTPException) as e:
            _reserve(NOT_ALLOWED)
        assert e.value.status_code == 403
        assert e.value.detail["reason"] == "model_not_allowed"

    def test_membership_is_by_model_not_by_spelling(self, env):
        """The admin write path stores canonical ids. A client naming the same
        model by another alias is naming an allowed model."""
        _put_routing_config(allowlist=[ALLOWED], quotas={}, fallback_default="off")
        ctx = _reserve(ALLOWED_DATED_ALIAS)
        assert ctx.selected_model == ALLOWED_DATED_ALIAS

    def test_an_allowlist_with_nothing_servable_here_refuses(self, env):
        """The failure Fable's audit named: when no allowlisted model speaks this
        route's protocol, the servability filter emptied the list and the fallback
        served the requested model — outside the allowlist, and with no per-model
        quota line, because the tenant configured none for a model it never
        expected to serve."""
        gpt = None
        from mvp.models import _REGISTRY
        for entry in _REGISTRY:
            if entry.wire_protocol == "responses" and entry.aliases:
                gpt = entry.aliases[0]
                break
        assert gpt is not None, "fixture needs a responses-protocol model"

        # Allowlist holds only a messages-protocol model; the request arrives on
        # the responses route asking for a responses-protocol model that is NOT
        # allowlisted.
        _put_routing_config(allowlist=[ALLOWED], quotas={}, fallback_default="off")
        with pytest.raises(HTTPException) as e:
            _reserve(gpt, wire_protocol="responses")
        assert e.value.status_code == 403
        assert e.value.detail["reason"] == "model_not_allowed"


class TestACapIsEnforcedInTheUnitItWasWrittenIn:

    def test_a_quota_in_an_unsupported_unit_refuses_the_request(self, env):
        """`ModelQuotaConfig.unit` parses as "tokens" by default while the
        chokepoint reserves micro-USD and never reads the unit, so a row written
        before the admin path pinned the unit — or written out of band — is
        enforced in a denomination it was not written in. Enforcing the wrong
        number is worse than refusing: the operator believes a cap is in force."""
        _put_routing_config(
            quotas={ALLOWED: {"unit": "tokens", "limit": 10_000_000}},
        )
        with pytest.raises(HTTPException) as e:
            _reserve(ALLOWED)
        assert e.value.status_code == 503
        assert e.value.detail["reason"] == "quota_unit_unsupported"

    def test_a_quota_row_keyed_by_another_spelling_still_binds(self, env):
        """The cap's subject is the model. A row written with a dated alias — by a
        writer that predates the canonical pin, or out of band — used to miss the
        canonical lookup entirely, so the configured cap contributed nothing to the
        admission write and the unit check never ran either."""
        _put_routing_config(
            quotas={ALLOWED_DATED_ALIAS: {"unit": "usd_micro", "limit": 40_000}},
        )
        ctx = _reserve(ALLOWED)
        assert ctx.quota_reserved_amount > 0

    def test_the_supported_unit_still_reserves(self, env):
        _put_routing_config(
            quotas={ALLOWED: {"unit": "usd_micro", "limit": 10_000_000}},
        )
        ctx = _reserve(ALLOWED)
        assert ctx.quota_reserved_amount > 0
