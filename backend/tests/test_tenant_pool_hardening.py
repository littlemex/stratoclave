"""Regression tests for the pool-budget hardening pass.

Each test here pins a *fail-open* or accounting hole found while hardening the
first cut of tenant dollar pools. They are written so they would FAIL against
the pre-hardening behaviour:

  - retry exhaustion under contention must fail **closed** (never slip a
    request through per-user-only while the pool is still present);
  - a suspended pool must reject immediately, not fall through;
  - an error path must release the pool hold (no reserved leak);
  - settle must be idempotent (a double-settle must not drive the pool
    negative);
  - cache tokens must be billed to the pool, not settled at zero;
  - idempotency tokens must be UNIQUE per logical write (a token derived
    from shared snapshot state collides across concurrent callers, which
    real DynamoDB rejects with IdempotentParameterMismatchException —
    found only by the live-DynamoDB harness, invisible under moto);
  - UsageLogs must persist the settled cost so the pool is auditable, and
    the pricing cache must fall back to defaults when overrides vanish.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from dynamo.tenant_budgets import (
    TenantBudgetsRepository,
    budget_sk,
    current_period,
    hold_sk,
    hold_sk_prefix,
    previous_period,
)
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


def _holds(seed, period=None):
    """Return the raw HOLD rows currently stored for the seed's tenant/period."""
    from boto3.dynamodb.conditions import Key

    repo = TenantBudgetsRepository()
    resp = repo._table.query(
        KeyConditionExpression=(
            Key("tenant_id").eq(seed["tenant_id"])
            & Key("sk").begins_with(hold_sk_prefix(period or seed["period"]))
        )
    )
    return resp.get("Items", [])


def _seed_expired_hold(seed, amount, *, period=None, hold_id=None, expires_at=1):
    """Write crash residue: an already-expired HOLD plus its uncollected share of
    the aggregate `pool_reserved_microusd` — exactly the state a task killed
    between reserve and settle leaves behind. Because expiry is embedded in the
    SK, an orphan is *created* expired (there is no separate "age it" step).
    Returns the full hold SK.
    """
    import uuid as _uuid

    period = period or seed["period"]
    hid = hold_id or f"orphan-{_uuid.uuid4()}"
    repo = TenantBudgetsRepository()
    sk = hold_sk(period, expires_at, hid)
    # 1) inflate the aggregate as the dead reserve had (create row if absent).
    repo._table.update_item(
        Key={"tenant_id": seed["tenant_id"], "sk": budget_sk(period)},
        UpdateExpression="ADD pool_reserved_microusd :a",
        ExpressionAttributeValues={":a": amount},
    )
    # 2) write the sibling HOLD, already expired.
    repo._table.put_item(
        Item={
            "tenant_id": seed["tenant_id"],
            "sk": sk,
            "hold_id": hid,
            "period": period,
            "amount_microusd": amount,
            "expires_at": expires_at,
            "created_at": "seed",
        }
    )
    return sk


# ---------------------------------------------------------------------------
# single-item refund/reserve must retry TransactionConflictException
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
# suspended pool must fail closed
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
# retry exhaustion under contention must fail closed
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


def test_contention_backoff_is_jittered_and_capped():
    """Full-jitter exponential backoff for the hot-row reserve retries.

    Regression: linear backoff (delay = base * attempt) synchronised a
    concurrent burst on one tenant's pool row so it fund-exhausted its retries
    and failed closed (503) under a 20-way live load. The jittered version must
    (a) grow with the attempt, (b) stay within [0, cap], and (c) actually vary
    run-to-run so colliding writers desynchronise.
    """
    cap = _pipeline._RESERVE_BACKOFF_CAP_SECONDS
    # Every sample is within [0, cap] for a range of attempts.
    for attempt in range(1, 12):
        for _ in range(20):
            d = _pipeline._contention_backoff(attempt)
            assert 0.0 <= d <= cap

    # High attempts saturate at the cap ceiling (full-jitter → mean ≈ cap/2),
    # and are not a single fixed value (jitter present).
    highs = [_pipeline._contention_backoff(10) for _ in range(50)]
    assert len(set(highs)) > 1  # jittered, not deterministic
    assert max(highs) <= cap

    # Early attempts have a strictly smaller ceiling than the cap, so the
    # window widens with the attempt rather than being flat.
    early_ceiling = min(cap, _pipeline._RESERVE_BACKOFF_SECONDS * (2 ** 1))
    assert early_ceiling < cap
    assert all(_pipeline._contention_backoff(1) <= early_ceiling for _ in range(50))


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
# variant: a genuinely deleted pool row DOES fall back to per-user-only
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
# error paths release the pool hold (no reserved leak)
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
# settle is idempotent (double-settle does not double-subtract)
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
# cache tokens are billed to the pool
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
# idempotency tokens must be UNIQUE per logical write
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


