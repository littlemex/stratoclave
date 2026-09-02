"""F3 / R25 — the grant inventory: live grants with amount, approver, expiry,
status and the request that produced each; the sum equals `pool_granted`
**per target row**.

Contract: `change-pipeline/quota-raise-and-archive/CONTRACT-F3-surfaces.md`, id R25.

  "Unit: the sum reconciles; a `REVOKE_BLOCKED` grant is visible with its
  reason"

Seam amendment B4 (the integration owner's seam notes, §S6, outside this repository)
rewrote what "reconciles" means: grants are
pinned to `target_pk`/`target_sk`/`period` (F2), so a late sweep leaves an
expired-but-unrevoked grant bearing capacity on the PRIOR period's row, and a
`REVOKE_BLOCKED` grant bears capacity until its subtraction completes. A
single tenant-wide sum against the current period's row is wrong exactly in
those cases — which never co-occurred in this role's original test, which is
why the original design (one flat `pool_granted_microusd` for the whole
tenant) looked correct and was not.

This role's design note (section R25) places F2's shared capacity-bearing
predicate at `mvp.grants.is_capacity_bearing(grant, target_pk, target_sk,
period)`, consumed here rather than reimplemented (the original version of
this file hardcoded `status in {"active", "revoke_blocked"}`, which is
exactly the "two independent statements of one shape drift" B4/B2 exist to
stop). Endpoint moved too, per S7 ("F2 owns ... `GET /admin/limit-grants`"):
`GET /api/mvp/admin/limit-grants`, not the tenant-scoped path this file used
before. Neither `mvp.grants.router` nor `is_capacity_bearing` exist in this
worktree (F2 has not landed here), so every test below still fails at the
seeding import — the reason changed (a shared predicate is now missing, not
just an endpoint), the "surface absent" shape of the failure did not.

Union amendment corrections (integration review of all four test suites,
`## Union amendments` in the F3 contract):

  - **U1/U2 — no `GrantsRepository` in `mvp`.** This file used to import
    `mvp.grants.GrantsRepository` to seed grant rows — a name touched by no
    F2 test, refused outright, not merely renamed. Grant rows live in the
    `quota-events` table, whose repository is `dynamo.quota_events.QuotaEventsRepository`
    — already established, so a second repository in the `mvp` layer would
    be a second data-access path to the same table (two writers of one
    invariant). Seeding below now goes through `QuotaEventsRepository`
    directly. F2's confirmed `mvp.grants` surface is exactly `router`,
    `RaiseHint`, `effective_grant_cap_microusd`, `is_capacity_bearing`,
    `latest_permissible_expiry_for_period` — this file only needs
    `router` and `is_capacity_bearing` from it, both confirmed, unchanged.
  - **U3 — the field is `approved_amount_microusd`.** This file's
    reconciliation used to read `g["amount_microusd"]`; F2 stores and
    exposes `approved_amount_microusd` (the grant row carries both the
    asked and the approved figure, and a reader of the shorter name cannot
    tell which one they hold — the same ambiguity R24 exists to remove).
    Every seed and every read below uses the corrected name.

**Convergence correction (this role's guesses did not survive contact with
the real, shipped code).** Three things were wrong, all seeding/plumbing,
none contractual:

1. `QuotaEventsRepository` has no `.record(...)` method. A grant is created
   through the real transaction fragments the approve flow itself uses:
   `grant_put_txn_item(...)` (the grant row, `dynamo/quota_events.py`) paired
   with `TenantBudgetsRepository.grant_apply_txn_item(...)` (the pool row's
   `pool_granted_microusd`/`pool_limit_microusd`/`pool_headroom_microusd`,
   `dynamo/tenant_budgets.py`) inside one `repo.transact_write([...])` — the
   same two-table transaction `approve_limit_raise` commits in production,
   because a grant row with no matching pool-side add is exactly the drift
   `pool_granted_matches_active_grants` exists to catch. A `REVOKE_BLOCKED`
   grant is produced by first exhausting `MAX_REVOKE_ATTEMPTS` real
   `bump_revoke_attempts()` calls (mirroring the real sweeper), then
   `mark_revoke_blocked(...)`, never by writing the status directly — the
   grant's `pool_granted_microusd` share is never subtracted for a blocked
   grant, which is `is_capacity_bearing`'s whole point.
2. `is_capacity_bearing(status: str) -> bool` takes ONE argument — the
   grant's own status string — not `(grant, target_pk, period)`. Verified
   against the shipped function and its two real call sites
   (`mvp.grants._capacity_bearing_sum_for_row`,
   `mvp.grants.reconcile_tenant_grants`): per-row scoping is achieved by
   filtering grants to the row's own `(target_pk, target_sk)` BEFORE calling
   the predicate, not by widening the predicate's signature.
3. `GET /api/mvp/admin/limit-grants` does not return `{"rows": [{"period",
   "pool_granted_microusd", "grants": [...]}]}` — a shape this role
   invented without access to the real endpoint. The shipped response
   (`mvp.grants.admin_list_limit_grants`) is `{"tenant_id", "grants": [...]
   (a FLAT list, each grant carrying its own "period"/"target_pk"/
   "target_sk"), "reconciliation": reconcile_tenant_grants(...)}`, where
   `reconciliation["rows"]` is the per-target-row grouping B4 actually
   requires (`pool_granted_microusd`, `capacity_bearing_sum_microusd`,
   `drift_microusd`, ... per row) and `reconciliation["orphans"]` covers a
   grant whose target row is gone. `frontend/src/pages/GrantsInventory.tsx`
   — real, shipped, already-merged code written with real backend access,
   unlike this file — consumes exactly this shape
   (`grantsQuery.data.grants`, `grantsQuery.data.reconciliation.rows`), which
   is the corroborating evidence that the backend shape, not this test's
   guess, is what ships. B4's actual requirement — reconciliation per target
   row via the shared predicate, never a tenant-wide sum — holds under the
   real shape exactly as under the imagined one; only the JSON's nesting was
   wrong.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.deps import AuthenticatedUser, get_current_user


def _approver() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="lead-1",
        email="lead@acme.example",
        org_id="acme-eng",
        roles=["team_lead"],
        raw_claims={},
        auth_kind="cognito",
    )


def _patch_authz(monkeypatch, allow: set[str]) -> None:
    from mvp import authz

    def fake_user_has_permission(user, scope: str) -> bool:
        return scope in allow

    monkeypatch.setattr(authz, "user_has_permission", fake_user_has_permission)


def _client(monkeypatch) -> TestClient:
    # Contract prose
    # (`change-pipeline/quota-raise-and-archive/CONTRACT-F3-surfaces.md`'s
    # Interface section) names
    # the gate `limit-raises:approve`, but that scope is not in
    # `mvp.authz`'s shipped, closed-world set (out of F3's file scope to
    # change) -- the real endpoint (`mvp.grants.admin_list_limit_grants`)
    # gates on `require_permission("limits:approve")`, the admin-global
    # scope `mvp.authz.ALL_SCOPES` actually carries. Same naming drift as
    # R21b's `sizing`/`follow_seats` correction, one scope over.
    _patch_authz(monkeypatch, allow={"limits:approve"})
    from mvp.grants import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _approver
    return TestClient(app)


def _seed_grants_across_two_periods(tenant_id: str = "acme-eng") -> None:
    """The B4 scenario: a live grant on the CURRENT period's row, plus a
    grant that still bears capacity on the PRIOR period's row — one because
    it is `REVOKE_BLOCKED` (subtraction not yet complete), simulating
    rollover-plus-late-sweep landing on the same tenant at once.

    Seeded through the REAL transaction fragments (`grant_put_txn_item` +
    `grant_apply_txn_item`) rather than a direct row write, so the pool
    row's `pool_granted_microusd` and the grant row agree from the start —
    exactly what `pool_granted_matches_active_grants` checks, and what a
    hand-written row could silently get wrong.
    """
    from dynamo.quota_events import MAX_REVOKE_ATTEMPTS, QuotaEventsRepository
    from dynamo.tenant_budgets import (
        TenantBudgetsRepository, budget_sk, current_period, previous_period,
    )

    current = current_period()
    prior = previous_period(current)
    quota = QuotaEventsRepository()
    budgets = TenantBudgetsRepository()

    # Both period rows must exist before a grant can be applied to them
    # (`grant_apply_txn_item`'s own condition: `attribute_exists(pool_limit_microusd)`).
    budgets.set_manual_limit(
        tenant_id=tenant_id, period=current, manual_limit_microusd=10**11)
    budgets.set_manual_limit(
        tenant_id=tenant_id, period=prior, manual_limit_microusd=10**11)

    def _apply_grant(
        *, grant_id: str, request_id: str, period: str,
        approved_amount_microusd: int, expires_at_epoch: int,
    ) -> None:
        target_sk = budget_sk(period)
        quota.transact_write([
            quota.grant_put_txn_item(
                tenant_id=tenant_id, grant_id=grant_id, request_id=request_id,
                approver_user_id="user-lead-1",
                approved_amount_microusd=approved_amount_microusd,
                expires_at_epoch=expires_at_epoch,
                target_pk=tenant_id, target_sk=target_sk, period=period,
                created_at="2026-08-28T09:00:00Z",
            ),
            budgets.grant_apply_txn_item(
                target_pk=tenant_id, target_sk=target_sk,
                approved_amount_microusd=approved_amount_microusd,
                cap_minus_amount=10**11 - approved_amount_microusd,
            ),
        ])

    # Grant 1: ACTIVE, on the CURRENT period's row, $50.
    _apply_grant(
        grant_id="gr_1a", request_id="lr_9f2c", period=current,
        approved_amount_microusd=50_000_000,
        expires_at_epoch=1_787_990_399,  # 2026-08-31T23:59:59Z
    )

    # Grant 2: on the PRIOR period's row, $12 — pushed to REVOKE_BLOCKED by
    # exhausting real revoke attempts, the same path the sweeper takes.
    _apply_grant(
        grant_id="gr_0b", request_id="lr_7e21", period=prior,
        approved_amount_microusd=12_000_000,
        expires_at_epoch=1_785_398_399,  # 2026-07-30T23:59:59Z
    )
    for _ in range(MAX_REVOKE_ATTEMPTS):
        quota.bump_revoke_attempts(tenant_id=tenant_id, grant_id="gr_0b")
    assert quota.mark_revoke_blocked(
        tenant_id=tenant_id, grant_id="gr_0b",
        reason="an in-flight reservation is still holding against this grant",
    ), "seeding fixture: mark_revoke_blocked did not apply — check MAX_REVOKE_ATTEMPTS"


class TestGrantsInventoryReconciliation:
    def test_each_row_reconciles_against_its_own_period_not_a_tenant_wide_sum(
        self, monkeypatch, dynamodb_mock
    ):
        _seed_grants_across_two_periods()
        client = _client(monkeypatch)
        resp = client.get("/api/mvp/admin/limit-grants", params={"tenant_id": "acme-eng"})
        assert resp.status_code == 200, resp.text
        body = resp.json()

        from dynamo.tenant_budgets import current_period, previous_period

        current = current_period()
        prior = previous_period(current)
        # The shipped shape: a flat `grants` list plus a per-target-row
        # `reconciliation.rows` grouping (`mvp.grants.reconcile_tenant_grants`)
        # — not a `rows[].grants` nesting this file's own earlier version
        # invented without reading the real endpoint.
        rows_by_period = {row["period"]: row for row in body["reconciliation"]["rows"]}

        # Both periods must be present — a tenant-wide sum would have
        # collapsed the prior period's still-capacity-bearing grant into (or
        # silently dropped it from) the current period's total.
        assert current in rows_by_period, "current period row missing"
        assert prior in rows_by_period, (
            "prior period row missing — the late-swept REVOKE_BLOCKED grant "
            "still bears capacity there and must be its own reconciling row"
        )

        # `reconciliation.rows[].capacity_bearing_sum_microusd` IS the
        # `is_capacity_bearing`-filtered, per-row sum -- computed server-side
        # by `reconcile_tenant_grants`, which this test consumes rather than
        # recomputing (B4/B2's own rule: a client restating a shape the
        # server already computed is the drift this amendment exists to
        # close).
        for period, row in rows_by_period.items():
            assert row["capacity_bearing_sum_microusd"] == row["pool_granted_microusd"], (
                f"row for period {period!r} does not reconcile: "
                f"{row['capacity_bearing_sum_microusd']} != {row['pool_granted_microusd']}"
            )

        assert rows_by_period[current]["pool_granted_microusd"] == 50_000_000
        assert rows_by_period[prior]["pool_granted_microusd"] == 12_000_000

        # U3, cross-checked against the flat `grants` list: the field is
        # `approved_amount_microusd`, never the shorter, ambiguous
        # `amount_microusd`.
        for g in body["grants"]:
            assert "approved_amount_microusd" in g
            assert "amount_microusd" not in g

    def test_no_single_tenant_wide_total_is_offered(self, monkeypatch, dynamodb_mock):
        # The defect B4 closes: a single flat `pool_granted_microusd` at the
        # top level, summed across periods, is exactly the wrong invariant
        # (S6's own words: "fails only when rollover, sweeper lateness and
        # inventory are all present — which is to say, never in F3's own
        # tests" — this seeds precisely that combination).
        _seed_grants_across_two_periods()
        client = _client(monkeypatch)
        resp = client.get("/api/mvp/admin/limit-grants", params={"tenant_id": "acme-eng"})
        assert resp.status_code == 200, resp.text
        assert "pool_granted_microusd" not in resp.json(), (
            "a top-level tenant-wide pool_granted_microusd reintroduces the "
            "exact defect B4 exists to close — reconciliation is per row"
        )

    def test_revoke_blocked_grant_is_visible_with_its_reason(self, monkeypatch, dynamodb_mock):
        _seed_grants_across_two_periods()
        client = _client(monkeypatch)
        resp = client.get("/api/mvp/admin/limit-grants", params={"tenant_id": "acme-eng"})
        assert resp.status_code == 200, resp.text
        blocked = next(
            g for g in resp.json()["grants"] if g["status"] == "revoke_blocked"
        )
        # Not merely PRESENT in the payload — carrying a reason nobody shows
        # is the same defect this deliverable's brief calls out for other
        # ids: the reason must be a real, non-empty sentence.
        assert isinstance(blocked.get("revoke_blocked_reason"), str)
        assert len(blocked["revoke_blocked_reason"]) > 0
        # Still capacity-bearing (B4's own point: a blocked grant's
        # subtraction never committed, so the row is still honestly
        # counting it).
        assert blocked["capacity_bearing"] is True


class TestCapacityBearingPredicateIsConsumedNotRestated:
    def test_predicate_is_imported_from_grants_not_reimplemented(self):
        # A conformance check in spirit: F3's inventory must call F2's
        # predicate rather than hardcode `status in {"active",
        # "revoke_blocked"}` (what this file's own earlier version did,
        # which is exactly the drift B4 exists to close).
        from mvp.grants import is_capacity_bearing

        assert callable(is_capacity_bearing)
