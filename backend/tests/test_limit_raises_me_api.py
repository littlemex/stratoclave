"""F3 / R24 — `GET /me/limit-raises` joins the approved amount, the expiry and
the approver.

Contract: `change-pipeline/quota-raise-and-archive/CONTRACT-F3-surfaces.md`, id R24.

  "A requester who asked $200 and was granted $50 currently sees only
  `APPROVED`, plans against her own number and hits the wall at his. Unit: a
  decided request carries them, a pending one carries none."

Seam amendment correction (the integration owner's seam notes, §S7, outside this repository): this endpoint is **F2-owned**, not
F1's — `GET /me/limit-raises` and `GET /admin/limit-grants` are both named in
S7 as F2's. This file used to import from `mvp.quota_raises`; every import
below now targets `mvp.grants` instead. F3's job is narrower than the
original version of this file assumed: F2 owns and tests the join itself
(the amount/expiry/approver are grant facts); F3 renders what F2 returns.
What remains F3's to verify here is the field-shape F3's frontend depends on
— effectively a conformance check — not a re-test of F2's join logic (which
would be exactly the "two independent statements of one shape drift" the
seam amendments exist to close).

Union amendment corrections (integration review of all four test suites,
`## Union amendments` in the F3 contract):

  - **U1/U2 — no `LimitRaisesRepository` in `mvp`.** This file used to
    import `mvp.grants.LimitRaisesRepository` to seed request rows — a name
    touched by no F2 test, refused outright. Request rows, like grant rows,
    live in the `quota-events` table; seeding now goes through
    `dynamo.quota_events.QuotaEventsRepository`, the one already-established
    repository for that table, rather than a second `mvp`-layer path to it.
    `router` (from `mvp.grants`) is confirmed and unchanged.

Neither `mvp.grants.router` nor `dynamo.quota_events.QuotaEventsRepository`
exist in this worktree (F2 has not landed here), so every test below still
fails at collection with `ModuleNotFoundError` — the same "surface absent"
reason as before; only the module each import targets changed.

Contract correction: the approver field is `approver_id` — a stable user id,
resolved to a display name by the console on demand — never an email address
carried on the wire. (Unaffected by U3 — U3 corrects the GRANT row's amount
field, `approved_amount_microusd` vs `amount_microusd`.)

**Further correction (convergence).** This file's own claim above --
"this file's request row already used `requested_amount_microusd` ...
correctly" -- was itself wrong, unverified against real code. F2's own
contract amendment U7 pins the REQUEST row's asked-amount field as
`asked_amount_microusd` ("the request body's field names are pinned here
because the journey layer had to guess them and guessed from the
`_microusd` convention ... if your implementation chose differently, these
win"), and the shipped `mvp.grants._request_public` emits exactly that
name. Fixed below.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.deps import AuthenticatedUser, get_current_user


def _requester() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="requester-1",
        email="requester@example",
        org_id="acme-eng",
        roles=["user"],
        raw_claims={},
        auth_kind="cognito",
    )


def _client(monkeypatch) -> TestClient:
    # `require_permission("limits:raise-self")` reads the REAL permissions
    # table (`dynamo.permissions.PermissionsRepository`, seeded in
    # production from `backend/permissions.json` at app startup) -- the
    # shared `dynamodb_mock` fixture (`backend/tests/conftest.py`) does not
    # create that table at all, so an un-patched real check 500s with
    # `ResourceNotFoundException` on every request here, unrelated to
    # anything this file is actually testing. Patched the same way
    # `test_grants_inventory_api.py` and `test_admin_pool_budget.py`
    # already do, rather than adding a table + seed step this file has no
    # need to own.
    from mvp import authz

    def _fake_user_has_permission(user, scope: str) -> bool:
        return scope == "limits:raise-self"

    monkeypatch.setattr(authz, "user_has_permission", _fake_user_has_permission)

    from mvp.grants import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _requester
    return TestClient(app)


def _seed_decided_request(*, requested_microusd: int, approved_microusd: int) -> str:
    """Seed one decided (approved) limit-raise request for the requester,
    granted for LESS than she asked — the id's own motivating case.

    **Convergence correction.** `QuotaEventsRepository` has no `.record(...)`
    method (this role's own guess, flagged as such, did not survive contact
    with the real code). A request is created through the real primitives
    the submit/approve flow itself uses: `put_request(...)` (PENDING,
    `dynamo/quota_events.py`) then `decide_request_txn_item(...)` inside a
    `transact_write` to move it to `STATUS_APPROVED` — the same pair
    `mvp.grants.approve_limit_raise` commits in production.
    """
    from dynamo.quota_events import STATUS_APPROVED, QuotaEventsRepository

    repo = QuotaEventsRepository()
    request_id = "lr_9f2c"
    repo.put_request(
        request_id=request_id, tenant_id="acme-eng", user_id="requester-1",
        asked_amount_microusd=requested_microusd, reason_code="cascade_shortfall",
        comment="need opus for the eval batch", limit_kind="tenant_dollar_pool",
    )
    repo.transact_write([
        repo.decide_request_txn_item(
            request_id=request_id, to_status=STATUS_APPROVED,
            decided_by="lead-1", decided_at="2026-08-30T09:02:00Z",
            read_revision=1, approved_amount_microusd=approved_microusd,
            grant_id="gr_9f2c", expires_at_epoch=1_787_990_399,  # 2026-08-31T23:59:59Z
        ),
    ])
    return request_id


def _seed_pending_request(*, requested_microusd: int) -> str:
    from dynamo.quota_events import QuotaEventsRepository

    repo = QuotaEventsRepository()
    request_id = "lr_a013"
    repo.put_request(
        request_id=request_id, tenant_id="acme-eng", user_id="requester-1",
        asked_amount_microusd=requested_microusd, reason_code="cascade_shortfall",
        comment=None, limit_kind="tenant_dollar_pool",
    )
    return request_id


class TestDecidedRequestJoin:
    def test_decided_request_carries_approved_amount_expiry_and_approver(
        self, monkeypatch, dynamodb_mock
    ):
        _seed_decided_request(
            requested_microusd=200_000_000, approved_microusd=50_000_000,
        )
        client = _client(monkeypatch)
        resp = client.get("/api/mvp/me/limit-raises")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        decided = next(r for r in body["requests"] if r["status"] == "APPROVED")
        # This is the exact defect: granted LESS than asked must be visible
        # as its OWN number, not as the requested amount and not as a bare
        # status string.
        assert decided["approved_amount_microusd"] == 50_000_000
        assert decided["approved_amount_microusd"] != decided["asked_amount_microusd"]
        assert decided["expires_at"] is not None
        # Contract correction: a stable id, never an address — the console
        # resolves it to a display name on demand, but the wire field is
        # never an email.
        assert decided["approver_id"] == "lead-1"
        assert "approver_email" not in decided

    def test_pending_request_carries_none_of_the_decision_fields(
        self, monkeypatch, dynamodb_mock
    ):
        _seed_pending_request(requested_microusd=12_000_000)
        client = _client(monkeypatch)
        resp = client.get("/api/mvp/me/limit-raises")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        pending = next(r for r in body["requests"] if r["status"] == "PENDING")
        assert pending["approved_amount_microusd"] is None
        assert pending["expires_at"] is None
        assert pending["approver_id"] is None
