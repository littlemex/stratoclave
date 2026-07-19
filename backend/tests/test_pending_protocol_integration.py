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
    from dynamo import tenant_budgets as _tb
    _tb._reset_budgets_low_level_client()


def test_per_tenant_canary_allowlist_routes_only_listed_tenant(dynamodb_mock, monkeypatch):
    """Canary rollout (docs/design/pending-protocol.md): with the GLOBAL flag OFF
    (default "transaction"), a tenant in STRATOCLAVE_RESERVE_PROTOCOL_TENANTS uses
    the PENDING marker path while every other tenant stays transaction-mode. This
    is the Shadow->Canary->Full lever — a single tenant flipped without a global
    switch."""
    from dynamo.tenant_budgets import marker_sk
    # global flag stays "transaction"; only `canary-t` is allowlisted.
    monkeypatch.setattr(_pipeline, "_RESERVE_PROTOCOL", "transaction")
    monkeypatch.setattr(_pipeline, "_RESERVE_PROTOCOL_TENANTS", frozenset({"canary-t"}))
    _pipeline._reset_low_level_client()
    from dynamo import tenant_budgets as _tb
    _tb._reset_budgets_low_level_client()
    assert _pipeline._reserve_protocol_for("canary-t") == "pending"
    assert _pipeline._reserve_protocol_for("other-t") == "transaction"
    assert _pipeline._reserve_protocol_for(None) == "transaction"
    # canary tenant → marker item written (pending path).
    ct, cp = _seed("canary-t")
    rc = _authorize(ct, 120_000, "ck")
    assert _pipeline.rehydrate_reservation_context(
        tenant_id=ct, period=cp, hold_id=rc.hold_id, hold_sk=rc.hold_sk) is not None
    assert TenantBudgetsRepository()._table.get_item(
        Key={"tenant_id": ct, "sk": marker_sk(rc.hold_id)}).get("Item") is not None
    # non-canary tenant → transaction path, NO marker item, RESERVE event written.
    ot, op = _seed("other-t")
    ro = _authorize(ot, 120_000, "ok")
    assert TenantBudgetsRepository()._table.get_item(
        Key={"tenant_id": ot, "sk": marker_sk(ro.hold_id)}).get("Item") is None
    assert CreditLedgerRepository().get_reserve(
        tenant_id=ot, period=op, hold_id=ro.hold_id) is not None


