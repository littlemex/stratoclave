"""Regression tests for the pool-budget hardening pass.

Each test here pins a *fail-open* or accounting hole that an adversarial review
found in the first cut of tenant dollar pools. They are written so they would
FAIL against the pre-hardening behaviour:

  - P0-1  retry exhaustion under contention must fail **closed** (never slip a
          request through per-user-only while the pool is still present);
  - P0-2  a suspended pool must reject immediately, not fall through;
  - P0-3  an error path must release the pool hold (no reserved leak);
  - P1-1  settle must be idempotent (a double-settle must not drive the pool
          negative);
  - P1-3  cache tokens must be billed to the pool, not settled at zero;
  - P1-2b idempotency tokens must be UNIQUE per logical write (a token derived
          from shared snapshot state collides across concurrent callers, which
          real DynamoDB rejects with IdempotentParameterMismatchException —
          found only by the live-DynamoDB harness, invisible under moto);
  - misc  UsageLogs must persist the settled cost so the pool is auditable, and
          the pricing cache must fall back to defaults when overrides vanish.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk, current_period
from mvp import _pipeline
from mvp._pipeline import (
    ReservationContext,
    release_pool,
    reserve_credit,
    settle_reservation_and_log,
)


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


def _user(seed) -> _User:
    return _User(user_id=seed["user_id"], org_id=seed["tenant_id"])


def _pool(seed):
    return TenantBudgetsRepository().pool_summary(seed["tenant_id"], seed["period"])


# ---------------------------------------------------------------------------
# P1-5b: single-item refund/reserve must retry TransactionConflictException
# ---------------------------------------------------------------------------
# Found live: under concurrency a settle's per-user refund (a single UpdateItem)
# collides with a concurrent pooled reserve's TransactWriteItems on the same
# row, and real DynamoDB raises TransactionConflictException. The old code only
# swallowed ConditionalCheckFailed, so the conflict propagated as an HTTP 500.
# refund()/reserve() must retry the transient conflict. moto never raises it, so
# we inject it into the underlying table client.
def test_refund_retries_transaction_conflict(seed_active_tenant, monkeypatch):
    from botocore.exceptions import ClientError
    from dynamo.user_tenants import UserTenantsRepository

    repo = UserTenantsRepository()
    seed = seed_active_tenant
    # Pre-spend some credit so there is something to refund.
    repo.reserve(user_id=seed["user_id"], tenant_id=seed["tenant_id"], tokens=5000)

    real_update = repo._table.update_item
    calls = {"n": 0}

    def flaky_update(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientError(
                {"Error": {"Code": "TransactionConflictException",
                           "Message": "Transaction is ongoing for the item"}},
                "UpdateItem",
            )
        return real_update(*args, **kwargs)

    monkeypatch.setattr(repo._table, "update_item", flaky_update)
    import dynamo.user_tenants as ut
    monkeypatch.setattr(ut.time, "sleep", lambda *_: None)

    # Must NOT raise; must retry past the injected conflict and actually refund.
    remaining = repo.refund(user_id=seed["user_id"], tenant_id=seed["tenant_id"], tokens=2000)
    assert calls["n"] == 2, "refund should retry once after the conflict"
    # 10000 total - (5000 reserved - 2000 refunded) = 7000 remaining.
    assert remaining == 7000


def test_reserve_retries_transaction_conflict(seed_active_tenant, monkeypatch):
    from botocore.exceptions import ClientError
    from dynamo.user_tenants import UserTenantsRepository

    repo = UserTenantsRepository()
    seed = seed_active_tenant
    real_update = repo._table.update_item
    calls = {"n": 0}

    def flaky_update(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientError(
                {"Error": {"Code": "TransactionConflictException",
                           "Message": "Transaction is ongoing for the item"}},
                "UpdateItem",
            )
        return real_update(*args, **kwargs)

    monkeypatch.setattr(repo._table, "update_item", flaky_update)
    import dynamo.user_tenants as ut
    monkeypatch.setattr(ut.time, "sleep", lambda *_: None)

    # Must retry past the conflict and reserve successfully (no 500).
    repo.reserve(user_id=seed["user_id"], tenant_id=seed["tenant_id"], tokens=3000)
    assert calls["n"] >= 2
    assert repo.remaining_credit(seed["user_id"], seed["tenant_id"]) == 7000


# ---------------------------------------------------------------------------
# P0-2: suspended pool must fail closed
# ---------------------------------------------------------------------------
def test_suspended_pool_rejects_immediately(seed_tenant_with_pool):
    """A suspended pool must 402 tenant_pool_exhausted — NOT fall through to a
    per-user-only reservation. Pre-fix, the reserve transaction's
    `status = active` condition failed every attempt and the request slipped
    through unpriced.
    """
    seed = seed_tenant_with_pool
    # Flip the pool to suspended.
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=seed["tenant_id"],
        period=seed["period"],
        pool_limit_microusd=seed["pool_limit_microusd"],
        status="suspended",
    )
    user = _user(seed)

    with pytest.raises(HTTPException) as exc:
        reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    assert exc.value.status_code == 402
    assert exc.value.detail["reason"] == "tenant_pool_exhausted"

    # The pool was not charged, and crucially the request did NOT succeed
    # off-pool: reserved stays at zero.
    assert _pool(seed)["pool_reserved_microusd"] == 0


# ---------------------------------------------------------------------------
# P0-1: retry exhaustion under contention must fail closed
# ---------------------------------------------------------------------------
def test_retry_exhaustion_fails_closed_not_open(seed_tenant_with_pool, monkeypatch):
    """If every pool reserve attempt is cancelled by a (simulated) concurrent
    writer, reserve_credit must raise 402 — never return a pool_active=False
    context that let the request through unpriced.
    """
    seed = seed_tenant_with_pool
    user = _user(seed)

    real_client = _pipeline._low_level_client()

    class _AlwaysCancels:
        def transact_write_items(self, **kwargs):
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [
                        {"Code": "ConditionalCheckFailed"},
                        {"Code": "None"},
                    ],
                },
                "TransactWriteItems",
            )

    monkeypatch.setattr(_pipeline, "_low_level_client", lambda: _AlwaysCancels())
    # Keep the sleep instant so the test is fast.
    monkeypatch.setattr(_pipeline.time, "sleep", lambda *_: None)

    with pytest.raises(HTTPException) as exc:
        reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    assert exc.value.status_code == 402
    assert exc.value.detail["reason"] == "tenant_pool_exhausted"

    # Nothing was committed on either side.
    assert _pool(seed)["pool_reserved_microusd"] == 0
    # Restore for other assertions in the process.
    _pipeline._LOW_LEVEL_CLIENT = real_client


def test_throttle_exhaustion_surfaces_503(seed_tenant_with_pool, monkeypatch):
    """When the cancellations are throttles (transient capacity), exhausting
    retries surfaces a retryable 503 rather than a misleading 402.
    """
    seed = seed_tenant_with_pool
    user = _user(seed)

    class _AlwaysThrottles:
        def transact_write_items(self, **kwargs):
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [
                        {"Code": "ThrottlingError"},
                        {"Code": "None"},
                    ],
                },
                "TransactWriteItems",
            )

    monkeypatch.setattr(_pipeline, "_low_level_client", lambda: _AlwaysThrottles())
    monkeypatch.setattr(_pipeline.time, "sleep", lambda *_: None)

    with pytest.raises(HTTPException) as exc:
        reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    assert exc.value.status_code == 503
    assert exc.value.detail["reason"] == "pool_reservation_contended"


# ---------------------------------------------------------------------------
# P0-1 (variant): a genuinely deleted pool row DOES fall back to per-user-only
# ---------------------------------------------------------------------------
def test_deleted_pool_row_falls_back_to_per_user(seed_tenant_with_pool):
    """The only legitimate fall-through: the pool row was removed mid-flight.
    Then per-user-only budgeting is correct (tenant is unlimited at pool level).
    """
    seed = seed_tenant_with_pool
    user = _user(seed)

    # Delete the pool row entirely.
    TenantBudgetsRepository()._table.delete_item(
        Key={"tenant_id": seed["tenant_id"], "sk": budget_sk(seed["period"])}
    )

    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    assert ctx.pool_active is False
    assert _pool(seed) is None


# ---------------------------------------------------------------------------
# P0-3: error paths release the pool hold (no reserved leak)
# ---------------------------------------------------------------------------
def test_release_pool_frees_reserved(seed_tenant_with_pool):
    """release_pool() must hand the outstanding reservation back to the pool so
    an error before settle does not leak pool_reserved forever.
    """
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=2_000_000)
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000

    release_pool(ctx)

    summary = _pool(seed)
    assert summary["pool_reserved_microusd"] == 0
    assert summary["pool_settled_microusd"] == 0  # nothing was actually spent
    assert summary["remaining_microusd"] == seed["pool_limit_microusd"]


def test_release_pool_is_idempotent(seed_tenant_with_pool):
    """Calling release_pool twice (e.g. handler + finally) must not drive
    pool_reserved negative.
    """
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=2_000_000)
    release_pool(ctx)
    release_pool(ctx)
    assert _pool(seed)["pool_reserved_microusd"] == 0


def test_release_pool_noop_on_unpooled_repo(seed_active_tenant):
    """release_pool() on a bare repository (no pool) must be a safe no-op — the
    route handlers call it unconditionally on error paths.
    """
    from dynamo.user_tenants import UserTenantsRepository

    # Must not raise.
    release_pool(UserTenantsRepository())


# ---------------------------------------------------------------------------
# P1-1: settle is idempotent (double-settle does not double-subtract)
# ---------------------------------------------------------------------------
def test_double_settle_does_not_go_negative(seed_tenant_with_pool):
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)

    common = dict(
        user=user,
        tenants_repo=ctx,
        reservation=4000,
        actual_input_tokens=100,
        actual_output_tokens=400,
        model_id="us.anthropic.claude-opus-4-7",
        context=ctx,
        actual_cost_microusd=500_000,
    )
    settle_reservation_and_log(**common)
    # A defensive second settle (streaming finally after an explicit settle).
    settle_reservation_and_log(**common)

    summary = _pool(seed)
    assert summary["pool_reserved_microusd"] == 0  # not -2_000_000
    assert summary["pool_settled_microusd"] == 500_000  # not 1_000_000


# ---------------------------------------------------------------------------
# P1-3: cache tokens are billed to the pool
# ---------------------------------------------------------------------------
def test_cache_tokens_are_billed_on_settle(seed_tenant_with_pool):
    """A settle that reports cache read/write tokens must charge the pool for
    them (auto-derived cost), not settle at the input/output cost alone.
    """
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)

    # opus cache_write is 6_250_000 micro-USD/MTok; 1_000_000 cache-write tokens
    # => exactly $6.25. With input/output=0 the settled cost must reflect cache.
    settle_reservation_and_log(
        user=user,
        tenants_repo=ctx,
        reservation=4000,
        actual_input_tokens=0,
        actual_output_tokens=0,
        model_id="us.anthropic.claude-opus-4-7",
        context=ctx,
        actual_cache_read_tokens=0,
        actual_cache_write_tokens=1_000_000,
    )
    # Settled spend is non-zero and equals the cache-write cost.
    assert _pool(seed)["pool_settled_microusd"] == 6_250_000


# ---------------------------------------------------------------------------
# misc: UsageLogs persists the settled cost (auditability)
# ---------------------------------------------------------------------------
def test_usage_log_records_cost_microusd(seed_tenant_with_pool):
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)
    settle_reservation_and_log(
        user=user,
        tenants_repo=ctx,
        reservation=4000,
        actual_input_tokens=1000,
        actual_output_tokens=2000,
        model_id="us.anthropic.claude-opus-4-7",
        context=ctx,
        actual_cost_microusd=1_234_567,
    )
    from dynamo import UsageLogsRepository

    resp = UsageLogsRepository()._table.query(
        KeyConditionExpression="tenant_id = :t",
        ExpressionAttributeValues={":t": seed["tenant_id"]},
    )
    items = resp.get("Items", [])
    assert items, "a usage log row must exist"
    assert any(int(it.get("cost_microusd", -1)) == 1_234_567 for it in items)


# ---------------------------------------------------------------------------
# Unit-crossing: admin API (USD cents) -> DDB (micro-USD) -> reserve ceiling
# ---------------------------------------------------------------------------
def test_dollar_cents_cross_the_stack_to_the_reserve_ceiling(monkeypatch, dynamodb_mock):
    """The most bug-prone boundary is the cents<->micro-USD conversion that
    crosses SPA -> admin API -> DDB -> reserve. Prove it lines up end-to-end:
    set a $5 pool through the *real* admin endpoint, then reserve exactly $5 of
    cost through the *real* pipeline and confirm the next micro-USD is rejected.
    A x10_000 conversion slip anywhere would make the ceiling land at the wrong
    place and fail this test.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from dynamo.tenants import TenantsRepository
    from dynamo.user_tenants import UserTenantsRepository
    from mvp import authz
    from mvp.admin_tenants import router
    from mvp.deps import AuthenticatedUser, get_current_user

    tenant_id = "cross-org"
    period = current_period()

    TenantsRepository().create(
        tenant_id=tenant_id,
        name="Cross Org",
        team_lead_user_id="admin-owned",
        default_credit=100_000,
        created_by="admin-1",
    )
    # Generous personal balance so the pool ceiling is the binding constraint.
    UserTenantsRepository().ensure(
        user_id="member-1", tenant_id=tenant_id, total_credit=1_000_000_000
    )

    monkeypatch.setattr(
        authz, "user_has_permission",
        lambda user, scope: scope in {"tenants:update", "tenants:read-all"},
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="admin-1", email="a@x", org_id=tenant_id, roles=["admin"],
        raw_claims={}, auth_kind="cognito",
    )
    client = TestClient(app)

    # $5.00 entered in the UI => 500 cents... the SPA sends whole cents:
    # $5.00 -> 500_00 cents.
    resp = client.put(
        f"/api/mvp/admin/tenants/{tenant_id}/pool-budget",
        json={"limit_usd_cents": 50000, "period": period},  # $500.00
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pool_limit_microusd"] == 500_000_000  # $500 in micro-USD

    member = _User(user_id="member-1", org_id=tenant_id)

    # Reserve exactly $500.00 of cost: must succeed and land the pool at 0
    # remaining.
    ctx = reserve_credit(member, 1000, pricing_key="opus", cost_microusd=500_000_000)
    assert ctx.pool_active is True
    summary = TenantBudgetsRepository().pool_summary(tenant_id, period)
    assert summary["remaining_microusd"] == 0

    # The very next micro-USD must be refused on the pool.
    with pytest.raises(HTTPException) as exc:
        reserve_credit(member, 1000, pricing_key="opus", cost_microusd=1)
    assert exc.value.detail["reason"] == "tenant_pool_exhausted"


# ---------------------------------------------------------------------------
# P1-2b: idempotency tokens must be UNIQUE per logical write
# ---------------------------------------------------------------------------
# Background: the first hardening pass derived the ClientRequestToken from the
# snapshot values (uuid5 of reserved/settled/amount). Under real concurrency
# every contender reads the same snapshot -> same token, but each transaction
# carries a distinct updated_at, so real DynamoDB rejects the collision with
# IdempotentParameterMismatchException. moto cannot reproduce that (no
# item-level transaction semantics), so these tests pin the *token contract*
# the live failure proved matters, rather than the DynamoDB error itself.


class _TokenSpyClient:
    """Wraps the real low-level client and records every ClientRequestToken."""

    def __init__(self, inner):
        self._inner = inner
        self.tokens: list = []

    def transact_write_items(self, **kwargs):
        self.tokens.append(kwargs.get("ClientRequestToken"))
        return self._inner.transact_write_items(**kwargs)


def _install_token_spy(monkeypatch):
    spy = _TokenSpyClient(_pipeline._low_level_client())
    monkeypatch.setattr(_pipeline, "_low_level_client", lambda: spy)
    return spy


def test_reserve_tokens_are_present_and_unique(seed_tenant_with_pool, monkeypatch):
    """Every reserve commit must carry a ClientRequestToken, and distinct
    reservations must use distinct tokens. A snapshot-derived token would repeat
    the SAME value for two reservations made from the same starting snapshot.
    """
    seed = seed_tenant_with_pool
    user = _user(seed)
    spy = _install_token_spy(monkeypatch)

    # Two sequential reserves from state that (deliberately) looks identical to
    # a naive snapshot hash on the pool side: both are $0-cost so they read the
    # same pool snapshot except for the per-user counter.
    reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)

    assert all(t for t in spy.tokens), "every reserve must carry a token"
    assert len(spy.tokens) == len(set(spy.tokens)), (
        f"reserve tokens must be unique per commit, got {spy.tokens}"
    )
    # And each token must be a valid UUID (DynamoDB requires <=36 chars).
    for t in spy.tokens:
        uuid.UUID(t)  # raises if malformed


