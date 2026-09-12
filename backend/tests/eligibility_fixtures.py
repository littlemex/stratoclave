"""Shared test-only support for the PR3 eligibility suite (C5/C6/C7/C8/C12).

NOT collected by pytest (no `test_` prefix) -- same convention as
`tests/billing_guards.py`, `tests/live_aws.py`, `tests/quota_events_fixtures.py`.

Everything here is glue: it builds fixtures and drives PRODUCTION callables
(`mvp.admin_entitlements.grant_entitlement`, the real `admin_routing` /
`admin_entitlements` / `anthropic` / `openai_responses` routers). It never
reimplements `mvp.eligibility.refusal_for` or any of the three refusal-code
predicates -- that would defeat the whole point of C5.

Registry injection. `mvp.models._REGISTRY`, `_ALIAS_MAP` and `_BEDROCK_ID_MAP`
are frozen module globals computed once at import from the registry document
(see `mvp/models.py`'s own comment on `_MAPPING`: "the legacy `_MAPPING` dict
... preserved for backward compatibility"). `resolve_model()`,
`canonical_model_id()` and `registry_entries()` are all DEFINED in
`mvp.models` and read those globals through their own module's `__globals__`,
so patching `mvp.models.<name>` via `monkeypatch.setattr` reaches every one of
them regardless of which module calls in -- but a module that did
`from .models import _REGISTRY` (a NAME, not a function) at ITS OWN top level
would hold a separate, un-patched copy. `mvp.openai_responses` and
`mvp.anthropic` did exactly this at the PR3 base commit; G1 (the
one-registry-accessor refactor) converted every consumer, including both of
those, to call `registry_entries()` instead, and added its own fail-closed
guard (`tests/test_registry_single_accessor.py`) so a local `_REGISTRY`
import cannot silently return. `_KNOWN_LOCAL_REGISTRY_IMPORTS` is therefore
empty post-G1; `install_registry` still defensively re-patches whatever it
lists, `raising=False`, so this fixture stays correct without a hand-edit if
a future module ever needs the same crutch again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.deps import AuthenticatedUser, get_current_user

# Modules known to import `_REGISTRY` as a local name rather than calling
# `mvp.models.registry_entries()` fresh. Patched defensively (raising=False)
# alongside the canonical `mvp.models` globals. Empty post-G1 (both former
# members converted to `registry_entries()`); kept as a list, not deleted,
# because the defensive re-patch is cheap insurance if one ever comes back.
_KNOWN_LOCAL_REGISTRY_IMPORTS: tuple[str, ...] = ()


def install_registry(monkeypatch, extra_entries: tuple, *, keep_real: bool = True) -> tuple:
    """Inject `extra_entries` into the live model registry for one test.

    Rebuilds every derived lookup `mvp/models.py` computes at import
    (`_ALIAS_MAP`, `_BEDROCK_ID_MAP`, `_MAPPING`, `_ALLOWED_BEDROCK_MODELS`,
    `_MESSAGES_ROUTE_ALIASES`) from the SAME construction rules the module
    itself uses (mirrored here, not re-derived differently), so
    `resolve_model()`, `resolve_bedrock_model()` and the pin/cascade paths
    all see the injected entries exactly as they would see a real one.

    `keep_real=True` (default) keeps every shipped entry reachable alongside
    the injected ones, so a test's synthetic fixture cannot accidentally
    break an unrelated real-registry code path (e.g. `DEFAULT_MODEL`).
    """
    import mvp.models as _models

    base = tuple(_models._REGISTRY) if keep_real else ()
    new_registry = base + tuple(extra_entries)
    monkeypatch.setattr(_models, "_REGISTRY", new_registry)
    # The derived maps below are the cache's *seed*, not what a resolver reads
    # directly: `registry_entries` and the alias maps are served from a composed
    # cache that copies them on refresh. Patching the seeds without dropping the
    # cache leaves the previous test's registry in force for a full TTL window.
    _models.invalidate_composed_registry()

    alias_map = {alias: entry for entry in new_registry for alias in entry.aliases}
    bedrock_id_map = {entry.bedrock_model_id: entry for entry in new_registry}
    monkeypatch.setattr(_models, "_STATIC_ALIAS_MAP", alias_map)
    monkeypatch.setattr(_models, "_STATIC_BEDROCK_ID_MAP", bedrock_id_map)

    mapping = {
        alias: entry.bedrock_model_id
        for entry in new_registry
        if entry.provider == "anthropic"
        for alias in entry.aliases
    }
    monkeypatch.setattr(_models, "_STATIC_MAPPING", mapping)

    allowed_bedrock = frozenset(list(mapping.values()) + [_models.DEFAULT_MODEL])
    monkeypatch.setattr(_models, "_STATIC_ALLOWED_BEDROCK_MODELS", allowed_bedrock)

    messages_route_aliases = frozenset(
        a for a, e in alias_map.items()
        if (mapping.get(a) is not None) or (e.bedrock_model_id in allowed_bedrock)
    )
    monkeypatch.setattr(_models, "_STATIC_MESSAGES_ROUTE_ALIASES", messages_route_aliases)

    for mod_name in _KNOWN_LOCAL_REGISTRY_IMPORTS:
        monkeypatch.setattr(f"{mod_name}._REGISTRY", new_registry, raising=False)

    return new_registry


@dataclass
class FakeUser:
    """A minimal `AuthenticatedUser` stand-in, matching the shape every other
    HTTP-boundary test in this suite already uses (`test_vsr_pin.py`'s
    `_FakeUser`, `test_entitlement_store.py`'s `_AdminUser`)."""

    user_id: str
    org_id: str
    email: str = "t@example.com"
    roles: list = field(default_factory=lambda: ["user"])
    auth_kind: str = "jwt"
    key_scopes: Optional[list] = None


def admin_actor(user_id: str = "admin-1", email: str = "admin@example.com") -> AuthenticatedUser:
    """An actor good enough to pass to `grant_entitlement`/`revoke_entitlement`
    directly (not through HTTP) -- both only read `.user_id` and `.email`."""
    return AuthenticatedUser(user_id=user_id, email=email, org_id="ops", roles=["admin"])


def grant(tenant_id: str, model_family: str, profile_scope: str, *, actor=None):
    """Call the PRODUCTION `grant_entitlement` directly (no HTTP hop needed
    for fixture setup) -- never a hand-rolled DynamoDB put of an
    `ENTITLEMENT#` row, which would stop being a test of C4's own store."""
    from mvp.admin_entitlements import grant_entitlement

    return grant_entitlement(
        tenant_id=tenant_id, model_family=model_family, profile_scope=profile_scope,
        actor=actor or admin_actor(),
    )


def _mock_converse(**kwargs):
    return {
        "output": {"message": {"content": [{"text": "hi from the mock"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 3, "outputTokens": 2},
    }


def build_app(monkeypatch, *, current_user_provider) -> FastAPI:
    """One FastAPI app mounting every router this suite needs to drive end to
    end: `/v1/messages` + `/v1/models` (anthropic), `/openai/v1/models`
    (openai_responses), and the two admin routers used to configure a tenant
    (routing config + entitlements). No prefix collisions between them.

    `current_user_provider` is a zero-arg callable returning the
    `AuthenticatedUser` for whichever request is about to be made -- a plain
    lambda closing over a mutable holder, so one TestClient can drive
    requests as different callers across a single test without rebuilding
    the app (mirrors `app.dependency_overrides[get_current_user] = lambda:
    ...` in every existing HTTP-boundary test here, generalised to be
    swappable mid-test).
    """
    import mvp.authz as _authz
    # Permission gating itself is exercised elsewhere (test_entitlements_permission.py,
    # TestPermissionGate in test_entitlement_store.py); this suite is about
    # eligibility, not the permission lattice, so it is bypassed here exactly
    # as test_routing_scope_axes.py and test_entitlement_store.py's
    # `app_and_client` fixture already do.
    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: True)

    from mvp.admin_entitlements import router as entitlements_router
    from mvp.admin_routing import router as routing_router
    from mvp.anthropic import router as anthropic_router
    from mvp.openai_responses import router as openai_router

    app = FastAPI()
    app.include_router(anthropic_router)
    app.include_router(openai_router)
    app.include_router(routing_router)
    app.include_router(entitlements_router)
    app.dependency_overrides[get_current_user] = current_user_provider
    return app


def build_client(monkeypatch, *, current_user_provider) -> TestClient:
    app = build_app(monkeypatch, current_user_provider=current_user_provider)
    # raise_server_exceptions=False: an implementation bug should show up as
    # an ordinary Response (500) the test can assert against, not an
    # in-process exception that aborts the test itself.
    return TestClient(app, raise_server_exceptions=False)


def patch_bedrock_converse(monkeypatch, side_effect=None):
    """Mock the Anthropic route's Bedrock client so a POST /v1/messages that
    reaches invocation returns a canned completion instead of touching AWS.
    Mirrors `test_vsr_pin.py`'s own `with patch("mvp.anthropic._bedrock_client")`
    pattern, but via monkeypatch so it composes with the other patches in
    this module inside one fixture."""
    from unittest.mock import MagicMock

    import mvp.anthropic as _anthropic_mod

    mock_client = MagicMock()
    mock_client.converse.side_effect = side_effect or _mock_converse
    monkeypatch.setattr(_anthropic_mod, "_bedrock_client", lambda: mock_client)
    return mock_client


def put_tenant_routing(client: TestClient, tenant_id: str, **body) -> Any:
    return client.put(f"/api/mvp/admin/tenants/{tenant_id}/routing-config", json=body)


def put_user_routing(client: TestClient, tenant_id: str, user_id: str, **body) -> Any:
    return client.put(
        f"/api/mvp/admin/tenants/{tenant_id}/users/{user_id}/routing-config", json=body)


def ensure_tenant_and_membership(tenant_id: str, user_id: str, *, total_credit: int = 10**12):
    """Seed the Tenants row (admin_routing's write path requires it to
    exist) and a funded UserTenants membership (the reserve chokepoint
    requires it), exactly as `test_routing_scope_axes.py` and
    `test_quota_cascade.py`'s own fixtures do."""
    from dynamo import UserTenantsRepository
    from dynamo.tenants import TenantsRepository

    TenantsRepository().create(
        tenant_id=tenant_id, team_lead_user_id="admin-1", name=tenant_id, created_by="admin-1")
    UserTenantsRepository().ensure(
        user_id=user_id, tenant_id=tenant_id, role="user", total_credit=total_credit)


def post_messages(client: TestClient, *, model: str, pin: Optional[str] = None,
                   headers: Optional[dict] = None):
    from mvp.deps import HDR_MODEL_PIN

    hdrs = dict(headers or {})
    if pin is not None:
        hdrs[HDR_MODEL_PIN] = pin
    return client.post("/v1/messages", headers=hdrs, json={
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 40, "stream": False,
    })
