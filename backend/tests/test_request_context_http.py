"""HTTP-boundary tests for correlation-id propagation (P0-12).

Full FastAPI TestClient with mocked auth + Bedrock, verifying:
  * a request with NO correlation headers still succeeds (backward compatible)
    and the server assigns + echoes a span id and a workflow-run id;
  * supplied x-sc-group-id / x-sc-workflow-run-id are echoed (run) and reach
    the routing layer's RouteRequest as opaque pass-through;
  * a malformed correlation header is rejected 400 before any work;
  * tenant_id on the RouteRequest is the authenticated org, never a header.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.anthropic import router as anthropic_router
from mvp.deps import get_current_user
from mvp.observability.context import (
    HDR_GROUP_ID,
    HDR_SPAN_ID,
    HDR_WORKFLOW_RUN_ID,
)


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


# Capture the RouteRequest the handler builds, to assert propagation.
_captured: dict = {}


@pytest.fixture
def api_client(dynamodb_mock, monkeypatch):
    _captured.clear()
    # Grant messages:send at the authz layer directly (auto-restored by
    # monkeypatch). The permissions ROLE→perms resolution + its 10s cache and
    # the DynamoDB permissions table are covered by dedicated RBAC tests; here
    # we isolate P0-12 (correlation propagation) from that machinery so full-
    # suite cache/table state can't turn this into a spurious 403.
    import mvp.authz as _authz
    monkeypatch.setattr(_authz, "user_has_permission", lambda user, perm: True)

    from dynamo.user_tenants import UserTenantsRepository
    UserTenantsRepository().ensure(
        user_id=_FakeUser().user_id, tenant_id=_FakeUser().org_id,
        role="user", total_credit=10**9)

    app = FastAPI()
    app.include_router(anthropic_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()

    import mvp.routing as routing_mod
    real_route = routing_mod.route_stream

    async def _spy_route(req):
        _captured["req"] = req
        return await real_route(req)

    with patch("mvp.routing.route_stream", _spy_route), \
         patch("mvp.routing.infrarouter.bedrock_client") as mock_routing, \
         patch("mvp.anthropic._bedrock_client") as mock_bedrock:
        mock_routing.return_value.converse_stream.side_effect = _mock_converse_stream
        mock_bedrock.return_value.converse.side_effect = _mock_converse
        yield TestClient(app)


def _post(client, headers=None, stream=True):
    return client.post("/v1/messages", headers=headers or {}, json={
        "model": "us.anthropic.claude-opus-4-7",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50, "stream": stream,
    })


class TestCorrelationHttp:
    def test_no_headers_still_succeeds_and_echoes_generated_ids(self, api_client):
        resp = _post(api_client)
        assert resp.status_code == 200
        # server assigned + echoed a span and a run id
        assert resp.headers[HDR_SPAN_ID].startswith("req_")
        assert resp.headers[HDR_WORKFLOW_RUN_ID].startswith("wr_")

    def test_supplied_ids_echoed_and_propagated_to_routing(self, api_client):
        resp = _post(api_client, headers={
            HDR_GROUP_ID: "code-review-agent",
            HDR_WORKFLOW_RUN_ID: "run-abc",
        })
        assert resp.status_code == 200
        # run id echoed back verbatim; span id server-assigned
        assert resp.headers[HDR_WORKFLOW_RUN_ID] == "run-abc"
        assert resp.headers[HDR_SPAN_ID] == _captured["req"].request_id
        # reached the routing layer as opaque pass-through
        req = _captured["req"]
        assert req.group_id == "code-review-agent"
        assert req.workflow_run_id == "run-abc"
        assert req.span_id == req.request_id
        # tenant is the authenticated org, never from a header
        assert req.tenant_id == "acme-eng"

    def test_group_id_header_cannot_set_tenant(self, api_client):
        # Even a group_id that looks like another tenant cannot change tenant_id.
        resp = _post(api_client, headers={HDR_GROUP_ID: "other-tenant"})
        assert resp.status_code == 200
        assert _captured["req"].tenant_id == "acme-eng"

    def test_malformed_group_id_rejected_400(self, api_client):
        resp = _post(api_client, headers={HDR_GROUP_ID: "has space"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["type"] == "invalid_correlation_header"

    def test_malformed_workflow_run_id_rejected_400(self, api_client):
        resp = _post(api_client, headers={HDR_WORKFLOW_RUN_ID: "bad#id"})
        assert resp.status_code == 400

    def test_non_streaming_echoes_correlation_headers(self, api_client):
        # V2 (Fable): the non-streaming path returns a plain dict, so FastAPI
        # must merge the injected response.headers. Assert corr headers land.
        resp = _post(api_client, stream=False)
        assert resp.status_code == 200
        assert resp.headers[HDR_SPAN_ID].startswith("req_")
        assert resp.headers[HDR_WORKFLOW_RUN_ID].startswith("wr_")

    def test_empty_header_treated_as_absent(self, api_client):
        # F1 (Fable) decision: empty ≡ absent, not 400. A blank header means
        # "no group / no run", so the server generates a run id and succeeds.
        resp = _post(api_client, headers={HDR_GROUP_ID: "", HDR_WORKFLOW_RUN_ID: "  "})
        assert resp.status_code == 200
        assert _captured["req"].group_id is None
        assert _captured["req"].workflow_run_id.startswith("wr_")
