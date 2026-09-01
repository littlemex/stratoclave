"""R21 (the F1 contract): the mode is a sentence, not a field -- backend
half. R21's own "Verified by": "Unit: the surfaces render it; a transition
between modes emits an audit event." This file covers:

  (a) the admin/team-lead pool-budget response carries a `mode_sentence`
      whose CONTENT names the seat count and rate in the seat-tracked case,
      and the figure in the manual case;
  (b) a transition between modes (seat-tracked -> manual, and manual ->
      seat-tracked) emits a NAMED audit event distinct from the existing
      `tenant_pool_budget_set` (which fires on every admin figure-set,
      transition or not, and so cannot by itself answer "did the MODE
      change", only "did an admin write happen").

Retargeted after reading the independent implementation (`wt-conv-f1`):

  - `pool_summary()`/the response carry NO field named `mode`. The
    repository reports `seat_tracked: bool`; the HTTP response
    (`PoolBudgetResponse`) carries `mode_sentence: str`, built by
    `mvp.admin_tenants._mode_sentence(summary)`. R21's own words are "the
    mode is a SENTENCE, not a field" -- a string-equality check on an enum
    the contract explicitly rejected is not evidence for this id, so this
    file drives the HTTP endpoint and asserts the sentence's CONTENT.
  - the mode-change audit event is emitted by the ROUTE
    (`mvp.admin_tenants.apply_pool_budget_request`, shared by the admin and
    team-lead PUT routes), not by the repository write. A repository method
    has no actor to attribute an audit line to, so driving
    `set_manual_limit`/`clear_manual_limit` directly -- as this file's first
    draft did -- can never observe an event: there is no actor for it to
    name, and an audit event with no actor is not an audit event. This file
    now drives `PUT /api/mvp/admin/tenants/{id}/pool-budget`.
  - the event's `before`/`after` carry `seat_tracked: bool`, not
    `mode: "seat_tracked" | "manual"`.

The frontend half (the sentence + resume action actually rendering) is
covered separately in
frontend/src/pages/admin/AdminTenantDetail.ceiling.test.tsx.

Design note: /Users/akazawt/tmp/stratoneed/change-pipeline/quota-raise-and-archive/design-F1.md
section 6.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dynamo import TenantBudgetsRepository, current_period
from dynamo.tenant_budgets import budget_sk
from dynamo.tenants import TenantsRepository
from mvp.deps import AuthenticatedUser, get_current_user

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


def _admin_actor() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="admin-1", email="admin@example", org_id="default-org",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


def _client(monkeypatch) -> TestClient:
    from mvp import authz
    from mvp.admin_tenants import router

    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _admin_actor
    return TestClient(app)


def _get_pool_budget(client: TestClient, tenant_id: str, period: str) -> dict:
    resp = client.get(f"/api/mvp/admin/tenants/{tenant_id}/pool-budget", params={"period": period})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_seat_tracked_mode_sentence_names_the_seat_count_and_the_rate(monkeypatch, dynamodb_mock):
    tenant_id, period = "seat-tracked-co", current_period()
    TenantsRepository().create(
        tenant_id=tenant_id, name="Seat Tracked Co",
        team_lead_user_id="admin-owned", created_by="admin-1",
    )
    _seed_row(tenant_id, period, seat_count=3)

    body = _get_pool_budget(_client(monkeypatch), tenant_id, period)

    assert body["seat_tracked"] is True
    assert body["manual_limit_microusd"] is None
    sentence = body["mode_sentence"]
    assert "3" in sentence, f"mode_sentence does not name the seat count (3): {sentence!r}"
    # $200/seat -- the rate is named as the resulting dollar figure/entitlement,
    # not necessarily the literal digits "200" (a sentence quotes a dollar
    # amount, not a bare rate), so check the rendered entitlement instead.
    assert "600" in sentence or "$600" in sentence, (
        f"mode_sentence does not name the 3-seat entitlement ($600): {sentence!r}"
    )


def test_manual_mode_sentence_names_the_figure(monkeypatch, dynamodb_mock):
    tenant_id, period = "manual-co", current_period()
    TenantsRepository().create(
        tenant_id=tenant_id, name="Manual Co",
        team_lead_user_id="admin-owned", created_by="admin-1",
    )
    _seed_row(tenant_id, period, seat_count=5, manual_limit_microusd=100_000_000)

    body = _get_pool_budget(_client(monkeypatch), tenant_id, period)

    assert body["seat_tracked"] is False
    assert body["manual_limit_microusd"] == 100_000_000
    sentence = body["mode_sentence"]
    assert "100" in sentence, f"mode_sentence does not name the $100 figure: {sentence!r}"


def test_manual_mode_sentence_at_zero_still_names_a_figure_not_absence(monkeypatch, dynamodb_mock):
    """mode must key off PRESENCE, matching R1's sentinel -- a manual figure
    of exactly 0 is still 'manual', never re-read as seat-tracked."""
    tenant_id, period = "manual-zero-co", current_period()
    TenantsRepository().create(
        tenant_id=tenant_id, name="Manual Zero Co",
        team_lead_user_id="admin-owned", created_by="admin-1",
    )
    _seed_row(tenant_id, period, seat_count=5, manual_limit_microusd=0)

    body = _get_pool_budget(_client(monkeypatch), tenant_id, period)

    assert body["seat_tracked"] is False
    assert body["manual_limit_microusd"] == 0
    sentence = body["mode_sentence"].lower()
    assert "0.00" in sentence or "$0" in sentence, (
        f"mode_sentence for a zero manual figure does not name $0.00: {sentence!r}"
    )


def _audit_mode_change_events(caplog, tenant_id: str) -> list[dict]:
    lines = [r.getMessage() for r in caplog.records if r.name == "stratoclave.audit"]
    events = [json.loads(line) for line in lines]
    return [e for e in events if e.get("event") == "tenant_pool_mode_changed"
            and e.get("target_id") == tenant_id]


def test_setting_a_figure_on_a_seat_tracked_row_emits_a_mode_change_audit_event(
    monkeypatch, dynamodb_mock, caplog,
):
    tenant_id, period = "acme-eng", current_period()
    TenantsRepository().create(
        tenant_id=tenant_id, name="Acme Eng",
        team_lead_user_id="admin-owned", created_by="admin-1",
    )
    _seed_row(tenant_id, period, seat_count=2)
    client = _client(monkeypatch)

    caplog.set_level(logging.INFO, logger="stratoclave.audit")
    resp = client.put(
        f"/api/mvp/admin/tenants/{tenant_id}/pool-budget",
        json={"limit_usd_cents": 50_000, "period": period},  # $500.00
    )
    assert resp.status_code == 200, resp.text

    mode_changes = _audit_mode_change_events(caplog, tenant_id)
    assert mode_changes, (
        f"no tenant_pool_mode_changed audit event was emitted for the "
        f"seat_tracked->manual transition on {tenant_id}"
    )
    assert mode_changes[-1]["before"]["seat_tracked"] is True
    assert mode_changes[-1]["after"]["seat_tracked"] is False


def test_returning_to_seats_emits_a_mode_change_audit_event(monkeypatch, dynamodb_mock, caplog):
    tenant_id, period = "acme-eng", current_period()
    TenantsRepository().create(
        tenant_id=tenant_id, name="Acme Eng",
        team_lead_user_id="admin-owned", created_by="admin-1",
    )
    _seed_row(tenant_id, period, seat_count=2, manual_limit_microusd=500_000_000)
    client = _client(monkeypatch)

    caplog.set_level(logging.INFO, logger="stratoclave.audit")
    resp = client.put(
        f"/api/mvp/admin/tenants/{tenant_id}/pool-budget",
        json={"follow_seats": True, "period": period},
    )
    assert resp.status_code == 200, resp.text

    mode_changes = _audit_mode_change_events(caplog, tenant_id)
    assert mode_changes, "no tenant_pool_mode_changed audit event was emitted on follow_seats"
    assert mode_changes[-1]["before"]["seat_tracked"] is False
    assert mode_changes[-1]["after"]["seat_tracked"] is True


def test_a_second_figure_change_with_no_mode_change_does_not_emit_the_mode_change_event(
    monkeypatch, dynamodb_mock, caplog,
):
    """Two manual writes in a row (already manual, still manual) is a figure
    change, not a mode transition -- it must not be double-counted as one."""
    tenant_id, period = "already-manual-co", current_period()
    TenantsRepository().create(
        tenant_id=tenant_id, name="Already Manual Co",
        team_lead_user_id="admin-owned", created_by="admin-1",
    )
    _seed_row(tenant_id, period, seat_count=2, manual_limit_microusd=500_000_000)
    client = _client(monkeypatch)

    caplog.set_level(logging.INFO, logger="stratoclave.audit")
    resp = client.put(
        f"/api/mvp/admin/tenants/{tenant_id}/pool-budget",
        json={"limit_usd_cents": 60_000, "period": period},  # $600.00, still manual
    )
    assert resp.status_code == 200, resp.text

    mode_changes = _audit_mode_change_events(caplog, tenant_id)
    assert not mode_changes, (
        f"a manual->manual figure change (no mode transition) emitted "
        f"tenant_pool_mode_changed anyway: {mode_changes}"
    )
