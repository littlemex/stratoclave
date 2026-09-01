"""R21 (the F1 contract): the mode is a sentence, not a field -- backend
half. R21's own "Verified by": "Unit: the surfaces render it; a transition
between modes emits an audit event." This file covers:

  (a) `pool_summary()`/`PoolBudgetResponse` carry enough for a caller to build
      the sentence at all (`seat_count`, `manual_limit_microusd`, `mode`) --
      today none of the three exists on the response;
  (b) a transition between modes (seat-tracked -> manual, and manual ->
      seat-tracked) emits a NAMED audit event distinct from the existing
      `tenant_pool_budget_set` (which fires on every admin figure-set,
      transition or not, and so cannot by itself answer "did the MODE
      change", only "did an admin write happen").

The frontend half (the sentence + resume action actually rendering) is
covered separately in
frontend/src/pages/admin/AdminTenantDetail.ceiling.test.tsx.

Today `TenantBudgetsRepository.pool_summary()` returns `sizing` (not `mode`)
and nothing named `seat_count`/`manual_limit_microusd`; `set_manual_limit`/
`clear_manual_limit` do not exist (R1), so no mode-transition audit event can
be emitted by anything. Every test below fails for one of those reasons.

Design note: /Users/akazawt/tmp/stratoneed/change-pipeline/quota-raise-and-archive/design-F1.md
section 6.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from dynamo import TenantBudgetsRepository, current_period
from dynamo.tenant_budgets import budget_sk

_SEAT_MICROUSD = 200 * 1_000_000


def _seed_row(tenant_id: str, period: str, *, seat_count: int, manual_limit_microusd=None) -> None:
    baseline = (
        int(manual_limit_microusd) if manual_limit_microusd is not None
        else seat_count * _SEAT_MICROUSD
    )
    item = {
        "tenant_id": tenant_id,
        "sk": budget_sk(period),
        "pool_limit_microusd": Decimal(baseline),
        "pool_headroom_microusd": Decimal(baseline),
        "pool_reserved_microusd": Decimal(0),
        "pool_settled_microusd": Decimal(0),
        "seat_count": Decimal(seat_count),
        "status": "active",
        "version": "3",
    }
    if manual_limit_microusd is not None:
        item["manual_limit_microusd"] = Decimal(int(manual_limit_microusd))
    TenantBudgetsRepository()._table.put_item(Item=item)


def test_pool_summary_reports_seat_tracked_mode_with_seat_count_and_no_manual_limit(
    dynamodb_mock,
):
    tenant_id, period = "seat-tracked-co", current_period()
    _seed_row(tenant_id, period, seat_count=3)

    summary = TenantBudgetsRepository().pool_summary(tenant_id, period)
    assert summary["mode"] == "seat_tracked"
    assert summary["seat_count"] == 3
    assert summary["manual_limit_microusd"] is None


def test_pool_summary_reports_manual_mode_with_the_figure(dynamodb_mock):
    tenant_id, period = "manual-co", current_period()
    _seed_row(tenant_id, period, seat_count=5, manual_limit_microusd=100_000_000)

    summary = TenantBudgetsRepository().pool_summary(tenant_id, period)
    assert summary["mode"] == "manual"
    assert summary["seat_count"] == 5
    assert summary["manual_limit_microusd"] == 100_000_000


def test_pool_summary_reports_manual_mode_even_at_zero(dynamodb_mock):
    """mode must key off PRESENCE, matching R1's sentinel -- a manual figure
    of exactly 0 is still 'manual', never re-read as 'seat_tracked'."""
    tenant_id, period = "manual-zero-co", current_period()
    _seed_row(tenant_id, period, seat_count=5, manual_limit_microusd=0)

    summary = TenantBudgetsRepository().pool_summary(tenant_id, period)
    assert summary["mode"] == "manual"
    assert summary["manual_limit_microusd"] == 0


def test_setting_a_manual_limit_on_a_seat_tracked_row_emits_a_mode_change_audit_event(
    dynamodb_mock, caplog,
):
    tenant_id, period = "acme-eng", current_period()
    _seed_row(tenant_id, period, seat_count=2)

    caplog.set_level(logging.INFO, logger="stratoclave.audit")
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=tenant_id, period=period, manual_limit_microusd=500_000_000
    )

    lines = [r.getMessage() for r in caplog.records if r.name == "stratoclave.audit"]
    events = [json.loads(line) for line in lines]
    mode_changes = [e for e in events if e.get("event") == "tenant_pool_mode_changed"
                    and e.get("target_id") == tenant_id]
    assert mode_changes, (
        f"no tenant_pool_mode_changed audit event was emitted for the "
        f"seat_tracked->manual transition on {tenant_id}: {events}"
    )
    assert mode_changes[-1]["before"]["mode"] == "seat_tracked"
    assert mode_changes[-1]["after"]["mode"] == "manual"


def test_clearing_a_manual_limit_emits_a_mode_change_audit_event(dynamodb_mock, caplog):
    tenant_id, period = "acme-eng", current_period()
    _seed_row(tenant_id, period, seat_count=2, manual_limit_microusd=500_000_000)

    caplog.set_level(logging.INFO, logger="stratoclave.audit")
    TenantBudgetsRepository().clear_manual_limit(tenant_id=tenant_id, period=period)

    lines = [r.getMessage() for r in caplog.records if r.name == "stratoclave.audit"]
    events = [json.loads(line) for line in lines]
    mode_changes = [e for e in events if e.get("event") == "tenant_pool_mode_changed"
                    and e.get("target_id") == tenant_id]
    assert mode_changes, "no tenant_pool_mode_changed audit event was emitted on clear"
    assert mode_changes[-1]["before"]["mode"] == "manual"
    assert mode_changes[-1]["after"]["mode"] == "seat_tracked"


def test_a_second_manual_set_with_no_mode_change_does_not_emit_the_mode_change_event(
    dynamodb_mock, caplog,
):
    """Two manual writes in a row (already manual, still manual) is a figure
    change, not a mode transition -- it must not be double-counted as one."""
    tenant_id, period = "already-manual-co", current_period()
    _seed_row(tenant_id, period, seat_count=2, manual_limit_microusd=500_000_000)

    caplog.set_level(logging.INFO, logger="stratoclave.audit")
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=tenant_id, period=period, manual_limit_microusd=600_000_000
    )

    lines = [r.getMessage() for r in caplog.records if r.name == "stratoclave.audit"]
    events = [json.loads(line) for line in lines]
    mode_changes = [e for e in events if e.get("event") == "tenant_pool_mode_changed"
                    and e.get("target_id") == tenant_id]
    assert not mode_changes, (
        "a manual->manual figure change (no mode transition) emitted "
        f"tenant_pool_mode_changed anyway: {mode_changes}"
    )
