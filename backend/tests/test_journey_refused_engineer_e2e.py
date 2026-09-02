"""Persona journey: the engineer who got refused, from the wall to the raise
and back to the wall.

These are NOT requirement tests. Each one walks a sequence a real person walks
and asks the only question that matters at that altitude: **does she get
through, and is what she is told true?** Every step below is satisfiable by
unit tests that pass in isolation while the person is still stuck or
misinformed — that is precisely the interval these tests occupy.

Drives the SAME surfaces her CLI and the console call (the pool wall in the
real credit pipeline, `POST/GET /me/limit-raises`, and the approver's
`POST /admin/limit-raises/{id}/approve`) against the REAL FastAPI routers on
moto DynamoDB, in one process, with the two actors switching seats mid-journey
the way they do in life.

The journeys, in the personas' own misled-first order:

  A. She is refused, granted, works, and hits the identical refusal again with
     nothing telling her a grant expired.
  B. She is approved for LESS than she asked and no surface tells her, so she
     plans against her own figure and hits the approver's.
  C. Her approved-then-expired request leaves her locked out of asking again
     by the daily one-request limit — the mechanism landing hardest on the
     person who did everything right.
  D. The token wall sells her a money raise, so she spends her one daily slot
     fixing the wrong problem.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from dynamo.tenant_budgets import TenantBudgetsRepository
from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository
from mvp import authz
from mvp._pipeline import reserve_credit, settle_reservation_and_log
from mvp.deps import AuthenticatedUser, get_current_user

TENANT = "journey-org"
HER = "engineer-1"
HIM = "approver-1"


@dataclass
class _PipelineUser:
    """The shape `reserve_credit`/`settle_reservation_and_log` take."""

    user_id: str
    org_id: str
    email: str = "her@journey"


class _Seat:
    """Whose seat the HTTP client is sitting in right now.

    A journey has two actors and one process. Both of her calls and both of his
    land on the same app, so the identity has to be switchable mid-test rather
    than fixed at client construction.
    """

    def __init__(self) -> None:
        self.user_id = HER
        self.email = "her@journey"

    def take(self, user_id: str, email: str) -> None:
        self.user_id = user_id
        self.email = email

    def current(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=self.user_id, email=self.email, org_id=TENANT,
            roles=["admin"], raw_claims={}, auth_kind="cognito",
        )


def _journey_client(monkeypatch) -> tuple[TestClient, _Seat]:
    """A client over every router this journey crosses.

    `mvp.grants.router` is the one export F2's contract pins (U1). There is
    no separate `mvp.quota_raises` module -- this role's own guess that the
    request side lived in a module F2 never named does not survive contact
    with the real tree: both the request side (`submit_own_limit_raise`
    etc.) and the approval side (`approve_limit_raise` etc.) are mounted on
    the SAME `mvp.grants.router`, so one `include_router` call reaches
    every surface this journey crosses.
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


def _seed_tenant_with_spent_pool(monkeypatch, *, limit_micro: int,
                                 settled_micro: int,
                                 her_token_credit: int = 1_000_000_000) -> str:
    """The personas' running scenario: a pool with real money already spent on
    it, so the wall she hits is the wall a live tenant hits.

    The spend is put there through the REAL pipeline (reserve then settle), not
    by writing counters, because a hand-written `pool_settled` is a pool no
    reserve has ever looked at.
    """
    import dynamo.tenant_budgets as _tb
    import mvp._pipeline as _pl

    period = _tb.current_period()
    monkeypatch.setattr(_pl, "current_period", lambda: period)

    TenantsRepository().create(
        tenant_id=TENANT, name="Journey", team_lead_user_id=HIM,
        default_credit=1_000_000, created_by=HIM,
    )
    # `ensure` is create-if-missing, so her allowance has to be right the first
    # time: a later call with a different figure is a silent no-op, and a
    # journey seeded that way would meet the wrong wall.
    UserTenantsRepository().ensure(
        user_id=HER, tenant_id=TENANT, role="user",
        total_credit=her_token_credit,
    )
    repo = TenantBudgetsRepository()
    # `set_pool_limit` was this role's own guess; the real, shipped setter is
    # `set_manual_limit` (`dynamo/tenant_budgets.py`).
    repo.set_manual_limit(
        tenant_id=TENANT, period=period, manual_limit_microusd=limit_micro,
    )
    if settled_micro:
        her = _PipelineUser(user_id=HER, org_id=TENANT)
        ctx = reserve_credit(her, 1000, pricing_key="opus",
                             cost_microusd=settled_micro)
        settle_reservation_and_log(
            user=her, tenants_repo=ctx, reservation=1000,
            actual_input_tokens=100, actual_output_tokens=200,
            model_id="us.anthropic.claude-opus-4-7", context=ctx,
            actual_cost_microusd=settled_micro,
        )
    return period


