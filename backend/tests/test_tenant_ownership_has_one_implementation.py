"""C6.10: the tenant-ownership rule has one statement, `authz.is_tenant_owner`.

The rule was written four times: `authz.require_tenant_owner` (which no route used),
`team_lead._require_owner`, `grants._require_owned` and the VSR config gate, and the
approval services held a fifth form inside their transaction. Two copies and the
transaction had lost the `team_lead` role conjunct, and the newest copy carried a
docstring saying it was "the same ownership check the rest of the team-lead surface
uses". Copies made to agree agree only until the next one is added, so the copies
are deleted and this file checks that none comes back:

- every function, class or module body under `backend/` (tests excepted) that
  names `team_lead_user_id`, as a string, a keyword, an attribute, a variable or a
  parameter, is listed below with what it does with it, so a new one fails until
  someone looks at it. It does not see a copy that never names the field (for
  example one built on `list_by_owner`), which is why C6.10 holds this half at B;
- every team-lead route that takes a `tenant_id` calls one of the two route gates,
  the limit-raise routes the owner-only one, and every call sits where an API key's
  scope has already been evaluated;
- the rule refuses a demoted owner on every path, tested with the outer permission
  gate out of the way so the inner layer is what is under test.
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timezone

import boto3
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from dynamo.tenants import TenantsRepository
from mvp.authz import is_tenant_owner, owned_or_admin_tenant_or_404, owned_tenant_or_404
from mvp.deps import AuthenticatedUser, get_current_user
from tests.quota_events_fixtures import (
    freeze_grants_clock,
    quota_events_table,
    seed_pool_with_grant_fields,
    seed_request,
)

assert quota_events_table  # imported for its pytest-fixture side effect

BACKEND = pathlib.Path(__file__).resolve().parents[1]
FIELD = "team_lead_user_id"
GATES = {"owned_tenant_or_404", "owned_or_admin_tenant_or_404"}

# "<path>:<qualified name>" -> what it does with `team_lead_user_id`. None of these
# decides whether the caller owns a tenant; that is `authz.is_tenant_owner`.
HANDLES_THE_OWNER_FIELD: dict[str, str] = {
    "mvp/authz.py:is_tenant_owner": "the rule itself",
    "mvp/admin_tenants.py:CreateTenantRequest": "the owner an admin names in a creation body",
    "mvp/admin_tenants.py:SetOwnerRequest": "the owner an admin names in a reassignment body",
    "mvp/admin_tenants.py:TenantItem": "the owner in the admin response",
    "mvp/admin_tenants.py:_to_tenant_item": "serialises the owner into the admin response",
    "mvp/admin_tenants.py:_verify_team_lead": "checks that an owner named in an admin body is a team lead",
    "mvp/admin_tenants.py:create_tenant": "passes the owner to the repository on admin creation",
    "mvp/admin_tenants.py:set_tenant_owner": "admin reassignment; reads the old owner for the audit",
    "mvp/discovery/ledger.py:ensure_system_tenant": "writes the sentinel owner of the system tenant",
    "mvp/grants.py:_authority_condition_check_item":
        "re-asserts the owner pointer inside the approval transaction, after is_tenant_owner",
    "mvp/team_lead.py:create_tenant": "pins the owner of a team lead's new tenant to the caller",
    "dynamo/tenants.py:TenantsRepository.list_by_owner": "queries the owner index",
    "dynamo/tenants.py:TenantsRepository.count_by_owner": "queries the owner index for the tenant limit",
    "dynamo/tenants.py:TenantsRepository.create": "writes the owner",
    "dynamo/tenants.py:TenantsRepository.set_owner": "writes a new owner",
    "dynamo/tenants.py:TenantsRepository.seed_default": "writes the sentinel owner of the default tenant",
}


def _modules(dirs=None):
    roots = dirs or [p for p in sorted(BACKEND.iterdir()) if p.is_dir()
                     and p.name != "tests" and not p.name.startswith((".", "__"))]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            yield path.relative_to(BACKEND).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _owner_field_holders(tree) -> set[str]:
    """Qualified names of the innermost function, class or module body that names
    the field, docstrings excepted."""
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
                  and n.body and isinstance(n.body[0], ast.Expr)
                  and isinstance(n.body[0].value, ast.Constant)}

    def names_field(n) -> bool:
        return (isinstance(n, ast.Constant) and isinstance(n.value, str) and FIELD in n.value
                and id(n) not in docstrings) \
            or (isinstance(n, (ast.keyword, ast.arg)) and n.arg == FIELD) \
            or (isinstance(n, ast.Name) and n.id == FIELD) \
            or (isinstance(n, ast.Attribute) and n.attr == FIELD)

    found: set[str] = set()

    def visit(node, qual):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, qual + [child.name])
                continue
            if names_field(child):
                found.add(".".join(qual) or "<module>")
            visit(child, qual)

    visit(tree, [])
    return found


def _name(node: ast.AST) -> str:
    return node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")


def _routes(tree):
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in fn.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) \
                        and d.func.attr in {"get", "post", "put", "patch", "delete"} and d.args \
                        and isinstance(d.args[0], ast.Constant):
                    yield fn, d.args[0].value


def _has_permission_gate(fn) -> bool:
    return any(isinstance(d, ast.Call) and _name(d.func) == "Depends" and d.args
               and isinstance(d.args[0], ast.Call) and _name(d.args[0].func) == "require_permission"
               for d in list(fn.args.defaults) + list(fn.args.kw_defaults))


def _calls(fn, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _name(n.func) == name]


# --- static checks ---------------------------------------------------------

def test_every_holder_of_the_owner_field_is_named():
    found = {f"{rel}:{q}" for rel, tree in _modules() for q in _owner_field_holders(tree)}
    unnamed = sorted(found - set(HANDLES_THE_OWNER_FIELD))
    stale = sorted(set(HANDLES_THE_OWNER_FIELD) - found)
    assert unnamed == [], (
        "these read or write team_lead_user_id; if one decides whether the caller "
        "owns a tenant, use authz.is_tenant_owner or a route gate instead, otherwise "
        f"name it in HANDLES_THE_OWNER_FIELD: {unnamed}")
    assert stale == [], f"named but no longer holding the field: {stale}"


def test_the_holder_detector_sees_each_spelling():
    """A copy that keeps the field in a constant, a local, a keyword or a parameter
    is still seen, at module, class or function level."""
    src = (
        'OWNER = "team_lead_user_id"\n'
        'class Body:\n    team_lead_user_id: str\n'
        'def a(t, u):\n    owner = t.get(OWNER)\n    return owner == u.user_id\n'
        'def b(repo, u):\n    return repo.create(team_lead_user_id=u.user_id)\n'
        'def c(team_lead_user_id):\n    return team_lead_user_id\n'
        'def d():\n    """team_lead_user_id in a docstring only."""\n'
    )
    assert _owner_field_holders(ast.parse(src)) == {"<module>", "Body", "b", "c"}


def test_every_team_lead_route_with_a_tenant_id_checks_ownership():
    missing, seen = [], 0
    for rel, tree in _modules([BACKEND / "mvp"]):
        for fn, path in _routes(tree):
            takes_tenant = "tenant_id" in [a.arg for a in fn.args.args + fn.args.kwonlyargs]
            if not takes_tenant or not ("/team-lead" in path or rel == "mvp/team_lead.py"):
                continue
            seen += 1
            gates = {_name(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)} & GATES
            want = {"owned_tenant_or_404"} if rel == "mvp/grants.py" else GATES
            if not gates or not gates <= want:
                missing.append(f"{rel}:{fn.lineno} {fn.name} {sorted(gates)}")
    assert seen >= 1, "found no team-lead routes with a tenant_id; the sweep is blind"
    assert missing == [], (
        "team-lead routes that take a tenant_id must call a route gate, and the "
        f"limit-raise routes the owner-only owned_tenant_or_404: {missing}")


def test_every_ownership_call_has_evaluated_the_key_scope():
    offenders = []
    for rel, tree in _modules():
        if rel == "mvp/authz.py":
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            own = [c for g in GATES for c in _calls(fn, g)]
            if not own or _has_permission_gate(fn):
                continue
            scope_lines = [c.lineno for c in _calls(fn, "ensure_key_scope")]
            offenders += [f"{rel}:{c.lineno} in {fn.name}" for c in own
                          if not any(line < c.lineno for line in scope_lines)]
    assert offenders == [], (
        "the route gates decide ownership only; gate the route with "
        f"require_permission or call ensure_key_scope first: {offenders}")


# --- the rule ----------------------------------------------------------------

def _user(user_id: str, *roles: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@example.com", org_id="org",
        roles=list(roles), raw_claims={}, auth_kind="cognito")


@pytest.fixture
def tenant(dynamodb_mock) -> str:
    TenantsRepository().create(
        tenant_id="owned-co", name="Owned Co", team_lead_user_id="tl-1", created_by="tl-1")
    return "owned-co"


def _refused(fn) -> bool:
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == 404 and e.detail == "Tenant not found"
        return True
    return False


def test_the_rule(tenant):
    row = TenantsRepository().get(tenant)
    assert is_tenant_owner(row, _user("tl-1", "team_lead"))
    assert not is_tenant_owner(row, _user("tl-1", "user")), "a demoted owner"
    assert not is_tenant_owner(row, _user("tl-2", "team_lead")), "another team lead"
    assert not is_tenant_owner(row, _user("tl-1", "admin")), "admin reach is not ownership"
    assert not is_tenant_owner(None, _user("tl-1", "team_lead"))


@pytest.mark.parametrize("gate", [owned_tenant_or_404, owned_or_admin_tenant_or_404])
def test_both_gates_apply_the_rule(tenant, gate):
    assert gate(tenant, _user("tl-1", "team_lead"))["tenant_id"] == tenant
    assert _refused(lambda: gate(tenant, _user("tl-1", "user")))
    assert _refused(lambda: gate(tenant, _user("tl-2", "team_lead")))
    assert _refused(lambda: gate("no-such", _user("tl-1", "team_lead")))
    assert _refused(lambda: gate("no-such", _user("a-1", "admin")))


def test_only_the_admin_gate_admits_an_admin(tenant):
    admin = _user("a-1", "admin")
    assert owned_or_admin_tenant_or_404(tenant, admin)["tenant_id"] == tenant
    assert _refused(lambda: owned_tenant_or_404(tenant, admin))


# --- through the routes, with the outer gate out of the way -----------------

@pytest.fixture
def allow_every_permission(monkeypatch):
    """The outer `require_permission` gate already refuses a demoted user today,
    because `user` holds none of the scopes these routes need. Allowing every
    permission removes that layer, so a refusal below can only come from the
    ownership rule."""
    from mvp import authz

    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)


def _client(actor: AuthenticatedUser) -> TestClient:
    from mvp import grants, team_lead

    app = FastAPI()
    app.include_router(team_lead.router)
    app.include_router(grants.router)
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


TENANT_ROUTES = [
    "/api/mvp/team-lead/tenants/owned-co",
    "/api/mvp/team-lead/tenants/owned-co/members",
    "/api/mvp/team-lead/limit-raises?tenant_id=owned-co",
    "/api/mvp/team-lead/limit-grants?tenant_id=owned-co",
]


@pytest.mark.parametrize("path", TENANT_ROUTES)
def test_a_demoted_owner_is_refused_by_each_router(allow_every_permission, tenant, path):
    resp = _client(_user("tl-1", "user")).get(path)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Tenant not found"


def test_a_demoted_owner_lists_no_tenants(allow_every_permission, tenant):
    resp = _client(_user("tl-1", "user")).get("/api/mvp/team-lead/tenants")
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenants"] == []
    owner = _client(_user("tl-1", "team_lead")).get("/api/mvp/team-lead/tenants")
    assert [t["tenant_id"] for t in owner.json()["tenants"]] == ["owned-co"]


def test_the_owner_passes_each_router(allow_every_permission, tenant):
    client = _client(_user("tl-1", "team_lead"))
    for path in TENANT_ROUTES:
        assert client.get(path).status_code == 200, path


@pytest.mark.parametrize("path", [
    "/api/mvp/team-lead/limit-raises?tenant_id=owned-co",
    "/api/mvp/team-lead/limit-grants?tenant_id=owned-co",
])
def test_the_limit_raise_routes_are_the_ownership_path_for_an_admin_too(
        allow_every_permission, tenant, path):
    """An admin decides raises on the admin routes; on these an admin who does not
    own the tenant is refused like anyone else."""
    resp = _client(_user("a-1", "admin")).get(path)
    assert resp.status_code == 404 and resp.json()["detail"] == "Tenant not found"


def test_an_admin_cannot_revoke_through_the_team_lead_route(allow_every_permission, tenant):
    resp = _client(_user("a-1", "admin")).post(
        "/api/mvp/team-lead/limit-grants/g-1/revoke?tenant_id=owned-co", json={})
    assert resp.status_code == 404 and resp.json()["detail"] == "Tenant not found"


# --- the approval services ---------------------------------------------------

_MID_PERIOD_EPOCH = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def pending_raise(tenant, quota_events_table, monkeypatch) -> str:
    seed_pool_with_grant_fields(
        tenant, "2026-09", pool_limit_microusd=1_000_000,
        pool_granted_microusd=0, grant_cap_microusd=10_000_000)
    seed_request(
        boto3.resource("dynamodb", region_name="us-east-1").Table("stratoclave-quota-events"),
        request_id="req-1", tenant_id=tenant, user_id="member-1", asked_amount_microusd=1_000)
    freeze_grants_clock(monkeypatch, _MID_PERIOD_EPOCH)
    return "req-1"


def test_a_demoted_owner_cannot_approve_or_reject(pending_raise):
    from mvp import grants

    demoted = _user("tl-1", "user")
    with pytest.raises(grants.AuthorityDenied):
        grants.approve_limit_raise(
            actor=demoted, request_id=pending_raise, approved_amount_microusd=1_000,
            expires_at=_MID_PERIOD_EPOCH + 300, as_owner=True)
    with pytest.raises(grants.AuthorityDenied):
        grants.reject_limit_raise(
            actor=demoted, request_id=pending_raise, decision_comment="no", as_owner=True)


def test_the_owner_can_approve_and_a_demoted_owner_cannot_revoke(pending_raise, tenant):
    from mvp import grants

    out = grants.approve_limit_raise(
        actor=_user("tl-1", "team_lead"), request_id=pending_raise,
        approved_amount_microusd=1_000, expires_at=_MID_PERIOD_EPOCH + 300, as_owner=True)
    assert out["grant"]["status"] == "ACTIVE"
    with pytest.raises(grants.AuthorityDenied):
        grants.revoke_grant(
            actor=_user("tl-1", "user"), tenant_id=tenant,
            grant_id=out["grant"]["grant_id"], as_owner=True)


def test_the_transaction_still_refuses_when_the_check_before_it_is_bypassed(
        pending_raise, monkeypatch):
    """R6: authority is a condition inside the transaction, not only a check made
    before it. With `is_tenant_owner` forced to yes, a caller the pointer does not
    name is still refused at commit."""
    from mvp import grants

    monkeypatch.setattr(grants, "is_tenant_owner", lambda tenant, user: True)
    with pytest.raises(grants.AuthorityDenied):
        grants.approve_limit_raise(
            actor=_user("tl-2", "team_lead"), request_id=pending_raise,
            approved_amount_microusd=1_000, expires_at=_MID_PERIOD_EPOCH + 300, as_owner=True)

