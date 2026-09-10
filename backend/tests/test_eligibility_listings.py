"""C8 -- both model-listing routes filtered per caller, exposing family and
scope, protocol shape preserved.

Handoff, verbatim: "Both keep their existing protocol shape and gain two
fields per element... An entry the caller cannot use is absent, not
present-and-marked... If the tenant, user or entitlement read fails, the
listing returns 503, never an empty or partial list."

Contract's own verification plan for C8: "three tenant pairs differing on
exactly one axis each, plus a shape assertion." This file supplies three
pairs -- entitlement, scope, model-policy -- for EACH route, plus the shape
assertion and the 503-on-read-failure cases.

Fixture entries use a `list-` alias prefix throughout so assertions can
select "the entries this file added" out of a response that also contains
every real, shipped registry entry (`keep_real=True` in `install_registry`)
without needing to enumerate or exclude the real ones by name.
"""
from __future__ import annotations

import pytest

from mvp.models import ModelEntry
from tests.eligibility_fixtures import (
    FakeUser,
    build_client,
    ensure_tenant_and_membership,
    grant,
    put_tenant_routing,
)
from tests.eligibility_fixtures import install_registry as _install_registry

USER = "user-c8-1"


def _entry(provider, wire_protocol, alias, family, scope, access, *, region="us-east-1"):
    bounded = scope not in ("global",)
    return ModelEntry(
        provider=provider,
        bedrock_model_id=f"{scope}.{provider}.{family}" if scope != "no_profile" else f"{provider}.{family}",
        bedrock_region=region, aliases=(alias,), wire_protocol=wire_protocol,
        pricing_key="default", model_family=family, profile_scope=scope, access=access,
        jurisdiction_bounded=bounded, jurisdiction=scope if bounded else None,
    )


# Anthropic-flavoured fixtures (for GET /v1/models).
A_BASELINE = _entry("anthropic", "messages", "list-alpha", "list-fam-alpha", "us", "general")
A_ENT = _entry("anthropic", "messages", "list-beta", "list-fam-beta", "us", "entitlement_required")
A_SCOPE = _entry("anthropic", "messages", "list-gamma", "list-fam-gamma", "apac", "general")
A_POLICY = _entry("anthropic", "messages", "list-delta", "list-fam-delta", "us", "general")

# OpenAI-flavoured fixtures (for GET /openai/v1/models).
O_BASELINE = _entry("openai", "responses", "list-alpha-o", "list-fam-alpha-o", "us", "general", region="us-east-2")
O_ENT = _entry("openai", "responses", "list-beta-o", "list-fam-beta-o", "us", "entitlement_required", region="us-east-2")
O_SCOPE = _entry("openai", "responses", "list-gamma-o", "list-fam-gamma-o", "apac", "general", region="us-east-2")
O_POLICY = _entry("openai", "responses", "list-delta-o", "list-fam-delta-o", "us", "general", region="us-east-2")


def _client_for(monkeypatch, dynamodb_mock, entries, tenant_id):
    _install_registry(monkeypatch, entries)
    user = FakeUser(user_id=USER, org_id=tenant_id)
    c = build_client(monkeypatch, current_user_provider=lambda: user)
    ensure_tenant_and_membership(tenant_id, USER)
    return c


def _by_alias(data, alias):
    return next((row for row in data if row.get("id") == alias), None)


# ---------------------------------------------------------------------------
# Anthropic route: GET /v1/models
# ---------------------------------------------------------------------------
class TestAnthropicListingEntitlementAxis:
    def test_granted_tenant_sees_the_entitled_entry(self, monkeypatch, dynamodb_mock):
        tenant = "c8-ant-ent-yes"
        c = _client_for(monkeypatch, dynamodb_mock, (A_BASELINE, A_ENT), tenant)
        grant(tenant, "list-fam-beta", "us")
        resp = c.get("/v1/models")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert _by_alias(data, "list-beta") is not None
        assert _by_alias(data, "list-alpha") is not None  # baseline unaffected

    def test_ungranted_tenant_does_not_see_it_absent_not_marked(self, monkeypatch, dynamodb_mock):
        tenant = "c8-ant-ent-no"
        c = _client_for(monkeypatch, dynamodb_mock, (A_BASELINE, A_ENT), tenant)
        # No grant.
        resp = c.get("/v1/models")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert _by_alias(data, "list-beta") is None, (
            f"an ungranted entitlement_required entry must be ABSENT, not "
            f"present with some disabled marker: {data!r}"
        )
        assert _by_alias(data, "list-alpha") is not None


