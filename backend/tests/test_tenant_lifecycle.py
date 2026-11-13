"""One tenant-creation operation for both routes (#243, H1; C6.11).

Both `POST /api/mvp/admin/tenants` and `POST /api/mvp/team-lead/tenants` go through
`mvp.tenant_lifecycle.create_tenant`. These tests check, through the routes, that a
tenant gets the same provisioning from either, that a step's declared policy decides
what its failure does, and that the only other writers of a tenant row are the two
bootstrap tenants the module names.
"""
from __future__ import annotations

import ast
import json
import logging
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dynamo import TenantBudgetsRepository, TenantsRepository, current_period
from mvp import tenant_lifecycle
from mvp.deps import AuthenticatedUser, get_current_user
from mvp.tenant_lifecycle import ProvisioningStep, StepPolicy

BACKEND = pathlib.Path(__file__).resolve().parents[1]
OWNER = "tl-1"


def _user(user_id: str, *roles: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=user_id, email=f"{user_id}@example.com", org_id="org",
                             roles=list(roles), raw_claims={}, auth_kind="cognito")


@pytest.fixture
def routes(dynamodb_mock, monkeypatch):
    """Both routers, with the permission gate open and the admin route's owner
    lookup answering that `tl-1` is a team lead (the Users table is not part of
    this test)."""
    from mvp import admin_tenants, authz, team_lead
    from mvp.routing import config as rc

    rc._cache.clear()
    monkeypatch.setattr(authz, "user_has_permission", lambda user, permission: True)
    monkeypatch.setattr(admin_tenants, "UsersRepository", lambda: type("U", (), {
        "get_by_user_id": staticmethod(lambda uid: {"user_id": uid, "roles": ["team_lead"]})})())
    monkeypatch.delenv("STRATOCLAVE_SHADOW_VSR_NEW_TENANT_DEFAULT", raising=False)

    def client(actor: AuthenticatedUser) -> TestClient:
        app = FastAPI()
        app.include_router(admin_tenants.router)
        app.include_router(team_lead.router)
        app.dependency_overrides[get_current_user] = lambda: actor
        return TestClient(app, raise_server_exceptions=False)

    def by_admin(name="Acme"):
        return client(_user("admin-1", "admin")).post(
            "/api/mvp/admin/tenants", json={"name": name, "team_lead_user_id": OWNER})

    def by_team_lead(name="Acme"):
        return client(_user(OWNER, "team_lead")).post(
            "/api/mvp/team-lead/tenants", json={"name": name})

    return {"admin": by_admin, "team_lead": by_team_lead}


def _audit_events(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "stratoclave.audit"]


def _side_effects(tenant_id: str) -> dict:
    from mvp.routing.config import get_tenant_routing_config

    return {
        "pool": TenantBudgetsRepository().get(tenant_id, current_period()) is not None,
        "shadow": get_tenant_routing_config(tenant_id).shadow_vsr,
    }


def _normalised(row: dict) -> dict:
    return {k: v for k, v in row.items()
            if k not in ("tenant_id", "created_at", "updated_at", "created_by")}


@pytest.mark.parametrize("route", ["admin", "team_lead"])
def test_each_route_gets_the_pool_the_shadow_default_and_one_audit_record(routes, route, caplog):
    caplog.set_level(logging.INFO, logger="stratoclave.audit")
    resp = routes[route]()
    assert resp.status_code == 201, resp.text
    tid = resp.json()["tenant_id"]
    assert _side_effects(tid) == {"pool": True, "shadow": True}
    events = [e["event"] for e in _audit_events(caplog) if e.get("target_id") == tid]
    created = {"admin": "tenant_created", "team_lead": "team_lead_tenant_created"}[route]
    assert events == [created, "tenant_pool_provisioned", "tenant_shadow_vsr_provisioned_default"], events


def test_both_routes_write_the_same_tenant(routes):
    a = routes["admin"]("Same").json()["tenant_id"]
    b = routes["team_lead"]("Same").json()["tenant_id"]
    rows = TenantsRepository()
    assert _normalised(rows.get(a)) == _normalised(rows.get(b))
    assert _side_effects(a) == _side_effects(b)


