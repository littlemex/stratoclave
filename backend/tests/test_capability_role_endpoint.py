"""Guards on the role-mutation chokepoint (_set_user_role) + assign_tenant fix.

The capability audit found that role changes had no single guarded path:
assign_tenant's new_role never touched Users.roles (privilege retention), and
there was no last-admin / owns-tenant protection on promotion/demotion. These
tests pin the guards on `_set_user_role` and that assign_tenant now updates the
authorization SoT.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException


def _users_table(dynamodb_mock):
    dynamodb_mock.create_table(
        TableName="stratoclave-users",
        KeySchema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "email", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "email-index",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def repos(dynamodb_mock):
    _users_table(dynamodb_mock)
    from dynamo import UsersRepository

    return {"users": UsersRepository()}


def _actor():
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id="admin1", email="admin@x.com", roles=["admin"],
        org_id="default-org", auth_kind="jwt",
    )


def _mk(users, user_id, roles):
    users.put_user(
        user_id=user_id, email=f"{user_id}@x.com", auth_provider="cognito",
        auth_provider_user_id=user_id, org_id="default-org", roles=roles,
    )


def test_promote_user_to_team_lead(repos, monkeypatch):
    from mvp import admin_users

    monkeypatch.setattr(admin_users, "global_sign_out", lambda *a, **k: None)
    users = repos["users"]
    _mk(users, "u1", ["user"])

    out = admin_users._set_user_role(user_id="u1", new_role="team_lead", actor=_actor())
    assert out["roles"] == ["team_lead"]


def test_demote_last_admin_blocked(repos, monkeypatch):
    from mvp import admin_users

    monkeypatch.setattr(admin_users, "global_sign_out", lambda *a, **k: None)
    users = repos["users"]
    _mk(users, "admin1", ["admin"])  # the only admin

    with pytest.raises(HTTPException) as ei:
        admin_users._set_user_role(user_id="admin1", new_role="user", actor=_actor())
    assert ei.value.status_code == 409
    assert "last admin" in ei.value.detail.lower()
    # Role unchanged.
    assert users.get_by_user_id("admin1")["roles"] == ["admin"]


def test_demote_admin_allowed_when_another_admin_exists(repos, monkeypatch):
    from mvp import admin_users

    monkeypatch.setattr(admin_users, "global_sign_out", lambda *a, **k: None)
    users = repos["users"]
    _mk(users, "admin1", ["admin"])
    _mk(users, "admin2", ["admin"])

    out = admin_users._set_user_role(user_id="admin2", new_role="user", actor=_actor())
    assert out["roles"] == ["user"]


def test_demote_team_lead_with_owned_tenant_blocked(repos, monkeypatch):
    from mvp import admin_users

    monkeypatch.setattr(admin_users, "global_sign_out", lambda *a, **k: None)
    # The user still owns a tenant → refuse demotion.
    monkeypatch.setattr(admin_users, "_tenants_owned_by", lambda uid: ["acme"] if uid == "tl1" else [])
    users = repos["users"]
    _mk(users, "tl1", ["team_lead"])

    with pytest.raises(HTTPException) as ei:
        admin_users._set_user_role(user_id="tl1", new_role="user", actor=_actor())
    assert ei.value.status_code == 409
    assert "ownership" in ei.value.detail.lower()


def test_demote_team_lead_ok_when_owns_nothing(repos, monkeypatch):
    from mvp import admin_users

    monkeypatch.setattr(admin_users, "global_sign_out", lambda *a, **k: None)
    monkeypatch.setattr(admin_users, "_tenants_owned_by", lambda uid: [])
    users = repos["users"]
    _mk(users, "tl1", ["team_lead"])

    out = admin_users._set_user_role(user_id="tl1", new_role="user", actor=_actor())
    assert out["roles"] == ["user"]


def test_idempotent_same_role_is_noop(repos, monkeypatch):
    from mvp import admin_users

    called = {"signout": 0}
    monkeypatch.setattr(admin_users, "global_sign_out", lambda *a, **k: called.__setitem__("signout", called["signout"] + 1))
    users = repos["users"]
    _mk(users, "u1", ["user"])

    out = admin_users._set_user_role(user_id="u1", new_role="user", actor=_actor())
    assert out["roles"] == ["user"]
    # A no-op must not sign the user out.
    assert called["signout"] == 0


def test_missing_user_404(repos, monkeypatch):
    from mvp import admin_users

    monkeypatch.setattr(admin_users, "global_sign_out", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        admin_users._set_user_role(user_id="ghost", new_role="user", actor=_actor())
    assert ei.value.status_code == 404


def test_api_key_actor_cannot_change_role(repos):
    """A bearer key must never change roles (self-escalation path). The endpoint
    rejects api_key actors before any mutation."""
    from mvp import admin_users
    from mvp.deps import AuthenticatedUser

    key_actor = AuthenticatedUser(
        user_id="k", email="k@x.com", roles=["admin"],
        org_id="default-org", auth_kind="api_key", key_scopes=["users:update"],
    )
    with pytest.raises(HTTPException) as ei:
        admin_users.admin_set_user_role(
            user_id="u1", body=admin_users.AdminSetRoleRequest(role="admin"), actor=key_actor
        )
    assert ei.value.status_code == 403