def test_pool_item_stays_small_across_many_reserves(dynamodb_mock, monkeypatch):
    """A′ (Fable next-step review): the CI regression guard that the separate-item
    marker keeps the pool item FLAT — the whole reason the map design was rejected.
    Under the pending path, 40 distinct reserves must NOT grow the pool item (each
    marker is its OWN item, not an entry on the pool item). This catches a code
    regression that reintroduces per-hold growth on the hot item, which the
    deductive WCU∝size argument assumes cannot happen."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pool-flat")
    b = TenantBudgetsRepository()
    size0 = b.pool_item_size_bytes(tenant, period)
    for i in range(40):
        _authorize(tenant, 1_000, f"flat-{i}")
    size1 = b.pool_item_size_bytes(tenant, period)
    # the pool item is a handful of fixed counters — small and essentially constant.
    assert size1 < 300, f"pool item grew to {size1} bytes (marker leaked onto it?)"
    # allow only tiny drift (updated_at timestamp etc.), NEVER per-hold growth.
    assert size1 - size0 < 40, f"pool item grew {size1 - size0}B over 40 reserves"


def test_audit_sweep_max_ttl_matches_authorize_clamp():
    """Fable PR-1 review non-blocking #4: the audit sweep's orphan age gate mirrors
    billing_authorize._TTL_MAX_SECONDS by hand. If the authorize clamp grows beyond
    24h, the gate would let a still-live hold's marker be settled (Bug 1 redux), so
    pin the two together."""
    from mvp import billing_authorize
    assert _pipeline._AUTHORIZE_MAX_TTL_SECONDS == billing_authorize._TTL_MAX_SECONDS


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


def _reserve_committed_marker(budgets, tenant, period, hold_id, amount, expires_at):
    """Helper: reserve via the marker commit (pool debit + SEPARATE marker item, in
    ONE TransactWriteItems — docs/design/pending-protocol.md PR-1) and put a PENDING
    hold — the state right after step 2, before step 3 (activate). Simulates a crash
    between commit and activate."""
    outcome = _pipeline._pending_commit_transact(
        budgets, tenant_id=tenant, period=period, hold_id=hold_id, amount=amount)
    assert outcome == budgets.RESERVE_APPLIED
    sk = _hsk(period, expires_at, hold_id)
    budgets._table.put_item(Item={
        "tenant_id": tenant, "sk": sk, "hold_id": hold_id, "period": period,
        "amount_microusd": amount, "expires_at": expires_at, "status": "PENDING",
        "source": "external",
    })
    return sk


def test_reserve_marker_idempotent_reissue(dynamodb_mock, monkeypatch):
    """The marker makes step-2 idempotent (I4): a re-issue of the SAME hold does
    NOT double-debit — the second commit's marker Put CCFs and reports
    RESERVE_ALREADY, pool unchanged."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-marker")
    budgets = TenantBudgetsRepository()
    hid = _pipeline._pending_hold_id(tenant, period, "pk-marker")
    o1 = _pipeline._pending_commit_transact(
        budgets, tenant_id=tenant, period=period, hold_id=hid, amount=200_000)
    o2 = _pipeline._pending_commit_transact(
        budgets, tenant_id=tenant, period=period, hold_id=hid, amount=200_000)
    assert o1 == budgets.RESERVE_APPLIED
    assert o2 == budgets.RESERVE_ALREADY          # idempotent, no second debit
    assert _pool(tenant, period)["pool_reserved_microusd"] == 200_000  # debited ONCE
    # the marker is a SEPARATE fixed-size item, not a map entry on the pool item.
    assert budgets.pool_marker_amount(tenant_id=tenant, period=period, hold_id=hid) == 200_000


def test_marker_is_a_separate_item_not_a_pool_map(dynamodb_mock, monkeypatch):
    """PR-1: the marker lives in its OWN item (SK=MARKER#<hold_id>), and the pool
    item carries NO `applied` map (the rejected design). This is the structural fix
    for the item-growth blowup."""
    from dynamo.tenant_budgets import budget_sk, marker_sk
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-sep")
    r = _authorize(tenant, 123_000, "pk-sep")
    b = TenantBudgetsRepository()
    marker = b._table.get_item(
        Key={"tenant_id": tenant, "sk": marker_sk(r.hold_id)}).get("Item")
    assert marker is not None
    assert int(marker["amount_microusd"]) == 123_000
    assert marker["marker_phase"] == "RESERVED"
    # the pool item does NOT carry an `applied` map anymore.
    pool = b._table.get_item(
        Key={"tenant_id": tenant, "sk": budget_sk(period)}).get("Item")
    assert "applied" not in pool