class TestAnthropicListingScopeAxis:
    def test_unrestricted_tenant_sees_the_out_of_default_scope_entry(self, monkeypatch, dynamodb_mock):
        tenant = "c8-ant-scope-wide"
        c = _client_for(monkeypatch, dynamodb_mock, (A_BASELINE, A_SCOPE), tenant)
        # No profile_scopes restriction at all -> absent axis does not restrict.
        resp = c.get("/v1/models")
        data = resp.json()["data"]
        assert _by_alias(data, "list-gamma") is not None
        assert _by_alias(data, "list-alpha") is not None  # us-scoped baseline still visible

    def test_us_restricted_tenant_does_not_see_the_apac_entry(self, monkeypatch, dynamodb_mock):
        tenant = "c8-ant-scope-narrow"
        c = _client_for(monkeypatch, dynamodb_mock, (A_BASELINE, A_SCOPE), tenant)
        r = put_tenant_routing(c, tenant, profile_scopes=["us"])
        assert r.status_code == 200, r.text
        resp = c.get("/v1/models")
        data = resp.json()["data"]
        assert _by_alias(data, "list-gamma") is None, (
            f"a us-restricted tenant must not see an apac-scoped entry: {data!r}"
        )
        assert _by_alias(data, "list-alpha") is not None  # still in scope


class TestAnthropicListingModelPolicyAxis:
    def test_open_allowlist_tenant_sees_both(self, monkeypatch, dynamodb_mock):
        tenant = "c8-ant-policy-open"
        c = _client_for(monkeypatch, dynamodb_mock, (A_BASELINE, A_POLICY), tenant)
        resp = c.get("/v1/models")
        data = resp.json()["data"]
        assert _by_alias(data, "list-alpha") is not None
        assert _by_alias(data, "list-delta") is not None

    def test_closed_allowlist_tenant_sees_only_the_listed_entry(self, monkeypatch, dynamodb_mock):
        tenant = "c8-ant-policy-closed"
        c = _client_for(monkeypatch, dynamodb_mock, (A_BASELINE, A_POLICY), tenant)
        r = put_tenant_routing(c, tenant, allowlist=["list-alpha"])
        assert r.status_code == 200, r.text
        resp = c.get("/v1/models")
        data = resp.json()["data"]
        assert _by_alias(data, "list-alpha") is not None
        assert _by_alias(data, "list-delta") is None, (
            f"an allowlist naming only list-alpha must exclude list-delta: {data!r}"
        )


def test_anthropic_listing_shape_is_additive_and_protocol_compatible(monkeypatch, dynamodb_mock):
    """Existing keys survive; family/scope are ADDITIONAL fields on the same
    flat element, not a nested regrouping -- `{"data": [...], "has_more",
    "first_id", "last_id"}` is the protocol-compatible shape Claude Desktop
    cowork probes."""
    tenant = "c8-ant-shape"
    c = _client_for(monkeypatch, dynamodb_mock, (A_BASELINE,), tenant)
    resp = c.get("/v1/models")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("data", "has_more", "first_id", "last_id"):
        assert key in body, f"top-level key {key!r} missing from {body!r}"
    row = _by_alias(body["data"], "list-alpha")
    assert row is not None
    for key in ("id", "type", "display_name", "created_at"):
        assert key in row, f"existing key {key!r} missing from listing element {row!r}"
    assert row.get("model_family") == "list-fam-alpha"
    assert row.get("profile_scope") == "us"


# ---------------------------------------------------------------------------
# OpenAI route: GET /openai/v1/models
# ---------------------------------------------------------------------------
class TestOpenAIListingEntitlementAxis:
    def test_granted_tenant_sees_the_entitled_entry(self, monkeypatch, dynamodb_mock):
        tenant = "c8-oai-ent-yes"
        c = _client_for(monkeypatch, dynamodb_mock, (O_BASELINE, O_ENT), tenant)
        grant(tenant, "list-fam-beta-o", "us")
        resp = c.get("/openai/v1/models")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert _by_alias(data, "list-beta-o") is not None

    def test_ungranted_tenant_does_not_see_it(self, monkeypatch, dynamodb_mock):
        tenant = "c8-oai-ent-no"
        c = _client_for(monkeypatch, dynamodb_mock, (O_BASELINE, O_ENT), tenant)
        resp = c.get("/openai/v1/models")
        data = resp.json()["data"]
        assert _by_alias(data, "list-beta-o") is None
        assert _by_alias(data, "list-alpha-o") is not None


