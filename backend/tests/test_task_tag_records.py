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
way to mutate them mid-flight). The two tests below are the closest
observable proxies the interface actually supports:

  (a) `RequestContext` is declared `@dataclass(frozen=True)` with `task_tag`
      as a plain field, not a method that re-derives on each read — so once
      built at the edge, the value literally cannot be reassigned.
  (b) `SpanDraft` (`mvp/observability/store.py`) is the OTHER place a
      resolved tag is threaded through, and it is frozen too, built once and
      handed to the (possibly-later, possibly-background) emit code. The
      draft carries only the ALREADY-RESOLVED `task_tag`/`task_tag_source`
      strings, never a raw header — so `_emit_sync` has nothing to re-derive
      from even if it wanted to; it can only write back what the draft
      already says. This is flagged in the handoff report as the chosen
      reading of P1.6, since the literal "header mutated after the edge"
      scenario has no HTTP-constructible form.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from unittest.mock import patch

import boto3
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
    def test_request_context_task_tag_field_is_frozen(self):
        """(a) RequestContext is a frozen dataclass; task_tag/task_tag_source
        are plain fields set once by build_request_context, never a
        re-derived property — so nothing downstream can cause a second
        resolution by mutating the context after the edge."""
        from mvp.observability.context import build_request_context

        ctx = build_request_context(
            tenant_id="acme-eng", group_id_header=None, workflow_run_id_header=None,
            task_tag_header="Original-Tag",
        )
        assert ctx.task_tag == "original-tag"
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.task_tag = "mutated-tag"
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.task_tag_source = "asserted"

    def test_span_and_rollup_write_back_exactly_the_drafts_already_resolved_value(
        self, dynamodb_mock,
    ):
        """(b) `SpanDraft` carries only the already-resolved strings; `_emit_sync`
        (mvp/observability/store.py) must write them back VERBATIM under
        `task_tag`/`task_tag_source`, on both the span item and the rollup
        item, 'always' per the interface (unlike UsageLogs, where the two
        kwargs are optional and default-free). If emit-time code re-derived
        the tag from something else instead of trusting the draft, this
        would fail by writing a different value (or none at all)."""
        from mvp.observability import store as S

        draft = S.SpanDraft(
            tenant_id="acme-eng", request_id="req_tag1", span_id="req_tag1",
            group_id=None, workflow_run_id="run-tag1", model_alias="m",
            committed_model_id="cm", committed_region="us-east-1",
            breaker_stage="closed", attempts_total=1, targets_distinct=1,
            stream=True, started_at_ms=1_000,
            task_tag="edge-resolved-tag", task_tag_source="asserted",
        )
        snap = S._AccSnapshot(
            input_tokens=1, output_tokens=2, cache_read_tokens=0,
            cache_write_tokens=0, stop_reason="end_turn", saw_final_usage=True,
        )
        S._emit_sync(draft, "completed", snap)

        table = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "stratoclave-observability")
        span = table.get_item(Key={
            "pk": "TENANT#acme-eng#RUN#run-tag1",
            "sk": f"SPAN#{1000:013d}#req_tag1"})["Item"]
        rollup = table.get_item(
            Key={"pk": "TENANT#acme-eng#RUN#run-tag1", "sk": "ROLLUP"})["Item"]

        assert span["task_tag"] == "edge-resolved-tag"
        assert span["task_tag_source"] == "asserted"
        assert rollup["task_tag"] == "edge-resolved-tag"
        assert rollup["task_tag_source"] == "asserted"
