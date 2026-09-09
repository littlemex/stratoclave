"""A request priced in dollars must record its cost whether or not a pool exists.

Phase 5 found the gap on real infrastructure. A tenant with a per-user dollar ceiling
and no tenant pool was enforced in dollars -- 1542 micro-USD on the quota row -- while
every by-tag row for the same requests read $0.00, because `settle_reservation_and_log`
computed the settled cost only when `context.pool_active`.

That gate was correct once: a pool was the only thing that priced a request in dollars,
so "no pool" and "no dollar cost" were one statement. The per-user ceiling reads only
`user_dollar_defaults` and never the pool, so the two came apart, and the report went
on summing an attribute that was no longer written.

These tests are about what the USAGE ROW says. They deliberately do not assert anything
about pool counters or ledger events: those stay gated on a pool, and a test that
watched them here would fail for the wrong reason if that gating were ever loosened.
"""
from __future__ import annotations

import pytest
from boto3.dynamodb.conditions import Key as boto3_key


def _user(tenant: str, user_id: str):
    from dynamo.user_tenants import UserTenantsRepository
    from mvp.deps import AuthenticatedUser

    UserTenantsRepository().ensure(
        user_id=user_id, tenant_id=tenant, role="user", total_credit=10 ** 12)
    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@test.example", org_id=tenant, roles=["user"],
        raw_claims={}, auth_kind="jwt", key_scopes=None, api_key_hash=None)


def _tenant(tenant: str, *, with_pool: bool):
    from dynamo.tenants import TenantsRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period

    TenantsRepository().create(
        tenant_id=tenant, name=tenant, team_lead_user_id="lead", default_credit=10 ** 9,
        created_by="test")
    period = current_period()
    if with_pool:
        TenantBudgetsRepository().set_manual_limit(
            tenant_id=tenant, period=period, manual_limit_microusd=1_000_000_000)
    return period


def _row(tenant: str) -> dict:
    from dynamo.usage_logs import UsageLogsRepository

    items = UsageLogsRepository()._table.query(
        KeyConditionExpression=boto3_key("tenant_id").eq(tenant)).get("Items", [])
    assert items, "settle wrote no usage row at all"
    return items[0]


class TestCostIsRecordedWithoutAPool:
    def test_a_tenant_with_no_pool_still_gets_its_cost_on_the_usage_row(
        self, dynamodb_mock,
    ):
        from mvp._pipeline import reserve_credit_for_model, settle_reservation_and_log

        tenant = "no-pool-priced"
        _tenant(tenant, with_pool=False)
        user = _user(tenant, "u-no-pool")

        ctx = reserve_credit_for_model(
            user, reservation_tokens=500, model_name="claude-sonnet-5",
            input_tokens_est=400, max_output_tokens=100,
            task_tag="migration", task_tag_source="asserted")
        # The precondition the fix rests on: the rate is frozen at reserve time on the
        # accounting path too, so nothing extra has to be fetched at settle. Asserted
        # rather than assumed -- if this ever stops holding, the test below would start
        # passing for a different reason.
        assert ctx.pricing_key, "no pricing key on an unpooled reservation"
        assert ctx.rate_snapshot is not None, (
            "no frozen rate on an unpooled reservation; the fix's premise is that "
            "`_price` freezes one on every path, including `accounting`")
        assert not ctx.pool_active, "this tenant was supposed to have no pool"

        settle_reservation_and_log(
            user=user, tenants_repo=ctx.tenants_repo, reservation=500,
            actual_input_tokens=350, actual_output_tokens=80,
            model_id="us.anthropic.claude-sonnet-5", context=ctx)

        item = _row(tenant)
        assert "cost_microusd" in item, (
            "an unpooled request was priced in dollars and its cost was not recorded; "
            "the by-tag report sums this attribute, so its absence is reported as $0.00 "
            "and a reader concludes the work was free")
        assert int(item["cost_microusd"]) > 0

    def test_a_pooled_tenant_is_unchanged(self, dynamodb_mock):
        from mvp._pipeline import reserve_credit_for_model, settle_reservation_and_log

        tenant = "with-pool-priced"
        _tenant(tenant, with_pool=True)
        user = _user(tenant, "u-pool")
        ctx = reserve_credit_for_model(
            user, reservation_tokens=500, model_name="claude-sonnet-5",
            input_tokens_est=400, max_output_tokens=100,
            task_tag="migration", task_tag_source="asserted")
        settle_reservation_and_log(
            user=user, tenants_repo=ctx.tenants_repo, reservation=500,
            actual_input_tokens=350, actual_output_tokens=80,
            model_id="us.anthropic.claude-sonnet-5", context=ctx)
        item = _row(tenant)
        assert int(item["cost_microusd"]) > 0