def test_two_reservations_from_identical_snapshot_differ(seed_tenant_with_pool, monkeypatch):
    """The exact live failure shape: two callers observing the identical pool
    snapshot (reserved=0, settled=0) must still emit different tokens. The old
    snapshot-derived token produced a collision here.
    """
    seed = seed_tenant_with_pool
    period = seed["period"]

    # Capture the token the reserve path would generate for a fixed snapshot by
    # exercising _fresh_idempotency_token directly through two reserves that
    # both start from (reserved=0, settled=0): reset the pool between them.
    def _reset_pool():
        TenantBudgetsRepository()._table.update_item(
            Key={"tenant_id": seed["tenant_id"], "sk": budget_sk(period)},
            UpdateExpression="SET pool_reserved_microusd = :z, pool_settled_microusd = :z",
            ExpressionAttributeValues={":z": 0},
        )

    user = _user(seed)
    spy = _install_token_spy(monkeypatch)

    _reset_pool()
    reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    first = list(spy.tokens)

    _reset_pool()
    reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    second = spy.tokens[len(first):]

    assert first and second
    assert set(first).isdisjoint(set(second)), (
        "reservations from an identical pool snapshot must not share a token "
        f"(first={first}, second={second})"
    )


def test_settle_reuses_one_token_across_its_retries(seed_tenant_with_pool, monkeypatch):
    """Settle must generate its token ONCE and reuse it across its own retries,
    so a lost-ack retry (identical params, no updated_at) dedupes to success.
    A fresh-per-attempt token on the settle path would instead risk a double
    apply. We force one transient failure and confirm the token is stable.
    """
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)

    real = _pipeline._low_level_client()
    calls = {"n": 0}
    seen_tokens: list = []

    class _FlakyOnce:
        def transact_write_items(self, **kwargs):
            seen_tokens.append(kwargs.get("ClientRequestToken"))
            calls["n"] += 1
            if calls["n"] == 1:
                raise ClientError(
                    {"Error": {"Code": "ThrottlingError"}}, "TransactWriteItems"
                )
            return real.transact_write_items(**kwargs)

    monkeypatch.setattr(_pipeline, "_low_level_client", lambda: _FlakyOnce())
    monkeypatch.setattr(_pipeline.time, "sleep", lambda *_: None)

    settle_reservation_and_log(
        user=user,
        tenants_repo=ctx,
        reservation=4000,
        actual_input_tokens=100,
        actual_output_tokens=400,
        model_id="us.anthropic.claude-opus-4-7",
        context=ctx,
        actual_cost_microusd=500_000,
    )

    assert calls["n"] == 2, "settle should have retried exactly once"
    assert len(seen_tokens) == 2
    assert seen_tokens[0] == seen_tokens[1], (
        "settle must reuse ONE token across its retries so the retry dedupes"
    )
    # The settlement actually landed.
    assert _pool(seed)["pool_settled_microusd"] == 500_000
    assert _pool(seed)["pool_reserved_microusd"] == 0


