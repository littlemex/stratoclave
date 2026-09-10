"""C17 — the `entitlements:grant` / `entitlements:read` permission.

PR2 handoff (change-pipeline/model-onboarding, PR2): "A new permission in
ALL_SCOPES and permissions.json, with its role grants." The handoff spells the
two scope names verbatim ("Use `entitlements:grant` and `entitlements:read`
-- grant and read separated because seeing what a tenant may use is not the
same authority as changing it"), so these two literal strings are read from
the spec, not invented.

These tests exercise the REAL `mvp.authz` machinery end to end (permissions.json
seeded into a moto-backed Permissions table, `user_has_permission` /
`effective_permissions` called directly) -- nothing here reimplements the
permission predicate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mvp.authz import ALL_SCOPES

NEW_SCOPES = ("entitlements:grant", "entitlements:read")


def _seed_permissions(dynamodb_mock):
    """Seed the real Permissions table from the repo's own permissions.json,
    exactly as tests/test_capability_role_and_permissions.py does -- this is
    the production document C17 edits, not a copy retyped here."""
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

    authz._clear_permissions_cache()


def _user(roles):
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id="u1", email="u1@example.com", roles=roles,
        org_id="default-org", auth_kind="jwt",
    )


# ---------------------------------------------------------------------------
# The scopes exist in the canonical universe.
# ---------------------------------------------------------------------------
def test_entitlements_scopes_are_in_all_scopes():
    """Pins C17's literal scope names against the production universe.
    Non-vacuous: on `origin/main` (pre-PR2) neither string is in ALL_SCOPES,
    so this fails until C17 lands, and a future rename/typo of either literal
    fails it again."""
    for scope in NEW_SCOPES:
        assert scope in ALL_SCOPES, f"{scope!r} missing from mvp.authz.ALL_SCOPES"


# ---------------------------------------------------------------------------
# Role grants (permissions.json), exercised through the real evaluator.
# ---------------------------------------------------------------------------
def test_admin_role_holds_both_entitlements_scopes(dynamodb_mock):
    """Non-vacuous: fails today because permissions.json's `admin` role does
    not yet list either scope; the assertion only becomes true once C17 adds
    the role grant."""
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import user_has_permission

    admin = _user(["admin"])
    for scope in NEW_SCOPES:
        assert user_has_permission(admin, scope) is True, (
            f"admin role does not grant {scope!r} -- C17 requires the root "
            f"role to hold both entitlements scopes"
        )


def test_end_user_role_does_not_hold_either_entitlements_scope(dynamodb_mock):
    """The `user` role (end-user, messages:send/apikeys/usage:read-self) must
    not gain either scope by accident -- least-privilege default for a new,
    money/access-widening authority. This assertion is also true before C17
    lands (permissions.json has no entitlements entries at all yet), so it is
    a guard against over-grant rather than proof C17 landed; the positive
    proof is the admin-role test above and the separation test below."""
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import user_has_permission

    end_user = _user(["user"])
    for scope in NEW_SCOPES:
        assert user_has_permission(end_user, scope) is False


def test_effective_permissions_admin_projects_entitlements_scopes(dynamodb_mock):
    """`effective_permissions` (the whoami projection) must show the same
    answer `user_has_permission` enforces -- reuses the existing
    test_effective_permissions_equals_enforcement invariant, specifically at
    the two new scopes."""
    _seed_permissions(dynamodb_mock)
    _clear_perm_cache()
    from mvp.authz import effective_permissions

    admin_perms = set(effective_permissions(_user(["admin"])))
    assert set(NEW_SCOPES) <= admin_perms


# ---------------------------------------------------------------------------
# C17 / the handoff's separation rationale: grant and read are NOT the same
# authority. Exercised structurally (independent of permissions.json's actual
# role assignment) via a synthetic role, so this holds regardless of which
# roles the implementation ultimately grants the two scopes to.
# ---------------------------------------------------------------------------
def test_holding_read_alone_does_not_satisfy_grant(monkeypatch):
    import mvp.authz as authz

    monkeypatch.setattr(authz, "_get_permissions_for_role",
                         lambda role: ["entitlements:read"] if role == "reader" else [])
    reader = _user(["reader"])
    assert authz.user_has_permission(reader, "entitlements:read") is True
    assert authz.user_has_permission(reader, "entitlements:grant") is False, (
        "entitlements:read must not imply entitlements:grant -- the handoff's "
        "whole reason for two scopes is that seeing a tenant's grants is a "
        "different authority from changing them"
    )


def test_holding_grant_alone_does_not_satisfy_read(monkeypatch):
    import mvp.authz as authz

    monkeypatch.setattr(authz, "_get_permissions_for_role",
                         lambda role: ["entitlements:grant"] if role == "granter" else [])
    granter = _user(["granter"])
    assert authz.user_has_permission(granter, "entitlements:grant") is True
    assert authz.user_has_permission(granter, "entitlements:read") is False, (
        "entitlements:grant must not imply entitlements:read either -- there "
        "is no read-breadth relationship between the two at all"
    )
