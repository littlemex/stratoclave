"""C9/C14 -- the tenant and user `profile_scopes` axes on the routing writer.

PR2 handoff: "A tenant may carry a scope set; a user may carry one. Both
restrict only: entitlement (sic) tenant set (sic) user set. Enforced at write
time -- a widening write is refused, naming the offending value -- and as an
intersection when read... An absent axis does not restrict. An axis present
but empty ([]) is a rejected write." Plus C14: "A scope set that names both a
bounded scope and an unbounded one is refused when written, with a message
naming both members... `{us}` alone and `{global}` alone are both valid."

C9 explicitly names the write path this lands on: "persisted through the
ROUTING WRITER" -- i.e. `mvp.admin_routing`'s existing
`PUT /api/mvp/admin/tenants/{tenant_id}/routing-config` and its user-scoped
sibling, NOT a new route. The field name is `profile_scopes` on both
`TenantRoutingConfigRequest` and `UserRoutingConfigRequest` (and the matching
response views) -- settled in the contract now (the registry's own field is
`profile_scope`, singular, so a second word for the same concept would have
been worse than the plural of the existing one). This file no longer treats
the name as a guess.

Only PR2's `tenants:update` writer + `tenants:read-all` reader path is
touched here (no request-time enforcement -- that is PR3). Also confirmed in
the contract: a tenant's `profile_scopes` write is NOT validated against the
aggregate of that tenant's C4 entitlement grants -- entitlement and the two
axes are independent restrictions intersected at request time in PR3, not a
write-time hierarchy -- so no such coupling is asserted here (see
`test_routing_config_write_creates_no_entitlement_item` below for the
positive form of that independence).
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.admin_routing import router as routing_router
from mvp.deps import get_current_user

TENANT = "acme-scope-eng"
USER = "u-scope-1"


@dataclass
class _AdminUser:
    user_id: str = "admin-1"
    org_id: str = "ops"
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
    # This suite is about profile_scopes validation/persistence, not the
    # entitlements:*/tenants:* permission gate -- bypass it, exactly as the
    # existing routing-config HTTP suite does.
    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: True)
    from dynamo.tenants import TenantsRepository
    TenantsRepository().create(tenant_id=TENANT, team_lead_user_id="admin-1",
                               name="Acme Scope", created_by="admin-1")
    from dynamo import UserTenantsRepository
    UserTenantsRepository().ensure(user_id=USER, tenant_id=TENANT, role="user",
                                   total_credit=10**9)
    from mvp.routing import config as rc
    rc._cache.clear()
    app = FastAPI()
    app.include_router(routing_router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser()
    return TestClient(app)


def _put_tenant(client, **body):
    return client.put(f"/api/mvp/admin/tenants/{TENANT}/routing-config", json=body)


def _get_tenant(client):
    return client.get(f"/api/mvp/admin/tenants/{TENANT}/routing-config")


def _put_user(client, **body):
    return client.put(
        f"/api/mvp/admin/tenants/{TENANT}/users/{USER}/routing-config", json=body)


def _get_user(client):
    return client.get(f"/api/mvp/admin/tenants/{TENANT}/users/{USER}/routing-config")


def _raw_tenant_item(dynamodb_mock):
    from mvp.routing.config import _TABLE
    resp = dynamodb_mock.Table(_TABLE).get_item(
        Key={"user_id": "CONFIG#ROUTING", "tenant_id": TENANT})
    return resp.get("Item")


def _raw_user_item(dynamodb_mock):
    from mvp.routing.config import _TABLE
    resp = dynamodb_mock.Table(_TABLE).get_item(
        Key={"user_id": f"CONFIG#ROUTING#USER#{USER}", "tenant_id": TENANT})
    return resp.get("Item")


# ---------------------------------------------------------------------------
# Absent vs empty (the allowlist "empty means everything" trap, called out
# by name in the handoff as a failure mode this axis must NOT inherit).
# ---------------------------------------------------------------------------
class TestAbsentVsEmpty:
    def test_absent_profile_scopes_does_not_restrict(self, client):
        r = _put_tenant(client, fallback_default="on")
        assert r.status_code == 200, r.text
        assert r.json().get("profile_scopes") is None
        g = _get_tenant(client)
        assert g.json().get("profile_scopes") is None

    def test_empty_tenant_profile_scopes_is_a_rejected_write(self, client):
        r = _put_tenant(client, profile_scopes=[])
        assert r.status_code == 400, r.text
        assert "profile_scopes" in r.text

    def test_empty_user_profile_scopes_is_a_rejected_write(self, client):
        r = _put_user(client, profile_scopes=[])
        assert r.status_code == 400, r.text
        assert "profile_scopes" in r.text


# ---------------------------------------------------------------------------
# C14 -- bounded vs unbounded.
# ---------------------------------------------------------------------------
class TestBoundedUnboundedContradiction:
    def test_us_alone_is_valid(self, client):
        assert _put_tenant(client, profile_scopes=["us"]).status_code == 200

    def test_global_alone_is_valid(self, client):
        assert _put_tenant(client, profile_scopes=["global"]).status_code == 200

    def test_jp_and_global_together_is_refused_naming_both(self, client):
        """The handoff's own literal example: '{jp, global}' reads like
        'Japan, or cheap' and means 'Japan, or anywhere on earth'."""
        r = _put_tenant(client, profile_scopes=["jp", "global"])
        assert r.status_code == 400, r.text
        assert "jp" in r.text and "global" in r.text

    def test_us_and_global_together_is_also_refused(self, client):
        r = _put_tenant(client, profile_scopes=["us", "global"])
        assert r.status_code == 400, r.text

    def test_contradiction_applies_to_the_user_axis_too(self, client):
        _put_tenant(client, profile_scopes=["us", "eu", "jp", "global"])
        r = _put_user(client, profile_scopes=["jp", "global"])
        assert r.status_code == 400, r.text
        assert "jp" in r.text and "global" in r.text

    def test_gov_is_accepted_as_a_bounded_scope_even_though_unbuilt(self, client):
        """The handoff: 'gov remains an accepted value because the
        vocabulary is derived; build nothing for it.' Structural acceptance
        only -- nothing else in this suite exercises gov."""
        assert _put_tenant(client, profile_scopes=["gov"]).status_code == 200


# ---------------------------------------------------------------------------
# Vocabulary: profile_scopes members must be real profile_scope values.
# ---------------------------------------------------------------------------
class TestVocabulary:
    def test_unknown_scope_token_is_refused(self, client):
        r = _put_tenant(client, profile_scopes=["atlantis"])
        assert r.status_code == 400, r.text
        assert "atlantis" in r.text


# ---------------------------------------------------------------------------
# C9 -- tenant set superset-or-equal user set, enforced at write time, not
# silently intersected.
# ---------------------------------------------------------------------------
class TestTenantSupersetUser:
    def test_user_subset_of_tenant_is_accepted(self, client):
        assert _put_tenant(client, profile_scopes=["us", "eu"]).status_code == 200
        r = _put_user(client, profile_scopes=["us"])
        assert r.status_code == 200, r.text

    def test_user_equal_to_tenant_is_accepted(self, client):
        assert _put_tenant(client, profile_scopes=["us", "eu"]).status_code == 200
        assert _put_user(client, profile_scopes=["us", "eu"]).status_code == 200

    def test_user_widening_beyond_tenant_is_refused_naming_the_offender(self, client, dynamodb_mock):
        assert _put_tenant(client, profile_scopes=["us"]).status_code == 200
        r = _put_user(client, profile_scopes=["us", "eu"])
        assert r.status_code == 400, r.text
        assert "eu" in r.text, (
            "the refusal must name the offending value (eu), not just say "
            f"'invalid': {r.text!r}"
        )
        # Not silently intersected: the write must have had NO effect at all
        # (no user config row created with the narrowed {us}).
        after = _get_user(client)
        assert after.json().get("configured") is False, (
            "a refused widening write must not leave behind a silently "
            "narrowed row"
        )
        assert _raw_user_item(dynamodb_mock) is None

    def test_user_profile_scopes_is_unrestricted_when_tenant_axis_is_absent(self, client):
        """An absent parent axis does not restrict -- the user may declare
        any valid profile_scopes when the tenant has none."""
        r = _put_user(client, profile_scopes=["jp"])
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# GET is raw, not intersected -- settled in the contract (the opposite of
# this file's first-draft assumption). `get_tenant_routing` returns
# `_parse_tenant_config(item)` verbatim, and `put_tenant_routing` is a full
# replace whose own docstring assumes "the UI pre-fills from GET and the CLI
# sends a full file" -- an intersecting GET would turn that ordinary
# read-modify-write into a silent narrowing of the user's stored document the
# next time anyone PUTs it back. So the CONFIG GET reports the raw stored
# value, and the EFFECTIVE (enforceable) value is a separate, explicit
# combinator: `mvp.routing.config.effective_profile_scopes(tenant, user)`,
# called directly here rather than reimplemented.
# ---------------------------------------------------------------------------
class TestRawGetVsEffectiveProfileScopes:
    def test_user_get_returns_the_raw_stored_value_after_tenant_narrows(self, client, dynamodb_mock):
        assert _put_tenant(client, profile_scopes=["us", "eu", "jp"]).status_code == 200
        assert _put_user(client, profile_scopes=["eu", "jp"]).status_code == 200

        # Tenant narrows AFTER the user's write was already validated and
        # stored.
        assert _put_tenant(client, profile_scopes=["us", "eu"]).status_code == 200

        raw_from_get = set(_get_user(client).json().get("profile_scopes") or [])
        assert raw_from_get == {"eu", "jp"}, (
            f"GET must return the user's OWN stored profile_scopes unchanged "
            f"by the tenant's later, unrelated narrowing -- an intersecting "
            f"GET would silently narrow the document on the next ordinary "
            f"read-modify-write PUT. Got {raw_from_get!r}"
        )
        # The underlying item agrees -- this is not merely a wire-response
        # nicety papering over a config object that was actually rewritten.
        raw_item = _raw_user_item(dynamodb_mock)
        assert raw_item is not None and set(raw_item.get("profile_scopes", [])) == {"eu", "jp"}, (
            f"the tenant's later narrowing must not rewrite the user's own "
            f"stored row: {raw_item!r}"
        )

    def test_effective_profile_scopes_intersects_tenant_and_user(self, client):
        assert _put_tenant(client, profile_scopes=["us", "eu", "jp"]).status_code == 200
        assert _put_user(client, profile_scopes=["eu", "jp"]).status_code == 200
        assert _put_tenant(client, profile_scopes=["us", "eu"]).status_code == 200

        # Imported lazily so a missing name fails only these two tests, not
        # collection of the whole file (see the C15 file for the same
        # discipline against --continue-on-collection-errors not being set).
        from mvp.routing.config import (
            effective_profile_scopes, get_tenant_routing_config, get_user_routing_config,
        )
        tenant_cfg = get_tenant_routing_config(TENANT)
        user_cfg = get_user_routing_config(TENANT, USER)
        effective = set(effective_profile_scopes(tenant_cfg, user_cfg) or [])
        assert effective == {"eu"}, (
            f"effective_profile_scopes(tenant, user) must intersect the "
            f"tenant's current {{us,eu}} with the user's stored {{eu,jp}} == "
            f"{{eu}}; got {effective!r}"
        )

    def test_effective_profile_scopes_when_user_axis_is_absent_is_the_tenant_set(self, client):
        """No user override at all -- the user inherits the tenant's set
        whole, not an intersection with 'nothing'."""
        assert _put_tenant(client, profile_scopes=["us", "eu"]).status_code == 200
        from mvp.routing.config import (
            effective_profile_scopes, get_tenant_routing_config, get_user_routing_config,
        )
        tenant_cfg = get_tenant_routing_config(TENANT)
        user_cfg = get_user_routing_config(TENANT, USER)
        assert user_cfg is None or user_cfg.profile_scopes is None
        effective = set(effective_profile_scopes(tenant_cfg, user_cfg) or [])
        assert effective == {"us", "eu"}, (
            f"an absent user axis must not restrict -- expected the tenant's "
            f"own {{us,eu}}, got {effective!r}"
        )

    def test_effective_profile_scopes_when_tenant_axis_is_absent_is_the_user_set(self, client):
        """The tenant has no profile_scopes at all -- an absent parent axis
        does not restrict, so the user's own set is fully effective."""
        assert _put_user(client, profile_scopes=["jp"]).status_code == 200
        from mvp.routing.config import (
            effective_profile_scopes, get_tenant_routing_config, get_user_routing_config,
        )
        tenant_cfg = get_tenant_routing_config(TENANT)
        assert tenant_cfg.profile_scopes is None
        user_cfg = get_user_routing_config(TENANT, USER)
        effective = set(effective_profile_scopes(tenant_cfg, user_cfg) or [])
        assert effective == {"jp"}, f"expected the user's own {{jp}}, got {effective!r}"

    def test_effective_profile_scopes_when_both_axes_are_absent_is_unrestricted(self, client):
        from mvp.routing.config import effective_profile_scopes, get_tenant_routing_config, get_user_routing_config
        tenant_cfg = get_tenant_routing_config(TENANT)
        user_cfg = get_user_routing_config(TENANT, USER)
        assert effective_profile_scopes(tenant_cfg, user_cfg) is None, (
            "both axes absent must mean 'no restriction', not an empty set "
            "(the same everything-vs-nothing distinction the write-time "
            "empty-list rejection exists to protect)"
        )


# ---------------------------------------------------------------------------
# C4/C9 boundary: writing the routing config must not touch grants, and vice
# versa (asserted properly in the entitlement-store suite against a granted
# triple; here we pin the routing-config side: a profile_scopes write must not
# create or disturb any ENTITLEMENT# item in the same table).
# ---------------------------------------------------------------------------
def test_routing_config_write_creates_no_entitlement_item(client, dynamodb_mock):
    from mvp.routing.config import _TABLE

    assert _put_tenant(client, profile_scopes=["us"]).status_code == 200
    assert _put_user(client, profile_scopes=["us"]).status_code == 200

    items = dynamodb_mock.Table(_TABLE).scan().get("Items", [])
    entitlement_items = [i for i in items if str(i.get("user_id", "")).startswith("ENTITLEMENT#")]
    assert entitlement_items == [], (
        f"a routing-config write must never create an ENTITLEMENT# item: "
        f"{entitlement_items!r}"
    )