def test_pending_commit_cancellation_reasons(dynamodb_mock, monkeypatch):
    """The CancellationReasons contract (Fable PR-1 Q4-item-2): a re-issue of the
    SAME hold → marker-side CCF → RESERVE_ALREADY (idempotent, no re-debit); a fresh
    hold that cannot fit → pool-side CCF → RESERVE_EXHAUSTED."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-ccf", limit=500_000)
    b = TenantBudgetsRepository()
    hid = _pipeline._pending_hold_id(tenant, period, "pk-ccf")
    assert _pipeline._pending_commit_transact(
        b, tenant_id=tenant, period=period, hold_id=hid, amount=300_000) == b.RESERVE_APPLIED
    # same hold again → marker attribute_not_exists CCF → ALREADY (not a 2nd debit).
    assert _pipeline._pending_commit_transact(
        b, tenant_id=tenant, period=period, hold_id=hid, amount=300_000) == b.RESERVE_ALREADY
    assert _pool(tenant, period)["pool_reserved_microusd"] == 300_000
    # a DIFFERENT hold that overflows the remaining 200_000 → pool CCF → EXHAUSTED.
    hid2 = _pipeline._pending_hold_id(tenant, period, "pk-ccf-2")
    assert _pipeline._pending_commit_transact(
        b, tenant_id=tenant, period=period, hold_id=hid2, amount=400_000) == b.RESERVE_EXHAUSTED
    assert _pool(tenant, period)["pool_reserved_microusd"] == 300_000   # unchanged
    # the exhausted hold left NO marker (pool untouched, leak-safe).
    assert b.pool_marker_amount(tenant_id=tenant, period=period, hold_id=hid2) is None


def test_capture_settles_marker_to_settled_phase(dynamodb_mock, monkeypatch):
    """After an HTTP capture settles the hold, the SEPARATE marker is transitioned
    RESERVED -> SETTLED + TTL (cleanup), not deleted — so a late reserve retry of
    the same key still dedupes, and the marker becomes GC-eligible."""
    _set_protocol(monkeypatch, "pending")
    tenant, _ = _seed("pend-capmark")
    c = _http_client(monkeypatch, tenant)
    r = c.post("/api/mvp/billing/authorize", headers={"Idempotency-Key": "cm1"},
               json={"amount_microusd": 220_000})
    assert r.status_code == 200, r.text
    auth_id = r.json()["authorization_id"]
    b = TenantBudgetsRepository()
    from mvp.billing_authorize import decode_authorization_id
    hold_id, _, _ = decode_authorization_id(auth_id)
    assert b.get_marker(tenant_id=tenant, hold_id=hold_id)["marker_phase"] == "RESERVED"
    cap = c.post(f"/api/mvp/billing/authorizations/{auth_id}/capture",
                 json={"actual_amount_microusd": 150_000})
    assert cap.status_code == 200, cap.text
    m = b.get_marker(tenant_id=tenant, hold_id=hold_id)
    assert m is not None and m["marker_phase"] == "SETTLED" and "ttl" in m


def test_reconcile_audit_settles_stranded_reserved_marker(dynamodb_mock, monkeypatch):
    """Fable PR-1 Q2 hole 3: a settle/reclaim returned headroom + deleted the hold,
    but its best-effort marker settle was lost — leaving a RESERVED marker with NO
    hold. The reconcile audit sweep settles it (phase CAS + TTL) WITHOUT crediting
    the pool (the terminal already did), so it stops looking outstanding + GCs."""
    from dynamo.tenant_budgets import marker_sk
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-stray")
    b = TenantBudgetsRepository()
    hid = _pipeline._pending_hold_id(tenant, period, "pk-stray")
    # Simulate the post-terminal state: a RESERVED marker whose hold row is GONE
    # (the settle deleted the hold + returned headroom, but the marker cleanup was
    # lost). created_at is OLD (beyond max hold TTL) so the age gate treats it as a
    # genuine orphan. Pool is at zero-reserved (headroom already returned).
    from datetime import datetime, timedelta, timezone
    old_iso = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    b._table.put_item(Item={
        "tenant_id": tenant, "sk": marker_sk(hid), "hold_id": hid, "period": period,
        "amount_microusd": 90_000, "marker_phase": "RESERVED", "created_at": old_iso})
    reserved_before = _pool(tenant, period)["pool_reserved_microusd"]
    summ = _pipeline.reconcile_pool(b, tenant, period)
    assert summ["stale_markers_settled"] == 1
    assert summ["recovered_microusd"] == 0                     # NOT credited
    assert _pool(tenant, period)["pool_reserved_microusd"] == reserved_before
    m = b.get_marker(tenant_id=tenant, hold_id=hid)
    assert m["marker_phase"] == "SETTLED" and "ttl" in m


def test_audit_sweep_ignores_other_period_markers(dynamodb_mock, monkeypatch):
    """Fable PR-1 final review (cross-period permanent leak): markers are keyed
    MARKER#<hold_id> (not period-scoped), so list_reserved_markers returns every
    period's markers. A PRIOR period's still-live EXPIRED_UNCREDITED hold's RESERVED
    marker must NOT be settled by the CURRENT period's reconcile — else its
    credit-back dies when reconcile(prev_period) runs. The current pass must leave
    a marker whose period != this period untouched."""
    from dynamo.tenant_budgets import current_period, marker_sk, previous_period
    _set_protocol(monkeypatch, "pending")
    period = current_period()
    prev = previous_period(period)
    tenant, _ = _seed("pend-xperiod")
    b = TenantBudgetsRepository()
    # A prev-period RESERVED marker (old enough to pass the age gate) whose hold is
    # a live EXPIRED_UNCREDITED row in the PREV period — recoverable by prev's pass.
    hid = _pipeline._pending_hold_id(tenant, prev, "pk-xp")
    from datetime import datetime, timedelta, timezone
    old_iso = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    b._table.put_item(Item={
        "tenant_id": tenant, "sk": marker_sk(hid), "hold_id": hid, "period": prev,
        "amount_microusd": 55_000, "marker_phase": "RESERVED", "created_at": old_iso})
    # Also place the prev-period EXPIRED_UNCREDITED hold so prev's reconcile can
    # recover it (the debited headroom is still held out).
    from dynamo.tenant_budgets import hold_sk as _hsk2
    b.set_pool_limit(tenant_id=tenant, period=prev, pool_limit_microusd=10_000_000_000)
    # reflect the debit that the prev-period marker represents on the prev pool.
    b._table.update_item(
        Key={"tenant_id": tenant, "sk": f"BUDGET#{prev}"},
        UpdateExpression="ADD pool_headroom_microusd :neg, pool_reserved_microusd :amt",
        ExpressionAttributeValues={":neg": -55_000, ":amt": 55_000})
    past = int(time.time()) - 10_000
    sk_prev = _hsk2(prev, past, hid)
    b._table.put_item(Item={
        "tenant_id": tenant, "sk": sk_prev, "hold_id": hid, "period": prev,
        "amount_microusd": 55_000, "expires_at": past, "status": "EXPIRED_UNCREDITED",
        "source": "external"})
    # (a) CURRENT period reconcile must NOT touch the prev-period marker.
    summ = _pipeline.reconcile_pool(b, tenant, period)
    assert summ["stale_markers_settled"] == 0
    assert b.get_marker(tenant_id=tenant, hold_id=hid)["marker_phase"] == "RESERVED"
    # (b) PREV period reconcile recovers it via credit-back (the real bug scenario:
    # the marker must still be RESERVED for this to succeed).
    summ_prev = _pipeline.reconcile_pool(b, tenant, prev)
    assert summ_prev["recovered_microusd"] == 55_000
    assert b.get_marker(tenant_id=tenant, hold_id=hid)["marker_phase"] == "SETTLED"
    assert _pool(tenant, prev)["pool_reserved_microusd"] == 0


def test_audit_sweep_settles_real_reserve_path_orphan(dynamodb_mock, monkeypatch):
    """Fable PR-1 final review confirmation 1 (POSITIVE): prove the audit sweep is
    NOT dead code — a marker created by the REAL reserve path (which stamps period),
    once its hold row is gone and it is old enough, IS settled by the sweep. A
    negative-only test would pass even if the whole sweep were disabled."""
    from datetime import datetime, timedelta, timezone

    from dynamo.tenant_budgets import marker_sk
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-realorphan")
    b = TenantBudgetsRepository()
    # Real reserve → real marker (with period stamped by reserve_commit_txn_items).
    r = _authorize(tenant, 44_000, "pk-realorphan")
    m0 = b.get_marker(tenant_id=tenant, hold_id=r.hold_id)
    assert m0["marker_phase"] == "RESERVED" and m0["period"] == period  # period IS stamped
    # Simulate the terminal having run (hold gone) but the best-effort marker settle
    # lost, and age it past the orphan gate.
    b._table.delete_item(Key={"tenant_id": tenant, "sk": r.hold_sk})
    old_iso = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    b._table.update_item(
        Key={"tenant_id": tenant, "sk": marker_sk(r.hold_id)},
        UpdateExpression="SET created_at = :c",
        ExpressionAttributeValues={":c": old_iso})
    summ = _pipeline.reconcile_pool(b, tenant, period)
    assert summ["stale_markers_settled"] == 1                    # sweep is ALIVE
    m1 = b.get_marker(tenant_id=tenant, hold_id=r.hold_id)
    assert m1["marker_phase"] == "SETTLED" and "ttl" in m1


def test_audit_sweep_skips_marker_without_period(dynamodb_mock, monkeypatch):
    """Fable PR-1 approve note 1: a marker missing the `period` attribute (e.g. a
    dev/staging row written by a pre-period-stamp build) must be SKIPPED, not
    crash or be misclassified — the sweep's period guard is fail-closed."""
    from datetime import datetime, timedelta, timezone

    from dynamo.tenant_budgets import marker_sk
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-noperiod")
    b = TenantBudgetsRepository()
    hid = _pipeline._pending_hold_id(tenant, period, "pk-noperiod")
    old_iso = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    # A RESERVED marker with NO `period` attribute.
    b._table.put_item(Item={
        "tenant_id": tenant, "sk": marker_sk(hid), "hold_id": hid,
        "amount_microusd": 33_000, "marker_phase": "RESERVED", "created_at": old_iso})
    summ = _pipeline.reconcile_pool(b, tenant, period)
    assert summ["stale_markers_settled"] == 0                    # skipped, not settled
    assert b.get_marker(tenant_id=tenant, hold_id=hid)["marker_phase"] == "RESERVED"