# ---------------------------------------------------------------------------
# orphan-reservation reaper
# ---------------------------------------------------------------------------
# release_pool() only runs on *handled* error paths. A task kill / OOM / deploy
# drain can end the process between reserve and settle with neither running,
# leaking that request's share of pool_reserved_microusd forever (no server-side
# timer). Every pooled reservation therefore writes a sibling HOLD row in the
# same transaction as the reserve; settle/release delete it; and each pooled
# reserve lazily reclaims a bounded number of *expired* holds. These tests pin:
#   - a hold is written on reserve and removed on settle/release;
#   - an expired orphan is reclaimed back into the aggregate by the next reserve;
#   - reclaim is idempotent (never double-subtracts, never goes negative);
#   - a not-yet-expired hold (a live request) is left untouched;
#   - the sweep is bounded per call.


def test_reserve_writes_a_hold(seed_tenant_with_pool):
    """A pooled reservation must persist a HOLD row carrying its amount, so a
    crash before settle leaves a reclaimable record rather than an invisible
    slice of pool_reserved."""
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=2_000_000)

    holds = _holds(seed)
    assert len(holds) == 1
    assert int(holds[0]["amount_microusd"]) == 2_000_000
    assert holds[0]["hold_id"] == ctx.hold_id
    assert "expires_at" in holds[0]
    # The hold's amount matches the outstanding aggregate reservation exactly.
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000


def test_settle_deletes_the_hold(seed_tenant_with_pool):
    """Settling a reservation must remove its hold in the same breath as it
    records spend, so the reaper never reclaims a request that completed."""
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)
    assert len(_holds(seed)) == 1

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
    assert _holds(seed) == []
    assert _pool(seed)["pool_reserved_microusd"] == 0
    assert _pool(seed)["pool_settled_microusd"] == 500_000


def test_release_deletes_the_hold(seed_tenant_with_pool):
    """release_pool() (handled error path) must delete the hold alongside
    handing the reserved amount back."""
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=2_000_000)
    assert len(_holds(seed)) == 1

    release_pool(ctx)
    assert _holds(seed) == []
    assert _pool(seed)["pool_reserved_microusd"] == 0


def test_sweep_reclaims_an_orphaned_hold(seed_tenant_with_pool):
    """The core self-heal: a hold whose owning process died (never settled,
    never released) and whose TTL has passed must be reclaimed by the next
    pooled reserve — its amount returned to the pool.
    """
    seed = seed_tenant_with_pool
    user = _user(seed)

    # Crash residue: an expired $3.00 hold that was never settled/released.
    _seed_expired_hold(seed, 3_000_000)
    assert _pool(seed)["pool_reserved_microusd"] == 3_000_000

    # A fresh, unrelated pooled reserve drives the sweep. It reclaims the orphan
    # (−3_000_000) and adds its own 1_000_000, netting 1_000_000 outstanding.
    reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)

    holds = _holds(seed)
    # Only the new reservation's hold remains; the orphan was deleted.
    assert len(holds) == 1
    assert int(holds[0]["amount_microusd"]) == 1_000_000
    assert _pool(seed)["pool_reserved_microusd"] == 1_000_000


