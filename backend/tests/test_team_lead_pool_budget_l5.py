"""L5 (docs/design/limits.md (C14)): a team lead may set their own tenant's
pool budget (today it is admin-only), reusing the existing ownership check and
the existing audit event.

Spec, from the Interface section only:

    | PUT | /team-lead/tenants/{id}/pool-budget | **New.** Same body and
    semantics, ownership-checked, same audit event |

and the L5 row's "Verified by": "Unit: a team lead sets their own tenant's
pool and is refused on another tenant's. The audit line carries before/after."

Today `mvp.team_lead.router` has NO `/pool-budget` route at all — every
request below 404s at the FastAPI routing layer with the generic
`{"detail": "Not Found"}` body, not the application's `_require_owner`
`{"detail": "Tenant not found"}` body. The "refused on another tenant's" test
distinguishes the two bodies specifically so it cannot pass today for the
wrong reason (a route that does not exist refuses EVERY tenant, owned or not,
identically).
"""
from __future__ import annotations

import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dynamo.tenants import TenantsRepository
from mvp.deps import AuthenticatedUser, get_current_user


def _patch_authz_allow_all(monkeypatch) -> None:
    """L5 is about ownership + audit, not about which permission scope string
    the endpoint is gated behind — the interface names no scope, so allow
    every scope rather than guess one."""
    from mvp import authz

    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)


def _team_lead_actor(user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@example", org_id="owned-co",
        roles=["team_lead"], raw_claims={}, auth_kind="cognito",
    )


def _client(monkeypatch, actor: AuthenticatedUser) -> TestClient:
    _patch_authz_allow_all(monkeypatch)
    from mvp.team_lead import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


def _seed_two_tenants() -> tuple[str, str]:
    tenants = TenantsRepository()
    owned = tenants.create(
        tenant_id="owned-co", name="Owned Co", team_lead_user_id="tl-1", created_by="tl-1"
    )
    other = tenants.create(
        tenant_id="other-co", name="Other Co", team_lead_user_id="tl-2", created_by="tl-2"
    )
    return owned["tenant_id"], other["tenant_id"]


def test_team_lead_can_set_own_tenants_pool_budget(monkeypatch, dynamodb_mock):
    owned, _other = _seed_two_tenants()
    client = _client(monkeypatch, _team_lead_actor("tl-1"))

    resp = client.put(
        f"/api/mvp/team-lead/tenants/{owned}/pool-budget",
        json={"limit_usd_cents": 40000, "period": "2026-07"},  # $400.00
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == owned
    assert body["pool_limit_microusd"] == 400_000_000


def test_team_lead_is_refused_on_another_tenants_pool_budget(monkeypatch, dynamodb_mock):
    owned, other = _seed_two_tenants()
    client = _client(monkeypatch, _team_lead_actor("tl-1"))

    resp = client.put(
        f"/api/mvp/team-lead/tenants/{other}/pool-budget",
        json={"limit_usd_cents": 1000},
    )
    assert resp.status_code == 404
    # Distinguishes a real ownership refusal from FastAPI's generic
    # route-not-found 404 (which today's missing route always returns,
    # regardless of which tenant_id is in the path).
    assert resp.json().get("detail") == "Tenant not found"


def test_audit_event_carries_before_and_after(monkeypatch, dynamodb_mock, caplog):
    owned, _other = _seed_two_tenants()
    client = _client(monkeypatch, _team_lead_actor("tl-1"))

    caplog.set_level(logging.INFO, logger="stratoclave.audit")
    resp = client.put(
        f"/api/mvp/team-lead/tenants/{owned}/pool-budget",
        json={"limit_usd_cents": 40000, "period": "2026-07"},
    )
    assert resp.status_code == 200, resp.text

    lines = [r.getMessage() for r in caplog.records if r.name == "stratoclave.audit"]
    assert lines, "no audit event was emitted for the team-lead pool-budget write"
    events = [json.loads(line) for line in lines]
    matching = [e for e in events if e.get("target_id") == owned and "after" in e]
    assert matching, f"no audit event carried an 'after' for {owned}: {events}"
    assert matching[-1]["actor_id"] == "tl-1"
    # "same audit event" as the admin route (mvp.admin_tenants.set_pool_budget
    # emits event="tenant_pool_budget_set").
    assert matching[-1]["event"] == "tenant_pool_budget_set"
    assert "before" in matching[-1]
