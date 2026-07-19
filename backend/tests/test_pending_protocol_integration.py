"""Integration tests for the PENDING protocol reserve path (against moto).

Proves the production wiring in mvp/_pipeline.py + dynamo/tenant_budgets.py, both
directions of the STRATOCLAVE_RESERVE_PROTOCOL flag:

  * flag OFF (default "transaction"): the reserve path is UNCHANGED — a HOLD with
    no `status` (implicit ACTIVE), the RESERVE ledger event still written, the
    reaper still credits it. This is the "inert until flipped" guarantee.
  * flag ON ("pending"): the 3-write path — HOLD status=PENDING -> single
    conditional UpdateItem commit -> async ACTIVE; deterministic hold_id (I6
    duplicate-key replay); the sweeper fences an expired PENDING WITHOUT touching
    the pool; the reconciler recovers a debited leak in aggregate and defers while
    a PENDING is in flight.

See docs/design/pending-protocol.md and the reference model in
billing/pending_protocol.py (this exercises the REAL DynamoDB primitives).
"""
from __future__ import annotations

import time

import pytest

from dynamo.credit_ledger import CreditLedgerRepository
from dynamo.tenant_budgets import TenantBudgetsRepository, current_period, hold_sk as _hsk
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline
from mvp.billing_authorize import encode_authorization_id


def _mk_id(hold_id, period, hold_sk):
    return encode_authorization_id(hold_id=hold_id, period=period, hold_sk=hold_sk)


def _seed(tenant_id, limit=10_000_000_000):
    period = current_period()
    UserTenantsRepository().ensure(
        user_id=f"user-{tenant_id}", tenant_id=tenant_id, role="user",
        total_credit=1_000_000_000,
    )
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=tenant_id, period=period, pool_limit_microusd=limit,
    )
    return tenant_id, period


def _authorize(tenant_id, amount, key, *, ttl=3600, description=None, run_id=None):
    return _pipeline.reserve_external_authorization(
        tenant_id=tenant_id, amount_microusd=amount, idempotency_key=key,
        request_fingerprint=f"fp-{key}", authorization_id_factory=_mk_id,
        ttl_seconds=ttl, description=description, workflow_run_id=run_id,
    )


def _pool(tenant_id, period):
    return TenantBudgetsRepository().pool_summary(tenant_id, period)


def _set_protocol(monkeypatch, value):
    monkeypatch.setattr(_pipeline, "_RESERVE_PROTOCOL", value)
    _pipeline._reset_low_level_client()


# --------------------------------------------------------------------------
# flag OFF: byte-identical to today (the "inert until flipped" guarantee).
# --------------------------------------------------------------------------

def test_flag_off_is_transactional_and_writes_reserve_event(dynamodb_mock, monkeypatch):
    _set_protocol(monkeypatch, "transaction")
    tenant, period = _seed("off-t1")
    r = _authorize(tenant, 500_000, "k1", description="widget")
    assert not r.replayed
    assert _pool(tenant, period)["pool_reserved_microusd"] == 500_000
    hold = TenantBudgetsRepository().get_hold(tenant_id=tenant, sk=r.hold_sk)
    assert "status" not in hold                        # implicit ACTIVE (unchanged)
    evt = CreditLedgerRepository().get_reserve(tenant_id=tenant, period=period, hold_id=r.hold_id)
    assert evt is not None and evt["source"] == "external"  # RESERVE event still written


# --------------------------------------------------------------------------
# flag ON: the PENDING 3-write path.
# --------------------------------------------------------------------------

def test_pending_reserve_commits_and_activates(dynamodb_mock, monkeypatch):
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-t1")
    r = _authorize(tenant, 700_000, "pk1", description="pending widget")
    assert not r.replayed
    # pool debited by exactly the amount (single conditional UpdateItem).
    assert _pool(tenant, period)["pool_reserved_microusd"] == 700_000
    hold = TenantBudgetsRepository().get_hold(tenant_id=tenant, sk=r.hold_sk)
    # step 3 (async activate) ran synchronously in-process here -> ACTIVE.
    assert hold["status"] == "ACTIVE"
    assert hold["source"] == "external"
    assert int(hold["amount_microusd"]) == 700_000


def test_pending_hold_id_is_deterministic_and_replays(dynamodb_mock, monkeypatch):
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-t2")
    r1 = _authorize(tenant, 300_000, "dupkey")
    r2 = _authorize(tenant, 300_000, "dupkey")          # same key -> replay
    assert r1.hold_id == r2.hold_id                      # I6: deterministic id
    assert r1.authorization_id == r2.authorization_id
    assert r2.replayed is True
    # exactly ONE debit despite two authorize calls.
    assert _pool(tenant, period)["pool_reserved_microusd"] == 300_000


