"""Capability management: role mutation (SoT = Users.roles) + effective perms.

Covers the capability-audit implementation:
  - UsersRepository.update_roles: replacement + optimistic lock + missing row.
  - effective_permissions: projection over ALL_SCOPES via the SAME evaluation
    the request path enforces (token vs api-key subject, fail-closed None).
The role-change HTTP endpoint guards (last-admin, team_lead-owns-tenant) are
exercised at the _set_user_role unit level in test_capability_role_endpoint.py.
"""
from __future__ import annotations

import pytest


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
def users_repo(dynamodb_mock):
    _users_table(dynamodb_mock)
    from dynamo import UsersRepository

    return UsersRepository()


# --------------------------------------------------------------------------
# update_roles: the single role-mutation storage primitive
# --------------------------------------------------------------------------
def test_update_roles_replaces_the_list(users_repo):
    users_repo.put_user(
        user_id="u1", email="u1@x.com", auth_provider="cognito",
        auth_provider_user_id="u1", org_id="default-org", roles=["admin"],
    )
    out = users_repo.update_roles("u1", ["user"])
    assert out is not None
    assert out["roles"] == ["user"]
    # Persisted.
    assert users_repo.get_by_user_id("u1")["roles"] == ["user"]


def test_update_roles_missing_user_returns_none(users_repo):
    assert users_repo.update_roles("ghost", ["user"]) is None


def test_update_roles_optimistic_lock_blocks_lost_update(users_repo):
    users_repo.put_user(
        user_id="u1", email="u1@x.com", auth_provider="cognito",
        auth_provider_user_id="u1", org_id="default-org", roles=["admin"],
    )
    # A concurrent writer moved the row to team_lead.
    users_repo.update_roles("u1", ["team_lead"])
    # Our stale-based update (expected admin) must be REJECTED (returns None),
    # not silently overwrite the concurrent change.
    assert users_repo.update_roles("u1", ["user"], expected_roles=["admin"]) is None
    # State is unchanged by the rejected write.
    assert users_repo.get_by_user_id("u1")["roles"] == ["team_lead"]


def test_update_roles_optimistic_lock_succeeds_when_expected_matches(users_repo):
    users_repo.put_user(
        user_id="u1", email="u1@x.com", auth_provider="cognito",
        auth_provider_user_id="u1", org_id="default-org", roles=["admin"],
    )
    out = users_repo.update_roles("u1", ["user"], expected_roles=["admin"])
    assert out is not None and out["roles"] == ["user"]


# --------------------------------------------------------------------------
# effective_permissions: projection over ALL_SCOPES via user_has_permission
# --------------------------------------------------------------------------
def _seed_permissions(dynamodb_mock):
    """Seed the Permissions table from permissions.json (role -> scopes)."""
    from pathlib import Path

    dynamodb_mock.create_table(
        TableName="stratoclave-permissions",
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    from dynamo.permissions import PermissionsRepository

    repo = PermissionsRepository()
    repo.seed_from_file(Path(__file__).resolve().parent.parent / "permissions.json")
    return repo


def _clear_perm_cache():
    import mvp.authz as authz

    # Role->permission lookups are cached; reset between seedings.
    if hasattr(authz, "_get_permissions_for_role") and hasattr(
        authz._get_permissions_for_role, "cache_clear"
    ):
        authz._get_permissions_for_role.cache_clear()


def test_effective_permissions_admin_token_is_broad(dynamodb_mock):
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import effective_permissions
    from mvp.deps import AuthenticatedUser

    admin = AuthenticatedUser(
        user_id="a", email="a@x.com", roles=["admin"],
        org_id="default-org", auth_kind="jwt",
    )
    perms = set(effective_permissions(admin))
    # admin holds the broad management scopes.
    assert {"users:create", "users:delete", "tenants:read-all", "apikeys:revoke"} <= perms
    # read-breadth implication is reflected: usage:read-all implies read-self.
    assert "usage:read-self" in perms


def test_effective_permissions_api_key_is_scope_intersect_roles(dynamodb_mock):
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import effective_permissions
    from mvp.deps import AuthenticatedUser

    # Owner is admin, but the key only carries messages:send. Effective = ∩.
    key = AuthenticatedUser(
        user_id="a", email="a@x.com", roles=["admin"],
        org_id="default-org", auth_kind="api_key", key_scopes=["messages:send"],
    )
    perms = effective_permissions(key)
    assert perms == ["messages:send"]
    # Crucially NOT the owner's admin scopes.
    assert "users:create" not in perms


def test_effective_permissions_none_scope_key_is_empty(dynamodb_mock):
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import effective_permissions
    from mvp.deps import AuthenticatedUser

    key = AuthenticatedUser(
        user_id="a", email="a@x.com", roles=["admin"],
        org_id="default-org", auth_kind="api_key", key_scopes=None,
    )
    # Fail-closed: a None-scope key can do nothing.
    assert effective_permissions(key) == []


def test_effective_permissions_equals_enforcement(dynamodb_mock):
    """The headline invariant (Fable I4): the whoami result is EXACTLY the set
    of scopes user_has_permission would allow — same function, no drift."""
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import ALL_SCOPES, effective_permissions, user_has_permission
    from mvp.deps import AuthenticatedUser

    for subject in (
        AuthenticatedUser(user_id="a", email="a@x.com", roles=["team_lead"],
                          org_id="t", auth_kind="jwt"),
        AuthenticatedUser(user_id="b", email="b@x.com", roles=["admin"],
                          org_id="t", auth_kind="api_key",
                          key_scopes=["messages:send", "usage:read-self"]),
    ):
        reported = set(effective_permissions(subject))
        enforced = {s for s in ALL_SCOPES if user_has_permission(subject, s)}
        assert reported == enforced
