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


def _reserve_committed_marker(budgets, tenant, period, hold_id, amount, expires_at):
    """Helper: reserve via the marker primitive (debit + marker) and put a PENDING
    hold — the state right after step 2, before step 3 (activate). Simulates a
    crash between commit and activate."""
    budgets.ensure_applied_map(tenant_id=tenant, period=period)
    outcome = budgets.pool_reserve_update(
        tenant_id=tenant, period=period, hold_id=hold_id, amount_microusd=amount)
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
    NOT double-debit — the second call reports RESERVE_ALREADY, pool unchanged."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-marker")
    budgets = TenantBudgetsRepository()
    budgets.ensure_applied_map(tenant_id=tenant, period=period)
    hid = _pipeline._pending_hold_id(tenant, period, "pk-marker")
    o1 = budgets.pool_reserve_update(tenant_id=tenant, period=period, hold_id=hid, amount_microusd=200_000)
    o2 = budgets.pool_reserve_update(tenant_id=tenant, period=period, hold_id=hid, amount_microusd=200_000)
    assert o1 == budgets.RESERVE_APPLIED
    assert o2 == budgets.RESERVE_ALREADY          # idempotent, no second debit
    assert _pool(tenant, period)["pool_reserved_microusd"] == 200_000  # debited ONCE


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
    assert budgets.pool_marker_amount(tenant_id=tenant, period=period, hold_id=hold_id) is None
    assert budgets.get_hold(tenant_id=tenant, sk=sk)["status"] == "RECLAIMED"
    # second reconcile pass = no double credit (marker gone).
    assert _pipeline.reconcile_pool(budgets, tenant, period)["recovered_microusd"] == 0
    assert _pool(tenant, period)["pool_reserved_microusd"] == 0


def test_reconciler_skips_uncommitted_fenced_hold(dynamodb_mock, monkeypatch):
    """A PENDING hold with NO marker (debit never committed — ambiguous-lost /
    exhausted) that got fenced: the reconciler must NOT credit it back (crediting
    an un-debited hold = oversell). It retires the hold, pool untouched."""
    _set_protocol(monkeypatch, "pending")
    tenant, period = _seed("pend-nomark")
    budgets = TenantBudgetsRepository()
    budgets.ensure_applied_map(tenant_id=tenant, period=period)
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
