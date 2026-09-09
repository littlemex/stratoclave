"""`measured_bound_microusd` must mean a bound was measured.

`UsageLogsRepository.record`'s own docstring promises the attribute is "absent when the
bound was never computed for this request (the `accounting` state)". It was not: when no
bound was passed, `reserve_credit` fell back to `cost_microusd` -- the legacy heuristic
estimate -- and wrote that under the bound's name.

The fallback's stated reason was that "bound and reserved coincide" when no explicit bound
is passed. That holds for the `enforced` state, and `enforced` never reached the fallback:
both production call sites pass `bound_microusd` whenever `_price` computed one. The only
state that reached it was `accounting`, where there is no bound at all. **The fallback fired
exactly where its justification does not hold.**

Why it is not cosmetic: the attribute exists for a shadow-run ratio analysis -- comparing the
bound against the real charge to decide whether the bound is tight enough to ENFORCE. A
mixture of bounds and heuristic estimates under one name makes that comparison, and the
decision drawn from it, quietly wrong. The row carries no `bound_mode`, so a reader cannot
filter the mixture apart afterwards.

Observed on real infrastructure first: a row carried a bound for a tenant with no pool row
and with the measurement flag unset, which `dollar_pool_bound_should_compute` says should
produce no bound at all.
"""
from __future__ import annotations

import pytest

MODEL = "claude-sonnet-5"


def _tenant(tenant: str, *, with_pool: bool) -> None:
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.tenants import TenantsRepository

    TenantsRepository().create(
        tenant_id=tenant, name=tenant, team_lead_user_id="lead",
        default_credit=10 ** 9, created_by="test")
    if with_pool:
        TenantBudgetsRepository().set_manual_limit(
            tenant_id=tenant, period=current_period(),
            manual_limit_microusd=1_000_000_000)


def _user(tenant: str, user_id: str):
    from dynamo.user_tenants import UserTenantsRepository
    from mvp.deps import AuthenticatedUser

    UserTenantsRepository().ensure(
        user_id=user_id, tenant_id=tenant, role="user", total_credit=10 ** 12)
    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@test.example", org_id=tenant, roles=["user"],
        raw_claims={}, auth_kind="jwt", key_scopes=None, api_key_hash=None)


class TestTheAccountingStateRecordsNoBound:
    def test_no_input_bytes_means_no_bound_on_the_context(self, dynamodb_mock):
        """The `accounting` state: the caller supplies no byte count, so no survey runs.

        This is the shape every route produces when `dollar_pool_bound_should_compute` says
        no -- no pool row and the measurement flag off.
        """
        from mvp._pipeline import reserve_credit_for_model

        tenant = "accounting-no-bound"
        _tenant(tenant, with_pool=False)
        user = _user(tenant, "u-acct")

        ctx = reserve_credit_for_model(
            user, reservation_tokens=500, model_name=MODEL,
            input_tokens_est=400, max_output_tokens=100)

        assert ctx.bound_mode is None, (
            "no bound was computed, so nothing should claim a bound strategy")
        assert ctx.measured_bound_microusd is None, (
            f"the context carries {ctx.measured_bound_microusd!r} as a measured bound when "
            f"no bound was measured; that value is the legacy heuristic estimate, and a "
            f"shadow-run ratio analysis would treat it as a bound")

    def test_no_bound_reaches_the_usage_row_either(self, dynamodb_mock):
        """The end a downstream reader sees, which is where the contract is written."""
        from boto3.dynamodb.conditions import Key as boto3_key
        from dynamo.usage_logs import UsageLogsRepository
        from mvp._pipeline import reserve_credit_for_model, settle_reservation_and_log

        tenant = "accounting-no-bound-row"
        _tenant(tenant, with_pool=False)
        user = _user(tenant, "u-acct-row")
        ctx = reserve_credit_for_model(
            user, reservation_tokens=500, model_name=MODEL,
            input_tokens_est=400, max_output_tokens=100)
        settle_reservation_and_log(
            user=user, tenants_repo=ctx.tenants_repo, reservation=500,
            actual_input_tokens=350, actual_output_tokens=80,
            model_id="us.anthropic.claude-sonnet-5", context=ctx)

        items = UsageLogsRepository()._table.query(
            KeyConditionExpression=boto3_key("tenant_id").eq(tenant)).get("Items", [])
        assert items, "settle wrote no usage row"
        item = items[0]
        assert "measured_bound_microusd" not in item, (
            f"the row carries measured_bound_microusd={item['measured_bound_microusd']!r} "
            f"for a request whose bound was never computed. `record`'s docstring promises "
            f"this attribute is absent in the accounting state, and the row carries no "
            f"`bound_mode`, so a reader cannot tell this from a real bound")
        # The cost IS present -- that is a different fact, and the one a reader wants here.
        assert "cost_microusd" in item

    @pytest.mark.parametrize("with_pool", [False, True])
    def test_a_surveyed_request_still_records_its_bound(self, dynamodb_mock, with_pool):
        """The other half: removing the fallback must not lose a bound that WAS measured.

        Parametrised over the pool because both the `measured` state (no pool, survey run)
        and the `enforced` state (pool, survey run) must record it -- the bound belongs to
        the survey, not to the pool.
        """
        from mvp._pipeline import reserve_credit_for_model

        tenant = f"surveyed-{with_pool}"
        _tenant(tenant, with_pool=with_pool)
        user = _user(tenant, "u-surveyed")
        ctx = reserve_credit_for_model(
            user, reservation_tokens=2500, model_name=MODEL,
            input_tokens_est=2000, max_output_tokens=400,
            input_bytes=6000, payload_hash="cafebabe", extra_input_tokens=0)

        assert ctx.bound_mode is not None, "a surveyed request should name its strategy"
        assert ctx.measured_bound_microusd is not None, (
            "a bound was measured and was not recorded; removing the fallback must not "
            "lose the value it was masking")
        assert ctx.measured_bound_microusd > 0