def test_fresh_idempotency_token_is_unique_uuid():
    """The token factory itself must return distinct valid UUIDs each call."""
    tokens = {_pipeline._fresh_idempotency_token() for _ in range(1000)}
    assert len(tokens) == 1000, "tokens must be unique"
    for t in tokens:
        uuid.UUID(t)  # valid UUID, <=36 chars


# ---------------------------------------------------------------------------
# misc: pricing cache falls back to defaults when the CURRENT pointer vanishes
# ---------------------------------------------------------------------------
def test_pricing_reverts_to_defaults_when_current_pointer_removed(dynamodb_mock):
    from dynamo.pricing_config import PricingConfigRepository
    from mvp import pricing
    from mvp.pricing import Rate, reset_cache

    reset_cache()
    repo = PricingConfigRepository()
    # Install an override that halves the opus input rate, then point CURRENT.
    repo.set_rates(
        version="v-test",
        rates={"opus": Rate(1, 2, 3, 4)},
    )
    # Force a reload and confirm the override took.
    pricing._cache._loaded_at = 0.0
    assert pricing._cache.get("opus", repo).input_per_mtok_microusd == 1

    # Remove the CURRENT pointer (overrides "deleted").
    repo._table.delete_item(Key={"pk": "CONFIG#pricing", "sk": "CURRENT"})
    pricing._cache._loaded_at = 0.0  # expire TTL

    # The cache must revert to built-in defaults, not keep the stale override.
    assert pricing._cache.get("opus", repo).input_per_mtok_microusd == 5_000_000
    reset_cache()