@pytest.mark.parametrize("value,shadow", [
    ("false", None), ("0", None), ("no", None), ("off", None), (" FALSE ", None), ("true", True)])
def test_the_opt_out_covers_both_routes(routes, monkeypatch, value, shadow):
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR_NEW_TENANT_DEFAULT", value)
    for route in ("admin", "team_lead"):
        tid = routes[route]().json()["tenant_id"]
        assert _side_effects(tid) == {"pool": True, "shadow": shadow}, route


def _with_step(monkeypatch, policy: StepPolicy, *, fails: bool) -> list[str]:
    ran: list[str] = []

    def run(new):
        ran.append(new.tenant_id)
        if fails:
            raise RuntimeError("step failed")

    monkeypatch.setattr(tenant_lifecycle, "STEPS",
                        tenant_lifecycle.STEPS + (ProvisioningStep("fake", policy, run),))
    return ran


@pytest.mark.parametrize("policy", [StepPolicy.REFUSES_BEFORE_ROW, StepPolicy.REQUIRED_AFTER_ROW])
@pytest.mark.parametrize("route", ["admin", "team_lead"])
def test_a_step_added_to_the_operation_runs_for_each_route(routes, route, policy, monkeypatch):
    """A step before the row is given the id the row is then written under."""
    ran = _with_step(monkeypatch, policy, fails=False)
    resp = routes[route]()
    assert resp.status_code == 201, resp.text
    assert ran == [resp.json()["tenant_id"]]


@pytest.mark.parametrize("route", ["admin", "team_lead"])
def test_a_refusing_step_fails_creation_and_leaves_no_tenant_row(routes, route, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="stratoclave.audit")
    ran = _with_step(monkeypatch, StepPolicy.REFUSES_BEFORE_ROW, fails=True)
    resp = routes[route]()
    assert resp.status_code == 500
    assert len(ran) == 1
    assert TenantsRepository().get(ran[0]) is None
    assert TenantBudgetsRepository().get(ran[0], current_period()) is None
    assert TenantsRepository().count_by_owner(OWNER) == 0
    assert _audit_events(caplog) == []


@pytest.mark.parametrize("route", ["admin", "team_lead"])
def test_a_best_effort_step_that_fails_does_not_fail_creation(routes, route, monkeypatch):
    warnings = []
    monkeypatch.setattr(tenant_lifecycle._log, "warning", lambda event, **kw: warnings.append((event, kw)))
    ran = _with_step(monkeypatch, StepPolicy.BEST_EFFORT, fails=True)
    resp = routes[route]()
    assert resp.status_code == 201, resp.text
    tid = resp.json()["tenant_id"]
    assert ran == [tid]
    assert [(e, kw["step"], kw["tenant_id"]) for e, kw in warnings] == [
        ("tenant_provisioning_step_failed", "fake", tid)]


@pytest.mark.parametrize("route", ["admin", "team_lead"])
def test_a_required_step_that_fails_fails_the_request_and_the_row_remains(routes, route, monkeypatch):
    """The stated residual of `REQUIRED_AFTER_ROW`: a tenant row without that step."""
    ran = _with_step(monkeypatch, StepPolicy.REQUIRED_AFTER_ROW, fails=True)
    assert routes[route]().status_code == 500
    assert TenantsRepository().get(ran[0]) is not None


@pytest.mark.parametrize("policy", [StepPolicy.REFUSES_BEFORE_ROW, StepPolicy.REQUIRED_AFTER_ROW])
def test_the_tenant_limit_refuses_before_any_step(routes, monkeypatch, policy):
    monkeypatch.setattr(TenantsRepository, "TEAM_LEAD_TENANT_LIMIT", 1)
    ran = _with_step(monkeypatch, policy, fails=False)
    assert routes["team_lead"]().status_code == 201
    second = routes["team_lead"]()
    assert second.status_code == 403 and "tenant_limit_exceeded" in second.text
    assert len(ran) == 1


# --- the writers of a tenant row ---------------------------------------------

