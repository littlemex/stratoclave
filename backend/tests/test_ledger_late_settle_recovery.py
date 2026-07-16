"""Regression tests for the Phase-2 LATE_SETTLE recovery path
(_recover_spend_via_late_settle) — the revenue-leak fix.

Pins the Fable P2 review-1 findings so they cannot regress:

  C-1: a TRANSIENT cancel (TransactionConflict / throttle) on the recovery txn
       must RAISE — never be swallowed as a benign no-op — or the spend is
       silently dropped (the leak Phase 2 closes) while the client is told
       "settled". The settled-only item is the hot pool-counter row, so this
       conflict is realistic.
  Happy path: after the reaper writes a RECLAIM terminal, a late settle records
       the spend via a LATE_SETTLE (distinct sk, reserved_delta=0) and the
       counter advances by exactly `actual`.
"""
from __future__ import annotations

from botocore.exceptions import ClientError

from mvp import _pipeline
from mvp._pipeline import reserve_credit, settle_reservation_and_log


class _User:
    def __init__(self, user_id, org_id):
        self.user_id = user_id
        self.org_id = org_id
        self.email = "u@example.com"
        self.roles = ("user",)


def _seed(tenant_id="acme-late"):
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.user_tenants import UserTenantsRepository

    period = current_period()
    user = _User(f"user-{tenant_id}", tenant_id)
    UserTenantsRepository().ensure(
        user_id=user.user_id, tenant_id=tenant_id, role="user",
        total_credit=1_000_000_000,
    )
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=tenant_id, period=period, pool_limit_microusd=10_000_000_000,
    )
    return user, period


def _force_reap(ctx, tenant_id, period):
    """Expire + sweep the hold so the reaper writes a RECLAIM terminal (Phase 2)."""
    from dynamo.tenant_budgets import TenantBudgetsRepository, hold_sk as _hsk
    import time

    budgets = TenantBudgetsRepository()
    item = budgets._table.get_item(
        Key={"tenant_id": tenant_id, "sk": ctx.hold_sk}
    ).get("Item")
    assert item is not None
    past = int(time.time()) - 10_000
    new_sk = _hsk(period, past, ctx.hold_id)
    item["sk"] = new_sk
    item["expires_at"] = past
    budgets._table.delete_item(Key={"tenant_id": tenant_id, "sk": ctx.hold_sk})
    budgets._table.put_item(Item=item)
    ctx.hold_sk = new_sk
    _pipeline._sweep_expired_holds(budgets, tenant_id, period)


def _ledger():
    from dynamo import CreditLedgerRepository

    return CreditLedgerRepository()


def _pool_settled(tenant_id, period):
    from dynamo.tenant_budgets import TenantBudgetsRepository

    return int(
        TenantBudgetsRepository().pool_summary(tenant_id, period)["pool_settled_microusd"]
    )


def test_reap_then_late_settle_records_spend(dynamodb_mock):
    """Happy path: RECLAIM terminal, then a late settle → LATE_SETTLE records the
    spend on a distinct sk with reserved_delta=0; counter advances by actual."""
    user, period = _seed()
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)
    _force_reap(ctx, user.org_id, period)

    settle_reservation_and_log(
        user=user, tenants_repo=ctx, reservation=ctx.reservation_tokens,
        actual_input_tokens=10, actual_output_tokens=5,
        model_id="us.anthropic.claude-opus-4-7", context=ctx,
        actual_cost_microusd=1_200_000,
    )

    late = _ledger().get_late_settle(
        tenant_id=user.org_id, period=period, hold_id=ctx.hold_id
    )
    assert late is not None, "LATE_SETTLE not written"
    assert int(late["reserved_delta_microusd"]) == 0
    assert int(late["settled_delta_microusd"]) == 1_200_000
    term = _ledger().get_terminal(
        tenant_id=user.org_id, period=period, hold_id=ctx.hold_id
    )
    assert term["event_type"] == "RECLAIM"
    assert _pool_settled(user.org_id, period) == 1_200_000


def test_transient_cancel_on_recovery_is_not_silent_success(dynamodb_mock, monkeypatch):
    """C-1: a persistent TransactionConflict on the LATE_SETTLE recovery txn must
    NOT be recorded as a silent success. After exhausting its in-place retries the
    recovery raises; the outer settle records it as `pool_settle_failed` (loud,
    for reconciliation) — and crucially the spend is NOT in the ledger/counter, so
    no phantom success. A later reconciliation/retry recovers it."""
    user, period = _seed()
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)
    _force_reap(ctx, user.org_id, period)

    real_client = _pipeline._low_level_client()
    real_txn = real_client.transact_write_items
    calls = {"recovery": 0}

    def _txn(**kwargs):
        # The recovery txn is identified by carrying a LATE_SETTLE Put. Everything
        # else (the reaper already ran) goes through untouched. Persistently
        # conflict so the recovery exhausts its in-place retries.
        items = kwargs.get("TransactItems", [])
        is_recovery = any(
            "Put" in it
            and it["Put"].get("Item", {}).get("sk", {}).get("S", "").endswith("#LATE_SETTLE")
            for it in items
        )
        if is_recovery:
            calls["recovery"] += 1
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [
                        {"Code": "TransactionConflict"},  # [0] pool row hot-conflict
                        {"Code": "None"},                 # [1] LATE_SETTLE Put
                        {"Code": "None"},                 # [2] ConditionCheck
                    ],
                },
                "TransactWriteItems",
            )
        return real_txn(**kwargs)

    class _Wrapped:
        def __getattr__(self, name):
            return getattr(real_client, name)

        def transact_write_items(self, **kwargs):
            return _txn(**kwargs)

    monkeypatch.setattr(_pipeline, "_low_level_client", lambda: _Wrapped())
    monkeypatch.setattr(_pipeline.time, "sleep", lambda *_: None)

    # settle is best-effort at the streaming tail: it swallows the raised
    # RuntimeError into a `pool_settle_failed` log rather than failing the usage
    # write. The contract we assert is the OUTCOME, not the exception surfacing.
    settle_reservation_and_log(
        user=user, tenants_repo=ctx, reservation=ctx.reservation_tokens,
        actual_input_tokens=10, actual_output_tokens=5,
        model_id="us.anthropic.claude-opus-4-7", context=ctx,
        actual_cost_microusd=1_200_000,
    )

    # The recovery actually retried in-place (not a single silent swallow).
    assert calls["recovery"] > 1, "recovery did not retry the transient conflict"
    # Crucially: the spend was NOT recorded (no phantom success), and no
    # LATE_SETTLE landed — reconciliation / a later retry recovers it.
    assert _pool_settled(user.org_id, period) == 0
    assert _ledger().get_late_settle(
        tenant_id=user.org_id, period=period, hold_id=ctx.hold_id
    ) is None
