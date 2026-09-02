"""Persona journey: the tenant administrator who decides, and lives with it a
week later.

These are NOT requirement tests. Each walks a sequence he walks and asks
whether the ceiling ends up where he intended and whether the numbers he
decided against were true when he read them. Every step is satisfiable by unit
tests that pass in isolation while his ceiling still lands wrong a week later —
that interval is what these tests occupy.

Drives the REAL admin surfaces (tenant creation, `PUT/GET
/admin/tenants/{id}/pool-budget`, the approval and revoke endpoints) and the
REAL credit pipeline on moto DynamoDB, so every figure asserted is the figure
the reserve path actually checks — never a figure a response merely reports.

The journeys, in the personas' misled-first order:

  E. He raises a figure while a grant is live, the grant ends, and the carried
     baseline must still be the figure he typed.
  F. He approves into a suspended pool, and the grant delivers nothing while
     ticking toward expiry and consuming the tenant's cap.
  G. Headroom is negative and every surface he reads clamps it to zero, so he
     grants $10 into a $30 hole and the grant visibly does nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from dynamo.tenant_budgets import TenantBudgetsRepository, seat_monthly_usd
from dynamo.user_tenants import UserTenantsRepository
from mvp import authz
from mvp._pipeline import reserve_credit, settle_reservation_and_log
from mvp.deps import AuthenticatedUser, get_current_user

HER = "engineer-2"
HIM = "approver-2"
_MICRO_PER_USD = 1_000_000


@dataclass
class _PipelineUser:
    """The shape `reserve_credit`/`settle_reservation_and_log` take."""

    user_id: str
    org_id: str
    email: str = "her@journey"


class _Seat:
    """Whose seat the client is in. He sets ceilings and approves; she spends.

    A journey has two actors and one process, so the identity has to be
    switchable mid-test rather than fixed at client construction.
    """

    def __init__(self) -> None:
        self.user_id = HIM
        self.email = "him@journey"
        self.org_id = "pending"

    def take(self, user_id: str, email: str) -> None:
        self.user_id = user_id
        self.email = email

    def current(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=self.user_id, email=self.email, org_id=self.org_id,
            roles=["admin"], raw_claims={}, auth_kind="cognito",
        )


def _journey_client(monkeypatch) -> tuple[TestClient, _Seat]:
    """Every router this journey crosses.

    `mvp.grants.router` is the one export F2's contract pins (U1). There is
    no separate `mvp.quota_raises` module -- both the request and approval
    sides are mounted on this one router.
    """
    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)

    import mvp.grants as grants_mod
    from mvp.admin_tenants import router as admin_tenants_router

    app = FastAPI()
    app.include_router(admin_tenants_router)
    app.include_router(grants_mod.router)

    seat = _Seat()
    app.dependency_overrides[get_current_user] = seat.current
    return TestClient(app), seat


def _seed_seat_tracked_tenant(monkeypatch, client: TestClient, seat: _Seat,
                              *, seats: int) -> tuple[str, str]:
    """A tenant whose ceiling follows its membership, created the way tenants
    are actually created.

    Provisioned through `POST /admin/tenants` rather than by writing a pool row,
    because the seat-tracked state is a property of how the row was made (no
    figure was ever typed on it) and a hand-written row is a row no seat writer
    has ever moved. The seats then arrive one membership at a time, which is the
    only way a real ceiling grows.
    """
    import dynamo.tenant_budgets as _tb
    import mvp._pipeline as _pl

    period = _tb.current_period()
    monkeypatch.setattr(_pl, "current_period", lambda: period)

    created = client.post(
        "/api/mvp/admin/tenants",
        json={"name": "Admin Journey", "team_lead_user_id": "admin-owned"},
    )
    assert created.status_code == 201, created.text
    tenant_id = created.json()["tenant_id"]
    seat.org_id = tenant_id

    for n in range(seats):
        UserTenantsRepository().ensure(
            user_id=f"seat-{n}", tenant_id=tenant_id, role="user",
            total_credit=1_000_000_000,
        )
    return period, tenant_id


def _summary(tenant_id: str, period: str) -> dict[str, int]:
    s = TenantBudgetsRepository().pool_summary(tenant_id, period)
    assert s is not None, f"no pool row for {tenant_id}/{period}"
    return s


def _limit(tenant_id: str, period: str) -> int:
    """The ceiling the reserve path checks — not the ceiling a response says."""
    return _summary(tenant_id, period)["pool_limit_microusd"]


def _headroom(tenant_id: str, period: str) -> int:
    """Signed. Clamping this is the whole subject of journey G."""
    return _summary(tenant_id, period)["pool_headroom_microusd"]


def _identity_holds(tenant_id: str, period: str) -> bool:
    s = _summary(tenant_id, period)
    return s["pool_headroom_microusd"] == (
        s["pool_limit_microusd"] - s["pool_reserved_microusd"]
        - s["pool_settled_microusd"]
    )


def _in(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _file(client: TestClient, tenant_id: str, *, asked: int, token: str):
    """File.

    **Convergence correction.** `SubmitLimitRaiseRequest` (`mvp/grants.py`)
    has no `tenant_id` field (`extra="forbid"`; the tenant is always the
    caller's own session), its real wall name is `tenant_dollar_pool`
    (`mvp.grants.POOL_WALL`), and "deadline" is not one of the four real
    `RAISE_REASON_CODES`.
    """
    del tenant_id  # kept for call-site symmetry; not a body field
    return client.post(
        "/api/mvp/me/limit-raises",
        json={
            "limit_kind": "tenant_dollar_pool",
            "asked_amount_microusd": asked,
            "reason_code": "usage_spike",
            "comment": "a week of headroom",
            "client_token": token,
        },
    )


def _approve(client: TestClient, request_id: str, *, approved: int,
             expires_in_minutes: int = 60 * 24 * 7, comment: str = "approved"):
    """`ApproveLimitRaiseRequest.expires_at` is `int` (epoch seconds,
    `mvp/grants.py`), not an ISO string."""
    return client.post(
        f"/api/mvp/admin/limit-raises/{request_id}/approve",
        json={
            "approved_amount_microusd": approved,
            "expires_at": int(
                datetime.fromisoformat(_in(expires_in_minutes)).timestamp()),
            "decision_comment": comment,
        },
    )


def _live_grant_id(client: TestClient, tenant_id: str) -> str:
    listed = client.get(f"/api/mvp/admin/limit-grants?tenant_id={tenant_id}")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    grants = payload["grants"] if isinstance(payload, dict) else payload
    # `_grant_public` (`mvp/grants.py`) lowercases the wire `status` (the
    # codebase's own convention -- the pool row's own `status` is lowercase
    # from the start); the stored, internal value is uppercase `ACTIVE`.
    live = [g for g in grants if g.get("status") == "active"]
    assert live, f"an approved grant that is in no inventory: {payload}"
    return live[0]["grant_id"]


def _whole_raise(client: TestClient, seat: _Seat, tenant_id: str, *,
                 asked: int, approved: int, token: str = "tok") -> str:
    """One whole raise, filed by her and approved by him, returning the grant
    id. Three journeys need a live grant and none of them is about how it got
    there."""
    seat.take(HER, "her@journey")
    filed = _file(client, tenant_id, asked=asked, token=token)
    assert filed.status_code == 201, filed.text
    request_id = filed.json()["request_id"]
    seat.take(HIM, "him@journey")
    decided = _approve(client, request_id, approved=approved)
    assert decided.status_code == 200, decided.text
    return _live_grant_id(client, tenant_id)


def _row_for(payload: Any, request_id: str) -> dict[str, Any]:
    rows = payload["requests"] if isinstance(payload, dict) else payload
    for row in rows:
        if row.get("request_id") == request_id:
            return row
    raise AssertionError(f"{request_id} missing from his queue: {payload}")


# ---------------------------------------------------------------------------
# Journey E — set, expire, and the figure the next period inherits
# ---------------------------------------------------------------------------


def test_journey_setting_a_figure_while_a_grant_is_live_lands_where_he_meant(
    monkeypatch, dynamodb_mock
):
    """Persona 2 questions 4 and 5 — second on his misled-first ranking.

    Ten seats is a seat-tracked ceiling. A $50 grant is live, so every screen
    shows the ceiling plus $50. He raises the tenant to $5,000. A week later the
    grant ends. What he would be told wrongly: that the figure he typed is the
    figure the ceiling will hold. If the setter computes its delta against the
    grant-inflated limit rather than against the baseline, the revoke subtracts
    the grant a second time and the ceiling lands $50 BELOW what he typed — a
    silent under-admission a week after the decision, with nothing connecting
    the two events, and a carried baseline that reproduces the error every month.

    Walks: seat-tracked -> grant $50 -> he re-types the inflated figure (must be
    refused, because honouring it folds granted money into the baseline
    permanently) -> he sets $5,000 -> the grant ends -> the ceiling is $5,000
    and the carried input is $5,000 too.
    """
    client, seat = _journey_client(monkeypatch)
    period, tenant_id = _seed_seat_tracked_tenant(
        monkeypatch, client, seat, seats=10
    )
    base = f"/api/mvp/admin/tenants/{tenant_id}/pool-budget"

    # He starts seat-tracked: the ceiling he "manages" without typing anything.
    seat_term = 10 * seat_monthly_usd() * _MICRO_PER_USD
    assert _limit(tenant_id, period) == seat_term, (
        "the seat-tracked baseline must be ten seats of money before any grant"
    )

    # She asks, he approves $50. Every surface now shows the inflated figure.
    grant_id = _whole_raise(client, seat, tenant_id, asked=100_000_000,
                            approved=50_000_000, token="tok-e")
    assert _limit(tenant_id, period) == seat_term + 50_000_000

    # The trap he is most likely to fall into: reading the number displayed as
    # the limit and typing it back. That figure contains the grant, so honouring
    # it double-counts $50 and folds granted money into the baseline for good.
    inflated_cents = (seat_term + 50_000_000) // 10_000
    retyped = client.put(base, json={"limit_usd_cents": inflated_cents,
                                     "period": period})
    assert retyped.status_code == 409, retyped.text
    detail = retyped.json()["detail"]
    # `apply_pool_budget_request` (`mvp/admin_tenants.py`) raises this 409
    # with the machine-readable code in `"type"`, not `"reason"`.
    assert detail["type"] == "figure_includes_active_grant"
    # The refusal has to decompose the number, or he cannot tell what to type
    # instead and the only escape is retyping the same figure.
    assert detail.get("pool_granted_microusd") == 50_000_000
    assert detail.get("baseline_microusd") == seat_term

    # He sets the figure he actually means. The grant is untouched.
    raised = client.put(base, json={"limit_usd_cents": 500_000, "period": period})
    assert raised.status_code == 200, raised.text
    assert _limit(tenant_id, period) == 5_050_000_000, (
        "his $5,000 must be a delta against the baseline, not against the "
        f"grant-inflated limit: got {_limit(tenant_id, period)}"
    )

    # A week later the grant ends. This is the step that lands wrong.
    # `POST .../revoke` requires `tenant_id` as a query param (the grant
    # row's own partition key) AND a body (`RevokeGrantRequest`, optional
    # `reason`) -- neither is optional at the transport level.
    ended = client.post(
        f"/api/mvp/admin/limit-grants/{grant_id}/revoke",
        params={"tenant_id": tenant_id}, json={},
    )
    assert ended.status_code == 200, ended.text
    assert _limit(tenant_id, period) == 5_000_000_000, (
        f"the ceiling landed at {_limit(tenant_id, period)} rather than the "
        "$5,000 he typed: the grant was subtracted from a baseline that had "
        "already absorbed it, so a decision he made a week ago silently "
        "under-admits now and no event links the two"
    )
    assert _identity_holds(tenant_id, period)

    # And the input the next period is computed from carries his figure, not his
    # figure minus a grant. A rollover reading a folded baseline reproduces the
    # same $50 error every month with nothing to trace it to.
    row = TenantBudgetsRepository().get(tenant_id, period)
    assert int(row["manual_limit_microusd"]) == 5_000_000_000, (
        "the carried baseline holds "
        f"{row.get('manual_limit_microusd')}: granted money has been folded "
        "into the figure he manages, so every future period inherits the error"
    )


# ---------------------------------------------------------------------------
# Journey F — approving into a pool that cannot spend
# ---------------------------------------------------------------------------


def test_journey_he_cannot_approve_into_a_suspended_pool(monkeypatch,
                                                         dynamodb_mock):
    """The personas' interaction case 5.

    A suspended pool refuses every reserve regardless of its ceiling. An
    approval's only guard is that a pool row exists, so a grant applies to a
    suspended pool cleanly and ticks toward its expiry delivering nothing. What
    he would be told wrongly: that he has just helped her. What she is told
    wrongly: `APPROVED`, while every request still fails — and because the ask
    consumed her daily slot, she cannot even re-ask that day.

    Walks: suspended pool -> she is refused -> she asks -> he approves -> the
    approval must be refused server-side, the ceiling must not move, and the
    request must survive a decision that could never have helped her.
    """
    client, seat = _journey_client(monkeypatch)
    period, tenant_id = _seed_seat_tracked_tenant(
        monkeypatch, client, seat, seats=5
    )
    UserTenantsRepository().ensure(
        user_id=HER, tenant_id=tenant_id, role="user", total_credit=1_000_000_000,
    )
    # `set_pool_limit` was this role's own guess; the real setter is
    # `set_manual_limit`.
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=tenant_id, period=period,
        manual_limit_microusd=1_000_000_000, status="suspended",
    )
    limit_before = _limit(tenant_id, period)

    # Her wall. Plenty of ceiling, nothing admissible — the ceiling is not what
    # is refusing her, which is exactly why a raise cannot fix it.
    her = _PipelineUser(user_id=HER, org_id=tenant_id)
    with pytest.raises(HTTPException) as exc:
        reserve_credit(her, 1000, pricing_key="opus", cost_microusd=1_000_000)
    assert exc.value.status_code == 402

    seat.take(HER, "her@journey")
    filed = _file(client, tenant_id, asked=50_000_000, token="tok-suspended")
    assert filed.status_code == 201, filed.text
    request_id = filed.json()["request_id"]

    seat.take(HIM, "him@journey")
    approved = _approve(client, request_id, approved=50_000_000,
                        expires_in_minutes=60 * 24, comment="helping")
    assert approved.status_code == 422, (
        "the approval into a suspended pool succeeded, so a grant now ticks "
        "toward its expiry delivering nothing while consuming the tenant's "
        f"remaining cap: {approved.status_code} {approved.text}"
    )
    assert "suspend" in str(approved.json()["detail"]).lower(), (
        "the refusal must say the pool is suspended, or he retries the same "
        "approval believing the amount or the window was the problem"
    )

    # Nothing moved, so nothing has to be unwound.
    assert _limit(tenant_id, period) == limit_before
    row = TenantBudgetsRepository().get(tenant_id, period)
    assert int(row.get("pool_granted_microusd", 0)) == 0

    # And the request survives a decision that could never have helped her: it
    # is still hers to withdraw and re-file once the pool is resumed.
    seat.take(HER, "her@journey")
    withdrawn = client.post(
        f"/api/mvp/me/limit-raises/{request_id}/withdraw", json={}
    )
    assert withdrawn.status_code in (200, 204), withdrawn.text


# ---------------------------------------------------------------------------
# Journey G — the deficit nobody is shown
# ---------------------------------------------------------------------------


def test_journey_a_negative_headroom_is_shown_to_both_of_them_unclamped(
    monkeypatch, dynamodb_mock
):
    """The personas' interaction cases 1 and 4, plus persona 2 question 1.

    The running scenario: a $100 ceiling with $90 settled, a $50 grant, and a
    $40 hold taken only because of the grant. The grant ends with the hold
    outstanding and headroom goes to MINUS $30 — legitimately, because the $40
    was admitted while granted. Every arithmetic step here is correct. The
    failure is what the two of them are told: a surface rendering
    `max(0, headroom)` shows $0, he grants $10 to fix it, headroom moves from
    -$30 to -$20, every request still fails, and the grant visibly did nothing
    with no surface anywhere showing the hole it fell into.

    Walks: grant -> hold -> the grant ends -> what HIS tenant read shows -> what
    HER new request records -> he grants $10 -> still refused, which is correct
    and had to be predictable from what he was shown -> the outstanding hold
    settles and the identity still holds.
    """
    client, seat = _journey_client(monkeypatch)
    period, tenant_id = _seed_seat_tracked_tenant(
        monkeypatch, client, seat, seats=0
    )
    UserTenantsRepository().ensure(
        user_id=HER, tenant_id=tenant_id, role="user", total_credit=1_000_000_000,
    )
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=tenant_id, period=period, manual_limit_microusd=100_000_000,
    )
    her = _PipelineUser(user_id=HER, org_id=tenant_id)

    # $90 of the $100 spent. Headroom $10.
    ctx = reserve_credit(her, 1000, pricing_key="opus", cost_microusd=90_000_000)
    settle_reservation_and_log(
        user=her, tenants_repo=ctx, reservation=1000,
        actual_input_tokens=100, actual_output_tokens=200,
        model_id="us.anthropic.claude-opus-4-7", context=ctx,
        actual_cost_microusd=90_000_000,
    )
    assert _headroom(tenant_id, period) == 10_000_000

    # A $50 grant: limit $150, headroom $60.
    grant_id = _whole_raise(client, seat, tenant_id, asked=50_000_000,
                            approved=50_000_000, token="tok-deficit")
    assert _limit(tenant_id, period) == 150_000_000
    assert _headroom(tenant_id, period) == 60_000_000

    # A $40 hold, admissible only because of the grant. Left outstanding.
    outstanding = reserve_credit(her, 1000, pricing_key="opus",
                                 cost_microusd=40_000_000)
    assert _headroom(tenant_id, period) == 20_000_000
    assert _identity_holds(tenant_id, period)

    # The grant ends with the hold still out. Reserved money is honoured, so
    # headroom goes signed-negative and that is correct: the $115 was admitted
    # while the grant was live.
    assert client.post(
        f"/api/mvp/admin/limit-grants/{grant_id}/revoke",
        params={"tenant_id": tenant_id}, json={},
    ).status_code == 200
    assert _headroom(tenant_id, period) == -30_000_000
    assert _identity_holds(tenant_id, period), "100 - 40 - 90 = -30"

    # What HE is shown when he opens the tenant. A response that clamps here is
    # the whole defect: it makes a $30 deficit look like an empty pool, and the
    # two call for opposite decisions.
    shown = client.get(
        f"/api/mvp/admin/tenants/{tenant_id}/pool-budget?period={period}"
    )
    assert shown.status_code == 200, shown.text
    body = shown.json()
    # `PoolBudgetResponse` (`mvp/admin_tenants.py`) names this field
    # `remaining_microusd`, not `pool_headroom_microusd` (that name is the
    # REPOSITORY-level dict's key, `TenantBudgetsRepository.pool_summary`,
    # not the wire response's).
    assert body.get("remaining_microusd") == -30_000_000, (
        f"the admin read reports {body.get('remaining_microusd')} "
        "for a pool that is $30 over its ceiling; a figure clamped at zero tells "
        "him to top up by any amount and hides that the first $30 of whatever he "
        "grants buys nothing"
    )

    # What SHE records when she asks again. The same number, signed, or the one
    # figure he decides against understates the hole by exactly the deficit.
    seat.take(HER, "her@journey")
    filed = _file(client, tenant_id, asked=10_000_000, token="tok-after-deficit")
    assert filed.status_code == 201, filed.text
    request_id = filed.json()["request_id"]
    seat.take(HIM, "him@journey")
    queue = client.get(f"/api/mvp/admin/limit-raises?tenant_id={tenant_id}")
    assert queue.status_code == 200, queue.text
    row = _row_for(queue.json(), request_id)
    assert row.get("observed_remaining_microusd") == -30_000_000, (
        f"her request recorded {row.get('observed_remaining_microusd')} as the "
        "remaining capacity; a figure clamped at zero is the number he decides "
        "against and it is understated by exactly the deficit"
    )

    # He grants the $10 she asked for. It correctly admits nothing — and the
    # point of the journey is that he could have known that from what he saw.
    assert _approve(client, request_id, approved=10_000_000,
                    expires_in_minutes=60 * 24).status_code == 200
    assert _headroom(tenant_id, period) == -20_000_000
    with pytest.raises(HTTPException) as exc:
        reserve_credit(her, 1, pricing_key="opus", cost_microusd=1)
    assert exc.value.status_code == 402

    # The hold that looked abandoned finally settles for less than it held.
    # Every move is an ADD, so the identity survives the whole sequence.
    settle_reservation_and_log(
        user=her, tenants_repo=outstanding, reservation=1000,
        actual_input_tokens=100, actual_output_tokens=200,
        model_id="us.anthropic.claude-opus-4-7", context=outstanding,
        actual_cost_microusd=25_000_000,
    )
    assert _identity_holds(tenant_id, period), (
        "the settle of a hold taken under a grant that has since ended broke "
        "the headroom identity"
    )
    assert _headroom(tenant_id, period) == -5_000_000  # 110 - 0 - 115