def _refused(cost_micro: int) -> dict[str, Any]:
    """Take a real refusal off the real reserve path and return its body.

    The body is what she actually sees; asserting on anything else would be
    asserting on a story about the refusal rather than the refusal.
    """
    her = _PipelineUser(user_id=HER, org_id=TENANT)
    with pytest.raises(HTTPException) as exc:
        reserve_credit(her, 1000, pricing_key="opus", cost_microusd=cost_micro)
    assert exc.value.status_code == 402, exc.value.detail
    detail = exc.value.detail
    assert isinstance(detail, dict), detail
    return detail


def _spend(cost_micro: int) -> None:
    """One admitted-and-settled request, the unit her work is made of."""
    her = _PipelineUser(user_id=HER, org_id=TENANT)
    ctx = reserve_credit(her, 1000, pricing_key="opus", cost_microusd=cost_micro)
    settle_reservation_and_log(
        user=her, tenants_repo=ctx, reservation=1000,
        actual_input_tokens=100, actual_output_tokens=200,
        model_id="us.anthropic.claude-opus-4-7", context=ctx,
        actual_cost_microusd=cost_micro,
    )


def _file_request(client: TestClient, *, asked_micro: int,
                  hint: Optional[dict[str, Any]] = None,
                  limit_kind: str = "tenant_dollar_pool",
                  client_token: str = "tok-1"):
    """She files.

    **Convergence correction.** `POST /api/mvp/me/limit-raises`
    (`SubmitLimitRaiseRequest`, `mvp/grants.py`) has no `tenant_id` field at
    all -- `extra="forbid"` -- and never can: "the tenant is the caller's
    own, read from their session... a requester asking for a raise on a
    tenant they are not in is not a request this endpoint has any way to
    authorise" (the endpoint's own docstring). The contract's "carried from
    the hint, never from ambient context" requirement (Interface section)
    is a CLIENT-side rule about which session/profile a console or CLI
    submits under, verified in the frontend journey and unit suites, not a
    wire field this single-tenant, single-seat backend journey can
    exercise. `hint` is accepted here only so callers that read a hint's
    `raise_hint.tenant_id` and pass it along keep compiling; it is no
    longer sent. `limit_kind`'s real wall name is `tenant_dollar_pool`
    (`mvp.grants.POOL_WALL`), not this role's guessed `tenant_pool`.
    """
    del hint  # kept as a parameter for call-site symmetry; not a body field
    return client.post(
        "/api/mvp/me/limit-raises",
        json={
            "limit_kind": limit_kind,
            "asked_amount_microusd": asked_micro,
            # "migration" is one of the four real `RAISE_REASON_CODES`
            # (`mvp/grants.py`) -- "deadline" is not one of them.
            "reason_code": "migration",
            "comment": "shipping the migration on Friday",
            "client_token": client_token,
        },
    )


