"""Tests for the task tag reaching a `UsageLogs` row.

A request that asserts a tag must produce a row carrying it. A row written
before this pair of attributes existed must never be mistaken for a row
that was deliberately left untagged — the two are different facts, and
merging them would make every historical row look intentionally
unlabelled. That same ambiguity can also arrive through a different path
than an old row: a reservation made with no request context at all must
still carry the sentinel pair all the way to settle, because a pair that a
missing context can null lands on exactly the same row shape as a legacy
one, just by a different route. And the value a row carries must be the
one resolved at the request's edge, not something re-derived later, because
a second read of the raw header could disagree with what the row claims was
resolved.

The no-request-context property is tested by driving a real reservation
and settle with no `RequestContext` in the chain. The resolved-at-the-edge
property has no HTTP-constructible test of its most literal form:
a sent request's headers are fixed for its whole lifetime, and there is no
library-level way to mutate them mid-flight after the edge has already
resolved them. Instead, the last test below overrides the
`get_request_context` FastAPI dependency directly with a `RequestContext`
resolved from one header string, while the live HTTP request carries a
DIFFERENT header string, and asserts the persisted row carries the
OVERRIDE's value — the one actually resolved at the edge — never a fresh
read of the live header. See that test's own docstring for why this
construction exercises the real code path without needing to know how the
code between the edge and the `UsageLogs` write is internally wired.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest
from boto3.dynamodb.conditions import Key as boto3_key
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# A request with a tag produces a UsageLogs row carrying it.
# Fixture modelled on tests/test_request_context_http.py's api_client.
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


def _mock_converse_stream(**kwargs):
    return {"stream": iter([
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "hi"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 2}}},
    ])}


def _mock_converse(**kwargs):
    return {
        "output": {"message": {"content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 5, "outputTokens": 2},
    }


@pytest.fixture
def api_client(dynamodb_mock, monkeypatch):
    from mvp.anthropic import router as anthropic_router
    from mvp.deps import get_current_user
    import mvp.authz as _authz

    monkeypatch.setattr(_authz, "user_has_permission", lambda user, perm: True)

    from dynamo.user_tenants import UserTenantsRepository
    UserTenantsRepository().ensure(
        user_id=_FakeUser().user_id, tenant_id=_FakeUser().org_id,
        role="user", total_credit=10**9)

    app = FastAPI()
    app.include_router(anthropic_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()

    with patch("mvp.routing.infrarouter.bedrock_client") as mock_routing, \
         patch("mvp.anthropic._bedrock_client") as mock_bedrock:
        mock_routing.return_value.converse_stream.side_effect = _mock_converse_stream
        mock_bedrock.return_value.converse.side_effect = _mock_converse
        yield TestClient(app)


def _post(client, headers=None):
    return client.post("/v1/messages", headers=headers or {}, json={
        "model": "us.anthropic.claude-opus-4-7",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50, "stream": False,
    })


def _usage_row_for(request_id: str) -> dict:
    from dynamo.usage_logs import UsageLogsRepository

    repo = UsageLogsRepository()
    resp = repo._table.query(
        KeyConditionExpression=boto3_key("tenant_id").eq(_FakeUser().org_id)
    )
    matches = [it for it in resp.get("Items", [])
               if it["timestamp_log_id"].endswith(f"#{request_id}")]
    assert matches, f"no UsageLogs row found for request_id={request_id}"
    return matches[0]


class TestATaggedRequestProducesATaggedRow:
    def test_asserted_tag_lands_on_the_usage_log_row(self, api_client):
        resp = _post(api_client, headers={"x-sc-task-tag": "Billing-Sync"})
        assert resp.status_code == 200
        span_id = resp.headers["x-sc-span-id"]
        item = _usage_row_for(span_id)
        assert item.get("task_tag") == "billing-sync", (
            "a request asserting x-sc-task-tag: Billing-Sync must produce a "
            f"UsageLogs row carrying its canonical form; got {item.get('task_tag')!r}"
        )
        assert item.get("task_tag_source") == "asserted"

    def test_absent_tag_still_lands_with_sentinel_and_absent_source(self, api_client):
        """A request with NO header still gets a task_tag written (SENTINEL,
        ABSENT) — distinguishing a genuinely untagged row from a legacy row
        with no attributes at all (see the legacy-row tests below)."""
        resp = _post(api_client)
        assert resp.status_code == 200
        span_id = resp.headers["x-sc-span-id"]
        item = _usage_row_for(span_id)
        assert item.get("task_tag") == "unlabelled"
        assert item.get("task_tag_source") == "absent"


# ---------------------------------------------------------------------------
# A legacy row (no tag attributes at all) must read as "unknown", never as
# "unlabelled"; record() never defaults the attribute at write time.
# ---------------------------------------------------------------------------

class TestLegacyRowsAreUnknownNotUnlabelled:
    def test_record_without_task_tag_kwargs_writes_neither_attribute(self, dynamodb_mock):
        """Exactly like `cache_read_tokens` / `fallback_reason` before it,
        `record()` must not default `task_tag`/`task_tag_source` when the
        caller omits them — a legacy row is simulated by calling `record()`
        the way every caller did before this pair of attributes existed:
        with no task-tag keywords at all."""
        from dynamo.usage_logs import UsageLogsRepository

        item = UsageLogsRepository().record(
            tenant_id="acme-eng", user_id="user-1", user_email="user@acme.example",
            model_id="claude-haiku-4-5", input_tokens=10, output_tokens=5,
        )
        assert "task_tag" not in item, (
            "record() must never substitute SENTINEL for an omitted task_tag "
            "— a legacy row must be indistinguishable from one written before "
            "this pair of attributes existed, i.e. carry neither attribute at all"
        )
        assert "task_tag_source" not in item

    def test_record_accepts_and_persists_both_when_both_supplied(self, dynamodb_mock):
        from dynamo.usage_logs import UsageLogsRepository

        item = UsageLogsRepository().record(
            tenant_id="acme-eng", user_id="user-1", user_email="user@acme.example",
            model_id="claude-haiku-4-5", input_tokens=10, output_tokens=5,
            task_tag="onboarding", task_tag_source="asserted",
        )
        assert item["task_tag"] == "onboarding"
        assert item["task_tag_source"] == "asserted"

    def test_legacy_row_is_counted_not_folded_under_sentinel(self, dynamodb_mock):
        """`aggregate_by_tag`'s `TagAggregate.legacy_rows` is the one reader
        of tag attributes on the read side. A legacy row (no attributes)
        must be counted in `legacy_rows` and must NOT contribute a `rows`
        entry keyed on the sentinel — that would assert "this request was
        unlabelled", a fact the row does not contain. A genuinely untagged
        row (sentinel + source=absent) DOES belong under the sentinel in
        `rows`, and the two must not be conflated."""
        from dynamo.usage_logs import UsageLogsRepository
        from dynamo.tenant_budgets import current_period

        repo = UsageLogsRepository()
        period = current_period()

        # A legacy row: no task_tag kwargs at all.
        repo.record(
            tenant_id="acme-eng", user_id="user-1", user_email="user@acme.example",
            model_id="claude-haiku-4-5", input_tokens=10, output_tokens=5,
            cost_microusd=100,
        )
        # A genuinely untagged row: header absent, resolved to the
        # sentinel with source=absent.
        repo.record(
            tenant_id="acme-eng", user_id="user-1", user_email="user@acme.example",
            model_id="claude-haiku-4-5", input_tokens=20, output_tokens=10,
            cost_microusd=200, task_tag="unlabelled", task_tag_source="absent",
        )

        result = repo.aggregate_by_tag(tenant_id="acme-eng", period=period)
        assert result.legacy_rows == 1, (
            f"expected exactly the one legacy row counted as legacy, got "
            f"{result.legacy_rows}"
        )
        sentinel_rows = [r for r in result.rows if r.task_tag == "unlabelled"]
        assert len(sentinel_rows) == 1, (
            "exactly one row should carry the sentinel in `rows` (the "
            "genuinely untagged request) — the legacy row must not also "
            f"appear here; got rows={result.rows!r}"
        )
        assert sentinel_rows[0].requests == 1
        assert sentinel_rows[0].cost_microusd == 200


# ---------------------------------------------------------------------------
# A reservation made with no request context at all must still carry the
# sentinel pair through to settle — never neither attribute.
# ---------------------------------------------------------------------------

class TestReservationWithNoRequestContextStillCarriesTheSentinelPair:
    """A route can reach the reservation chokepoint (`reserve_credit_for_model`)
    with no `RequestContext` at all and pass `task_tag=None,
    task_tag_source=None` — the same shape every route already sends for its
    other correlation ids when the context is absent (`ctx.X if ctx else
    None`). The reservation this produces, and the `UsageLogs` row a
    subsequent settle writes for it, must carry the sentinel pair
    (`"unlabelled"` / `"absent"`) — never neither attribute, which would make
    the row indistinguishable from one written before this pair of
    attributes existed. A pair that can be silently nulled by an absent
    context is the same ambiguity a legacy row exists to be told apart
    from, arriving through a different path than a legacy row does.
    """

    def _seed(self, tenant: str) -> str:
        from dynamo.tenants import TenantsRepository
        from dynamo.tenant_budgets import TenantBudgetsRepository, current_period

        TenantsRepository().create(
            tenant_id=tenant, name="No Request Context", team_lead_user_id="admin-owned",
            default_credit=10_000_000, created_by="test")
        period = current_period()
        TenantBudgetsRepository().set_manual_limit(
            tenant_id=tenant, period=period, manual_limit_microusd=1_000_000_000)
        return period

    def test_settle_without_a_request_context_writes_the_sentinel_pair(
        self, dynamodb_mock,
    ):
        from mvp._pipeline import reserve_credit_for_model, settle_reservation_and_log
        from dynamo.user_tenants import UserTenantsRepository
        from dynamo.usage_logs import UsageLogsRepository
        from mvp.deps import AuthenticatedUser

        tenant = "no-request-context-tenant"
        user_id = "user-no-ctx"
        self._seed(tenant)
        UserTenantsRepository().ensure(
            user_id=user_id, tenant_id=tenant, role="user", total_credit=10 ** 12)
        user = AuthenticatedUser(
            user_id=user_id, email="noctx@test.example", org_id=tenant, roles=["user"],
            raw_claims={}, auth_kind="jwt", key_scopes=None, api_key_hash=None,
        )

        ctx = reserve_credit_for_model(
            user, reservation_tokens=500, model_name="claude-sonnet-5",
            input_tokens_est=400, max_output_tokens=100,
            # No RequestContext reached this call — the same shape a route
            # already sends for group_id/workflow_run_id/request_id when
            # its own context is None.
            task_tag=None, task_tag_source=None,
        )
        assert ctx.task_tag == "unlabelled", (
            "a reservation made with no request context must carry the "
            f"sentinel, not None — got {ctx.task_tag!r}"
        )
        assert ctx.task_tag_source == "absent"

        settle_reservation_and_log(
            user=user, tenants_repo=ctx.tenants_repo, reservation=500,
            actual_input_tokens=350, actual_output_tokens=80,
            model_id="us.anthropic.claude-sonnet-5", context=ctx,
        )

        items = UsageLogsRepository()._table.query(
            KeyConditionExpression=boto3_key("tenant_id").eq(tenant)
        ).get("Items", [])
        assert items, "settle did not write a UsageLogs row"
        item = items[0]

        assert "task_tag" in item and "task_tag_source" in item, (
            "a request with no request context must still write BOTH "
            "attributes, carrying the sentinel pair — writing neither "
            "attribute makes this row indistinguishable from one written "
            "before the pair existed, which is the exact ambiguity this "
            "pair exists to prevent"
        )
        assert item["task_tag"] == "unlabelled"
        assert item["task_tag_source"] == "absent"

    def test_the_settle_ledger_event_carries_the_pair(self, dynamodb_mock):
        """The SETTLE event is where the charge and its attribution meet.

        The usage row and the ledger row are written by the same settle, but by
        different builders, and only one of them was wired: the settle event read
        `group_id` out of `facts` and not the pair, while the pair was sitting in
        the same dict. So a row asserting what was spent carried no answer to what
        it was spent on, and every check that looked at the usage row passed.

        Drives the real settle rather than the builder, because the builder always
        wrote whatever it was handed — the defect was entirely in what it was
        handed.
        """
        from mvp._pipeline import reserve_credit_for_model, settle_reservation_and_log
        from dynamo.credit_ledger import CreditLedgerRepository, ledger_pk
        from dynamo.user_tenants import UserTenantsRepository
        from mvp.deps import AuthenticatedUser

        tenant = "settle-ledger-tag-tenant"
        user_id = "user-ledger-tag"
        period = self._seed(tenant)
        UserTenantsRepository().ensure(
            user_id=user_id, tenant_id=tenant, role="user", total_credit=10 ** 12)
        user = AuthenticatedUser(
            user_id=user_id, email="ledger@test.example", org_id=tenant, roles=["user"],
            raw_claims={}, auth_kind="jwt", key_scopes=None, api_key_hash=None,
        )

        ctx = reserve_credit_for_model(
            user, reservation_tokens=500, model_name="claude-sonnet-5",
            input_tokens_est=400, max_output_tokens=100,
            task_tag="migration-42", task_tag_source="asserted",
        )
        settle_reservation_and_log(
            user=user, tenants_repo=ctx.tenants_repo, reservation=500,
            actual_input_tokens=350, actual_output_tokens=80,
            model_id="us.anthropic.claude-sonnet-5", context=ctx,
        )

        rows = CreditLedgerRepository()._table.query(
            KeyConditionExpression=boto3_key("pk").eq(ledger_pk(tenant, period))
        ).get("Items", [])
        settles = [r for r in rows if r.get("event_type") == "SETTLE"]
        assert settles, (
            f"no SETTLE event was written; ledger rows were "
            f"{[r.get('event_type') for r in rows]}"
        )
        for row in settles:
            assert row.get("task_tag") == "migration-42", (
                "the SETTLE event must carry the tag the reservation was made "
                f"under; got {row.get('task_tag')!r}. An aggregation over the "
                "ledger — the record of what was actually charged — would "
                "otherwise have no attribution at all"
            )
            assert row.get("task_tag_source") == "asserted"


# ---------------------------------------------------------------------------
# Resolved once at the edge and carried, not re-read at emit time.
# ---------------------------------------------------------------------------

class TestTheTagIsCarriedFromTheEdgeNotReReadAtEmit:
    """Overrides the `get_request_context` FastAPI dependency with a
    `RequestContext` resolved from ONE header string, while the live HTTP
    request carries a DIFFERENT header string, and asserts the persisted
    row carries the OVERRIDE's value — not whatever a fresh read of the
    live header would have produced. A test that only checked that
    `RequestContext` is frozen, or that a hand-built span record echoes
    whatever it is constructed with, would pass equally well against an
    implementation that re-reads the header at emit time, since neither of
    those checks ever puts a second, different header value in front of
    the code under test — this one does.

    The construction: `get_request_context` (`mvp.deps`) is the FastAPI
    dependency that resolves the tag "at the edge" — it is already
    `Depends(...)`-injected into `mvp.anthropic`'s handler (imported by name
    from `mvp.deps`, so it is the identical callable object regardless of
    which module's namespace names it). Overriding it via
    `app.dependency_overrides` replaces dependency resolution entirely: the
    override can return a `RequestContext` built with ANY `task_tag`,
    independent of whatever header the live HTTP request actually carries.

    So the request sent on the wire carries `x-sc-task-tag: Header-Value`
    (which `task_tag.resolve` would canonicalise to `"header-value"` if
    read fresh), while the injected context carries `task_tag=
    "context-value"` (built via the real `build_request_context`, with a
    DIFFERENT header string, so nothing here hand-constructs an internal
    field). A correct implementation carries the edge value through to the
    `UsageLogs` row: `"context-value"`. An implementation that re-reads the
    header at emit time would instead write `"header-value"` — the two are
    deliberately different strings so a re-reading implementation cannot
    accidentally satisfy this test.
    """

    def test_settle_path_carries_the_edge_context_not_a_fresh_header_read(
        self, dynamodb_mock, monkeypatch,
    ):
        from mvp.anthropic import router as anthropic_router
        from mvp.deps import get_current_user, get_request_context
        from mvp.observability.context import build_request_context
        import mvp.authz as _authz

        monkeypatch.setattr(_authz, "user_has_permission", lambda user, perm: True)

        from dynamo.user_tenants import UserTenantsRepository
        UserTenantsRepository().ensure(
            user_id=_FakeUser().user_id, tenant_id=_FakeUser().org_id,
            role="user", total_credit=10**9)

        # Built through the real constructor (not hand-assembled), with a
        # header string the live HTTP request will NOT send.
        edge_ctx = build_request_context(
            tenant_id=_FakeUser().org_id, group_id_header=None,
            workflow_run_id_header=None, task_tag_header="Context-Value",
        )
        assert edge_ctx.task_tag == "context-value"  # sanity: canonical form

        app = FastAPI()
        app.include_router(anthropic_router)
        app.dependency_overrides[get_current_user] = lambda: _FakeUser()
        app.dependency_overrides[get_request_context] = lambda: edge_ctx

        with patch("mvp.routing.infrarouter.bedrock_client") as mock_routing, \
             patch("mvp.anthropic._bedrock_client") as mock_bedrock:
            mock_routing.return_value.converse_stream.side_effect = _mock_converse_stream
            mock_bedrock.return_value.converse.side_effect = _mock_converse
            client = TestClient(app)
            # The WIRE header differs from the injected context's tag. If
            # anything downstream re-read this header instead of trusting
            # edge_ctx, the row would carry "header-value" instead.
            resp = client.post("/v1/messages", headers={"x-sc-task-tag": "Header-Value"},
                                json={
                                    "model": "us.anthropic.claude-opus-4-7",
                                    "messages": [{"role": "user", "content": "hi"}],
                                    "max_tokens": 50, "stream": False,
                                })

        assert resp.status_code == 200, resp.text
        item = _usage_row_for(edge_ctx.request_id)
        assert item.get("task_tag") == "context-value", (
            "the UsageLogs row must carry the value the EDGE (injected "
            "RequestContext) resolved, not a fresh read of the live "
            f"x-sc-task-tag header — got {item.get('task_tag')!r} (the "
            "header's own canonical form would have been 'header-value')"
        )
        assert item.get("task_tag") != "header-value"
        assert item.get("task_tag_source") == "asserted"