def test_audit_sweep_never_settles_a_live_holds_marker(dynamodb_mock, monkeypatch):
    """Fable PR-1 review Bug 1 (permanent leak): a committed PENDING/ACTIVE hold's
    RESERVED marker must NOT be settled by the audit sweep just because the hold is
    not EXPIRED_UNCREDITED yet — else its later EXPIRED_UNCREDITED credit-back would
    fail forever. The sweep must skip a marker whose hold row (ANY status) exists,
    AND skip a young orphan whose hold row might merely be lagging the read."""
    from dynamo.tenant_budgets import marker_sk
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-live")
    b = TenantBudgetsRepository()
    # A live committed hold (PENDING, marker RESERVED, headroom debited).
    hid = _pipeline._pending_hold_id(tenant, period, "pk-live")
    past = int(time.time()) - 10_000
    sk = _reserve_committed_marker(b, tenant, period, hid, 120_000, past)  # writes fresh created_at
    assert _pool(tenant, period)["pool_reserved_microusd"] == 120_000
    # reconcile runs while the hold is still PENDING: the sweep must NOT settle the
    # marker (its hold row exists), and the young-orphan age gate is a second guard.
    summ = _pipeline.reconcile_pool(b, tenant, period)
    assert summ["stale_markers_settled"] == 0
    assert b.get_marker(tenant_id=tenant, hold_id=hid)["marker_phase"] == "RESERVED"
    # now the sweeper fences it and reconcile recovers it — the credit-back the Bug-1
    # settle would have killed still works.
    b._table.update_item(Key={"tenant_id": tenant, "sk": sk},
                         UpdateExpression="SET #s = :e",
                         ExpressionAttributeNames={"#s": "status"},
                         ExpressionAttributeValues={":e": "EXPIRED_UNCREDITED"})
    summ2 = _pipeline.reconcile_pool(b, tenant, period)
    assert summ2["recovered_microusd"] == 120_000
    assert _pool(tenant, period)["pool_reserved_microusd"] == 0


