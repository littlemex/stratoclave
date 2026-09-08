"""Persona journey: the tenant administrator (and the team lead who owns one
tenant) deciding whether a month's spend matched the work people said they
were doing.

These are NOT requirement tests. Each walks a sequence he walks and asks
whether the report supports the conclusion he is about to draw from it --
**what can he conclude, and where will he conclude something the records do
not support?** That second half is the one this layer exists to catch;
every step below is satisfiable by unit tests that pass in isolation while
his conclusion is still wrong.

Drives the REAL admin (`GET /admin/tenants/{id}/usage/by-tag`) and team-lead
(`GET /team-lead/tenants/{id}/usage/by-tag`) routers, the REAL permission
lattice (seeded from the shipped `permissions.json`, never monkeypatched
around), and the REAL reserve/settle pipeline stamped with the resolved tag
exactly the way `reserve_credit_for_model`'s own chokepoint stamps it -- on
moto DynamoDB.

The journeys:
  D. Two engineers do the same migration work; only one tags it. What the
     tenant-wide report lets him conclude about the tenant's migration
     spend, and the one conclusion it does not support.
  E. One tagger, one non-tagger, on the administrator's one tenant-wide
     screen: what it tells him about each of them, and what it still
     cannot.
  F. The team lead who owns this one tenant, and the admin who owns every
     tenant, reading the same tenant and period side by side.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dynamo.tenant_budgets import TenantBudgetsRepository
from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository
from mvp._pipeline import reserve_credit, settle_reservation_and_log
from mvp.deps import AuthenticatedUser, get_current_user
from mvp.observability.context import build_request_context

TENANT = "tagjourney-tenant"
ENG_A = "engineer-tag-a"
ENG_B = "engineer-tag-b"
ADMIN = "admin-tag-1"
LEAD = "lead-tag-1"


@dataclass
class _PipelineUser:
    """The shape `reserve_credit`/`settle_reservation_and_log` take."""

    user_id: str
    org_id: str
    email: str = "user@journey"


class _Seat:
    """Whose seat the HTTP client is sitting in right now. Both his reads
    happen on the same process, so the identity has to be switchable mid-test
    rather than fixed at client construction -- the same reason the existing
    grant journeys switch seats mid-test."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.user_id = ADMIN
        self.email = "admin@journey"
        self.roles: list[str] = ["admin"]

    def take(self, user_id: str, email: str, roles: list[str]) -> None:
        self.user_id = user_id
        self.email = email
        self.roles = roles

    def current(self) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=self.user_id, email=self.email, org_id=self.tenant_id,
            roles=self.roles, raw_claims={}, auth_kind="cognito",
        )


def _seed_permissions(dynamodb_mock) -> None:
    """The real role -> permission table, not a monkeypatched
    `user_has_permission` -- so `usage:read-all` (admin) and
    `usage:read-own-tenant` (team_lead) are the actual gates a live role
    clears, not a bypass this journey pretends past."""
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


def _seed_tenant(monkeypatch, *, members: list[str]) -> str:
    """One tenant, owned by `LEAD`, with real money in its pool and every
    name in `members` provisioned -- a period pinned so every call below and
    every read afterwards agree on which month is "now"."""
    import dynamo.tenant_budgets as _tb
    import mvp._pipeline as _pl

    period = _tb.current_period()
    monkeypatch.setattr(_pl, "current_period", lambda: period)

    TenantsRepository().create(
        tenant_id=TENANT, name="Admin Tag Journey", team_lead_user_id=LEAD,
        default_credit=1_000_000, created_by=LEAD,
    )
    for m in members:
        UserTenantsRepository().ensure(
            user_id=m, tenant_id=TENANT, role="user", total_credit=1_000_000_000,
        )
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=TENANT, period=period, manual_limit_microusd=2_000_000_000,
    )
    return period


def _send(
    user: _PipelineUser, *, header, cost_micro: int,
    input_tokens: int = 100, output_tokens: int = 200,
    model: str = "us.anthropic.claude-opus-4-7",
) -> None:
    """One admitted-and-settled request that arrived carrying `header` under
    `x-sc-task-tag`, resolved by the REAL edge function
    `build_request_context` and stamped onto the reservation exactly the way
    `reserve_credit_for_model`'s own chokepoint stamps it -- see
    `test_journey_task_tag_engineer_e2e.py`'s `_send` for the full rationale;
    duplicated here rather than imported because each journey file seeds and
    owns its own tenant."""
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
    from mvp.team_lead import router as team_lead_router

    app = FastAPI()
    app.include_router(admin_tenants_router)
    app.include_router(team_lead_router)
    app.dependency_overrides[get_current_user] = seat.current
    return TestClient(app)