def test_sweep_is_idempotent_no_double_subtract(seed_tenant_with_pool):
    """Two overlapping sweeps of the same expired hold must reclaim it exactly
    once — the conditional Delete makes the aggregate decrement happen a single
    time, never driving pool_reserved negative.
    """
    seed = seed_tenant_with_pool
    _seed_expired_hold(seed, 2_000_000)
    repo = TenantBudgetsRepository()

    n1 = _pipeline._sweep_expired_holds(repo, seed["tenant_id"], seed["period"])
    n2 = _pipeline._sweep_expired_holds(repo, seed["tenant_id"], seed["period"])

    assert n1 == 1, "first sweep reclaims the orphan"
    assert n2 == 0, "second sweep finds nothing left"
    summary = _pool(seed)
    assert summary["pool_reserved_microusd"] == 0  # exactly once, not -2_000_000
    assert summary["remaining_microusd"] == seed["pool_limit_microusd"]
    assert _holds(seed) == []


def test_sweep_leaves_unexpired_holds_untouched(seed_tenant_with_pool):
    """A live request's hold (TTL not yet passed) must NOT be reclaimed —
    reclaiming an in-flight reservation would double-count when it settles.
    """
    seed = seed_tenant_with_pool
    user = _user(seed)
    reserve_credit(user, 1000, pricing_key="opus", cost_microusd=2_000_000)
    # Hold's expires_at is ~now + 1h (the default TTL), i.e. firmly in the future.

    repo = TenantBudgetsRepository()
    reclaimed = _pipeline._sweep_expired_holds(repo, seed["tenant_id"], seed["period"])

    assert reclaimed == 0
    assert len(_holds(seed)) == 1
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000


def test_sweep_is_bounded_per_call(seed_tenant_with_pool, monkeypatch):
    """The sweep must reclaim at most _SWEEP_MAX_HOLDS holds per call so it can
    never turn the hot reserve path into an unbounded scan; the remainder are
    left for subsequent requests.
    """
    seed = seed_tenant_with_pool
    monkeypatch.setattr(_pipeline, "_SWEEP_MAX_HOLDS", 3)

    # Seed 7 expired orphan holds (distinct expiries so SK order is stable).
    repo = TenantBudgetsRepository()
    for i in range(7):
        _seed_expired_hold(seed, 100_000, hold_id=f"bnd-{i}", expires_at=100 + i)
    assert len(_holds(seed)) == 7

    reclaimed = _pipeline._sweep_expired_holds(repo, seed["tenant_id"], seed["period"])
    assert reclaimed == 3, "sweep is capped at _SWEEP_MAX_HOLDS per call"
    assert len(_holds(seed)) == 4, "the rest wait for the next request"


def test_sweep_survives_query_failure(seed_tenant_with_pool, monkeypatch):
    """A failing reaper must never fail the live request driving it: a query
    error is swallowed (logged) and the reserve proceeds."""
    seed = seed_tenant_with_pool
    user = _user(seed)

    def boom(*a, **k):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query"
        )

    monkeypatch.setattr(TenantBudgetsRepository, "query_expired_holds", boom)

    # Must not raise despite the sweep blowing up.
    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    assert ctx.pool_active is True
    assert _pool(seed)["pool_reserved_microusd"] == 1_000_000


# ===========================================================================
# Second-pass review fixes (a hardening review of the reaper)
# ===========================================================================
# Each test below pins one finding from the review of the reaper and would FAIL
# against the pre-fix behaviour.


