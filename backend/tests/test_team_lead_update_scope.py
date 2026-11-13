"""`PATCH /api/mvp/team-lead/tenants/{id}` is a write, so it is gated on a write.

The route renames a tenant and changes its `default_credit`, the amount each new
member is given. It was gated on `tenants:read-own`. A team lead signed in through
the console holds both the read and the write permission, so nothing looked wrong
from there; the gap was an API key the team lead had narrowed to `tenants:read-own`
for a read-only dashboard, which could change the tenant's money settings.

These tests seed the permissions table from the shipped `permissions.json` through
the repository's own loader, not an allow-all stub, because the defect was in which
permission the route names.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dynamo.tenants import TenantsRepository
from mvp.deps import AuthenticatedUser, get_current_user

PERMISSIONS = pathlib.Path(__file__).resolve().parents[1] / "permissions.json"


@pytest.fixture
def shipped_role_grants(dynamodb_mock):
    from dynamo import PermissionsRepository
    from dynamo.permissions import _permissions_table_name
    from mvp import authz

    dynamodb_mock.create_table(
        TableName=_permissions_table_name(),
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    PermissionsRepository().seed_from_file(PERMISSIONS)
    authz._clear_permissions_cache()
    yield
    authz._clear_permissions_cache()


def _actor(*, auth_kind: str, scopes: list[str] | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="tl-1", email="tl-1@example.com", org_id="owned-co",
        roles=["team_lead"], raw_claims={}, auth_kind=auth_kind, key_scopes=scopes,
    )


def _client(actor: AuthenticatedUser) -> TestClient:
    from mvp.team_lead import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


@pytest.fixture
def owned_tenant(dynamodb_mock) -> str:
    TenantsRepository().create(
        tenant_id="owned-co", name="Owned Co", team_lead_user_id="tl-1",
        created_by="tl-1", default_credit=1000)
    return "owned-co"


def _patch(client: TestClient, tenant_id: str):
    return client.patch(
        f"/api/mvp/team-lead/tenants/{tenant_id}",
        json={"name": "Renamed", "default_credit": 999_999})


def test_a_signed_in_team_lead_can_still_update_their_tenant(shipped_role_grants, owned_tenant):
    resp = _patch(_client(_actor(auth_kind="cognito")), owned_tenant)
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_credit"] == 999_999


def test_a_read_only_key_cannot_change_the_tenant(shipped_role_grants, owned_tenant):
    resp = _patch(_client(_actor(auth_kind="api_key", scopes=["tenants:read-own"])), owned_tenant)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "Missing permission: tenants:update-own"
    stored = TenantsRepository().get(owned_tenant)
    assert stored["name"] == "Owned Co"
    assert int(stored["default_credit"]) == 1000


def test_a_key_holding_the_write_permission_can_update(shipped_role_grants, owned_tenant):
    resp = _patch(_client(_actor(auth_kind="api_key", scopes=["tenants:update-own"])), owned_tenant)
    assert resp.status_code == 200, resp.text
    assert TenantsRepository().get(owned_tenant)["name"] == "Renamed"