#: Every call of `TenantsRepository.create` or `.seed_default` outside the repository,
#: by enclosing function (or `<module>`).
WRITERS = {
    "mvp/tenant_lifecycle.py:create_tenant": "the creation operation",
    "mvp/discovery/ledger.py:ensure_system_tenant": "the model-discovery system tenant (bootstrap)",
    "bootstrap/seed.py:seed_default_tenant": "DEFAULT_ORG_ID (bootstrap)",
}


def _repo_names(node) -> set[str]:
    """Names and attributes assigned `TenantsRepository()` directly in `node`'s body,
    by `=` or an annotated assignment."""
    names: set[str] = set()
    for child in ast.walk(node):
        value = getattr(child, "value", None)
        if isinstance(child, (ast.Assign, ast.AnnAssign)) and isinstance(value, ast.Call) \
                and getattr(value.func, "id", "") == "TenantsRepository":
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, ast.Attribute):
                    names.add(t.attr)
    return names


def _tenant_row_writers(tree) -> set[str]:
    """Calls of `.create` / `.seed_default` on `TenantsRepository()` itself or on a
    name or attribute assigned from it anywhere in the module, by enclosing function."""
    repos = _repo_names(tree)
    found: set[str] = set()

    def visit(node, qual):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, qual + [child.name])
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                    and child.func.attr in ("create", "seed_default"):
                target = child.func.value
                if (isinstance(target, ast.Call) and getattr(target.func, "id", "") == "TenantsRepository") \
                        or (isinstance(target, ast.Name) and target.id in repos) \
                        or (isinstance(target, ast.Attribute) and target.attr in repos):
                    found.add(".".join(qual) or "<module>")
            visit(child, qual)

    visit(tree, [])
    return found


def test_the_only_writers_of_a_tenant_row_are_the_operation_and_the_two_bootstrap_tenants():
    found = set()
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(("tests/", ".")) or rel == "dynamo/tenants.py" or "/." in rel:
            continue
        found |= {f"{rel}:{q}" for q in _tenant_row_writers(ast.parse(path.read_text(encoding="utf-8")))}
    assert found == set(WRITERS), sorted(found ^ set(WRITERS))


def test_the_writer_scan_sees_each_spelling():
    src = (
        "def late():\n    REPO.create(name='x')\n"
        "REPO = TenantsRepository()\nREPO.create(name='x')\n"
        "def a():\n    TenantsRepository().create(name='x')\n"
        "def b():\n    t: TenantsRepository = TenantsRepository()\n    t.seed_default(tenant_id='d')\n"
        "class C:\n    def __init__(self):\n        self._tenants = TenantsRepository()\n"
        "    def make(self):\n        self._tenants.create(name='x')\n"
        "def c(other):\n    other.create(name='x')\n"
    )
    assert _tenant_row_writers(ast.parse(src)) == {"late", "<module>", "a", "b", "C.make"}


def test_the_default_tenant_is_outside_the_lifecycle(dynamodb_mock, monkeypatch):
    """Pinned rather than implied: seeding DEFAULT_ORG_ID writes the tenant row and
    nothing a creation route provisions."""
    from bootstrap.seed import seed_default_tenant
    from mvp.routing import config as rc
    from mvp.routing.config import get_tenant_routing_config

    rc._cache.clear()
    monkeypatch.setenv("DEFAULT_ORG_ID", "default-org")
    tid = seed_default_tenant()["tenant_id"]
    assert TenantsRepository().get(tid) is not None
    assert TenantBudgetsRepository().get(tid, current_period()) is None
    assert get_tenant_routing_config(tid).shadow_vsr is None


def test_the_discovery_tenant_is_outside_the_lifecycle(dynamodb_mock):
    from mvp.discovery.ledger import SYSTEM_TENANT_ID, ensure_system_tenant
    from mvp.routing import config as rc
    from mvp.routing.config import get_tenant_routing_config

    rc._cache.clear()
    ensure_system_tenant()
    assert TenantsRepository().get(SYSTEM_TENANT_ID) is not None
    assert TenantBudgetsRepository().get(SYSTEM_TENANT_ID, current_period()) is None
    assert get_tenant_routing_config(SYSTEM_TENANT_ID).shadow_vsr is None
