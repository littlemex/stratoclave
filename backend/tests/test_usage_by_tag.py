"""Tests for grouping usage by (user, tag, period), and for what the
aggregation's response tells a consumer of it.

Three requests split across two tags, plus one untagged, must aggregate
into exactly three rows with correct per-row totals — tested directly
against `UsageLogsRepository.aggregate_by_tag`, since the endpoint that
will eventually call it is reachable only by its HTTP path, while the
aggregation itself is fully specified as a `TagAggregate` of
`TagAggregateRow`.

The response must also state, as fields rather than as documentation, that
the tag is the caller's own unverified assertion, and the caveats that
make its retention figure honest: DynamoDB TTL deletion is asynchronous, so
a row past the retention figure can still be returned, and a period
straddling that boundary can fold incompletely. `TagAggregate` itself
carries no such fields (only rows / truncated / pages_read / legacy_rows),
so this half is verifiable only over real HTTP, mounting the three routers
the by-tag paths actually live in: `mvp.me`, `mvp.admin_tenants`, and
`mvp.team_lead`. Because the admin route and its team-lead mirror share one
implementation, this file also asserts they return byte-identical bodies
for the same tenant and period — nothing else here checks that directly,
and two routes could otherwise pass individually while quietly diverging.

The aggregation reads a tenant's partition over a period and folds it in
memory, which is unbounded for a large tenant, so it must stop at a named,
bounded page count and say so honestly in the response rather than reading
past it silently — tested against real moto pagination (DynamoDB's own
~1MB-per-response page boundary), not an artificially small `Limit` this
suite controls, so the test holds regardless of what per-page `Limit` the
implementation happens to choose internally.

A `period` that does not match `YYYY-MM` is FastAPI's own 422 (`Query`
pattern validation) — the same mechanism every sibling endpoint already
uses to validate a period, so a malformed value here must not be a
hand-rolled, endpoint-specific error.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest
from boto3.dynamodb.conditions import Key as boto3_key


# ---------------------------------------------------------------------------
# Grouping by (user, tag, period): three requests under two tags plus one
# untagged aggregate into three rows with correct totals.
# ---------------------------------------------------------------------------

class TestUsageAggregatesByUserTagAndPeriod:
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
# The page bound is real and named, and truncation is reported honestly
# rather than silently reading past it.
# ---------------------------------------------------------------------------

class TestPageBoundAndTruncationFlag:
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
# The response states tag_is_caller_asserted and retention_policy_days
# (with its two caveat booleans) as FIELDS, and the admin/team-lead mirror
# cannot drift. Only reachable over real HTTP (see module docstring), so
# this mounts the three routers the by-tag endpoints actually live in.
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
    """Mounts the three routers the by-tag endpoints actually live in:
    `mvp.me` (`/me/usage/by-tag`), `mvp.admin_tenants` (the tenant-scoped
    admin route AND the one shared implementation — the same
    admin-plus-team-lead pattern the pool-budget routes already use), and
    `mvp.team_lead` (the mirror that calls that same shared
    implementation)."""
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


class TestByTagResponseDisclosesRetentionCaveatsAndValidatesPeriod:
    def test_by_tag_response_carries_both_statements_as_fields(self, by_tag_client):
        """The response states `retention_policy_days`, and
        `history_horizon_days` must be absent rather than kept alongside it
        as an alias: the old name claimed a query horizon the query does
        not enforce, since DynamoDB TTL deletion is asynchronous, so a row
        older than the number can still be returned and a period straddling
        it can fold incompletely. Keeping both names would ship that false
        claim beside its own correction, and two fields carrying one fact
        is a duplication this suite treats as a defect on sight -- this is
        a deliberate naming decision this test enforces, not a test written
        to match whatever an implementation happens to call the field.

        `tag_is_caller_asserted` is a separate statement and still asserted
        here. The two caveat booleans
        (`retention_deletion_is_asynchronous`,
        `retention_boundary_period_may_fold_incompletely`) are the part that
        makes the retained number honest -- a consumer of this JSON is told
        both the number and its limits, so a test that checked only the
        number and not these caveats would be checking the weaker half of
        the same obligation.
        """
        from dynamo.tenant_budgets import current_period

        resp = by_tag_client.get(f"/api/mvp/me/usage/by-tag?period={current_period()}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tag_is_caller_asserted"] is True, (
            "the by-tag response must state, as a field, that the tag is "
            "the caller's unverified assertion"
        )
        assert body["retention_policy_days"] == 90, (
            "the by-tag response must state, as a field, the 90-day "
            f"retention policy -- got {body.get('retention_policy_days')!r}"
        )
        assert "history_horizon_days" not in body, (
            "history_horizon_days must be absent outright (not kept as an "
            "alias beside retention_policy_days) -- its return would ship "
            "the false 'query horizon' claim next to its own correction, "
            "which is exactly the duplication one name for one number is "
            "meant to prevent"
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
        """`period` is validated by the SAME `Query(default=None,
        pattern=r"^\\d{4}-\\d{2}$")` every sibling endpoint uses
        (`team_lead.py:269`, `admin_tenants.py:967` etc.), so a malformed
        value is FastAPI's own 422 -- the framework's validation, not a
        hand-rolled endpoint-specific error. Do not "fix" this to 400:
        consistency with the rest of the API validating a period the same
        way is the reason to keep it a 422 here.
        """
        resp = by_tag_client.get("/api/mvp/me/usage/by-tag?period=not-a-period")
        assert resp.status_code == 422, (
            f"malformed period must be FastAPI's 422 (Query pattern "
            f"validation) -- got {resp.status_code}: {resp.text}"
        )


class TestAdminAndTeamLeadCannotDrift:
    """The admin route and its team-lead mirror share one implementation so
    the two cannot drift apart. Nothing else in this suite checks that
    directly — a test could pass both routes individually while each
    hand-rolled its own, subtly different, aggregation logic. This asserts
    the observable consequence: for the same tenant and period, the two
    routes must return the identical body.
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
            "which is exactly the drift a shared implementation is meant "
            "to prevent"
        )
