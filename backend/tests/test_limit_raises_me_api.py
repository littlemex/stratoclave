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
field, `approved_amount_microusd` vs `amount_microusd`; this file's request
row already used `requested_amount_microusd`/`approved_amount_microusd`
correctly.)
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
    # from mvp.grants import router  -- module does not exist yet (F2 has
    # not landed in this worktree). This import is what actually fails;
    # everything below documents the shape that must exist once it does.
    from mvp.grants import router  # noqa: F401  (expected ImportError)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _requester
    return TestClient(app)


def _seed_decided_request(*, requested_microusd: int, approved_microusd: int) -> str:
    """Seed one decided (approved) limit-raise request for the requester,
    granted for LESS than she asked — the id's own motivating case.

    Seeded through `QuotaEventsRepository` (U2), not a second `mvp`-layer
    repository. `dynamo.quota_events` does not exist in this worktree either
    (F2 has not landed here) — this still fails at import.
    """
    from dynamo.quota_events import QuotaEventsRepository

    repo = QuotaEventsRepository()
    return repo.record(
        user_id="requester-1",
        tenant_id="acme-eng",
        reason="cascade_shortfall",
        comment="need opus for the eval batch",
        requested_amount_microusd=requested_microusd,
        status="approved",
        approved_amount_microusd=approved_microusd,
        expires_at="2026-08-31T23:59:59Z",
        approver_id="lead-1",
    )


def _seed_pending_request(*, requested_microusd: int) -> str:
    from dynamo.quota_events import QuotaEventsRepository

    repo = QuotaEventsRepository()
    return repo.record(
        user_id="requester-1",
        tenant_id="acme-eng",
        reason="cascade_shortfall",
        comment="",
        requested_amount_microusd=requested_microusd,
        status="pending",
    )


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
        decided = next(r for r in body["requests"] if r["status"] == "approved")
        # This is the exact defect: granted LESS than asked must be visible
        # as its OWN number, not as the requested amount and not as a bare
        # status string.
        assert decided["approved_amount_microusd"] == 50_000_000
        assert decided["approved_amount_microusd"] != decided["requested_amount_microusd"]
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
        pending = next(r for r in body["requests"] if r["status"] == "pending")
        assert pending["approved_amount_microusd"] is None
        assert pending["expires_at"] is None
        assert pending["approver_id"] is None