# --- reaper-then-settle must not double-subtract reserved ------------------
def test_settle_after_reaper_reclaim_does_not_double_subtract(seed_tenant_with_pool):
    """THE money bug. A request reserves, its hold outlives the TTL, the reaper
    reclaims it (returning reserved), THEN the original request finally settles.
    The settle must record spend WITHOUT subtracting reserved again — otherwise
    pool_reserved goes permanently negative and the tenant can exceed the pool
    forever. Pre-fix (unconditional hold delete + unconditional reserved ADD)
    this drove reserved to -cost.
    """
    seed = seed_tenant_with_pool
    user = _user(seed)
    # Reserve $2.00 for real (writes the hold + inflates reserved to 2_000_000).
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000

    # The reaper reclaims this very hold (simulate the TTL passing under it):
    # delete the hold + return its reserved share, exactly as _sweep does.
    budgets = TenantBudgetsRepository()
    _pipeline._low_level_client().transact_write_items(
        TransactItems=[
            _pipeline._pool_settle_items(
                table_name=budgets.table_name, tenant_id=seed["tenant_id"],
                period=seed["period"], reserved_microusd=2_000_000,
                actual_microusd=0, reclaimed_microusd=2_000_000,
            ),
            budgets.reclaim_hold_txn_item(tenant_id=seed["tenant_id"], sk=ctx.hold_sk),
        ],
        ClientRequestToken=_pipeline._fresh_idempotency_token(),
    )
    assert _pool(seed)["pool_reserved_microusd"] == 0  # reaper returned it
    assert _holds(seed) == []

    # Now the original request settles $0.50 of actual spend.
    settle_reservation_and_log(
        user=user, tenants_repo=ctx, reservation=4000,
        actual_input_tokens=100, actual_output_tokens=400,
        model_id="us.anthropic.claude-opus-4-7", context=ctx,
        actual_cost_microusd=500_000,
    )

    summary = _pool(seed)
    # reserved must NOT go negative; settled records the real spend once.
    assert summary["pool_reserved_microusd"] == 0, "must not re-subtract reserved"
    assert summary["pool_settled_microusd"] == 500_000
    assert summary["remaining_microusd"] == seed["pool_limit_microusd"] - 500_000


# --- Limit is applied before the (removed) filter; orphan must not hide -----
def test_orphan_not_buried_behind_live_holds(seed_tenant_with_pool, monkeypatch):
    """With the old begins_with + expires_at FILTER, DynamoDB's Limit cut the
    page across live holds (arbitrary uuid SK order), so an expired orphan behind
    Limit live holds was never returned → permanent leak. Ranging by embedded
    expiry makes the query return the orphan regardless of how many live holds
    exist. Simulate: many live (future-expiry) holds + one expired orphan, with a
    tiny sweep cap; the orphan must still be reclaimed.
    """
    seed = seed_tenant_with_pool
    monkeypatch.setattr(_pipeline, "_SWEEP_MAX_HOLDS", 2)

    # 20 live holds (far-future expiry) — these must never be swept.
    for i in range(20):
        _seed_expired_hold(seed, 10_000, hold_id=f"live-{i}", expires_at=9_999_999_999)
    # One genuinely expired orphan.
    _seed_expired_hold(seed, 700_000, hold_id="the-orphan", expires_at=5)

    repo = TenantBudgetsRepository()
    reclaimed = _pipeline._sweep_one_period(
        repo, seed["tenant_id"], seed["period"], _pipeline._SWEEP_MAX_HOLDS
    )
    assert reclaimed == 1, "the expired orphan must be found despite 20 live holds"
    # The orphan is gone; all 20 live holds remain.
    remaining_ids = {h["hold_id"] for h in _holds(seed)}
    assert "the-orphan" not in remaining_ids
    assert len([i for i in remaining_ids if i.startswith("live-")]) == 20


# --- previous period is swept too ------------------------------------------
def test_sweep_reclaims_previous_period_orphan(seed_tenant_with_pool):
    """A hold orphaned in the final moments of a month must still be reclaimed
    after the month rolls over — the sweep looks at the previous period too.
    """
    seed = seed_tenant_with_pool
    prev = previous_period(seed["period"])
    # Seed a pool row + expired orphan under the PREVIOUS period.
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=seed["tenant_id"], period=prev, pool_limit_microusd=5_000_000
    )
    _seed_expired_hold(seed, 1_200_000, period=prev, expires_at=5)
    assert _holds(seed, period=prev)

    repo = TenantBudgetsRepository()
    # Sweeping the CURRENT period must also drain the previous period's orphan.
    _pipeline._sweep_expired_holds(repo, seed["tenant_id"], seed["period"])

    assert _holds(seed, period=prev) == []
    prev_summary = TenantBudgetsRepository().pool_summary(seed["tenant_id"], prev)
    assert prev_summary["pool_reserved_microusd"] == 0


