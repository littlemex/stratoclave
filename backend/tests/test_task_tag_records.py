"""PR1 (per-user-money-raises, task tags) — P1.1, P1.5, P1.6.

Contract: `change-pipeline/per-user-money-raises/03-impl/HANDOFF-PR1.md`.

  P1.1 "No record carries a task tag, so usage cannot be grouped by the work
  it was for | H | Requirement 4 is unsatisfiable without it | a request with
  a tag produces a `UsageLogs` row carrying it"

  P1.5 "A record written before this PR must not read as `unlabelled` | A |
  Absence is a legacy fact, not a labelling fact... | a row without the
  attributes is reported as `unknown`, never as `unlabelled`; the reader has
  no default"

  P1.6 "The tag must be resolved once at the edge and carried, not re-read at
  emit time | D | A second read can disagree with the value the record
  claims was resolved | a request whose header is mutated after the edge
  still records the edge's value"

P1.6's own literal scenario — a request whose header changes AFTER the edge
resolved it — cannot be constructed against a real HTTP client (a sent
request's headers are fixed for its whole lifetime; there is no library-level
way to mutate them mid-flight).

Amendment A2 replaces that scenario with a constructible one, and this file
now tests it directly rather than through a structural proxy: the original
two proxy tests (RequestContext's frozen-ness; SpanDraft echoing whatever it
is handed) each asserted a fact a RE-READING implementation would ALSO
satisfy, so neither one actually distinguished "carried" from "re-read".
`TestP1_6_ResolvedOnceAndCarried` now overrides the `get_request_context`
FastAPI dependency (`mvp.deps`) with a `RequestContext` resolved from ONE
header string, while the live HTTP request carries a DIFFERENT header
string, and asserts the persisted row carries the OVERRIDE's value — the one
"the edge" actually resolved — not whatever a fresh read of the live header
would have produced. See that class's own docstring for why this
construction is faithful to the interface (`get_request_context` IS "the
edge" the interface's `build_request_context` describes) without needing to
know or guess how the code between the edge and the `UsageLogs` write is
internally wired.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest
from boto3.dynamodb.conditions import Key as boto3_key
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# P1.1 — a request with a tag produces a UsageLogs row carrying it.
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


class TestP1_1_ATaggedRequestProducesATaggedRow:
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
        ABSENT) — distinguishing a post-PR untagged row from a pre-PR
        legacy row with no attributes at all (see P1.5 below)."""
        resp = _post(api_client)
        assert resp.status_code == 200
        span_id = resp.headers["x-sc-span-id"]
        item = _usage_row_for(span_id)
        assert item.get("task_tag") == "unlabelled"
        assert item.get("task_tag_source") == "absent"


# ---------------------------------------------------------------------------
# P1.5 — a pre-PR row (no tag attributes at all) must read as "unknown",
# never as "unlabelled"; record() never defaults the attribute at write time.
# ---------------------------------------------------------------------------

class TestP1_5_LegacyRowsAreUnknownNotUnlabelled:
    def test_record_without_task_tag_kwargs_writes_neither_attribute(self, dynamodb_mock):
        """Write-side precondition for P1.5: exactly like `cache_read_tokens`
        / `fallback_reason` before it, `record()` must not default
        `task_tag`/`task_tag_source` when the caller omits them — a legacy
        (pre-PR) row is simulated by calling `record()` the way every
        caller did before this PR: with no task-tag keywords at all."""
        from dynamo.usage_logs import UsageLogsRepository

        item = UsageLogsRepository().record(
            tenant_id="acme-eng", user_id="user-1", user_email="user@acme.example",
            model_id="claude-haiku-4-5", input_tokens=10, output_tokens=5,
        )
        assert "task_tag" not in item, (
            "record() must never substitute SENTINEL for an omitted task_tag "
            "— a legacy row must be indistinguishable from one written before "
            "this PR shipped, i.e. carry neither attribute at all"
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
        """Read-side: `aggregate_by_tag`'s own contract (dynamo/usage_logs.py
        `TagAggregate.legacy_rows`) is the one described reader of tag
        attributes. A pre-PR row (no attributes) must be counted in
        `legacy_rows` and must NOT contribute a `rows` entry keyed on the
        sentinel — that would assert "this request was unlabelled", a fact
        the row does not contain. A genuine post-PR untagged row (sentinel +
        source=absent) DOES belong under the sentinel in `rows`, and the two
        must not be conflated."""
        from dynamo.usage_logs import UsageLogsRepository
        from dynamo.tenant_budgets import current_period

        repo = UsageLogsRepository()
        period = current_period()

        # A pre-PR row: no task_tag kwargs at all.
        repo.record(
            tenant_id="acme-eng", user_id="user-1", user_email="user@acme.example",
            model_id="claude-haiku-4-5", input_tokens=10, output_tokens=5,
            cost_microusd=100,
        )
        # A genuine post-PR untagged row: header absent, resolved to the
        # sentinel with source=absent.
        repo.record(
            tenant_id="acme-eng", user_id="user-1", user_email="user@acme.example",
            model_id="claude-haiku-4-5", input_tokens=20, output_tokens=10,
            cost_microusd=200, task_tag="unlabelled", task_tag_source="absent",
        )

        result = repo.aggregate_by_tag(tenant_id="acme-eng", period=period)
        assert result.legacy_rows == 1, (
            f"expected exactly the one pre-PR row counted as legacy, got "
            f"{result.legacy_rows}"
        )
        sentinel_rows = [r for r in result.rows if r.task_tag == "unlabelled"]
        assert len(sentinel_rows) == 1, (
            "exactly one row should carry the sentinel in `rows` (the genuine "
            "post-PR untagged request) — the legacy row must not also appear "
            f"here; got rows={result.rows!r}"
        )
        assert sentinel_rows[0].requests == 1
        assert sentinel_rows[0].cost_microusd == 200


# ---------------------------------------------------------------------------
# P1.6 — resolved once at the edge and carried, not re-read at emit time.
# ---------------------------------------------------------------------------

class TestP1_6_ResolvedOnceAndCarried:
    """Amendment A2 replaces the original proxy tests here (RequestContext's
    frozen-ness; SpanDraft echoing itself) with the real test the
    coordinator specified: a re-reading implementation would satisfy both of
    those proxies too, since neither one ever puts a SECOND, DIFFERENT
    header value in front of the code under test.

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
    header at emit time (the defect P1.6 exists to catch) would instead
    write `"header-value"` — the two are deliberately different strings so
    a re-reading implementation cannot accidentally satisfy this test.
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