def test_credit_back_transient_conflict_does_not_falsely_report_credited(dynamodb_mock, monkeypatch):
    """Fable PR-1 review Bug 2: pool_credit_back must RAISE (not return False) on a
    TransactionCanceledException that is NOT the marker phase CAS (e.g. the pool row
    is missing → pool-side attribute_exists fails). Returning False there would make
    the reconciler retire the hold and strand the marker = permanent leak. A False
    is returned ONLY for a genuine marker-side CCF (already SETTLED)."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-cberr")
    b = TenantBudgetsRepository()
    hid = _pipeline._pending_hold_id(tenant, period, "pk-cberr")
    _pipeline._pending_commit_transact(b, tenant_id=tenant, period=period, hold_id=hid, amount=50_000)
    # Genuine already-SETTLED → marker-side CCF → False (leak-safe, no raise).
    b.marker_settle_best_effort(tenant_id=tenant, hold_id=hid)
    assert b.pool_credit_back(tenant_id=tenant, period=period, hold_id=hid) is False
    # Now a transient: DELETE the pool row so the pool item's
    # attribute_exists(tenant_id) fails (a pool-side cancel) while the marker phase
    # CAS is fine — the whole txn cancels having committed NOTHING. pool_credit_back
    # must RAISE (retryable), NOT return False (which would look like "already
    # credited" and let the reconciler retire the hold + strand the marker).
    from dynamo.tenant_budgets import budget_sk
    hid2 = _pipeline._pending_hold_id(tenant, period, "pk-cberr2")
    _pipeline._pending_commit_transact(b, tenant_id=tenant, period=period, hold_id=hid2, amount=50_000)
    b._table.delete_item(Key={"tenant_id": tenant, "sk": budget_sk(period)})
    import pytest as _pytest
    from botocore.exceptions import ClientError
    with _pytest.raises(ClientError):
        b.pool_credit_back(tenant_id=tenant, period=period, hold_id=hid2)


def test_reconcile_does_not_retire_hold_on_transient_credit_back(dynamodb_mock, monkeypatch):
    """Fable PR-1 review Bug 2 (loop): when pool_credit_back RAISES a transient
    (nothing committed), reconcile_pool must NOT retire the EXPIRED_UNCREDITED hold
    — it stays reclaimable for the next pass. A subsequent clean pass recovers it."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-transient")
    b = TenantBudgetsRepository()
    hold_id = _pipeline._pending_hold_id(tenant, period, "pk-transient")
    past = int(time.time()) - 10_000
    sk = _reserve_committed_marker(b, tenant, period, hold_id, 70_000, past)
    b._table.update_item(Key={"tenant_id": tenant, "sk": sk},
                         UpdateExpression="SET #s = :e",
                         ExpressionAttributeNames={"#s": "status"},
                         ExpressionAttributeValues={":e": "EXPIRED_UNCREDITED"})
    # Force pool_credit_back to raise a transient on the first reconcile pass.
    real = b.pool_credit_back
    calls = {"n": 0}

    def _flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientError({"Error": {"Code": "TransactionCanceledException"},
                               "CancellationReasons": [{"Code": "TransactionConflict"},
                                                       {"Code": "None"}]},
                              "TransactWriteItems")
        return real(**kw)

    from botocore.exceptions import ClientError
    monkeypatch.setattr(TenantBudgetsRepository, "pool_credit_back",
                        lambda self, **kw: _flaky(**kw))
    summ1 = _pipeline.reconcile_pool(b, tenant, period)
    assert summ1["recovered_microusd"] == 0           # transient: nothing recovered
    # hold NOT retired — still EXPIRED_UNCREDITED, marker still RESERVED (recoverable).
    assert b.get_hold(tenant_id=tenant, sk=sk)["status"] == "EXPIRED_UNCREDITED"
    assert b.get_marker(tenant_id=tenant, hold_id=hold_id)["marker_phase"] == "RESERVED"
    # next pass (no fault): recovers exactly once.
    summ2 = _pipeline.reconcile_pool(b, tenant, period)
    assert summ2["recovered_microusd"] == 70_000
    assert _pool(tenant, period)["pool_reserved_microusd"] == 0


