"""HOLD-row enrichment + capture/void dual-read / HOLD-only equivalence.

Two-item migration step 2/3 (docs/design/ledger-hot-path.md): the HOLD row is
promoted to the synchronous source of truth (source / amount / rate_snapshot /
run_id folded on), so capture/void can rehydrate from the HOLD ALONE and the
RESERVE ledger event can go async. These tests prove, against moto:

  - an external authorize writes the enrichment onto the HOLD row;
  - rehydrate is EQUIVALENT whether it reads the HOLD-only (flag on) or dual-reads
    (flag off): same amount, pricing_key, run attribution;
  - the C-1 security gate holds under BOTH modes — an INLINE hold's token can
    never rehydrate (source != "external"), and a MISSING source fails closed;
  - a legacy hold (no enrichment) still rehydrates via the RESERVE-event fallback
    when the flag is off.
"""
from __future__ import annotations

import pytest

from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk, current_period
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline
from mvp.billing_authorize import encode_authorization_id


def _seed(tenant_id: str) -> str:
    period = current_period()
    UserTenantsRepository().ensure(
        user_id=f"u-{tenant_id}", tenant_id=tenant_id, role="user",
        total_credit=1_000_000_000,
    )
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=tenant_id, period=period, pool_limit_microusd=10_000_000_000,
    )
    return period


def _authorize(tenant_id: str, amount: int, key: str, *, run_id=None):
    return _pipeline.reserve_external_authorization(
        tenant_id=tenant_id,
        amount_microusd=amount,
        idempotency_key=key,
        request_fingerprint=f"fp-{key}",
        authorization_id_factory=lambda hid, p, hsk: encode_authorization_id(
            hold_id=hid, period=p, hold_sk=hsk),
        ttl_seconds=3600,
        description=f"desc-{key}",
        workflow_run_id=run_id,
    )


def _set_hold_only(monkeypatch, on: bool):
    monkeypatch.setattr(_pipeline, "_CAPTURE_HOLD_ONLY", on)


def test_authorize_enriches_hold_row(dynamodb_mock):
    tid = "enrich-t1"
    period = _seed(tid)
    r = _authorize(tid, 2_000_000, "k1", run_id="run-abc")
    hold = TenantBudgetsRepository().get_hold(tenant_id=tid, sk=r.hold_sk)
    assert hold["source"] == "external"
    assert int(hold["amount_microusd"]) == 2_000_000
    assert hold["run_id"] == "run-abc"
    assert hold.get("payload_hash") == "fp-k1"
    assert "run_id_source" not in hold  # real run id, not a fallback


def test_authorize_without_run_id_marks_fallback(dynamodb_mock):
    tid = "enrich-t2"
    _seed(tid)
    r = _authorize(tid, 1_000_000, "k2", run_id=None)
    hold = TenantBudgetsRepository().get_hold(tenant_id=tid, sk=r.hold_sk)
    assert hold["run_id"] == r.hold_id          # fell back to hold_id
    assert hold["run_id_source"] == "hold_id_fallback"


@pytest.mark.parametrize("hold_only", [False, True])
def test_rehydrate_equivalent_dualread_and_holdonly(dynamodb_mock, monkeypatch, hold_only):
    tid = f"rehy-{int(hold_only)}"
    period = _seed(tid)
    r = _authorize(tid, 3_000_000, f"k-{hold_only}", run_id="run-xyz")
    _set_hold_only(monkeypatch, hold_only)
    ctx = _pipeline.rehydrate_reservation_context(
        tenant_id=tid, period=period, hold_id=r.hold_id, hold_sk=r.hold_sk)
    assert ctx is not None
    assert ctx.source == "external"
    assert ctx.pool_reserved_microusd == 3_000_000
    assert ctx.workflow_run_id == "run-xyz"
    assert ctx.tenant_id == tid
    assert ctx.hold_id == r.hold_id


@pytest.mark.parametrize("hold_only", [False, True])
def test_c1_gate_denies_inline_hold(dynamodb_mock, monkeypatch, hold_only):
    """An inline LLM hold (source='inline') must NEVER be rehydratable via the
    external capture/void path — under dual-read AND HOLD-only."""
    tid = f"c1-{int(hold_only)}"
    period = _seed(tid)
    budgets = TenantBudgetsRepository()
    # Write an inline-tagged hold directly via the enriched builder + a low-level
    # transact, mimicking the inline reserve path.
    import time
    from dynamo.tenant_budgets import hold_sk as _hsk
    hold_id = "inline-hold-1"
    exp = int(time.time()) + 3600
    item = budgets.hold_put_txn_item(
        tenant_id=tid, period=period, hold_id=hold_id,
        amount_microusd=500_000, expires_at_epoch=exp, source="inline",
    )["Put"]["Item"]
    # deserialize the low-level item to the resource API for a direct put
    budgets._table.put_item(Item={
        "tenant_id": tid, "sk": _hsk(period, exp, hold_id), "hold_id": hold_id,
        "period": period, "amount_microusd": 500_000, "expires_at": exp,
        "source": "inline",
    })
    _set_hold_only(monkeypatch, hold_only)
    ctx = _pipeline.rehydrate_reservation_context(
        tenant_id=tid, period=period, hold_id=hold_id,
        hold_sk=_hsk(period, exp, hold_id))
    assert ctx is None, "inline hold must not be externally rehydratable (C-1)"


def test_c1_missing_source_fails_closed_under_hold_only(dynamodb_mock, monkeypatch):
    """A hold with NO source attribute, under HOLD-only, must be denied (a legacy
    row is not capturable once the RESERVE-event fallback is off)."""
    tid = "c1-nosrc"
    period = _seed(tid)
    import time
    from dynamo.tenant_budgets import hold_sk as _hsk
    hold_id = "nosrc-hold"
    exp = int(time.time()) + 3600
    TenantBudgetsRepository()._table.put_item(Item={
        "tenant_id": tid, "sk": _hsk(period, exp, hold_id), "hold_id": hold_id,
        "period": period, "amount_microusd": 400_000, "expires_at": exp,
    })
    _set_hold_only(monkeypatch, True)
    ctx = _pipeline.rehydrate_reservation_context(
        tenant_id=tid, period=period, hold_id=hold_id,
        hold_sk=_hsk(period, exp, hold_id))
    assert ctx is None, "missing source must fail closed under HOLD-only"


def test_legacy_hold_rehydrates_via_reserve_event_fallback(dynamodb_mock, monkeypatch):
    """A pre-enrichment hold (no source on the HOLD) still rehydrates via the
    RESERVE-event fallback when the flag is OFF (dual-read / migration window)."""
    tid = "legacy-t"
    period = _seed(tid)
    # Real authorize writes BOTH the enriched hold AND the RESERVE event; strip
    # the HOLD enrichment to simulate a pre-migration hold.
    r = _authorize(tid, 1_500_000, "legacy-k", run_id="run-legacy")
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": tid, "sk": r.hold_sk},
        UpdateExpression="REMOVE #s, run_id, rate_snapshot",
        ExpressionAttributeNames={"#s": "source"},
    )
    _set_hold_only(monkeypatch, False)  # dual-read: fallback allowed
    ctx = _pipeline.rehydrate_reservation_context(
        tenant_id=tid, period=period, hold_id=r.hold_id, hold_sk=r.hold_sk)
    assert ctx is not None, "legacy hold must rehydrate via RESERVE-event fallback"
    assert ctx.source == "external"
    assert ctx.pool_reserved_microusd == 1_500_000
