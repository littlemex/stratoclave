"""A per-user money ceiling must be walked down to what the request actually cost.

Admission reserves the BOUND -- the most the request could cost -- and settle moves `used`
from that bound to the real figure. For a tenant with no dollar pool the settle never ran,
because the call that does it sat inside a block gated on `pool_active`, while the two
counters it settles belong to walls configured independently of any pool.

Observed on real infrastructure: a ceiling charged 1542 micro-USD for a request that cost
41, so a member exhausted their personal allowance roughly 37 times too early. Not a money
leak -- the direction is conservative, it never over-admits -- but a member hits a wall long
before they have spent what the wall is for, and nothing tells them why.

The asymmetry below is what makes this an oversight rather than a decision, and it is worth a
test of its own: the RELEASE path was already unconditional, so a failed request gave its
reservation back and a successful one did not.
"""
from __future__ import annotations

import pytest


def _tenant(tenant: str, *, with_pool: bool, ceiling_microusd: int = 5_000_000) -> str:
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.tenants import TenantsRepository

    repo = TenantsRepository()
    repo.create(tenant_id=tenant, name=tenant, team_lead_user_id="lead",
                default_credit=10 ** 9, created_by="test")
    period = current_period()
    if with_pool:
        TenantBudgetsRepository().set_manual_limit(
            tenant_id=tenant, period=period, manual_limit_microusd=1_000_000_000)
    # A ceiling in force for the CURRENT period. Written directly because the setter
    # refuses a period that has begun, which is the invariant the ceiling rests on.
    repo._table.update_item(
        Key={"tenant_id": tenant},
        UpdateExpression="SET user_dollar_defaults = :m, user_dollar_defaults_version = :v",
        ExpressionAttributeValues={":m": {"2020-01": ceiling_microusd}, ":v": 1})
    return period


def _user(tenant: str, user_id: str):
    from dynamo.user_tenants import UserTenantsRepository
    from mvp.deps import AuthenticatedUser

    UserTenantsRepository().ensure(
        user_id=user_id, tenant_id=tenant, role="user", total_credit=10 ** 12)
    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@test.example", org_id=tenant, roles=["user"],
        raw_claims={}, auth_kind="jwt", key_scopes=None, api_key_hash=None)


def _used(tenant: str, user_id: str, period: str) -> int:
    from mvp.routing.user_dollar_quota import _table, uq_pk, uq_sk

    got = _table().get_item(
        Key={"pk": uq_pk(tenant, user_id), "sk": uq_sk(period)}, ConsistentRead=True)
    item = got.get("Item") or {}
    return int(item.get("used", 0) or 0)


def _reserve(user):
    from mvp._pipeline import reserve_credit_for_model

    return reserve_credit_for_model(
        user, reservation_tokens=500, model_name="claude-sonnet-5",
        input_tokens_est=400, max_output_tokens=100,
        task_tag="migration", task_tag_source="asserted")


class TestTheCeilingIsSettledToTheActual:
    @pytest.mark.parametrize("with_pool", [False, True])
    def test_used_falls_from_the_reserved_bound_to_the_actual_spend(
        self, dynamodb_mock, with_pool,
    ):
        """Parametrised over the pool deliberately.

        The pooled case already worked, and running both through one test is what states
        that the pool is IRRELEVANT to this counter -- which is the whole point. A test
        written only for the unpooled case would leave a reader wondering whether the two
        are supposed to differ.
        """
        from mvp._pipeline import settle_reservation_and_log

        tenant = f"ceiling-settle-{with_pool}"
        period = _tenant(tenant, with_pool=with_pool)
        user = _user(tenant, "u-settle")

        ctx = _reserve(user)
        reserved = int(getattr(ctx, "uq_reserved_amount", 0) or 0)
        assert reserved > 0, (
            "the per-user ceiling reserved nothing, so this test cannot say anything about "
            "settling it")
        assert _used(tenant, "u-settle", period) == reserved, (
            "admission should have charged the bound")

        settle_reservation_and_log(
            user=user, tenants_repo=ctx.tenants_repo, reservation=500,
            actual_input_tokens=350, actual_output_tokens=80,
            model_id="us.anthropic.claude-sonnet-5", context=ctx)

        after = _used(tenant, "u-settle", period)
        assert after < reserved, (
            f"`used` stayed at the reserved bound {reserved} after settle (now {after}); "
            f"a member's ceiling is consumed at the most the request COULD have cost "
            f"instead of what it did, so they hit the wall far too early")
        assert after > 0, f"settle drove `used` to {after}; it should hold the actual spend"

    def test_a_failed_request_still_gives_the_reservation_back(self, dynamodb_mock):
        """The other half of the asymmetry that made the defect findable.

        Release was already unconditional. This pins it so a future change cannot "fix"
        the symmetry by gating release too -- which would balance the code and leak a
        member's ceiling on every failed request.
        """
        from mvp._pipeline import release_pool

        tenant = "ceiling-release"
        period = _tenant(tenant, with_pool=False)
        user = _user(tenant, "u-release")
        ctx = _reserve(user)
        reserved = int(getattr(ctx, "uq_reserved_amount", 0) or 0)
        assert reserved > 0
        assert _used(tenant, "u-release", period) == reserved

        release_pool(ctx)

        assert _used(tenant, "u-release", period) == 0, (
            "a failed request must not leave the ceiling consumed until period rollover")

    def test_settling_twice_does_not_double_apply(self, dynamodb_mock):
        """Moving the call outside the pool block also moved it outside
        `_pool_finalized`, which is what guarded the pool from a double settle. The
        quota side has its own idempotency -- it clears both reserved amounts in a
        `finally` -- and this is the test that says so rather than trusting it.
        """
        from mvp._pipeline import settle_reservation_and_log

        tenant = "ceiling-twice"
        period = _tenant(tenant, with_pool=False)
        user = _user(tenant, "u-twice")
        ctx = _reserve(user)

        kwargs = dict(
            user=user, tenants_repo=ctx.tenants_repo, reservation=500,
            actual_input_tokens=350, actual_output_tokens=80,
            model_id="us.anthropic.claude-sonnet-5", context=ctx)
        settle_reservation_and_log(**kwargs)
        once = _used(tenant, "u-twice", period)
        settle_reservation_and_log(**kwargs)
        twice = _used(tenant, "u-twice", period)

        assert once == twice, (
            f"a second settle changed `used` from {once} to {twice}; the adjustment was "
            f"applied twice, which would drive a member's ceiling below what they spent")


