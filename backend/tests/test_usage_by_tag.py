"""PR1 (per-user-money-raises, task tags) — P1.7, P1.8, P1.9.

Contract: `change-pipeline/per-user-money-raises/03-impl/HANDOFF-PR1.md`.

  P1.7 "Usage cannot be grouped by (user, tag, period) | H | Requirement 4 |
  three requests under two tags and one untagged aggregate into three rows
  with correct totals" — tested directly against
  `UsageLogsRepository.aggregate_by_tag`, the interface's fully-specified new
  read (`dynamo/usage_logs.py`). The endpoint that will eventually call it is
  named only by path in the interface, not by file, so the row-shape /
  totals contract is pinned at the repository boundary, which the interface
  DOES name precisely (a `TagAggregate` of `TagAggregateRow`).

  P1.8 "The aggregation must state that the tag is the caller's unverified
  assertion, and must state its 90-day horizon | A | ... | the response
  carries both statements as fields, not as prose in a doc" — `TagAggregate`
  itself has no `tag_is_caller_asserted` / `retention_policy_days` fields (see
  the interface's own dataclass listing: rows / truncated / pages_read /
  legacy_rows only) — those fields are added ONLY in the HTTP response
  body shown in the interface's "Endpoints" section. So P1.8, uniquely among
  this file's three entries, can only be verified over real HTTP.

  Amendment A7.2 renames `history_horizon_days` to `retention_policy_days`
  and REMOVES the old name (not kept as an alias): DynamoDB TTL deletion is
  asynchronous, so the old name claimed a query horizon the query does not
  enforce. A6.3 adds two caveat booleans that make the retained number
  honest: `retention_deletion_is_asynchronous` and
  `retention_boundary_period_may_fold_incompletely`. This is a
  contract-driven test change, not a test fitted to code — see the test's
  own docstring below.

  Amendment A2 names the endpoint homes: `GET /me/usage/by-tag` lives in
  `backend/mvp/me.py`; the admin route AND the one shared implementation
  live in `backend/mvp/admin_tenants.py` (A4 corrects A2, which first said
  `admin_usage.py`: the route is the tenant-scoped
  `/admin/tenants/{tenant_id}/usage/by-tag`, and the shared implementation
  the pool-budget routes already use for exactly this admin-plus-team-lead
  pattern lives in `admin_tenants.py`); the team-lead mirror lives in
  `backend/mvp/team_lead.py` and calls that same shared implementation, the
  way the pool-budget routes already do. Tested by mounting exactly those
  three routers (no `main.app`, now that the homes are named), and — because
  "one shared implementation so the two cannot drift" is itself a contracted
  property that nothing else in this suite checks — by asserting the admin
  and team-lead routes return byte-identical bodies for the same tenant and
  period.

  P1.9 "The aggregation reads the tenant partition over a period and folds
  in memory, which is unbounded for a large tenant | B | ... | a bounded
  page count with an explicit truncation flag in the response; the bound is
  a named constant" — tested against real moto pagination (DynamoDB's own
  ~1MB-per-response page boundary), not an artificially small `Limit` this
  suite controls, so the test holds regardless of what per-page `Limit` the
  implementation happens to choose internally.

Error contract (shared by all three by-tag endpoints), per Amendment A1:
`period` not matching `YYYY-MM` is **422**, not 400. The interface's original
Error Contracts section stated "400... matching the existing usage
endpoints" — a self-contradiction, since every sibling endpoint validates
`period` with `Query(default=None, pattern=r"^\\d{4}-\\d{2}$")`
(`backend/mvp/team_lead.py:269`, `backend/mvp/admin_tenants.py:967/:1013/
:1067/:1108`), which FastAPI answers with its own 422. A1 withdraws the 400:
`period` is validated by that same `Query(pattern=...)`, so a malformed
value is the FRAMEWORK's validation error, not a hand-rolled one — do not
"fix" this back to 400; the whole point of A1 is that one way of validating
a period across the API beats the specific digits the interface first wrote.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest
from boto3.dynamodb.conditions import Key as boto3_key


# ---------------------------------------------------------------------------
# P1.7 — grouping by (user, tag, period): three requests under two tags plus
# one untagged aggregate into three rows with correct totals.
# ---------------------------------------------------------------------------

class TestP1_7_GroupingByUserTagPeriod:
    def test_two_tags_and_untagged_aggregate_into_three_rows(self, dynamodb_mock):
        from dynamo.usage_logs import UsageLogsRepository
        from dynamo.tenant_budgets import current_period

        repo = UsageLogsRepository()
        period = current_period()
        tenant = "acme-eng"
        user = "user-1"

        # Two requests under "billing-sync".
        repo.record(tenant_id=tenant, user_id=user, user_email="u@x", model_id="m",
                    input_tokens=10, output_tokens=5, cost_microusd=100,
                    task_tag="billing-sync", task_tag_source="asserted")
        repo.record(tenant_id=tenant, user_id=user, user_email="u@x", model_id="m",
                    input_tokens=20, output_tokens=8, cost_microusd=150,
                    task_tag="billing-sync", task_tag_source="asserted")
        # One request under "onboarding".
        repo.record(tenant_id=tenant, user_id=user, user_email="u@x", model_id="m",
                    input_tokens=5, output_tokens=2, cost_microusd=40,
                    task_tag="onboarding", task_tag_source="asserted")
        # One untagged (header absent) request.
        repo.record(tenant_id=tenant, user_id=user, user_email="u@x", model_id="m",
                    input_tokens=7, output_tokens=3, cost_microusd=30,
                    task_tag="unlabelled", task_tag_source="absent")

        result = repo.aggregate_by_tag(tenant_id=tenant, period=period)
        assert len(result.rows) == 3, f"expected 3 rows, got {result.rows!r}"

        by_tag = {r.task_tag: r for r in result.rows}
        assert set(by_tag) == {"billing-sync", "onboarding", "unlabelled"}

        billing = by_tag["billing-sync"]
        assert billing.requests == 2
        assert billing.cost_microusd == 250
        assert billing.input_tokens == 30
        assert billing.output_tokens == 13

        onboarding = by_tag["onboarding"]
        assert onboarding.requests == 1
        assert onboarding.cost_microusd == 40

        untagged = by_tag["unlabelled"]
        assert untagged.requests == 1
        assert untagged.cost_microusd == 30

        assert result.legacy_rows == 0
        assert result.truncated is False

    def test_user_id_filter_restricts_to_one_member(self, dynamodb_mock):
        from dynamo.usage_logs import UsageLogsRepository
        from dynamo.tenant_budgets import current_period

        repo = UsageLogsRepository()
        period = current_period()
        tenant = "acme-eng"

        repo.record(tenant_id=tenant, user_id="user-a", user_email="a@x", model_id="m",
                    input_tokens=10, output_tokens=5, cost_microusd=100,
                    task_tag="billing-sync", task_tag_source="asserted")
        repo.record(tenant_id=tenant, user_id="user-b", user_email="b@x", model_id="m",
                    input_tokens=99, output_tokens=99, cost_microusd=9999,
                    task_tag="billing-sync", task_tag_source="asserted")

        result = repo.aggregate_by_tag(tenant_id=tenant, period=period, user_id="user-a")
        assert len(result.rows) == 1
        assert result.rows[0].user_id == "user-a"
        assert result.rows[0].cost_microusd == 100, (
            "user_id filter must exclude user-b's spend from the aggregate"
        )


# ---------------------------------------------------------------------------
# P1.9 — the page bound is real and named, and truncation is reported
# honestly rather than silently reading past it.
# ---------------------------------------------------------------------------

class TestP1_9_PageBoundIsReal:
    def test_max_pages_is_a_named_constant(self):
        from dynamo.usage_logs import MAX_AGGREGATE_PAGES

        assert isinstance(MAX_AGGREGATE_PAGES, int) and MAX_AGGREGATE_PAGES > 0

    def test_truncation_is_reported_honestly_at_a_real_dynamo_page_boundary(
        self, dynamodb_mock,
    ):
        """3200 real rows reliably span >=2 native DynamoDB query pages under
        moto's own ~1MB-per-response emulation (measured directly against
        this worktree: 3200 rows -> 3030 in the first page, 170 in the
        second) — independent of whatever per-page `Limit` the
        implementation internally chooses. Calling `aggregate_by_tag` with
        `max_pages=1` must stop after the first real page: `truncated=True`,
        `pages_read=1`, and a folded request COUNT strictly less than the
        3200 seeded (proof it actually stopped, not that it silently read
        everything and merely reported a bound in passing)."""
        from dynamo.usage_logs import UsageLogsRepository
        from dynamo.tenant_budgets import current_period

        repo = UsageLogsRepository()
        period = current_period()
        tenant = "acme-eng"
        n = 3200
        t0 = time.time()
        for _ in range(n):
            repo.record(tenant_id=tenant, user_id="user-1", user_email="u@x",
                        model_id="claude-haiku-4-5", input_tokens=100, output_tokens=50,
                        cost_microusd=42, task_tag="billing-sync",
                        task_tag_source="asserted")
        # Sanity: the seed itself must actually span 2+ real Dynamo pages,
        # or this test would prove nothing regardless of the implementation.
        page1 = repo._table.query(KeyConditionExpression=boto3_key("tenant_id").eq(tenant))
        assert "LastEvaluatedKey" in page1, (
            f"seed of {n} rows (took {time.time() - t0:.1f}s) did not span a "
            "second real DynamoDB page under moto -- this test's premise "
            "requires it to; raise n"
        )

        result = repo.aggregate_by_tag(tenant_id=tenant, period=period, max_pages=1)
        assert result.truncated is True
        assert result.pages_read == 1
        total_requests = sum(r.requests for r in result.rows)
        assert 0 < total_requests < n, (
            f"expected a partial fold strictly less than {n} seeded requests "
            f"(max_pages=1 should have stopped early), got {total_requests}"
        )


# ---------------------------------------------------------------------------
# P1.8 — the response states tag_is_caller_asserted and retention_policy_days
# (with its two caveat booleans, A6.3/A7.2) as FIELDS, and the admin/team-lead
# mirror cannot drift. Only reachable
# over real HTTP (see module docstring). Amendment A2 names the endpoint
# homes, so this mounts exactly those three routers rather than main.app.
# ---------------------------------------------------------------------------

@dataclass
class _FakeAdmin:
    """An admin actor: passes `require_permission` (monkeypatched wide open
    below regardless of the exact scope name each route ends up using) AND
    `team_lead._require_owner`'s admin bypass, so the SAME actor can hit the
    `/me`, `/admin` and `/team-lead` routes without needing a second,
    ownership-scoped identity just to prove the admin/team-lead bodies
    match."""

    user_id: str = "admin-usage-by-tag"
    org_id: str = "acme-eng"
    email: str = "admin@example.com"
    roles: list = None
    auth_kind: str = "jwt"
    key_scopes: list = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["admin"]


@pytest.fixture
def by_tag_client(dynamodb_mock, monkeypatch):
    """Mounts exactly the three routers Amendment A2 names as the by-tag
    endpoints' homes, corrected by A4: `mvp.me` (`/me/usage/by-tag`),
    `mvp.admin_tenants` (the tenant-scoped admin route AND the one shared
    implementation — A4 withdraws A2's `admin_usage.py`: the shared
    admin-plus-team-lead pattern already lives in `admin_tenants.py`, where
    the pool-budget routes keep theirs), and `mvp.team_lead` (the mirror
    that calls that same shared implementation)."""
    import mvp.authz as _authz
    monkeypatch.setattr(_authz, "user_has_permission", lambda user, perm: True)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mvp.deps import get_current_user
    from mvp.me import router as me_router
    from mvp.admin_tenants import router as admin_tenants_router
    from mvp.team_lead import router as team_lead_router
    from dynamo.tenants import TenantsRepository

    # `team_lead._require_owner` looks the tenant up regardless of actor
    # role, so it must exist even for the admin bypass path.
    TenantsRepository().create(
        tenant_id="acme-eng", name="Acme Eng", team_lead_user_id="someone-else",
        default_credit=100_000, created_by="admin-usage-by-tag",
    )

    app = FastAPI()
    app.include_router(me_router)
    app.include_router(admin_tenants_router)
    app.include_router(team_lead_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeAdmin()
    return TestClient(app)


class TestP1_8_ResponseStatesCallerAssertedAndHorizonAsFields:
    def test_by_tag_response_carries_both_statements_as_fields(self, by_tag_client):
        """Amendment A7.2 renames `history_horizon_days` to
        `retention_policy_days` and REMOVES the old name outright (it is not
        kept as an alias): the old name claimed a query horizon the query
        does not enforce, since DynamoDB TTL deletion is asynchronous, so a
        row older than the number can still be returned and a period
        straddling it can fold incompletely. This is a contract-driven test
        change, not a test fitted to code -- the field rename and the two
        new caveat booleans below come from A7.2/A6.3, not from reading any
        implementation.

        `tag_is_caller_asserted` is unaffected by A7.2 and is still
        asserted here. The two new caveats
        (`retention_deletion_is_asynchronous`,
        `retention_boundary_period_may_fold_incompletely`) are the part that
        makes the retained number honest -- P1.8 is about what a consumer of
        this JSON is told, so a test that checked only the number and not
        these caveats would be checking the weaker half of the same
        obligation.
        """
        from dynamo.tenant_budgets import current_period

        resp = by_tag_client.get(f"/api/mvp/me/usage/by-tag?period={current_period()}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tag_is_caller_asserted"] is True, (
            "the by-tag response must state, as a field, that the tag is the "
            "caller's unverified assertion -- P1.8"
        )
        assert body["retention_policy_days"] == 90, (
            "the by-tag response must state, as a field, the 90-day "
            f"retention policy -- got {body.get('retention_policy_days')!r}"
        )
        assert "history_horizon_days" not in body, (
            "A7.2 removed history_horizon_days outright (not kept as an "
            "alias beside retention_policy_days) -- its return would ship "
            "the false 'query horizon' claim next to its own correction, "
            "which is exactly the duplication A7.2 exists to prevent"
        )
        assert body["retention_deletion_is_asynchronous"] is True, (
            "the response must state, as a field, that TTL deletion is "
            "asynchronous -- without it, retention_policy_days reads as an "
            "enforced horizon rather than the policy figure it actually is"
        )
        assert body["retention_boundary_period_may_fold_incompletely"] is True, (
            "the response must state, as a field, that a period straddling "
            "the retention boundary may fold incompletely"
        )
        assert "rows" in body and "truncated" in body and "legacy_rows" in body

    def test_period_not_matching_yyyy_mm_is_422(self, by_tag_client):
        """Amendment A1: the 400 is withdrawn. `period` is validated by the
        SAME `Query(default=None, pattern=r"^\\d{4}-\\d{2}$")` every sibling
        endpoint uses (`team_lead.py:269`, `admin_tenants.py:967` etc.), so a
        malformed value is FastAPI's own 422 -- the framework's validation,
        not a hand-rolled endpoint-specific error. Do not "fix" this back to
        400: consistency with the rest of the API is the reason this clause
        exists at all.
        """
        resp = by_tag_client.get("/api/mvp/me/usage/by-tag?period=not-a-period")
        assert resp.status_code == 422, (
            f"malformed period must be FastAPI's 422 (Query pattern "
            f"validation), per amendment A1 -- got {resp.status_code}: {resp.text}"
        )


class TestP1_8_AdminAndTeamLeadCannotDrift:
    """'One shared implementation so the two cannot drift' (interface,
    Endpoints section) is itself a contracted property. Nothing else in
    this suite checks it directly — a test could pass both routes
    individually while each hand-rolled its own, subtly different,
    aggregation logic. This asserts the observable consequence: for the
    same tenant and period, the two routes must return the identical body.
    """

    def test_admin_and_team_lead_bodies_match_for_the_same_tenant_and_period(
        self, by_tag_client,
    ):
        from dynamo.usage_logs import UsageLogsRepository
        from dynamo.tenant_budgets import current_period

        period = current_period()
        UsageLogsRepository().record(
            tenant_id="acme-eng", user_id="user-1", user_email="u@x",
            model_id="m", input_tokens=10, output_tokens=5, cost_microusd=100,
            task_tag="billing-sync", task_tag_source="asserted",
        )

        admin_resp = by_tag_client.get(
            f"/api/mvp/admin/tenants/acme-eng/usage/by-tag?period={period}"
        )
        team_lead_resp = by_tag_client.get(
            f"/api/mvp/team-lead/tenants/acme-eng/usage/by-tag?period={period}"
        )
        assert admin_resp.status_code == 200, admin_resp.text
        assert team_lead_resp.status_code == 200, team_lead_resp.text
        assert admin_resp.json() == team_lead_resp.json(), (
            "the admin route and the team-lead mirror must return the "
            "IDENTICAL body for the same tenant/period -- a difference here "
            "means the two routes are not actually sharing one implementation, "
            "which is the exact drift the interface's 'one shared "
            "implementation' sentence exists to prevent"
        )
