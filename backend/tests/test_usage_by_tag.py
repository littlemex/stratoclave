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
  itself has no `tag_is_caller_asserted` / `history_horizon_days` fields (see
  the interface's own dataclass listing: rows / truncated / pages_read /
  legacy_rows only) — those two fields are added ONLY in the HTTP response
  body shown in the interface's "Endpoints" section. So P1.8, uniquely among
  this file's three entries, can only be verified over real HTTP. Tested
  against `GET /api/mvp/me/usage/by-tag`, reached through the fully-assembled
  `main.app` (not a hand-picked router module) because the interface names
  the PATH, never the FILE that will define it — going through `main.app`
  is the one way to test the contracted path without guessing which of
  `mvp/me.py` / a new `mvp/usage_by_tag.py` / elsewhere ends up owning it.

  P1.9 "The aggregation reads the tenant partition over a period and folds
  in memory, which is unbounded for a large tenant | B | ... | a bounded
  page count with an explicit truncation flag in the response; the bound is
  a named constant" — tested against real moto pagination (DynamoDB's own
  ~1MB-per-response page boundary), not an artificially small `Limit` this
  suite controls, so the test holds regardless of what per-page `Limit` the
  implementation happens to choose internally.

Error contract (shared by all three by-tag endpoints): "`period` not
matching `YYYY-MM` is a 400, matching the existing usage endpoints." This is
flagged in the handoff report: the ACTUAL existing usage endpoints that
validate a `period` this way (`admin_tenants.py`'s pool-budget routes, via
`Query(pattern=r"^\\d{4}-\\d{2}$")`) return FastAPI's default 422 for that
failure, not 400 — measured directly against this worktree, see the report.
The interface's Error Contracts section states "400" as a bare fact twice
elsewhere in the document; I have taken that literal, twice-stated number as
the authoritative commitment over the (inaccurate) precedent it cites, since
the number itself, not the analogy, is the actual interface obligation.
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
# P1.8 — the response states tag_is_caller_asserted and history_horizon_days
# as FIELDS. Only reachable over real HTTP (see module docstring).
# ---------------------------------------------------------------------------

@dataclass
class _FakeUser:
    user_id: str = "user-11111111-1111-1111-1111-111111111111"
    org_id: str = "acme-eng"
    email: str = "test@example.com"
    roles: list = None
    auth_kind: str = "jwt"
    key_scopes: list = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["user"]


@pytest.fixture
def full_app_client(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz
    monkeypatch.setattr(_authz, "user_has_permission", lambda user, perm: True)

    import main
    from mvp.deps import get_current_user
    from fastapi.testclient import TestClient

    main.app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    try:
        with TestClient(main.app) as client:
            yield client
    finally:
        main.app.dependency_overrides.pop(get_current_user, None)


class TestP1_8_ResponseStatesCallerAssertedAndHorizonAsFields:
    def test_by_tag_response_carries_both_statements_as_fields(self, full_app_client):
        from dynamo.tenant_budgets import current_period

        resp = full_app_client.get(
            f"/api/mvp/me/usage/by-tag?period={current_period()}"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tag_is_caller_asserted"] is True, (
            "the by-tag response must state, as a field, that the tag is the "
            "caller's unverified assertion -- P1.8"
        )
        assert body["history_horizon_days"] == 90, (
            "the by-tag response must state, as a field, the 90-day history "
            f"horizon -- got {body.get('history_horizon_days')!r}"
        )
        assert "rows" in body and "truncated" in body and "legacy_rows" in body

    def test_period_not_matching_yyyy_mm_is_400(self, full_app_client):
        """See module docstring: the interface's Error Contracts section
        states this as a bare '400' twice; the endpoint it cites as
        precedent actually returns 422 in this codebase today (verified
        directly). This test enforces the literal, explicitly-stated
        interface number."""
        resp = full_app_client.get("/api/mvp/me/usage/by-tag?period=not-a-period")
        assert resp.status_code == 400, (
            f"malformed period must be a 400 per the interface's Error "
            f"Contracts section, got {resp.status_code}: {resp.text}"
        )