class TestUnknownCostIsNotRecordedAsZero:
    def test_no_rate_and_no_pool_writes_NO_cost_attribute_rather_than_zero(
        self, dynamodb_mock,
    ):
        """The subtle half of the fix, and the reason the fallback stayed pool-gated.

        The no-rate branch settles at `pool_reserved_microusd`, which is a sound upper
        bound for a POOLED reservation. For an unpooled one it is 0, so relaxing the
        whole block would have written `cost_microusd = 0` -- and a stored zero asserts
        the request was free, which is worse than recording nothing. The entire defect
        is that a reader cannot tell those apart, so a fix that manufactures more zeros
        makes it worse.
        """
        from mvp._pipeline import reserve_credit_for_model, settle_reservation_and_log

        tenant = "no-pool-no-rate"
        _tenant(tenant, with_pool=False)
        user = _user(tenant, "u-unpriced")
        ctx = reserve_credit_for_model(
            user, reservation_tokens=500, model_name="claude-sonnet-5",
            input_tokens_est=400, max_output_tokens=100,
            task_tag="migration", task_tag_source="asserted")
        # A reservation restored from an older ledger event carries no snapshot. Forced
        # here because no in-band path produces one on this version -- pricing fails
        # closed -- and this is precisely the shape the branch exists for.
        ctx.rate_snapshot = None

        settle_reservation_and_log(
            user=user, tenants_repo=ctx.tenants_repo, reservation=500,
            actual_input_tokens=350, actual_output_tokens=80,
            model_id="us.anthropic.claude-sonnet-5", context=ctx)

        item = _row(tenant)
        assert "cost_microusd" not in item, (
            f"an unprice-able request recorded a cost of {item.get('cost_microusd')!r}; "
            "it must record NO attribute, so the aggregation counts it as a request "
            "whose cost is unknown rather than one that was free")

    def test_the_aggregation_counts_that_row_as_missing_a_cost(self, dynamodb_mock):
        """The other end of the same fact: the report must say the total is incomplete."""
        from dynamo.tenant_budgets import current_period
        from dynamo.usage_logs import UsageLogsRepository

        repo = UsageLogsRepository()
        repo.record(tenant_id="agg", user_id="u", user_email="u@x", model_id="m",
                    input_tokens=1, output_tokens=1, cost_microusd=90,
                    task_tag="t", task_tag_source="asserted")
        repo.record(tenant_id="agg", user_id="u", user_email="u@x", model_id="m",
                    input_tokens=1, output_tokens=1,
                    task_tag="t", task_tag_source="asserted")
        row = {r.task_tag: r for r in repo.aggregate_by_tag(
            tenant_id="agg", period=current_period()).rows}["t"]
        assert row.requests == 2
        assert row.cost_microusd == 90
        assert row.requests_without_cost == 1


class TestTheReserveItselfCarriesTheRate:
    """The gap the first version of these tests had, and why real hardware found it.

    `test_a_tenant_with_no_pool_still_gets_its_cost_on_the_usage_row` passed against the
    unfixed reserve, because it called `reserve_credit_for_model` and handed the returned
    context straight to settle -- and on THAT path the snapshot survives. The HTTP path
    for a tenant with a per-user money ceiling and no pool goes through
    `_reserve_quota_without_pool`, which took the pricing KEY and dropped the SNAPSHOT.
    So settle found no rate, recorded no cost, and the report said the work was free.

    These tests assert the CONTEXT, not the row, because that is where the fact was lost.
    A test that only watched the row would pass again the moment any other path happened
    to supply a rate.
    """

    def test_the_money_ceiling_path_keeps_the_rate_it_admitted_at(self, dynamodb_mock):
        from dynamo.tenant_budgets import current_period
        from dynamo.tenants import TenantsRepository
        from mvp._pipeline import reserve_credit_for_model

        tenant = "ceiling-no-pool"
        _tenant(tenant, with_pool=False)
        # A per-user money ceiling in force, and NO pool: the configuration that made the
        # dropped snapshot observable. Written directly because the setter refuses a
        # period that has already begun, which is the invariant the ceiling rests on.
        TenantsRepository()._table.update_item(
            Key={"tenant_id": tenant},
            UpdateExpression=(
                "SET user_dollar_defaults = :m, user_dollar_defaults_version = :v"),
            ExpressionAttributeValues={":m": {"2020-01": 5_000_000}, ":v": 1},
        )
        user = _user(tenant, "u-ceiling")

        ctx = reserve_credit_for_model(
            user, reservation_tokens=500, model_name="claude-sonnet-5",
            input_tokens_est=400, max_output_tokens=100,
            task_tag="migration", task_tag_source="asserted")

        assert not ctx.pool_active, "this tenant was supposed to have no pool"
        assert ctx.pricing_key, "the pricing key was dropped too"
        assert ctx.rate_snapshot is not None, (
            "the reservation was admitted at a known rate and did not keep it; settle "
            "cannot price the request, records no cost, and the by-tag report reads "
            "$0.00 for work that was charged")

    def test_the_row_then_carries_a_cost(self, dynamodb_mock):
        """The end the reader sees, through the same path as the test above."""
        from dynamo.tenants import TenantsRepository
        from mvp._pipeline import reserve_credit_for_model, settle_reservation_and_log

        tenant = "ceiling-no-pool-row"
        _tenant(tenant, with_pool=False)
        TenantsRepository()._table.update_item(
            Key={"tenant_id": tenant},
            UpdateExpression=(
                "SET user_dollar_defaults = :m, user_dollar_defaults_version = :v"),
            ExpressionAttributeValues={":m": {"2020-01": 5_000_000}, ":v": 1},
        )
        user = _user(tenant, "u-ceiling-row")
        ctx = reserve_credit_for_model(
            user, reservation_tokens=500, model_name="claude-sonnet-5",
            input_tokens_est=400, max_output_tokens=100,
            task_tag="migration", task_tag_source="asserted")
        settle_reservation_and_log(
            user=user, tenants_repo=ctx.tenants_repo, reservation=500,
            actual_input_tokens=350, actual_output_tokens=80,
            model_id="us.anthropic.claude-sonnet-5", context=ctx)
        item = _row(tenant)
        assert "cost_microusd" in item and int(item["cost_microusd"]) > 0, (
            f"row recorded {item.get('cost_microusd')!r}")