class TestOpenAIListingScopeAxis:
    def test_unrestricted_tenant_sees_the_apac_entry(self, monkeypatch, dynamodb_mock):
        tenant = "c8-oai-scope-wide"
        c = _client_for(monkeypatch, dynamodb_mock, (O_BASELINE, O_SCOPE), tenant)
        resp = c.get("/openai/v1/models")
        data = resp.json()["data"]
        assert _by_alias(data, "list-gamma-o") is not None

    def test_us_restricted_tenant_does_not_see_the_apac_entry(self, monkeypatch, dynamodb_mock):
        tenant = "c8-oai-scope-narrow"
        c = _client_for(monkeypatch, dynamodb_mock, (O_BASELINE, O_SCOPE), tenant)
        r = put_tenant_routing(c, tenant, profile_scopes=["us"])
        assert r.status_code == 200, r.text
        resp = c.get("/openai/v1/models")
        data = resp.json()["data"]
        assert _by_alias(data, "list-gamma-o") is None


class TestOpenAIListingModelPolicyAxis:
    def test_open_allowlist_tenant_sees_both(self, monkeypatch, dynamodb_mock):
        tenant = "c8-oai-policy-open"
        c = _client_for(monkeypatch, dynamodb_mock, (O_BASELINE, O_POLICY), tenant)
        resp = c.get("/openai/v1/models")
        data = resp.json()["data"]
        assert _by_alias(data, "list-alpha-o") is not None
        assert _by_alias(data, "list-delta-o") is not None

    def test_closed_allowlist_tenant_sees_only_the_listed_entry(self, monkeypatch, dynamodb_mock):
        tenant = "c8-oai-policy-closed"
        c = _client_for(monkeypatch, dynamodb_mock, (O_BASELINE, O_POLICY), tenant)
        r = put_tenant_routing(c, tenant, allowlist=["list-alpha-o"])
        assert r.status_code == 200, r.text
        resp = c.get("/openai/v1/models")
        data = resp.json()["data"]
        assert _by_alias(data, "list-alpha-o") is not None
        assert _by_alias(data, "list-delta-o") is None


def test_openai_listing_shape_is_additive_and_protocol_compatible(monkeypatch, dynamodb_mock):
    tenant = "c8-oai-shape"
    c = _client_for(monkeypatch, dynamodb_mock, (O_BASELINE,), tenant)
    resp = c.get("/openai/v1/models")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("object") == "list"
    assert "data" in body
    row = _by_alias(body["data"], "list-alpha-o")
    assert row is not None
    for key in ("id", "object", "created", "owned_by"):
        assert key in row, f"existing key {key!r} missing from listing element {row!r}"
    assert row.get("model_family") == "list-fam-alpha-o"
    assert row.get("profile_scope") == "us"


# ---------------------------------------------------------------------------
# Failure paths: a tenant/user/entitlement read failure is 503, never an
# empty or partial list.
# ---------------------------------------------------------------------------
class TestListingFailsClosedOnReadFailure:
    def test_anthropic_listing_503_when_entitlement_store_is_unreachable(
        self, monkeypatch, dynamodb_mock,
    ):
        tenant = "c8-ant-503-entitlement"
        c = _client_for(monkeypatch, dynamodb_mock, (A_BASELINE, A_ENT), tenant)

        from mvp.admin_entitlements import EntitlementStoreUnavailable

        def _boom(tenant_id):
            raise EntitlementStoreUnavailable("moto-simulated outage")

        monkeypatch.setattr("mvp.admin_entitlements.list_entitlements", _boom)
        resp = c.get("/v1/models")
        assert resp.status_code == 503, (
            f"an unreadable entitlement store must be 503, never a silently "
            f"short list: {resp.status_code} {resp.text!r}"
        )

    def test_anthropic_listing_503_when_tenant_routing_config_is_unreadable(
        self, monkeypatch, dynamodb_mock,
    ):
        tenant = "c8-ant-503-tenant-cfg"
        c = _client_for(monkeypatch, dynamodb_mock, (A_BASELINE,), tenant)

        from mvp.routing.config import RoutingConfigUnavailable

        def _boom(tenant_id):
            raise RoutingConfigUnavailable("moto-simulated outage")

        monkeypatch.setattr("mvp.routing.config.get_tenant_routing_config", _boom)
        resp = c.get("/v1/models")
        assert resp.status_code == 503, (
            f"an unreadable tenant routing config must be 503: "
            f"{resp.status_code} {resp.text!r}"
        )

    def test_openai_listing_503_when_entitlement_store_is_unreachable(
        self, monkeypatch, dynamodb_mock,
    ):
        tenant = "c8-oai-503-entitlement"
        c = _client_for(monkeypatch, dynamodb_mock, (O_BASELINE, O_ENT), tenant)

        from mvp.admin_entitlements import EntitlementStoreUnavailable

        def _boom(tenant_id):
            raise EntitlementStoreUnavailable("moto-simulated outage")

        monkeypatch.setattr("mvp.admin_entitlements.list_entitlements", _boom)
        resp = c.get("/openai/v1/models")
        assert resp.status_code == 503, (
            f"{resp.status_code} {resp.text!r}"
        )
