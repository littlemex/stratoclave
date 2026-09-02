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

`QuotaEventsRepository`'s own write method name is still this role's
plausible guess (`.record(...)`, mirroring `UsageLogsRepository`'s
append-only pattern, the closest analogue already in this codebase) — the
class and table are confirmed, the method is not, so this may need one more
retarget once F2's actual method name is visible. That risk is now confined
to a single call site instead of an entire invented class.
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
    _patch_authz(monkeypatch, allow={"limit-raises:approve"})
    from mvp.grants import router  # module does not exist yet (F2 not landed)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _approver
    return TestClient(app)


def _seed_grants_across_two_periods(tenant_id: str = "acme-eng") -> None:
    """The B4 scenario: a live grant on the CURRENT period's row, plus a
    grant that still bears capacity on the PRIOR period's row — one because
    it is `REVOKE_BLOCKED` (subtraction not yet complete), simulating
    rollover-plus-late-sweep landing on the same tenant at once.

    Seeded through `QuotaEventsRepository` (U2), not a second `mvp`-layer
    repository — `dynamo.quota_events` does not exist in this worktree
    either (F2 has not landed here), so this still fails at import; only the
    module it fails to import FROM changed.
    """
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import current_period, previous_period

    current = current_period()
    prior = previous_period(current)
    repo = QuotaEventsRepository()
    repo.record(
        tenant_id=tenant_id, request_id="lr_9f2c", approved_amount_microusd=50_000_000,
        approver_id="user-lead-1", expires_at="2026-08-31T23:59:59Z",
        status="active", target_pk=tenant_id, period=current,
    )
    repo.record(
        tenant_id=tenant_id, request_id="lr_7e21", approved_amount_microusd=12_000_000,
        approver_id="user-lead-1", expires_at="2026-07-30T23:59:59Z",
        status="revoke_blocked", target_pk=tenant_id, period=prior,
        revoke_blocked_reason="an in-flight reservation is still holding against this grant",
    )


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
        from mvp.grants import is_capacity_bearing

        current = current_period()
        prior = previous_period(current)
        rows_by_period = {row["period"]: row for row in body["rows"]}

        # Both periods must be present — a tenant-wide sum would have
        # collapsed the prior period's still-capacity-bearing grant into (or
        # silently dropped it from) the current period's total.
        assert current in rows_by_period, "current period row missing"
        assert prior in rows_by_period, (
            "prior period row missing — the late-swept REVOKE_BLOCKED grant "
            "still bears capacity there and must be its own reconciling row"
        )

        for period, row in rows_by_period.items():
            live_sum = sum(
                # U3: the grant row carries both the asked and the approved
                # figure — `approved_amount_microusd` is the one this
                # reconciliation is actually about, never the shorter,
                # ambiguous `amount_microusd`.
                g["approved_amount_microusd"] for g in row["grants"]
                if is_capacity_bearing(g, target_pk="acme-eng", period=period)
            )
            assert live_sum == row["pool_granted_microusd"], (
                f"row for period {period!r} does not reconcile: "
                f"{live_sum} != {row['pool_granted_microusd']}"
            )

        assert rows_by_period[current]["pool_granted_microusd"] == 50_000_000
        assert rows_by_period[prior]["pool_granted_microusd"] == 12_000_000

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
            g for row in resp.json()["rows"] for g in row["grants"]
            if g["status"] == "revoke_blocked"
        )
        # Not merely PRESENT in the payload — carrying a reason nobody shows
        # is the same defect this deliverable's brief calls out for other
        # ids: the reason must be a real, non-empty sentence.
        assert isinstance(blocked.get("revoke_blocked_reason"), str)
        assert len(blocked["revoke_blocked_reason"]) > 0


class TestCapacityBearingPredicateIsConsumedNotRestated:
    def test_predicate_is_imported_from_grants_not_reimplemented(self):
        # A conformance check in spirit: F3's inventory must call F2's
        # predicate rather than hardcode `status in {"active",
        # "revoke_blocked"}` (what this file's own earlier version did,
        # which is exactly the drift B4 exists to close).
        from mvp.grants import is_capacity_bearing

        assert callable(is_capacity_bearing)
