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
    """Drive the HOLD-only path via the ENRICHMENT_EPOCH gate (finding 2 fix).
    on=True  -> epoch in the distant past, so EVERY hold (even a source-less
                legacy row) is post-epoch and takes the HOLD-only path.
    on=False -> epoch unset, so ONLY holds already carrying `source` take
                HOLD-only and a legacy row falls back to the RESERVE event.
    This preserves the original flag's semantics exactly."""
    monkeypatch.setattr(_pipeline, "_ENRICHMENT_EPOCH", 0.0 if on else None)


def test_authorize_enriches_hold_row(dynamodb_mock):
    tid = "enrich-t1"
    _seed(tid)
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
    assert item["source"] == {"S": "inline"}  # builder tags the inline source
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
    """A POST-EPOCH hold with NO source attribute must be denied: the epoch gate
    routes it to the HOLD-only path (created_at >= epoch) and C-1 fails closed on
    the missing source (a corruption/bug case — an enriched-era hold must carry
    source). The created_at is set so this genuinely exercises HOLD-only, not the
    legacy fallback."""
    tid = "c1-nosrc"
    period = _seed(tid)
    import time
    from dynamo.tenant_budgets import hold_sk as _hsk
    hold_id = "nosrc-hold"
    exp = int(time.time()) + 3600
    TenantBudgetsRepository()._table.put_item(Item={
        "tenant_id": tid, "sk": _hsk(period, exp, hold_id), "hold_id": hold_id,
        "period": period, "amount_microusd": 400_000, "expires_at": exp,
        "created_at": "2026-07-19T00:00:00+00:00",  # post-epoch (epoch=0.0)
    })
    _set_hold_only(monkeypatch, True)
    ctx = _pipeline.rehydrate_reservation_context(
        tenant_id=tid, period=period, hold_id=hold_id,
        hold_sk=_hsk(period, exp, hold_id))
    assert ctx is None, "missing source must fail closed under HOLD-only"


def test_holdonly_amount_mismatch_raises_inconsistent(dynamodb_mock, monkeypatch):
    """H-A money-safety must survive the migration: on the HOLD-only path, if the
    HOLD amount and the still-synchronous RESERVE event's reserved_delta disagree,
    rehydrate RAISES ExternalHoldInconsistent (settling would move pool_reserved by
    an amount the ledger never recorded — I2 break). This is the reason the
    HOLD-only path keeps reading the RESERVE event during step 3."""
    tid = "holdonly-mm"
    period = _seed(tid)
    r = _authorize(tid, 500_000, "mm-holdonly", run_id="run-mm")
    # Corrupt ONLY the HOLD amount; the RESERVE event keeps 500k.
    budgets = TenantBudgetsRepository()
    item = budgets._table.get_item(Key={"tenant_id": tid, "sk": r.hold_sk})["Item"]
    item["amount_microusd"] = 600_000
    budgets._table.put_item(Item=item)
    _set_hold_only(monkeypatch, True)  # HOLD-only path
    with pytest.raises(_pipeline.ExternalHoldInconsistent):
        _pipeline.rehydrate_reservation_context(
            tenant_id=tid, period=period, hold_id=r.hold_id, hold_sk=r.hold_sk)


def test_enrichment_epoch_parses_iso_and_epoch_seconds():
    """_parse_epoch_env accepts both an epoch-seconds number and an ISO-8601
    timestamp, and a naive ISO string is assumed UTC (no naive/aware mixing)."""
    assert _pipeline._parse_epoch_env(None) is None
    assert _pipeline._parse_epoch_env("  ") is None
    assert _pipeline._parse_epoch_env("1700000000") == 1700000000.0
    aware = _pipeline._parse_epoch_env("2026-07-19T00:00:00+00:00")
    naive = _pipeline._parse_epoch_env("2026-07-19T00:00:00")  # assumed UTC
    assert aware == naive is not None


def test_enrichment_epoch_fail_fast_on_garbage():
    """A typo'd epoch env must FAIL-FAST, not silently become None (which would
    leave the process permanently on step-2 behaviour after an operator believed
    they cut over)."""
    with pytest.raises(ValueError):
        _pipeline._parse_epoch_env("not-a-timestamp")


def test_hold_created_epoch_handles_missing_and_bad(dynamodb_mock):
    """A hold with no/garbage created_at → None → routed to the money-safe
    (pre-epoch legacy) path, never mis-classified as post-epoch."""
    assert _pipeline._hold_created_epoch({}) is None
    assert _pipeline._hold_created_epoch({"created_at": "garbage"}) is None
    naive = _pipeline._hold_created_epoch({"created_at": "2026-07-19T00:00:00"})
    aware = _pipeline._hold_created_epoch({"created_at": "2026-07-19T00:00:00+00:00"})
    assert naive == aware is not None


def test_pre_epoch_hold_uses_legacy_fallback_not_hold_only(dynamodb_mock, monkeypatch):
    """A hold minted BEFORE the enrichment epoch, with its source stripped, must
    take the legacy RESERVE-event fallback (finding 2: it must NOT be forced onto
    the HOLD-only path where it would 404 an authorized txn)."""
    tid = "epoch-legacy"
    period = _seed(tid)
    r = _authorize(tid, 1_200_000, "epoch-k", run_id="run-e")
    # Strip enrichment AND stamp an OLD created_at (pre-epoch).
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": tid, "sk": r.hold_sk},
        UpdateExpression="REMOVE #s, run_id, rate_snapshot SET created_at = :c",
        ExpressionAttributeNames={"#s": "source"},
        ExpressionAttributeValues={":c": "2020-01-01T00:00:00+00:00"},
    )
    # Epoch is set to a recent time; the old hold is pre-epoch.
    monkeypatch.setattr(_pipeline, "_ENRICHMENT_EPOCH",
                        _pipeline._parse_epoch_env("2026-07-01T00:00:00+00:00"))
    ctx = _pipeline.rehydrate_reservation_context(
        tenant_id=tid, period=period, hold_id=r.hold_id, hold_sk=r.hold_sk)
    assert ctx is not None, "pre-epoch hold must rehydrate via RESERVE-event fallback"
    assert ctx.pool_reserved_microusd == 1_200_000


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