def _approve(client: TestClient, request_id: str, *, approved_micro: int,
             expires_at: str, comment: str = "half of the ask, one week"):
    """Approve. `ApproveLimitRaiseRequest.expires_at` is `int` (epoch
    seconds, `mvp/grants.py`) -- an ISO string does not validate, so the
    epoch conversion happens here rather than at every call site."""
    return client.post(
        f"/api/mvp/admin/limit-raises/{request_id}/approve",
        json={
            "approved_amount_microusd": approved_micro,
            "expires_at": int(datetime.fromisoformat(expires_at).timestamp()),
            "decision_comment": comment,
        },
    )


def _in(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _quota_events_table():
    import boto3

    import os

    name = os.environ.get(
        "DYNAMODB_QUOTA_EVENTS_TABLE",
        f"{os.environ.get('STRATOCLAVE_PREFIX', 'stratoclave')}-quota-events",
    )
    return boto3.resource("dynamodb", region_name="us-east-1").Table(name)


def _her_grant_id(client: TestClient) -> str:
    """Find her grant through the approver's inventory — the only shipped
    surface that returns grant rows at all (no `/me` surface does, which is
    persona 1's step-4 grievance and the reason this helper has to borrow his
    seat to learn her grant's id)."""
    listed = client.get(f"/api/mvp/admin/limit-grants?tenant_id={TENANT}")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    grants = payload["grants"] if isinstance(payload, dict) else payload
    assert grants, f"her approved grant is in no inventory: {payload}"
    return grants[0]["grant_id"]


def _her_deadline_passes(grant_id: str, *, minutes_ago: int) -> datetime:
    """Move her grant's deadline into the past.

    Time has to pass for this journey to exist, and R11 pins the minimum window
    at 300 seconds, so a test cannot wait it out. No contract exposes a clock
    seam or a mutator for a grant row, so this writes the durable record
    directly on the key F2 pins (`TENANT#<tenant_id>` / `GRANT#<grant_id>`) in
    the table F2 names. That is the least-invented route available and it is
    still a workaround: a journey that needs elapsed time needs an injectable
    clock, and that is recorded as a gap rather than papered over.
    """
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    # `expires_at` is stored as an epoch INT throughout `mvp/grants.py`
    # (`grant_put_txn_item`'s own `expires_at_epoch` -> `{"N": ...}`) -- an
    # ISO string here would change the attribute's DynamoDB type and break
    # every real reader (`int(item["expires_at"])`).
    _quota_events_table().update_item(
        Key={"pk": f"TENANT#{TENANT}", "sk": f"GRANT#{grant_id}"},
        UpdateExpression="SET expires_at = :e",
        ExpressionAttributeValues={":e": int(when.timestamp())},
    )
    # Moving `expires_at` into the past is not, by itself, what a real
    # deployment observes: a grant's `status` stays ACTIVE (still
    # capacity-bearing, still the slot's holder) until the periodic sweeper
    # (`mvp.grants.sweep_expired_grants`) actually revokes it -- verified
    # against real code: `_expired_grant_reason_for_pool_wall` only
    # considers grants whose STORED status is already `GRANT_EXPIRED`, and
    # `_slot_holder_is_still_holding` frees the slot only once the grant is
    # no longer capacity-bearing. This role's own design note flagged the
    # missing sweep entry point as a gap; it exists
    # (`mvp.grants.sweep_expired_grants`), so the journey runs it rather
    # than assuming the passage of a timestamp alone is what a live
    # deployment would have observed by the time she notices.
    from mvp.grants import sweep_expired_grants

    sweep_expired_grants(now_epoch=int(datetime.now(timezone.utc).timestamp()))
    return when


def _mentions_an_expiry(blob: Any) -> bool:
    """Does this refusal tell her a grant expired, in any form she could act on?

    **Convergence correction.** A blind `repr(blob).lower()` substring
    search for `"expir"` was tried first and does not survive contact with
    the real hint shape: `RaiseHintCandidate.grant_expired` (`mvp/grants.py`)
    is present on EVERY candidate, unconditionally, `False` by default --
    so its own KEY NAME (`"grant_expired"`, which contains `"expir"`)
    matches this search whether or not a grant actually expired, making the
    heuristic unable to fail. The real positive signal is one of: the
    candidate's `grant_expired` flag actually being `True`, or the
    human-readable `message` (which `_refusal_body` appends expiry wording
    to ONLY when that flag is true, `_pipeline.py`) mentioning it -- both
    checked directly rather than guessed at.
    """
    if not isinstance(blob, dict):
        return False
    message = str(blob.get("message") or "")
    if "expir" in message.lower():
        return True
    hint = blob.get("raise_hint")
    if isinstance(hint, dict):
        for candidate in hint.get("candidates") or []:
            if isinstance(candidate, dict) and candidate.get("grant_expired") is True:
                return True
    return False


# ---------------------------------------------------------------------------
# Journey A — the identical second refusal
# ---------------------------------------------------------------------------


def test_journey_second_refusal_after_her_grant_expired_says_so(
    monkeypatch, dynamodb_mock
):
    """Persona 1, step 4 — the top of her misled-first ranking.

    She is refused, asks, is approved, works for a few hours, and then hits a
    402 that is byte-identical to the one she started with. What she would be
    told wrongly: that nothing has changed since her first refusal, when in
    fact the capacity she was given has gone away. Acting on the first refusal
    means "ask for a raise"; acting on the second means "my raise ended" — and
    a body that cannot tell them apart sends her to file a request she is not
    allowed to file (journey C) instead of telling her to wait for the period.

    Walks: refusal -> request -> approval -> admitted work -> the grant's
    expiry passes -> refusal again. Asserts only that the second refusal is
    distinguishable from the first and names the expiry, which is the one fact
    that changed between them.
    """
    period = _seed_tenant_with_spent_pool(
        monkeypatch, limit_micro=100_000_000, settled_micro=90_000_000
    )
    client, seat = _journey_client(monkeypatch)

    # 1. The wall she starts at: $10 of headroom, a $30 request.
    first = _refused(30_000_000)
    assert first["reason"] == "tenant_pool_exhausted"
    assert "raise_hint" in first, (
        "the pool wall must carry the hint that starts this journey"
    )
    assert not _mentions_an_expiry(first), (
        "the FIRST refusal must not claim an expiry — she has no grant yet, and "
        "a body that names an expiry here would make the second refusal "
        "indistinguishable in the other direction"
    )

    # 2. She files against the wall the hint named, asking for $200.
    filed = _file_request(client, asked_micro=200_000_000,
                          hint=first["raise_hint"])
    assert filed.status_code == 201, filed.text
    request_id = filed.json()["request_id"]

    # 3. He approves $50, expiring in 40 minutes — a window she is inside.
    seat.take(HIM, "him@journey")
    expires_at = _in(40)
    approved = _approve(client, request_id, approved_micro=50_000_000,
                        expires_at=expires_at)
    assert approved.status_code == 200, approved.text
    seat.take(HER, "her@journey")

    # 4. The next ordinary request just works — no flag, no header. The grant
    #    lifted the ceiling to $150, so $30 of her $60 headroom is admissible.
    _spend(30_000_000)
    summary = TenantBudgetsRepository().pool_summary(TENANT, period)
    assert summary["pool_limit_microusd"] == 150_000_000, (
        "the grant must reach the ceiling the reserve path actually checks"
    )

    # 5. Her deadline passes while she is mid-task. Nothing told her it would,
    #    and nothing tells her afterwards either.
    seat.take(HIM, "him@journey")
    grant_id = _her_grant_id(client)
    seat.take(HER, "her@journey")
    _her_deadline_passes(grant_id, minutes_ago=5)

    # 6. Her deadline has passed and the sweeper has already revoked the
    #    grant (step 5): the ceiling reverted to $100 the instant it ran,
    #    against $120 already settled (the $90 baseline plus step 4's
    #    $30) -- so she is back over the wall on her very next request,
    #    five minutes inside the naming window (expiry <= now <= expiry +
    #    15 minutes). There is no further successful spend in between: the
    #    reversion itself is what puts her over, not a fresh request that
    #    tips her over.
    second = _refused(30_000_000)

    # 7. The whole journey reduces to this comparison. Two refusals, two
    #    different meanings, and she can only act correctly if the bodies
    #    differ.
    assert second != first, (
        "the second refusal is byte-identical to the first: nothing in it says "
        "her grant expired, so 'ask for a raise' and 'your raise ended' are "
        "the same message"
    )
    assert _mentions_an_expiry(second), (
        "a refusal inside the naming window must name the expiry that caused "
        "it; without that she cannot tell an expiry from the wall she started "
        "at, and the CLI sends her to file a request the slot will refuse"
    )


# ---------------------------------------------------------------------------
# Journey B — approved for less, and never told
# ---------------------------------------------------------------------------


def test_journey_the_amount_she_was_granted_reaches_her(monkeypatch,
                                                        dynamodb_mock):
    """Persona 1 step 4 / persona 2 question 3 — the highest-mislead gap on the
    approver's side, seen from her seat.

    She asks for $200. He approves $50. Her request reads `APPROVED`. What she
    would be told wrongly: that she got what she asked for. She plans a $200
    job against a $150 ceiling and hits his figure with no warning, and she
    cannot plan around a deadline she was never shown either — both the amount
    and the expiry live on the grant row, and a `/me` surface that returns only
    the request row shows her neither.

    Walks: refusal -> ask $200 -> approved $50 -> she reads her own request
    list, BEFORE spending anything. The point of ordering it that way is that
    everything she needs must be legible in advance; learning her grant's size
    by hitting it is the bug.
    """
    _seed_tenant_with_spent_pool(
        monkeypatch, limit_micro=100_000_000, settled_micro=90_000_000
    )
    client, seat = _journey_client(monkeypatch)

    hint = _refused(30_000_000)["raise_hint"]
    filed = _file_request(client, asked_micro=200_000_000, hint=hint)
    assert filed.status_code == 201, filed.text
    request_id = filed.json()["request_id"]

    # While it is PENDING there is nothing to tell her, and the surface must
    # not invent a figure — an unpopulated approved amount reading as her ask
    # is the same lie arriving earlier.
    pending = client.get("/api/mvp/me/limit-raises")
    assert pending.status_code == 200, pending.text
    row = _find(pending.json(), request_id)
    # Stored uppercase (`STATUS_PENDING`, `dynamo/quota_events.py`); the wire
    # projection (`_request_public`, `mvp/grants.py`) carries that spelling
    # verbatim -- the same value `capacity_bearing` is computed from
    # elsewhere on this same row, so the two must never disagree.
    assert row["status"] == "PENDING"
    assert not row.get("approved_amount_microusd"), (
        "a pending request must carry no approved amount"
    )

    seat.take(HIM, "him@journey")
    expires_at = _in(60 * 24)
    assert _approve(client, request_id, approved_micro=50_000_000,
                    expires_at=expires_at).status_code == 200
    seat.take(HER, "her@journey")

    decided = client.get("/api/mvp/me/limit-raises")
    assert decided.status_code == 200, decided.text
    row = _find(decided.json(), request_id)

    assert row["status"] == "APPROVED"
    # The figure she must plan against, under the one name that says which of
    # the two amounts on the row it is.
    assert row.get("approved_amount_microusd") == 50_000_000, (
        "she was approved for $50 and her own request list does not say so; "
        "`APPROVED` next to her $200 ask reads as $200"
    )
    # And both figures present, because "you got less" is a comparison and she
    # cannot make it from one number.
    assert row.get("asked_amount_microusd") == 200_000_000
    assert row["approved_amount_microusd"] < row["asked_amount_microusd"]
    # The deadline, before it arrives rather than by dying at it.
    assert row.get("expires_at"), (
        "no surface returns the expiry she was given, so she cannot plan "
        "around a deadline she was never shown"
    )
    # `expires_at` is the wire's epoch-int convention (`_request_public`),
    # not an ISO string.
    assert row["expires_at"] > int(datetime.now(timezone.utc).timestamp())
    # Who decided, as a stable id the console resolves — never an address.
    assert row.get("approver_id") == HIM
    assert "@" not in str(row.get("approver_id"))
    # And why she got half, so the next ask is a better ask.
    assert row.get("decision_comment"), (
        "an approval for less than the ask with no reason invites the identical "
        "re-file tomorrow"
    )


def _find(payload: Any, request_id: str) -> dict[str, Any]:
    rows = payload["requests"] if isinstance(payload, dict) else payload
    for row in rows:
        if row.get("request_id") == request_id:
            return row
    raise AssertionError(f"{request_id} missing from her own request list: {payload}")


# ---------------------------------------------------------------------------
# Journey C — the slot that punishes the model citizen
# ---------------------------------------------------------------------------


def test_journey_a_decided_request_does_not_leave_her_locked_out(monkeypatch,
                                                                dynamodb_mock):
    """Persona 1 step 5, and the personas' "one thing a real user will hate".

    She was refused, asked, was approved, and was working. Her grant ends. The
    only thing the CLI suggests is to file again, and the answer is `409
    raise_request_daily_limit`: *you already asked today*. What she would be
    told wrongly: that this is a second speculative ask, when her first ask was
    decided hours ago and consumed. The anti-spam mechanism lands hardest on
    the person who did everything right, for up to 24 hours, in a timezone
    nobody states.

    Walks: file -> file again while PENDING (correctly refused, and the refusal
    must be actionable) -> approved -> the grant expires -> file again. The last
    step must succeed: a decided request frees the day's slot.
    """
    _seed_tenant_with_spent_pool(
        monkeypatch, limit_micro=100_000_000, settled_micro=90_000_000
    )
    client, seat = _journey_client(monkeypatch)
    hint = _refused(30_000_000)["raise_hint"]

    first = _file_request(client, asked_micro=200_000_000, hint=hint,
                          client_token="tok-first")
    assert first.status_code == 201, first.text
    request_id = first.json()["request_id"]

    # A genuinely speculative second ask, while the first is still PENDING, is
    # correctly refused — but the refusal has to be actionable, or "wait" and
    # "broken" look the same.
    speculative = _file_request(client, asked_micro=300_000_000, hint=hint,
                                client_token="tok-second")
    assert speculative.status_code == 409, speculative.text
    body = speculative.json()["detail"]
    # `DailySlotOccupied.code` (`mvp/grants.py`) is
    # `limit_raise_daily_slot_occupied`, carried in `"type"` (`GrantError`'s
    # own shape) -- this role's guessed code string and key name
    # (`"reason"`) were both wrong.
    assert body["type"] == "limit_raise_daily_slot_occupied"
    assert body.get("holder_request_id") == request_id, (
        "the 409 must name the request holding her slot, or she cannot tell "
        "which ask is in the way"
    )
    # The extra kwarg `DailySlotOccupied` is raised with is `reset_at`, not
    # `slot_resets_at`.
    reset = str(body.get("reset_at", ""))
    assert reset, "the 409 must say when the slot resets"
    assert re.search(r"(Z|[+-]\d{2}:?\d{2}|UTC|[A-Za-z]+/[A-Za-z_]+)", reset), (
        "the slot key is a yyyy-mm-dd in *some* timezone and no surface states "
        "which; a reset time with no zone is a reset time she cannot wait for"
    )

    # He decides it. Her grant runs its course and ends.
    seat.take(HIM, "him@journey")
    expires_at = _in(20)
    assert _approve(client, request_id, approved_micro=50_000_000,
                    expires_at=expires_at).status_code == 200
    grant_id = _her_grant_id(client)
    seat.take(HER, "her@journey")
    _her_deadline_passes(grant_id, minutes_ago=1)

    # The same calendar day, one decided request behind her, mid-task. This is
    # the step the whole feature exists to serve and the one the slot refuses.
    again = _file_request(client, asked_micro=100_000_000, hint=hint,
                          client_token="tok-third")
    assert again.status_code == 201, (
        "her slot is still held by a request that was decided and consumed, so "
        "the person who did everything right is hard-stopped until a day "
        "boundary in an unstated timezone: "
        f"{again.status_code} {again.text}"
    )


# ---------------------------------------------------------------------------
# Journey D — the wrong wall
# ---------------------------------------------------------------------------


def test_journey_the_token_wall_never_sells_her_a_money_raise(monkeypatch,
                                                              dynamodb_mock):
    """Persona 1 step 1 — second on her misled-first ranking.

    Two walls refuse her and only one is grantable. If the token refusal also
    carries a hint, or the CLI prints the request command after any refusal,
    she files a money request to fix a token problem. What she would be told
    wrongly: that a raise fixes this. He grants $50, nothing changes, and her
    one daily slot is gone — so the misdirection costs her the ability to ask
    about the wall that *is* grantable.

    Walks: token wall -> the refusal must not offer a raise -> she asks anyway
    -> refused for naming a wall nobody can grant -> and she can still file the
    request that would actually help, today. The last step is the journey: a
    refused misdirected ask must not consume the slot.
    """
    # Money in the pool, nothing on her personal token allowance: the wall she
    # meets is the ungrantable one, which is the whole premise of this journey.
    period = _seed_tenant_with_spent_pool(
        monkeypatch, limit_micro=100_000_000, settled_micro=0,
        her_token_credit=1,
    )
    client, seat = _journey_client(monkeypatch)

    her = _PipelineUser(user_id=HER, org_id=TENANT)
    with pytest.raises(HTTPException) as exc:
        reserve_credit(her, 100_000, pricing_key="opus", cost_microusd=1_000_000)
    assert exc.value.status_code == 402
    token_wall = exc.value.detail
    assert token_wall["reason"] == "personal_budget_exhausted"

    # 1. The ungrantable wall must not advertise the grantable path.
    assert "raise_hint" not in token_wall, (
        "the token wall carries a money-raise hint, so she files a money "
        "request to fix a token problem and burns her only daily slot on a "
        "request that cannot help her"
    )

    # 2. If she asks anyway — a CLI that prints the command after "a refusal"
    #    rather than after a *pool* refusal will make her — the server refuses
    #    rather than accepting a valid-looking request nobody can act on.
    misdirected = _file_request(client, asked_micro=50_000_000,
                                limit_kind="user_token_quota",
                                client_token="tok-wrong-wall")
    assert misdirected.status_code == 422, misdirected.text
    # `GrantError.as_detail()` (`mvp/grants.py`) is `{"type": <code>,
    # "message": ..., **extra}` -- the machine-readable code lives in
    # `"type"`, never `"reason"` (that key belongs to `_refusal_body`'s
    # DIFFERENT shape, the one the two money-wall refusals above use).
    assert misdirected.json()["detail"]["type"] == "unknown_limit_kind"

    # 3. And the refusal left her slot alone. This is the whole point: a
    #    request that was never actionable must not cost her the one that is.
    real = _file_request(client, asked_micro=50_000_000,
                         limit_kind="tenant_dollar_pool", client_token="tok-right-wall")
    assert real.status_code == 201, (
        "the misdirected request consumed her daily slot, so being sent to the "
        "wrong wall costs her the ability to ask about the right one until a "
        f"day boundary: {real.status_code} {real.text}"
    )
    assert period  # the pinned period the whole journey ran against