class TestTheSettleTokenIsPerReservation:
    """The settle's idempotency token must not be shared between requests.

    It was derived from `hold_id or period`. An unpooled reservation has no hold, so the
    seed became the bare period string -- the same for every unpooled request in the month,
    across every tenant. The first settle of a period succeeded and every later one sent
    that token with a different delta, which DynamoDB refuses with
    `IdempotentParameterMismatchException`; the surrounding `except` swallowed it and the
    counter kept the reserved bound.

    Found only on real infrastructure. moto does not enforce `ClientRequestToken`
    idempotency, so every one of these settles "succeeded" locally -- which is why these
    tests assert the TOKEN rather than the resulting counter. A test that checked `used`
    would pass under moto against the broken code.
    """

    def test_two_reservations_in_one_period_get_different_tokens(self, dynamodb_mock):
        from mvp._pipeline import _settle_token_seed, _derived_token

        class _Ctx:
            hold_id = None
            request_id = None
            period = "2026-09"
            _settle_seed = None

        a, b = _Ctx(), _Ctx()
        seed_a, seed_b = _settle_token_seed(a), _settle_token_seed(b)
        assert seed_a != seed_b, (
            "two reservations in the same period share a settle token; the second one's "
            "transaction is refused as an idempotency mismatch and its counter is never "
            "adjusted")
        assert _derived_token(seed_a, "quota-uq-x") != _derived_token(seed_b, "quota-uq-x")

    def test_the_same_reservation_keeps_its_token_across_retries(self, dynamodb_mock):
        """The other requirement, and it pulls the opposite way.

        A lost-ack retry of ONE settle must re-send the same token, or the unconditional
        `ADD used :d` applies twice and drives `used` below what the member spent.
        """
        from mvp._pipeline import _settle_token_seed

        class _Ctx:
            hold_id = None
            request_id = None
            period = "2026-09"
            _settle_seed = None

        ctx = _Ctx()
        assert _settle_token_seed(ctx) == _settle_token_seed(ctx)

    def test_a_request_id_is_preferred_over_the_period(self, dynamodb_mock):
        from mvp._pipeline import _settle_token_seed

        class _Ctx:
            hold_id = None
            request_id = "req_abc123"
            period = "2026-09"
            _settle_seed = None

        assert _settle_token_seed(_Ctx()) == "req_abc123"

    def test_a_pooled_reservation_still_keys_on_its_hold(self, dynamodb_mock):
        """Pooled behaviour is deliberately unchanged, so this pins it."""
        from mvp._pipeline import _settle_token_seed

        class _Ctx:
            hold_id = "hold-1"
            request_id = "req_abc123"
            period = "2026-09"
            _settle_seed = None

        assert _settle_token_seed(_Ctx()) == "hold-1"

    def test_the_period_is_never_the_seed(self, dynamodb_mock):
        """The specific regression, stated as its own assertion.

        Named separately from the uniqueness test above because a future refactor could
        reintroduce a period fallback while still returning distinct values for two
        contexts that happen to differ in some other field.
        """
        from mvp._pipeline import _settle_token_seed

        class _Ctx:
            hold_id = None
            request_id = None
            period = "2026-09"
            _settle_seed = None

        assert _settle_token_seed(_Ctx()) != "2026-09"
