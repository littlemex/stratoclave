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
     spend, and the one conclusion it still does not support -- on purpose.
  E. One tagger, one non-tagger, on the administrator's one tenant-wide
     screen: what it tells him about each of them now, and the one thing it
     still does not.
  F. The team lead who owns this one tenant, and the admin who owns every
     tenant, reading the same tenant and period side by side.

Journeys D and E were updated after their first versions found that a
row's total could not be told apart from the work's total, and that two
"unlabelled" rows for two different people were indistinguishable beyond
the user_id column. Two rows-level counts (`absent_count`,
`dropped_grammar_count`) and a `tag_total_is_a_lower_bound` disclosure flag
were added in response -- both journeys now assert what changed. Neither
finding was fully closed, deliberately: attributing an untagged request to
the work it names would mean the gateway inferring what the request was
for, the boundary this design is built against, and the two rows still
carry the identical tag string no matter how differently their counts
read. Both journeys say so explicitly rather than passing quietly.
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
    "user_id", "task_tag", "requests", "absent_count", "dropped_grammar_count",
    "cost_microusd",
    # How many of `requests` carried no cost at all, so `cost_microusd` is missing
    # them. Declared here deliberately rather than by loosening the assertion below:
    # that assertion exists so a row cannot silently gain a field, and it did its job --
    # this addition had to be decided, not absorbed. It is part of the SAME question
    # this journey asks ("is this figure the spend?"), because a total that is missing
    # requests is a different kind of floor from one that is merely missing the
    # untagged ones.
    "requests_without_cost",
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

    What the report does NOT let him conclude: that `migration-42`'s total
    IS the tenant's migration spend for the month. It is a floor, not a
    total. This used to be silent -- nothing in the response said so before
    B's own timesheet did. It no longer is: `tag_total_is_a_lower_bound`
    is now a field on the response itself, and B's row now carries
    `absent_count`, so he can see, directly, that four of her requests never
    asserted anything at all.

    What is still true, deliberately, after that fix: knowing a shortfall
    EXISTS is not the same as being able to size or attribute it. He can see
    `absent_count=4` on B's row; he cannot see how many of those four were
    migration work and how many were her unrelated lookup, because nothing
    short of the gateway inferring what an untagged request was for could
    tell him -- and that inference is the boundary this design is built
    against, not a gap left open by oversight. This journey now ends
    knowing the shortfall exists and still unable to attribute it; that is
    the honest end state, not a residual defect.
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
    assert set(b_row.keys()) == _ROW_FIELDS

    # The fix: the response now says its own totals are a floor, and B's row
    # now says how many of her requests never asserted anything at all.
    assert body["tag_total_is_a_lower_bound"] is True, (
        "the response must disclose that a tag's total is a floor on the "
        f"work it names, not the work's total: {body}"
    )
    assert b_row["absent_count"] == 4, (
        f"B's row must now count her four unasserted requests: {b_row}"
    )
    assert b_row["dropped_grammar_count"] == 0, (
        "none of B's calls carried a header at all, so none were dropped"
    )
    assert migration_row["absent_count"] == 0 and migration_row["dropped_grammar_count"] == 0, (
        "every request under A's own tag is a real assertion"
    )

    # What is still true, on purpose: `absent_count=4` tells him a shortfall
    # of unknown composition exists; it does not tell him how much of it is
    # migration work. There is no field that could -- attributing an
    # untagged request to the tag it should have carried is exactly the
    # inference this design refuses to make.
    real_migration_spend = 60_000_000 + 3 * 20_000_000
    assert migration_row["cost_microusd"] < real_migration_spend, (
        "the assertion this journey exists to fail loudly the day "
        "aggregation starts finding B's untagged migration work on its own: "
        f"tagged={migration_row['cost_microusd']} real={real_migration_spend}"
    )
    assert b_row["absent_count"] == b_row["requests"], (
        "the count says ALL four are unattributed; it cannot say which "
        "three were secretly migration work -- there is no field for that "
        f"split, deliberately: {b_row}"
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
    partition alone.

    This journey's first version ended on a gap: A's "unlabelled" $2 and
    B's "unlabelled" $24 carried the identical tag string, and nothing but
    the user_id column he reads himself said one was a deliberate one-off
    and the other was the entirety of her month. `absent_count` mitigates
    that, and this journey now asserts the mitigation directly: A's
    outlier row reads `absent_count=1`, B's reads `absent_count=4` -- a
    small number next to a large one, legible without opening the user_id
    column at all. What the fix does NOT change, and this journey still
    names: both rows carry the identical `task_tag` string, "unlabelled",
    either way. The count differs; the string he would filter or group by
    does not.
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

    # The mitigation: he no longer needs the user_id column to tell an
    # outlier from a whole untagged month -- the counts alone say so.
    a_unlabelled = by_key[(ENG_A, "unlabelled")]
    b_unlabelled = by_key[(ENG_B, "unlabelled")]
    assert a_unlabelled["absent_count"] == 1, (
        f"A's one deliberate outlier: {a_unlabelled}"
    )
    assert b_unlabelled["absent_count"] == 4, (
        f"B's entire untagged month: {b_unlabelled}"
    )
    assert a_unlabelled["absent_count"] < b_unlabelled["absent_count"], (
        "a small count next to a large one is legible on its own, without "
        "reading which user_id owns which row"
    )

    # What the fix does NOT change, and this journey still names: the tag
    # string itself -- the thing a filter or a group-by would key on -- is
    # still identical for both of them.
    assert a_unlabelled["task_tag"] == b_unlabelled["task_tag"] == "unlabelled", (
        "the count differs; the string he would filter or group by does not"
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
    the tenant he actually owns -- including the two newer per-row counts,
    `absent_count`/`dropped_grammar_count`, which this parity check covers
    for free since it compares the whole body rather than naming fields),
    and separately that neither body carries anything the other should not
    -- no email, no PII hash, nothing beyond the eight fields
    `UsageByTagRow` declares -- so "the same" is not hiding "and also more"
    on either side.
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