# --- settle after the pool row vanished must not create a ghost row --------
def test_settle_after_pool_row_deleted_is_noop(seed_tenant_with_pool):
    """If the pool row is deleted mid-flight, an in-flight settle must NOT
    recreate it as a ghost row carrying negative reserved (a later set_pool_limit
    would preserve that and inflate the next period's budget).
    """
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)

    # Delete the whole pool row (admin removed the budget mid-request).
    TenantBudgetsRepository()._table.delete_item(
        Key={"tenant_id": seed["tenant_id"], "sk": budget_sk(seed["period"])}
    )

    # Settle must be a no-op on the (absent) pool row, not resurrect it.
    settle_reservation_and_log(
        user=user, tenants_repo=ctx, reservation=4000,
        actual_input_tokens=100, actual_output_tokens=400,
        model_id="us.anthropic.claude-opus-4-7", context=ctx,
        actual_cost_microusd=500_000,
    )
    # No pool row exists → pool_summary is None (not a ghost with negative reserved).
    assert _pool(seed) is None


# --- the hold TTL has a hard floor -----------------------------------------
def test_hold_ttl_has_a_floor(monkeypatch):
    """A mis-set env var (e.g. a throwaway "60") must not shrink the TTL below
    the floor, or every in-flight hold would be falsely reaped."""
    import importlib

    monkeypatch.setenv("STRATOCLAVE_POOL_HOLD_TTL_SECONDS", "60")
    reloaded = importlib.reload(_pipeline)
    try:
        assert reloaded._HOLD_TTL_SECONDS >= reloaded._HOLD_TTL_FLOOR_SECONDS
        assert reloaded._HOLD_TTL_SECONDS == reloaded._HOLD_TTL_FLOOR_SECONDS
    finally:
        # Restore the module to the default env for the rest of the suite.
        monkeypatch.delenv("STRATOCLAVE_POOL_HOLD_TTL_SECONDS", raising=False)
        importlib.reload(_pipeline)


# --- amount<=0 holds are cleaned, not skipped forever ----------------------
def test_zero_amount_hold_is_deleted_not_skipped(seed_tenant_with_pool):
    """A zero-amount hold ties up no budget but must be deleted so it stops
    being scanned every sweep (pre-fix it was `continue`d and left forever)."""
    seed = seed_tenant_with_pool
    # Seed a zero-amount expired hold WITHOUT inflating reserved.
    repo = TenantBudgetsRepository()
    sk = hold_sk(seed["period"], 5, "zero-amt")
    repo._table.put_item(
        Item={
            "tenant_id": seed["tenant_id"], "sk": sk, "hold_id": "zero-amt",
            "period": seed["period"], "amount_microusd": 0, "expires_at": 5,
            "created_at": "seed",
        }
    )
    assert len(_holds(seed)) == 1

    _pipeline._sweep_expired_holds(repo, seed["tenant_id"], seed["period"])
    assert _holds(seed) == [], "zero-amount hold must be deleted, not skipped"


# ---------------------------------------------------------------------------
# F1 regression (final Fable review): every ClientRequestToken must fit
# DynamoDB's 36-char limit. The settled-only fallback derives its token from
# the primary; a naive f"{token}-so" would be 39 chars and ValidationException
# on every reaper-race settle (silent revenue leak).
# ---------------------------------------------------------------------------
def test_all_idempotency_tokens_within_dynamodb_limit():
    from mvp import _pipeline as p

    for _ in range(1000):
        primary = p._fresh_idempotency_token()
        assert len(primary) <= 36, f"primary token too long: {len(primary)}"
        derived = p._derived_token(primary, "settled-only")
        assert len(derived) <= 36, f"derived token too long: {len(derived)}"
        # deterministic per (primary, tag) so a lost-ack retry dedupes
        assert derived == p._derived_token(primary, "settled-only")
        # distinct from the primary and from a different tag
        assert derived != primary
        assert derived != p._derived_token(primary, "other")