_ROW_FIELDS = {
    "user_id", "task_tag", "requests", "cost_microusd",
    "input_tokens", "output_tokens",
}


# ---------------------------------------------------------------------------
# Journey D -- migration spend has a floor, not a total
# ---------------------------------------------------------------------------


def test_journey_migration_spend_has_a_floor_not_a_total(monkeypatch, dynamodb_mock):
    """A week after the migration, he opens the tenant-wide `usage/by-tag`
    report and asks the only question a report like this can answer: did
    the month's spend match what people say they did?

    Two engineers did real migration work. Engineer A tagged every call
    `migration-42`. Engineer B did the identical kind of work, the same
    week, but her header never went out; her calls land under the
    sentinel, indistinguishable there from an unrelated lookup she also ran
    that same afternoon.

    What he CAN correctly conclude: at least A's total of migration work
    happened, because the `migration-42` row is exact -- `tenant_id`
    partitions the read, so no other member's spend could ever land under
    it, checked directly below rather than assumed.

    What the report does NOT let him conclude, and the gap this journey
    exists to name: that `migration-42`'s total IS the tenant's migration
    spend for the month. It is a floor, not a total, and there is nothing
    in the response that would have told him so before B's own timesheet
    said otherwise.
    """
    _seed_permissions(dynamodb_mock)
    period = _seed_tenant(monkeypatch, members=[ENG_A, ENG_B])
    a = _PipelineUser(user_id=ENG_A, org_id=TENANT)
    b = _PipelineUser(user_id=ENG_B, org_id=TENANT)

    for _ in range(4):
        _send(a, header="migration-42", cost_micro=15_000_000)
    for _ in range(3):
        _send(b, header=None, cost_micro=20_000_000)
    _send(b, header=None, cost_micro=1_000_000)

    seat = _Seat(TENANT)
    seat.take(ADMIN, "admin@journey", ["admin"])
    client = _journey_client(seat)
    resp = client.get(f"/api/mvp/admin/tenants/{TENANT}/usage/by-tag?period={period}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_key = {(r["user_id"], r["task_tag"]): r for r in body["rows"]}

    migration_row = by_key[(ENG_A, "migration-42")]
    assert migration_row["cost_microusd"] == 60_000_000, (
        "what CAN be trusted: A's own row is exact -- nobody else's spend "
        f"could have landed under it: {body}"
    )

    b_row = by_key[(ENG_B, "unlabelled")]
    assert b_row["cost_microusd"] == 61_000_000, (
        f"B's migration work and her unrelated lookup are one number: {b_row}"
    )
    assert set(b_row.keys()) == _ROW_FIELDS, (
        "the report does not, and structurally cannot, break B's number "
        f"apart by what the work actually was: {b_row}"
    )

    real_migration_spend = 60_000_000 + 3 * 20_000_000
    assert migration_row["cost_microusd"] < real_migration_spend, (
        "the assertion this journey exists to fail loudly the day "
        "aggregation starts finding B's untagged migration work on its own: "
        f"tagged={migration_row['cost_microusd']} real={real_migration_spend}"
    )


# ---------------------------------------------------------------------------
# Journey E -- the tagger and the non-tagger on one screen
# ---------------------------------------------------------------------------


def test_journey_the_tagger_and_the_non_tagger_on_one_screen(
    monkeypatch, dynamodb_mock
):
    """Two members of one tenant: A tags everything under "release-9" except
    one deliberate outlier call of her own; B never sends the header, ever.
    The administrator reads the tenant-wide report once.

    Walks: A's tagged work plus her one outlier, B's four untagged calls,
    then one read of `GET /admin/tenants/{id}/usage/by-tag`. Asserts what
    the screen tells him about each of them -- A gets TWO rows (her tagged
    work kept separate from her one outlier), B gets exactly ONE -- and
    that neither member's number contains so much as one micro-dollar of
    the other's, checked directly rather than trusted from the tenant
    partition alone. Also names what the screen still does NOT tell him:
    A's "unlabelled" $2 and B's "unlabelled" $24 carry the identical tag
    string, and nothing but the user_id column he reads himself says one is
    a deliberate one-off and the other is the entirety of her month.
    """
    _seed_permissions(dynamodb_mock)
    period = _seed_tenant(monkeypatch, members=[ENG_A, ENG_B])
    a = _PipelineUser(user_id=ENG_A, org_id=TENANT)
    b = _PipelineUser(user_id=ENG_B, org_id=TENANT)

    for _ in range(5):
        _send(a, header="release-9", cost_micro=3_000_000)
    _send(a, header=None, cost_micro=2_000_000)
    for _ in range(4):
        _send(b, header=None, cost_micro=6_000_000)

    seat = _Seat(TENANT)
    seat.take(ADMIN, "admin@journey", ["admin"])
    client = _journey_client(seat)
    resp = client.get(f"/api/mvp/admin/tenants/{TENANT}/usage/by-tag?period={period}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_key = {(r["user_id"], r["task_tag"]): r for r in body["rows"]}

    assert len(body["rows"]) == 3, f"expected A's two rows plus B's one: {body}"
    assert by_key[(ENG_A, "release-9")]["cost_microusd"] == 15_000_000
    assert by_key[(ENG_A, "unlabelled")]["cost_microusd"] == 2_000_000
    assert by_key[(ENG_B, "unlabelled")]["cost_microusd"] == 24_000_000

    total = sum(r["cost_microusd"] for r in body["rows"])
    assert total == 15_000_000 + 2_000_000 + 24_000_000, (
        "neither member's row contains a cent of the other's spend"
    )

    # What the screen still does not tell him: the two "unlabelled" rows
    # carry the identical tag string and mean two entirely different things.
    assert (
        by_key[(ENG_A, "unlabelled")]["task_tag"]
        == by_key[(ENG_B, "unlabelled")]["task_tag"]
        == "unlabelled"
    )


# ---------------------------------------------------------------------------
# Journey F -- the team lead and the admin see the same tenant
# ---------------------------------------------------------------------------


def test_journey_the_team_lead_and_the_admin_see_the_same_tenant(
    monkeypatch, dynamodb_mock
):
    """The team lead who owns this one tenant, and the admin who owns every
    tenant, both look at the same tenant and period.

    Walks: seed mixed tagged/untagged spend across two members, read the
    SAME tenant and period first as the team lead (`GET
    /team-lead/tenants/{id}/usage/by-tag`, gated on `usage:read-own-tenant`
    plus tenant ownership) and then as the admin (`GET
    /admin/tenants/{id}/usage/by-tag`, gated on `usage:read-all`) -- two
    different routers, two different permission scopes, both reaching
    `mvp.admin_tenants.usage_by_tag_response`, the one shared
    implementation.

    Asserts the two bodies are byte-for-byte the same (a fact the team lead
    can see must never read as a different number on the admin screen for
    the tenant he actually owns), and separately that neither body carries
    anything the other should not -- no email, no PII hash, nothing beyond
    the six fields `UsageByTagRow` declares -- so "the same" is not hiding
    "and also more" on either side.
    """
    _seed_permissions(dynamodb_mock)
    period = _seed_tenant(monkeypatch, members=[ENG_A, ENG_B])
    a = _PipelineUser(user_id=ENG_A, org_id=TENANT)
    b = _PipelineUser(user_id=ENG_B, org_id=TENANT)
    _send(a, header="release-9", cost_micro=9_000_000)
    _send(b, header=None, cost_micro=4_000_000)

    seat = _Seat(TENANT)
    client = _journey_client(seat)

    seat.take(LEAD, "lead@journey", ["team_lead"])
    as_lead = client.get(
        f"/api/mvp/team-lead/tenants/{TENANT}/usage/by-tag?period={period}"
    )
    assert as_lead.status_code == 200, as_lead.text

    seat.take(ADMIN, "admin@journey", ["admin"])
    as_admin = client.get(
        f"/api/mvp/admin/tenants/{TENANT}/usage/by-tag?period={period}"
    )
    assert as_admin.status_code == 200, as_admin.text

    assert as_lead.json() == as_admin.json(), (
        "the same tenant and period must read the same on both screens: "
        f"lead={as_lead.json()} admin={as_admin.json()}"
    )
    for row in as_lead.json()["rows"]:
        assert set(row.keys()) == _ROW_FIELDS, (
            f"a row is carrying more than the six declared fields: {row}"
        )
        assert "@" not in row["user_id"], "user_id must be an id, never an email"
