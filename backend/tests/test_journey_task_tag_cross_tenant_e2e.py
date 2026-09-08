"""Persona journey: a member of one tenant, and everyone who is not.

These are NOT requirement tests. Each walks a sequence a real person walks
and asks the one question a tenant boundary exists to answer: **can either
of them see the other's tags or totals, through any of the three routes?**
Every step below is satisfiable by unit tests that pass in isolation while
that boundary still leaks on some OTHER route than the one the isolated
test happened to check -- three surfaces share one report-builder
(`mvp.admin_tenants.usage_by_tag_response`), and a leak on any one of them
is a leak this whole feature was supposed to never allow.

Drives all three real routers (`GET /me/usage/by-tag`, `GET
/admin/tenants/{id}/usage/by-tag`, `GET /team-lead/tenants/{id}/usage/
by-tag`) together in one process, the REAL permission lattice (seeded from
the shipped `permissions.json`, never monkeypatched around), and the REAL
reserve/settle pipeline stamped with the resolved tag exactly the way
`reserve_credit_for_model`'s own chokepoint stamps it -- on moto DynamoDB,
across TWO real tenants.

The journeys:
  G. A member of tenant 2 tries every trick a client could try against
     `/me/usage/by-tag` to pull tenant 1's numbers into her own report.
  H. A team lead who owns tenant 1 tries to read tenant 2's report through
     his own route; a plain member of tenant 2 tries to read tenant 1's
     through the admin route.
  I. Two different tenants' engineers happen to type the identical tag
     string. Neither tenant's total includes so much as one micro-dollar of
     the other's, and the one identity that legitimately CAN see both is
     named and contrasted rather than assumed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dynamo.tenant_budgets import TenantBudgetsRepository
from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository
from mvp._pipeline import reserve_credit, settle_reservation_and_log
from mvp.deps import AuthenticatedUser, get_current_user
from mvp.observability.context import build_request_context

T1 = "tagjourney-cross-t1"
T2 = "tagjourney-cross-t2"
L1 = "lead-cross-t1"
L2 = "lead-cross-t2"
E1 = "engineer-cross-t1"
E2 = "engineer-cross-t2"
E3 = "engineer-cross-t2b"
ADMIN = "admin-cross-1"


@dataclass
class _PipelineUser:
    """The shape `reserve_credit`/`settle_reservation_and_log` take."""

    user_id: str
    org_id: str
    email: str = "user@journey"


class _Seat:
    """Whose seat the client is sitting in, INCLUDING which tenant they
    authenticate into -- unlike the single-tenant journeys, this file needs
    the seat to carry its own `org_id` since different personas belong to
    different tenants and switch mid-test."""

    def __init__(self) -> None:
        self.user_id = ADMIN
        self.email = "admin@journey"
        self.org_id = T1
        self.roles: list[str] = ["admin"]

    def take(self, user_id: str, email: str, roles: list[str], org_id: str) -> None:
        self.user_id = user_id
        self.email = email
        self.roles = roles
        self.org_id = org_id

    def current(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=self.user_id, email=self.email, org_id=self.org_id,
            roles=self.roles, raw_claims={}, auth_kind="cognito",
        )


def _seed_permissions(dynamodb_mock) -> None:
    """The real role -> permission table, not a monkeypatched
    `user_has_permission` -- the whole point of this file is which real
    gate stops which real seat, so the gates have to be real."""
    import pathlib as _p

    dynamodb_mock.create_table(
        TableName="stratoclave-permissions",
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    from dynamo.permissions import PermissionsRepository

    PermissionsRepository().seed_from_file(
        _p.Path(__file__).resolve().parent.parent / "permissions.json"
    )
    import mvp.authz as authz

    authz._clear_permissions_cache()


def _seed_two_tenants(monkeypatch) -> str:
    """Two real, independent tenants -- T1 owned by L1 with member E1, T2
    owned by L2 with members E2 and E3 -- both with real money in their
    pools, sharing one pinned period so a read of either is unambiguous
    about which month is "now"."""
    import dynamo.tenant_budgets as _tb
    import mvp._pipeline as _pl

    period = _tb.current_period()
    monkeypatch.setattr(_pl, "current_period", lambda: period)

    TenantsRepository().create(
        tenant_id=T1, name="Cross Tenant Journey 1", team_lead_user_id=L1,
        default_credit=1_000_000, created_by=L1,
    )
    TenantsRepository().create(
        tenant_id=T2, name="Cross Tenant Journey 2", team_lead_user_id=L2,
        default_credit=1_000_000, created_by=L2,
    )
    for user_id, tenant_id in [(E1, T1), (E2, T2), (E3, T2)]:
        UserTenantsRepository().ensure(
            user_id=user_id, tenant_id=tenant_id, role="user",
            total_credit=1_000_000_000,
        )
    for tenant_id in (T1, T2):
        TenantBudgetsRepository().set_manual_limit(
            tenant_id=tenant_id, period=period, manual_limit_microusd=1_000_000_000,
        )
    return period


def _send(
    user: _PipelineUser, *, header: Optional[str], cost_micro: int,
    input_tokens: int = 100, output_tokens: int = 200,
    model: str = "us.anthropic.claude-opus-4-7",
) -> None:
    """One admitted-and-settled request that arrived carrying `header` under
    `x-sc-task-tag`, resolved by the REAL edge function
    `build_request_context` and stamped onto the reservation exactly the way
    `reserve_credit_for_model`'s own chokepoint stamps it -- see
    `test_journey_task_tag_engineer_e2e.py`'s `_send` for the full
    rationale; duplicated here rather than imported because each journey
    file seeds and owns its own tenants."""
    ctx_headers = build_request_context(
        tenant_id=user.org_id, group_id_header=None,
        workflow_run_id_header=None, task_tag_header=header,
    )
    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=cost_micro)
    ctx.task_tag = ctx_headers.task_tag
    ctx.task_tag_source = ctx_headers.task_tag_source
    settle_reservation_and_log(
        user=user, tenants_repo=ctx, reservation=1000,
        actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
        model_id=model, context=ctx, actual_cost_microusd=cost_micro,
    )


def _journey_client(seat: _Seat) -> TestClient:
    from mvp.admin_tenants import router as admin_tenants_router
    from mvp.me import router as me_router
    from mvp.team_lead import router as team_lead_router

    app = FastAPI()
    app.include_router(me_router)
    app.include_router(admin_tenants_router)
    app.include_router(team_lead_router)
    app.dependency_overrides[get_current_user] = seat.current
    return TestClient(app)


# ---------------------------------------------------------------------------
# Journey G -- every trick a T2 member could try against her own /me route
# ---------------------------------------------------------------------------


def test_journey_a_t2_members_own_report_cannot_be_made_to_show_t1(
    monkeypatch, dynamodb_mock
):
    """Engineer 2 (tenant 2) tags her own work "migration-42" -- the SAME
    string engineer 1 (tenant 1) tags his real, larger spend under.
    `/me/usage/by-tag` takes no `tenant_id` and no `user_id` from the
    caller (`mvp.me.my_usage_by_tag`'s own contract: both are always the
    authenticated principal's), so there is no field to put another
    tenant's id into -- but a client could still try appending one as a
    stray query parameter, on the chance the route reads it anyway.

    Walks: E1 tags real spend in T1, E2 tags a smaller amount under the
    identical string in T2, then E2 reads her own `/me/usage/by-tag` --
    once plainly, once with `user_id=E1` tacked onto the query string
    despite the route declaring no such parameter. Asserts both reads
    return ONLY E2's own, smaller number, never E1's, and never the sum of
    the two.
    """
    _seed_permissions(dynamodb_mock)
    period = _seed_two_tenants(monkeypatch)
    e1 = _PipelineUser(user_id=E1, org_id=T1)
    e2 = _PipelineUser(user_id=E2, org_id=T2)

    for _ in range(5):
        _send(e1, header="migration-42", cost_micro=50_000_000)
    _send(e2, header="migration-42", cost_micro=3_000_000)

    seat = _Seat()
    seat.take(E2, "e2@journey", ["user"], T2)
    client = _journey_client(seat)

    plain = client.get(f"/api/mvp/me/usage/by-tag?period={period}")
    assert plain.status_code == 200, plain.text
    plain_body = plain.json()
    assert len(plain_body["rows"]) == 1
    assert plain_body["rows"][0]["cost_microusd"] == 3_000_000
    assert plain_body["rows"][0]["user_id"] == E2

    probed = client.get(
        f"/api/mvp/me/usage/by-tag?period={period}&user_id={E1}"
    )
    assert probed.status_code == 200, probed.text
    assert probed.json() == plain_body, (
        "an undeclared 'user_id' query parameter must not change whose "
        f"report this is: plain={plain_body} probed={probed.json()}"
    )
    for row in probed.json()["rows"]:
        assert row["user_id"] == E2, (
            f"E2's own report must never carry another user's row: {row}"
        )
        assert row["cost_microusd"] != 50_000_000, (
            "E1's tenant-1 total must never appear on E2's tenant-2 report"
        )


# ---------------------------------------------------------------------------
# Journey H -- the wrong owner, the wrong permission
# ---------------------------------------------------------------------------


def test_journey_the_team_lead_of_one_tenant_cannot_read_the_other(
    monkeypatch, dynamodb_mock
):
    """L1 owns T1, not T2. He tries his own route -- the one he legitimately
    uses every month for his own tenant -- against T2's id, on the chance
    ownership is checked loosely or not at all.

    Walks: seed both tenants with real spend, then L1 requests `GET
    /team-lead/tenants/{T2}/usage/by-tag`. Asserts a 404 -- the same
    unified "tenant not found" `_require_owner` gives a non-owner for ANY
    tenant, existent or not, so the response shape cannot be used to probe
    which tenant ids are real -- and, as a same-request contrast, that his
    identical call against his OWN tenant (T1) succeeds and reports his real
    number.
    """
    _seed_permissions(dynamodb_mock)
    period = _seed_two_tenants(monkeypatch)
    e2 = _PipelineUser(user_id=E2, org_id=T2)
    e1 = _PipelineUser(user_id=E1, org_id=T1)
    _send(e2, header="q4-cleanup", cost_micro=8_000_000)
    _send(e1, header="q4-cleanup", cost_micro=6_000_000)

    seat = _Seat()
    seat.take(L1, "l1@journey", ["team_lead"], T1)
    client = _journey_client(seat)

    wrong = client.get(f"/api/mvp/team-lead/tenants/{T2}/usage/by-tag?period={period}")
    assert wrong.status_code == 404, (
        f"L1 does not own T2 and must not read it: {wrong.status_code} {wrong.text}"
    )

    own = client.get(f"/api/mvp/team-lead/tenants/{T1}/usage/by-tag?period={period}")
    assert own.status_code == 200, own.text
    assert own.json()["rows"][0]["cost_microusd"] == 6_000_000, (
        "the SAME identity, the SAME route, his own tenant: this must "
        f"succeed with his real number, not be collateral damage: {own.text}"
    )


def test_journey_a_plain_member_cannot_reach_the_admin_route_at_all(
    monkeypatch, dynamodb_mock
):
    """Engineer 2 holds the plain "user" role -- `usage:read-self` only, no
    tenant-wide or cross-tenant read (`permissions.json`). She tries the
    admin route directly, against her OWN tenant, on the chance the gate is
    weaker than her own role's permission list.

    Walks: `GET /admin/tenants/{T2}/usage/by-tag` as E2. Asserts 403 --
    refused on the permission she is missing, not merely redirected to a
    narrower view -- and contrasts it with the SAME route succeeding for
    the admin identity, which legitimately holds `usage:read-all`.
    """
    _seed_permissions(dynamodb_mock)
    period = _seed_two_tenants(monkeypatch)
    e2 = _PipelineUser(user_id=E2, org_id=T2)
    _send(e2, header="q4-cleanup", cost_micro=8_000_000)

    seat = _Seat()
    seat.take(E2, "e2@journey", ["user"], T2)
    client = _journey_client(seat)

    denied = client.get(f"/api/mvp/admin/tenants/{T2}/usage/by-tag?period={period}")
    assert denied.status_code == 403, (
        f"a plain member holds no admin scope, even for her own tenant: "
        f"{denied.status_code} {denied.text}"
    )

    seat.take(ADMIN, "admin@journey", ["admin"], T2)
    allowed = client.get(f"/api/mvp/admin/tenants/{T2}/usage/by-tag?period={period}")
    assert allowed.status_code == 200, allowed.text


# ---------------------------------------------------------------------------
# Journey I -- the same tag string, two tenants, and who really can see both
# ---------------------------------------------------------------------------


def test_journey_the_same_tag_string_in_two_tenants_never_sums(
    monkeypatch, dynamodb_mock
):
    """E1 (T1) and E3 (T2) -- two unrelated engineers in two unrelated
    tenants -- both happen to name their work "migration-42". Nothing about
    the tag namespace is tenant-scoped in how a human reads it; only the
    storage partition (`tenant_id` as the DynamoDB PK `aggregate_by_tag`
    queries on) is. This journey exists to CHECK that fact rather than
    assume it.

    Walks: both engineers tag real spend under the identical string, then
    each tenant's owner reads their own tenant-wide report. Asserts T1's
    `migration-42` total is EXACTLY E1's spend (never E1 + E3), and T2's is
    EXACTLY E3's (never E3 + E1) -- and, as the one identity that
    legitimately differs, that the ADMIN role reading BOTH tenants back to
    back sees each tenant's own correct, still-separate number rather than
    a merged one. Seeing both tenants is the admin's real, by-design power;
    the point is that even holding it, the same-named tag in each tenant
    still resolves to two different numbers, not one.
    """
    _seed_permissions(dynamodb_mock)
    period = _seed_two_tenants(monkeypatch)
    e1 = _PipelineUser(user_id=E1, org_id=T1)
    e3 = _PipelineUser(user_id=E3, org_id=T2)

    for _ in range(3):
        _send(e1, header="migration-42", cost_micro=10_000_000)
    for _ in range(2):
        _send(e3, header="migration-42", cost_micro=7_000_000)

    seat = _Seat()
    seat.take(L1, "l1@journey", ["team_lead"], T1)
    client = _journey_client(seat)
    t1_report = client.get(
        f"/api/mvp/team-lead/tenants/{T1}/usage/by-tag?period={period}"
    )
    assert t1_report.status_code == 200, t1_report.text
    t1_row = t1_report.json()["rows"][0]
    assert t1_row["cost_microusd"] == 30_000_000, (
        f"T1's total must be exactly E1's spend, never E1 + E3: {t1_report.json()}"
    )

    seat.take(L2, "l2@journey", ["team_lead"], T2)
    t2_report = client.get(
        f"/api/mvp/team-lead/tenants/{T2}/usage/by-tag?period={period}"
    )
    assert t2_report.status_code == 200, t2_report.text
    t2_row = t2_report.json()["rows"][0]
    assert t2_row["cost_microusd"] == 14_000_000, (
        f"T2's total must be exactly E3's spend, never E3 + E1: {t2_report.json()}"
    )

    # The one identity that really can see both, back to back, in the same
    # process -- and still gets two different, correct numbers, not one.
    seat.take(ADMIN, "admin@journey", ["admin"], T1)
    admin_t1 = client.get(f"/api/mvp/admin/tenants/{T1}/usage/by-tag?period={period}")
    seat.take(ADMIN, "admin@journey", ["admin"], T2)
    admin_t2 = client.get(f"/api/mvp/admin/tenants/{T2}/usage/by-tag?period={period}")
    assert admin_t1.json()["rows"][0]["cost_microusd"] == 30_000_000
    assert admin_t2.json()["rows"][0]["cost_microusd"] == 14_000_000
    assert admin_t1.json() == t1_report.json(), (
        "the admin's view of T1 must match the team lead's own view of T1"
    )
