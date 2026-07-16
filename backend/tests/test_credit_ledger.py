"""Credit ledger Phase 1: SETTLE events co-located in the settle transaction.

Verifies the invariants that matter for a money source of truth (Fable design):
  - a settle writes exactly one SETTLE event with the right signed deltas and
    frozen pricing attribution;
  - the ledger's derived settled total equals the budget counter (I1);
  - the terminal event is idempotent — a re-settle of the same hold does not
    double-write and does not double-count (I3, terminal exclusivity);
  - a bare reserve (no settle) leaves the ledger empty (no premature spend).
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from dynamo.tenant_budgets import budget_sk as _budget_sk
from mvp._pipeline import reserve_credit, release_pool, settle_reservation_and_log


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"
    roles: tuple = ("user",)


def _ledger():
    from dynamo import CreditLedgerRepository

    return CreditLedgerRepository()


def _pool(seed):
    from dynamo.tenant_budgets import TenantBudgetsRepository

    return TenantBudgetsRepository().pool_summary(seed["tenant_id"], seed["period"])


@pytest.fixture
def _stub_usage(monkeypatch):
    # settle_reservation_and_log also writes UsageLogs; stub it so these tests
    # isolate the ledger/pool behaviour.
    import mvp._pipeline as pipeline

    monkeypatch.setattr(pipeline, "_write_usage_log", lambda *a, **k: None, raising=False)
    yield


def _settle(user, ctx, *, model_id, tok_in, tok_out):
    settle_reservation_and_log(
        user=user,
        tenants_repo=ctx,
        reservation=ctx.reservation_tokens,
        actual_input_tokens=tok_in,
        actual_output_tokens=tok_out,
        model_id=model_id,
        context=ctx,
    )


def test_settle_writes_one_settle_event_with_signed_deltas(seed_tenant_with_pool, _stub_usage):
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000

    _settle(user, ctx, model_id="us.anthropic.claude-opus-4-7", tok_in=1000, tok_out=500)

    events = _ledger().events_for_run(tenant_id=seed["tenant_id"], run_id=ctx.hold_id)
    settle_events = [e for e in events if e["event_type"] == "SETTLE"]
    assert len(settle_events) == 1
    ev = settle_events[0]
    # reserved is returned (negative), actual spend recorded (non-negative).
    assert int(ev["reserved_delta_microusd"]) == -2_000_000
    assert int(ev["settled_delta_microusd"]) >= 0
    assert ev["hold_id"] == ctx.hold_id
    assert ev["model_id"] == "us.anthropic.claude-opus-4-7"
    assert ev["settle_reason"] == "completion"


def test_ledger_settled_total_matches_counter(seed_tenant_with_pool, _stub_usage):
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    # Two independent reservations + settles in the same period.
    for cost in (1_000_000, 1_500_000):
        ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=cost)
        _settle(user, ctx, model_id="us.anthropic.claude-opus-4-7", tok_in=100, tok_out=50)

    derived = _ledger().sum_settled_microusd(
        tenant_id=seed["tenant_id"], period=seed["period"]
    )
    counter = _pool(seed)["pool_settled_microusd"]
    # I1: the ledger's derived settled total equals the materialized counter.
    assert derived == counter


def test_resettle_is_idempotent_no_double_count(seed_tenant_with_pool, _stub_usage):
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)
    _settle(user, ctx, model_id="us.anthropic.claude-opus-4-7", tok_in=1000, tok_out=500)

    settled_after_first = _pool(seed)["pool_settled_microusd"]

    # Force a second settle of the SAME reservation (defensive double-settle):
    # clear the once-guard so the code path actually re-runs the transaction.
    ctx._pool_finalized = False
    _settle(user, ctx, model_id="us.anthropic.claude-opus-4-7", tok_in=1000, tok_out=500)

    # The terminal ledger event's attribute_not_exists makes the re-settle a
    # no-op: exactly one SETTLE event and the counter did not advance again.
    events = _ledger().events_for_run(tenant_id=seed["tenant_id"], run_id=ctx.hold_id)
    assert len([e for e in events if e["event_type"] == "SETTLE"]) == 1
    assert _pool(seed)["pool_settled_microusd"] == settled_after_first


def test_reaper_race_settle_records_ledger_with_zero_reserved_delta(
    seed_tenant_with_pool, _stub_usage
):
    """If the reaper reclaimed the hold before settle (hold_gone path), the
    settled-only follow-up must still record the spend in the ledger — but with
    reserved_delta=0, because the reaper already returned `reserved` (Fable impl
    review Bug 1). The event must mirror the counter move (settled-only)."""
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)

    # Simulate the reaper having reclaimed this hold: delete the HOLD row and
    # return its reserved share to the pool, so settle takes the hold_gone path.
    from dynamo.tenant_budgets import TenantBudgetsRepository

    budgets = TenantBudgetsRepository()
    budgets._table.delete_item(
        Key={"tenant_id": seed["tenant_id"], "sk": ctx.hold_sk}
    )
    budgets._table.update_item(
        Key={"tenant_id": seed["tenant_id"], "sk": _budget_sk(seed["period"])},
        UpdateExpression="ADD pool_reserved_microusd :d",
        ExpressionAttributeValues={":d": -2_000_000},
    )

    _settle(user, ctx, model_id="us.anthropic.claude-opus-4-7", tok_in=1000, tok_out=500)

    events = _ledger().events_for_run(tenant_id=seed["tenant_id"], run_id=ctx.hold_id)
    settle_events = [e for e in events if e["event_type"] == "SETTLE"]
    assert len(settle_events) == 1
    ev = settle_events[0]
    # reserved was already returned by the reaper → this event must NOT claim to
    # release it again.
    assert int(ev["reserved_delta_microusd"]) == 0
    assert int(ev["settled_delta_microusd"]) >= 0
    assert ev["settle_reason"] == "reaper_race"
    # I1 still holds: derived settled == counter.
    assert _ledger().sum_settled_microusd(
        tenant_id=seed["tenant_id"], period=seed["period"]
    ) == _pool(seed)["pool_settled_microusd"]


def test_bare_reserve_then_release_writes_no_settle_event(seed_tenant_with_pool, _stub_usage):
    seed = seed_tenant_with_pool
    user = _User(user_id=seed["user_id"], org_id=seed["tenant_id"])
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)
    hold_id = ctx.hold_id
    # Invoke-time failure path: release, never settle → no SETTLE spend recorded.
    release_pool(ctx)

    events = _ledger().events_for_run(tenant_id=seed["tenant_id"], run_id=hold_id)
    assert [e for e in events if e["event_type"] == "SETTLE"] == []
    # And the derived settled total is still zero.
    assert _ledger().sum_settled_microusd(
        tenant_id=seed["tenant_id"], period=seed["period"]
    ) == 0