def test_sweeper_fences_expired_pending_without_touching_pool(dynamodb_mock, monkeypatch):
    """A committed PENDING that never activated (crash before step 3) and expired:
    the sweeper fences it to EXPIRED_UNCREDITED WITHOUT crediting the pool (it
    cannot know the debit committed); the reconciler recovers it via the marker."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-fence")
    budgets = TenantBudgetsRepository()
    hold_id = _pipeline._pending_hold_id(tenant, period, "pk-fence")
    past = int(time.time()) - 10_000
    sk = _reserve_committed_marker(budgets, tenant, period, hold_id, 250_000, past)
    assert _pool(tenant, period)["pool_reserved_microusd"] == 250_000
    fenced = _pipeline.sweep_fence_pending(budgets, tenant, period)
    assert fenced == 1
    assert budgets.get_hold(tenant_id=tenant, sk=sk)["status"] == "EXPIRED_UNCREDITED"
    # pool NOT credited by the fence (leak, still reserved + marker present).
    assert _pool(tenant, period)["pool_reserved_microusd"] == 250_000
    assert budgets.pool_marker_amount(tenant_id=tenant, period=period, hold_id=hold_id) == 250_000
    # reconciler recovers via the marker (exactly-once), retires the hold.
    summ = _pipeline.reconcile_pool(budgets, tenant, period)
    assert summ["recovered_microusd"] == 250_000
    assert _pool(tenant, period)["pool_reserved_microusd"] == 0
    # PR-1 separate-item marker: credit-back does NOT delete the marker — it
    # transitions RESERVED -> SETTLED + TTL (so a late reserve retry still dedupes).
    m = budgets.get_marker(tenant_id=tenant, hold_id=hold_id)
    assert m is not None and m["marker_phase"] == "SETTLED" and "ttl" in m
    assert budgets.get_hold(tenant_id=tenant, sk=sk)["status"] == "RECLAIMED"
    # second reconcile pass = no double credit (marker no longer RESERVED -> phase
    # CAS in pool_credit_back fails).
    assert _pipeline.reconcile_pool(budgets, tenant, period)["recovered_microusd"] == 0
    assert _pool(tenant, period)["pool_reserved_microusd"] == 0


def test_reconciler_skips_uncommitted_fenced_hold(dynamodb_mock, monkeypatch):
    """A PENDING hold with NO marker (debit never committed — ambiguous-lost /
    exhausted) that got fenced: the reconciler must NOT credit it back (crediting
    an un-debited hold = oversell). It retires the hold, pool untouched."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-nomark")
    budgets = TenantBudgetsRepository()
    hold_id = _pipeline._pending_hold_id(tenant, period, "pk-nomark")
    past = int(time.time()) - 10_000
    sk = _hsk(period, past, hold_id)
    # PENDING hold, NO pool debit / NO marker (uncommitted).
    budgets._table.put_item(Item={
        "tenant_id": tenant, "sk": sk, "hold_id": hold_id, "period": period,
        "amount_microusd": 300_000, "expires_at": past, "status": "EXPIRED_UNCREDITED",
        "source": "external",
    })
    assert _pool(tenant, period)["pool_reserved_microusd"] == 0
    summ = _pipeline.reconcile_pool(budgets, tenant, period)
    assert summ["recovered_microusd"] == 0        # NOT credited (no marker)
    assert _pool(tenant, period)["pool_reserved_microusd"] == 0  # no oversell
    assert budgets.get_hold(tenant_id=tenant, sk=sk)["status"] == "RECLAIMED"


