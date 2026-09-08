"""A malformed task tag must not refuse the request.

A tag that CAN refuse a request has to be sequenced against the credit
reservation, and there is no correct place to put that check: before the
reserve it would mask the 402 that names the wall a grant could raise;
after the reserve, a refusal would strand counters that were already
debited. So the live HTTP edge (through `mvp.deps.get_request_context`,
wired the same way `x-sc-group-id` / `x-sc-workflow-run-id` already are —
see `tests/test_request_context_http.py`, which this file's fixture is
modelled on almost verbatim) must not turn a bad `x-sc-task-tag` header into
a 400, unlike `x-sc-group-id`, which DOES 400 on the same kind of malformed
value. That asymmetry — same grammar, opposite failure mode — is exactly
why `task_tag.resolve` is a separate function from the correlation-id
validator, deliberately never sharing its raising behaviour.
`test_task_tag_resolve.py` already pins the pure-function half of this
guarantee (`resolve` never raises); this file pins the HTTP half.

At the base commit, `mvp.deps.get_request_context` takes no
`task_tag_header`, so nothing yet reads `x-sc-task-tag` at all — every test
below observes a 200 with no evidence the tag was ever looked at, and the
persisted `UsageLogs` row carries no `task_tag`/`task_tag_source` keys.
Nothing here fails on an exception; it fails because the assertions about
those two keys being present and correct do not hold yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest
from boto3.dynamodb.conditions import Key as boto3_key
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.anthropic import router as anthropic_router
from mvp.deps import get_current_user


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


def _post(client, headers=None, stream=False):
    return client.post("/v1/messages", headers=headers or {}, json={
        "model": "us.anthropic.claude-opus-4-7",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50, "stream": stream,
    })


def _usage_row_for(request_id: str) -> dict:
    """The persisted UsageLogs row this request produced, found by suffix
    on `timestamp_log_id` ('{iso}#{request_id}') — the same convention
    `mvp.admin_usage`/`mvp.me` read against."""
    from dynamo.usage_logs import UsageLogsRepository

    repo = UsageLogsRepository()
    resp = repo._table.query(
        KeyConditionExpression=boto3_key("tenant_id").eq(_FakeUser().org_id)
    )
    matches = [it for it in resp.get("Items", [])
               if it["timestamp_log_id"].endswith(f"#{request_id}")]
    assert matches, f"no UsageLogs row found for request_id={request_id}"
    return matches[0]


class TestMalformedTagNeverRefuses:
    def test_no_tag_header_baseline_is_200(self, api_client):
        """Control: establishes what 'the same status as no tag at all'
        means for this endpoint, under this fixture."""
        resp = _post(api_client)
        assert resp.status_code == 200

    @pytest.mark.parametrize("bad", [
        "has space",           # malformed
        "a" * 65,               # over-long
        "tag\x00name",          # control character (NUL)
        "tag\rname",            # control character (CR)
        "tag#name",             # DynamoDB key delimiter
    ])
    def test_malformed_tag_returns_200_same_as_no_tag(self, api_client, bad):
        resp = _post(api_client, headers={"x-sc-task-tag": bad})
        assert resp.status_code == 200, (
            f"a malformed x-sc-task-tag ({bad!r}) refused the request with "
            f"{resp.status_code} — it must be sequenced as if the header "
            "were never sent"
        )

    @pytest.mark.parametrize("bad", [
        "has space",
        "a" * 65,
        "tag\x00name",
        "tag#name",
    ])
    def test_malformed_tag_row_records_dropped_grammar(self, api_client, bad):
        resp = _post(api_client, headers={"x-sc-task-tag": bad})
        assert resp.status_code == 200
        span_id = resp.headers["x-sc-span-id"]
        item = _usage_row_for(span_id)
        assert item.get("task_tag") == "unlabelled", (
            f"malformed tag {bad!r} must be recorded as the sentinel, "
            f"got {item.get('task_tag')!r}"
        )
        assert item.get("task_tag_source") == "dropped_grammar", (
            f"malformed tag {bad!r} must record source dropped_grammar, "
            f"got {item.get('task_tag_source')!r}"
        )