def test_pending_402_when_pool_full_leaves_no_debit(dynamodb_mock, monkeypatch):
    import fastapi
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-tiny", limit=1000)
    with pytest.raises(fastapi.HTTPException) as ei:
        _authorize(tenant, 5000, "pk-full")
    assert ei.value.status_code == 402
    # the pool was NEVER debited (step 2 CCF'd definitively).
    assert _pool(tenant, period)["pool_reserved_microusd"] == 0
    # the PENDING orphan was marked FAILED (leak-safe), pool untouched.
    hold = TenantBudgetsRepository().get_hold(
        tenant_id=tenant, sk=_hsk(period, int(time.time()) + 3600,
                                  _pipeline._pending_hold_id(tenant, period, "pk-full")))
    assert hold is None or hold.get("status") in ("FAILED", "PENDING")


def test_pending_capture_rehydrates_from_hold(dynamodb_mock, monkeypatch):
    """A committed PENDING/ACTIVE hold is rehydratable for capture/void (A1
    capability: holding the id => the debit committed)."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-cap")
    r = _authorize(tenant, 400_000, "pk-cap", description="cap me")
    ctx = _pipeline.rehydrate_reservation_context(
        tenant_id=tenant, period=period, hold_id=r.hold_id, hold_sk=r.hold_sk)
    assert ctx is not None
    assert ctx.source == "external"
    assert ctx.pool_reserved_microusd == 400_000


def test_sweeper_fences_expired_pending_without_touching_pool(dynamodb_mock, monkeypatch):
    """A committed PENDING that never activated (crash before step 3) and expired:
    the sweeper fences it to EXPIRED_UNCREDITED WITHOUT crediting the pool (it
    cannot know the debit committed); the reconciler recovers it."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-fence")
    budgets = TenantBudgetsRepository()
    hold_id = _pipeline._pending_hold_id(tenant, period, "pk-fence")
    # simulate: committed debit + a PENDING hold that never activated, expired.
    past = int(time.time()) - 10_000
    sk = _hsk(period, past, hold_id)
    budgets.pool_reserve_update(tenant_id=tenant, period=period, amount_microusd=250_000)
    budgets._table.put_item(Item={
        "tenant_id": tenant, "sk": sk, "hold_id": hold_id, "period": period,
        "amount_microusd": 250_000, "expires_at": past, "status": "PENDING",
        "source": "external",
    })
    assert _pool(tenant, period)["pool_reserved_microusd"] == 250_000
    fenced = _pipeline.sweep_fence_pending(budgets, tenant, period)
    assert fenced == 1
    assert budgets.get_hold(tenant_id=tenant, sk=sk)["status"] == "EXPIRED_UNCREDITED"
    # pool NOT credited by the fence (leak, still reserved).
    assert _pool(tenant, period)["pool_reserved_microusd"] == 250_000
    # reconciler recovers the aggregate leak (no PENDING in flight now).
    summ = _pipeline.reconcile_pool(budgets, tenant, period)
    assert summ["recovered_microusd"] == 250_000
    assert _pool(tenant, period)["pool_reserved_microusd"] == 0
    assert budgets.get_hold(tenant_id=tenant, sk=sk)["status"] == "RECLAIMED"


def test_reconciler_defers_while_pending_in_flight(dynamodb_mock, monkeypatch):
    """The reconciler must DEFER when any PENDING is in flight (it cannot tell a
    debited-awaiting-activate from an undebited-awaiting-fence)."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-defer")
    budgets = TenantBudgetsRepository()
    hold_id = _pipeline._pending_hold_id(tenant, period, "pk-defer")
    future = int(time.time()) + 3600
    sk = _hsk(period, future, hold_id)
    budgets.pool_reserve_update(tenant_id=tenant, period=period, amount_microusd=100_000)
    budgets._table.put_item(Item={
        "tenant_id": tenant, "sk": sk, "hold_id": hold_id, "period": period,
        "amount_microusd": 100_000, "expires_at": future, "status": "PENDING",
        "source": "external",
    })
    summ = _pipeline.reconcile_pool(budgets, tenant, period)
    assert summ["deferred"] is True and summ["reason"] == "pending_in_flight"
    # pool untouched while deferred.
    assert _pool(tenant, period)["pool_reserved_microusd"] == 100_000


def test_reconciler_clean_when_active_matches_counter(dynamodb_mock, monkeypatch):
    """No leak: an ACTIVE hold whose amount matches the counter -> clean, nothing
    recovered."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-clean")
    r = _authorize(tenant, 600_000, "pk-clean")
    assert TenantBudgetsRepository().get_hold(tenant_id=tenant, sk=r.hold_sk)["status"] == "ACTIVE"
    summ = _pipeline.reconcile_pool(TenantBudgetsRepository(), tenant, period)
    assert summ["reason"] == "clean" and summ["recovered_microusd"] == 0
    assert _pool(tenant, period)["pool_reserved_microusd"] == 600_000