def test_reconciler_clean_when_active(dynamodb_mock, monkeypatch):
    """A live ACTIVE hold is left alone by the reconciler (only EXPIRED_UNCREDITED
    holds are candidates)."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-clean")
    r = _authorize(tenant, 600_000, "pk-clean")
    assert TenantBudgetsRepository().get_hold(tenant_id=tenant, sk=r.hold_sk)["status"] == "ACTIVE"
    summ = _pipeline.reconcile_pool(TenantBudgetsRepository(), tenant, period)
    assert summ["recovered_microusd"] == 0
    assert _pool(tenant, period)["pool_reserved_microusd"] == 600_000


# --------------------------------------------------------------------------
# HTTP endpoint integration under flag-on — closes the capture/void 404 gap
# (the C-1 gate read the RESERVE event, which the PENDING path does not write).
# --------------------------------------------------------------------------

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from mvp.deps import AuthenticatedUser, get_current_user  # noqa: E402


def _http_client(monkeypatch, org):
    from mvp import authz
    from mvp.billing_authorize import router
    monkeypatch.setattr(authz, "user_has_permission",
                        lambda user, scope: scope in {"billing:write", "billing:read"})
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="u-http", email="u@e.com", org_id=org, roles=("team_lead",),
        raw_claims={}, auth_kind="cognito")
    return TestClient(app)


def test_http_authorize_capture_roundtrip_flag_on(dynamodb_mock, monkeypatch):
    """flag-on: authorize -> get -> capture through the HTTP endpoints. Proves the
    C-1 gate (HOLD-first) no longer 404s a pending-mode authorization (the bug the
    test-gap audit found: _require_external read a RESERVE event the pending path
    never writes)."""
    _set_protocol(monkeypatch, "pending")
    tenant, _ = _seed("acme-http-pend")
    c = _http_client(monkeypatch, tenant)
    r = c.post("/api/mvp/billing/authorize", headers={"Idempotency-Key": "hk1"},
               json={"amount_microusd": 500_000})
    assert r.status_code == 200, r.text
    assert r.json()["replayed"] is False
    auth_id = r.json()["authorization_id"]
    # GET must NOT 404 (the regression) — it resolves via the HOLD's source.
    g = c.get(f"/api/mvp/billing/authorizations/{auth_id}")
    assert g.status_code == 200, g.text
    assert g.json()["status"] == "authorized"
    assert g.json()["amount_microusd"] == 500_000
    # capture settles the hold.
    cap = c.post(f"/api/mvp/billing/authorizations/{auth_id}/capture",
                 json={"actual_amount_microusd": 400_000})
    assert cap.status_code == 200, cap.text
    assert _pool(tenant, current_period())["pool_reserved_microusd"] == 0
    assert _pool(tenant, current_period())["pool_settled_microusd"] == 400_000


def test_http_authorize_void_roundtrip_flag_on(dynamodb_mock, monkeypatch):
    """flag-on: authorize -> void returns the full reservation (marker removed)."""
    _set_protocol(monkeypatch, "pending")
    tenant, _ = _seed("acme-http-void")
    c = _http_client(monkeypatch, tenant)
    r = c.post("/api/mvp/billing/authorize", headers={"Idempotency-Key": "vk1"},
               json={"amount_microusd": 350_000})
    assert r.status_code == 200, r.text
    assert r.json()["replayed"] is False
    auth_id = r.json()["authorization_id"]
    v = c.post(f"/api/mvp/billing/authorizations/{auth_id}/void")
    assert v.status_code == 200, v.text
    assert _pool(tenant, current_period())["pool_reserved_microusd"] == 0


def test_http_capture_idempotent_replay_flag_on(dynamodb_mock, monkeypatch):
    """flag-on: a duplicate Idempotency-Key replays the SAME authorization (200/
    replayed) with addressing that still resolves for capture — not a broken
    hold_sk (Fable review bug 1)."""
    _set_protocol(monkeypatch, "pending")
    tenant, _ = _seed("acme-http-idem")
    c = _http_client(monkeypatch, tenant)
    body = {"amount_microusd": 200_000}
    r1 = c.post("/api/mvp/billing/authorize", headers={"Idempotency-Key": "dk"}, json=body)
    r2 = c.post("/api/mvp/billing/authorize", headers={"Idempotency-Key": "dk"}, json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["replayed"] is False and r2.json()["replayed"] is True
    assert r1.json()["authorization_id"] == r2.json()["authorization_id"]
    # the replayed authorization_id still resolves + captures (addressing intact).
    cap = c.post(f"/api/mvp/billing/authorizations/{r2.json()['authorization_id']}/capture",
                 json={"actual_amount_microusd": 100_000})
    assert cap.status_code == 200, cap.text
    assert _pool(tenant, current_period())["pool_reserved_microusd"] == 0  # debited once
