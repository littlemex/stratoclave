"""Tests for the per-request `cost_microusd` and `request_id` fields the
caller can now read back from `GET /api/mvp/me/usage-history`.

Covers:
  * `UsageLogsRepository.record` persists an explicit `request_id` attribute
    (equal to the id it already folds into the `timestamp_log_id` sort key),
    rather than requiring a reader to parse that key's internal
    `{iso}#{log_id}` shape to recover it
  * a legacy-shaped row (written before `request_id` existed as its own
    attribute) reads back as `None`, never as the row's own key suffix
  * `cost_microusd` is already stored by `record()`; `None` (not priced) and
    `0` (priced, rounded to nothing) must read back as DIFFERENT values —
    the same "absent is a different fact from zero" convention this table's
    other optional fields already use
  * the HTTP surface (`GET /api/mvp/me/usage-history`) carries both fields
    through unchanged
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.deps import get_current_user
from mvp.me import router as me_router


# ---------------------------------------------------------------------------
# Repository level: record() -> raw DynamoDB item
# ---------------------------------------------------------------------------
def test_record_persists_request_id_as_its_own_attribute(dynamodb_mock):
    from dynamo.usage_logs import UsageLogsRepository

    repo = UsageLogsRepository()
    repo.record(
        tenant_id="t1", user_id="u1", user_email="e@x.com",
        model_id="m", input_tokens=10, output_tokens=5,
        request_id="req_abc123",
    )
    items = repo._table.query(
        KeyConditionExpression=__import__("boto3").dynamodb.conditions.Key(
            "tenant_id"
        ).eq("t1")
    )["Items"]
    assert len(items) == 1
    item = items[0]
    # Explicitly stored, not merely derivable by parsing the SK.
    assert item["request_id"] == "req_abc123"
    # And it is still the same id folded into the SK (no second source of
    # truth was invented — this is a copy of the same value).
    assert item["timestamp_log_id"].endswith("#req_abc123")


def test_record_without_request_id_still_writes_the_minted_one(dynamodb_mock):
    """When the caller supplies no `request_id`, `record()` mints a fresh
    uuid for `log_id` and writes THAT as `request_id` too — the row always
    carries its own id going forward; there is no third state."""
    from dynamo.usage_logs import UsageLogsRepository

    repo = UsageLogsRepository()
    written = repo.record(
        tenant_id="t1", user_id="u1", user_email="e@x.com",
        model_id="m", input_tokens=1, output_tokens=1,
    )
    assert written["request_id"]
    assert written["timestamp_log_id"].endswith(f"#{written['request_id']}")


def test_cost_microusd_zero_and_absent_are_different_stored_facts(dynamodb_mock):
    from dynamo.usage_logs import UsageLogsRepository

    repo = UsageLogsRepository()
    priced_at_zero = repo.record(
        tenant_id="t1", user_id="u1", user_email="e@x.com",
        model_id="m", input_tokens=1, output_tokens=1, cost_microusd=0,
    )
    not_priced = repo.record(
        tenant_id="t1", user_id="u1", user_email="e@x.com",
        model_id="m", input_tokens=1, output_tokens=1,
    )
    assert priced_at_zero["cost_microusd"] == 0
    assert "cost_microusd" not in not_priced


# ---------------------------------------------------------------------------
# HTTP surface: GET /api/mvp/me/usage-history
# ---------------------------------------------------------------------------
@dataclass
class _FakeUser:
    user_id: str = "u1"
    org_id: str = "acme-eng"
    email: str = "u1@example.com"
    roles: list = None
    auth_kind: str = "jwt"
    key_scopes: list = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["user"]


@pytest.fixture
def client(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz

    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: True)
    app = FastAPI()
    app.include_router(me_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    return TestClient(app)


def test_usage_history_carries_cost_and_request_id(client, dynamodb_mock):
    from dynamo.usage_logs import UsageLogsRepository

    repo = UsageLogsRepository()
    repo.record(
        tenant_id="acme-eng", user_id="u1", user_email="u1@example.com",
        model_id="anthropic.claude-opus", input_tokens=100, output_tokens=50,
        cost_microusd=1234, request_id="req_xyz789",
    )
    # A TRUE legacy row: written before `request_id`/`cost_microusd` existed
    # as their own attributes, i.e. neither is present at all. `record()`
    # itself cannot produce this shape any more (it always writes a
    # `request_id`, minted if the caller supplies none) — this simulates a
    # row from before that change, as `test_record_read_roundtrip_moto`
    # (test_usage_fallback_property.py) does for `requested_model_id`.
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    repo._table.put_item(Item={
        "tenant_id": "acme-eng",
        "timestamp_log_id": f"{recent}#legacy-uuid-1",
        "user_id": "u1",
        "model_id": "anthropic.claude-opus",
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
        "recorded_at": recent,
    })

    resp = client.get("/api/mvp/me/usage-history")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["history"]) == 2

    by_cost = {row["cost_microusd"]: row for row in body["history"]}
    priced = by_cost[1234]
    assert priced["request_id"] == "req_xyz789"

    legacy = by_cost[None]
    assert legacy["request_id"] is None


def test_usage_history_cost_zero_is_not_confused_with_unpriced(client, dynamodb_mock):
    from dynamo.usage_logs import UsageLogsRepository

    repo = UsageLogsRepository()
    repo.record(
        tenant_id="acme-eng", user_id="u1", user_email="u1@example.com",
        model_id="m", input_tokens=1, output_tokens=1, cost_microusd=0,
        request_id="req_free",
    )

    body = client.get("/api/mvp/me/usage-history").json()
    assert len(body["history"]) == 1
    row = body["history"][0]
    assert row["cost_microusd"] == 0
    assert row["request_id"] == "req_free"
