"""`GET /api/mvp/admin/usage-logs` must refuse an unfiltered request with 400
rather than attempt a DynamoDB Scan.

Real-machine finding (CloudFront test deployment `scco`, 2026-11-14): the ECS
task role is granted dynamodb:Scan on a fixed allowlist of admin-console
tables (`iac/lib/ecs-stack.ts`'s `ScanLimitedToAdminConsoleTables`, P0-10
security review) that deliberately excludes `usage-logs` — it is one of the
audit-critical tables a Scan-capable RCE could bulk-exfiltrate. This route's
"no tenant_id/user_id -> full Scan" fallback was never reconciled with that
policy, so calling it with neither filter — exactly what the Admin > Usage
web page did on first render, before an operator ever touched the filter
form — raised `botocore.exceptions.ClientError: AccessDeniedException` and
surfaced to the browser/CLI as an unhandled 500.

This module cannot reproduce the IAM denial itself (moto does not enforce
real IAM policies), so it pins the part that IS unit-testable: the route
refuses before ever calling Scan, and still serves normally once a filter
is supplied.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.admin_usage import router as usage_router
from mvp.deps import get_current_user


@dataclass
class _AdminUser:
    user_id: str = "admin-1"
    org_id: str = "default-org"
    email: str = "admin@example.com"
    roles: list = None
    auth_kind: str = "jwt"
    key_scopes: list = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["admin"]


@pytest.fixture
def client(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz

    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: True)
    app = FastAPI()
    app.include_router(usage_router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser()
    return TestClient(app)


def test_no_filter_is_refused_with_400_not_500(client):
    resp = client.get("/api/mvp/admin/usage-logs?limit=100")
    assert resp.status_code == 400
    assert "tenant_id or user_id is required" in resp.json()["detail"]


def test_tenant_filter_alone_is_served(client):
    resp = client.get("/api/mvp/admin/usage-logs?tenant_id=default-org&limit=100")
    assert resp.status_code == 200
    assert resp.json() == {"logs": [], "next_cursor": None}


def test_user_filter_alone_is_served(client):
    resp = client.get("/api/mvp/admin/usage-logs?user_id=some-user&limit=100")
    assert resp.status_code == 200
    assert resp.json() == {"logs": [], "next_cursor": None}
