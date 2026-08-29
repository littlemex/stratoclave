"""Contract 6.1 — a gate evaluates role ∧ credential scope, or refuses.

`user_has_permission` implements the intersection correctly, and every shipped
route reaches it through `require_permission`, so the scope-narrowing guarantee
holds today. What does not hold is that it CANNOT be bypassed: two role-only
dependencies exist beside it (`require_any_role`, `require_tenant_owner`) and they
read `user.roles` alone. An API key carries its owner's full roles, so a key
issued with a narrowed scope satisfies them.

Nothing routes through those two today — which is why this is a shape to close
rather than a hole to patch. The tests below pin both halves: the dependencies
themselves refuse a narrowed credential they cannot evaluate, and no route
dependency may gate on roles alone.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi import HTTPException

from mvp.authz import require_any_role
from mvp.deps import AuthenticatedUser


def _key_user(*roles: str, scopes: list[str]) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-1", email="u@example.com", org_id="org-1",
        roles=list(roles), auth_kind="api_key", key_scopes=scopes,
    )


def _jwt_user(*roles: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-1", email="u@example.com", org_id="org-1",
        roles=list(roles), auth_kind="jwt",
    )


class TestARoleOnlyGateCannotAdmitANarrowedKey:

    def test_a_jwt_with_the_role_passes(self):
        dep = require_any_role("admin")
        assert dep(user=_jwt_user("admin")).user_id == "u-1"

    def test_a_jwt_without_the_role_is_refused(self):
        dep = require_any_role("admin")
        with pytest.raises(HTTPException) as e:
            dep(user=_jwt_user("user"))
        assert e.value.status_code == 403

    def test_an_api_key_is_refused_when_the_gate_names_no_scope(self):
        """The key's roles are its owner's, so admitting on roles alone hands a
        narrowed credential its owner's authority. A gate that cannot say which
        scope it needs cannot evaluate the intersection, so it must refuse."""
        dep = require_any_role("admin")
        with pytest.raises(HTTPException) as e:
            dep(user=_key_user("admin", scopes=["messages:send"]))
        assert e.value.status_code == 403

    def test_an_api_key_passes_when_it_holds_the_named_scope(self):
        dep = require_any_role("admin", scope="users:read")
        assert dep(user=_key_user("admin", scopes=["users:read"])).user_id == "u-1"

    def test_an_api_key_without_the_named_scope_is_refused(self):
        dep = require_any_role("admin", scope="users:read")
        with pytest.raises(HTTPException) as e:
            dep(user=_key_user("admin", scopes=["messages:send"]))
        assert e.value.status_code == 403

    def test_a_named_scope_does_not_rescue_a_missing_role(self):
        dep = require_any_role("admin", scope="users:read")
        with pytest.raises(HTTPException) as e:
            dep(user=_key_user("user", scopes=["users:read"]))
        assert e.value.status_code == 403


class TestAuthenticationIsNotRegistration:
    """C6.3 — no identity acquires a budget implicitly.

    A valid Cognito access token proves the pool knows the subject. It does not
    say the operator granted that subject a tenant or a budget. The JWT path used
    to synthesize `roles=["user"]` and `org_id=DEFAULT_ORG_ID` and write the row,
    so any pool member could spend the default tenant's pool by calling an
    inference route once."""

    def _drive(self, monkeypatch, *, record, auto_provision=False):
        from mvp import deps

        written: list = []

        class _Users:
            def get_by_user_id(self, sub):
                return record

            def put_user(self, **kwargs):
                written.append(kwargs)

        monkeypatch.setattr(deps, "UsersRepository", _Users)
        monkeypatch.setattr(
            deps, "_decode_cognito_access_token",
            lambda token: {"sub": "sub-unregistered", "iat": 1},
        )
        monkeypatch.setattr(deps, "_fetch_email_from_cognito", lambda sub: "")
        monkeypatch.setenv(
            "STRATOCLAVE_COGNITO_AUTO_PROVISION", "true" if auto_provision else "")
        return deps, written

    def test_an_unregistered_subject_is_refused(self, monkeypatch):
        deps, written = self._drive(monkeypatch, record=None)
        with pytest.raises(HTTPException) as e:
            deps.get_current_user(authorization="Bearer sometoken")
        assert e.value.status_code == 403
        assert written == [], "a refused authentication must not register anyone"

    def test_a_registered_subject_still_authenticates(self, monkeypatch):
        deps, _ = self._drive(
            monkeypatch,
            record={"user_id": "sub-unregistered", "email": "u@example.com",
                    "roles": ["user"], "org_id": "org-1"},
        )
        user = deps.get_current_user(authorization="Bearer sometoken")
        assert user.org_id == "org-1" and user.roles == ["user"]

    def test_an_operator_can_opt_into_auto_provisioning(self, monkeypatch):
        """Kept as an explicit policy rather than removed, so a deployment that
        relied on the old behaviour can restore it deliberately — and so the
        change is visible in configuration rather than in a surprise 403."""
        deps, written = self._drive(monkeypatch, record=None, auto_provision=True)
        user = deps.get_current_user(authorization="Bearer sometoken")
        assert user.roles == ["user"]
        assert written and written[0]["user_id"] == "sub-unregistered"


class TestNoRouteGatesOnRolesAlone:

    def test_every_route_dependency_evaluates_the_intersection(self):
        """A static check, because the failure mode is a route added later. Any
        endpoint whose `Depends(...)` names a role-only gate is reported here with
        its file and line rather than discovered by an auditor."""
        root = pathlib.Path(__file__).resolve().parents[1]
        role_only = {"require_any_role", "require_tenant_owner"}
        offenders: list[str] = []

        for path in (root / "mvp").rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef) and not isinstance(
                        node, ast.AsyncFunctionDef):
                    continue
                is_route = any(
                    isinstance(d, ast.Call)
                    and isinstance(d.func, ast.Attribute)
                    and d.func.attr in {"get", "post", "put", "patch", "delete"}
                    for d in node.decorator_list
                )
                if not is_route:
                    continue
                for default in list(node.args.defaults) + list(node.args.kw_defaults):
                    if not isinstance(default, ast.Call):
                        continue
                    fn = default.func
                    if isinstance(fn, ast.Name) and fn.id == "Depends":
                        inner = default.args[0] if default.args else None
                        name = None
                        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                            name = inner.func.id
                        elif isinstance(inner, ast.Name):
                            name = inner.id
                        if name in role_only:
                            # A gate that names a scope has evaluated the
                            # intersection; only the bare form is an offender.
                            named_scope = isinstance(inner, ast.Call) and any(
                                kw.arg == "scope" for kw in inner.keywords)
                            if not named_scope:
                                offenders.append(
                                    f"{path.relative_to(root)}:{node.lineno} "
                                    f"{node.name} -> {name}")
        assert offenders == [], offenders
