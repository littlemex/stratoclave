"""C12 -- the one second-scope registry entry this PR reads:
`global.anthropic.claude-fable-5`.

"C12 must be shown to WORK, not only to be refused: an entitled,
scope-permitted tenant invokes it successfully. Refusal-only tests would
pass on an entry that can never be used by anyone." (task instructions,
restating the contract's own C12 verification row.)

This file does NOT edit `backend/mvp/defaults/models.json` -- landing that
registry edit is explicitly the production author's job for this same PR
(landing that file is this same PR's C12 item), and
this worktree is tests-only. Instead it builds a FIXTURE `ModelEntry` whose
fields are copied verbatim from the handoff's own C12 spec, and exercises it
through `install_registry` -- the same "injected test-only entry" technique
the contract's own Ordering section requires for reproducing the C6 bypass,
applied here to prove C12 WORKS once entitled and scope-permitted.

Fields, verbatim from the handoff:
  bedrock_model_id: global.anthropic.claude-fable-5
  profile_scope: global, jurisdiction_bounded: false, no jurisdiction
  model_family: claude-fable-5 (same family as the shipped `us.` entry)
  access: entitlement_required (NOT general)
  aliases: [claude-fable-5-global]
  pricing_key: fable-global

provider/wire_protocol/bedrock_region are not named in the handoff's C12
section; this file uses the values the ALREADY-SHIPPED `us.anthropic.
claude-fable-5` entry carries (`backend/mvp/defaults/models.json`), since
C12 is explicitly "a second scope of a model already served" -- read
directly from that file rather than guessed, so this is not a sixth flagged
divergence.
"""
from __future__ import annotations

import pytest

from mvp.models import ModelEntry
from tests.eligibility_fixtures import (
    FakeUser,
    build_client,
    ensure_tenant_and_membership,
    grant,
    patch_bedrock_converse,
    post_messages,
    put_tenant_routing,
)
from tests.eligibility_fixtures import install_registry as _install_registry

USER = "user-c12-1"
FABLE_FAMILY = "claude-fable-5"
FABLE_GLOBAL_ALIAS = "claude-fable-5-global"
FABLE_GLOBAL_BEDROCK_ID = "global.anthropic.claude-fable-5"

# Verbatim from the handoff's C12 section.
C12_ENTRY = ModelEntry(
    provider="anthropic",                  # from the shipped us. sibling
    bedrock_model_id=FABLE_GLOBAL_BEDROCK_ID,
    bedrock_region="us-east-1",             # from the shipped us. sibling (advisory for `messages`)
    aliases=(FABLE_GLOBAL_ALIAS,),
    wire_protocol="messages",               # from the shipped us. sibling
    pricing_key="fable-global",
    model_family=FABLE_FAMILY,
    profile_scope="global",
    access="entitlement_required",
    jurisdiction_bounded=False,
    jurisdiction=None,
)


@pytest.fixture
def client(dynamodb_mock, monkeypatch):
    _install_registry(monkeypatch, (C12_ENTRY,))
    user_holder = {"user": None}
    c = build_client(monkeypatch, current_user_provider=lambda: user_holder["user"])
    patch_bedrock_converse(monkeypatch)
    return c, user_holder


def _as_tenant(client_and_holder, tenant_id, user_id=USER):
    c, holder = client_and_holder
    holder["user"] = FakeUser(user_id=user_id, org_id=tenant_id)
    ensure_tenant_and_membership(tenant_id, user_id)
    return c


class TestC12Works:
    def test_entitled_scope_permitted_tenant_invokes_it_successfully(self, client):
        tenant = "c12-works"
        c = _as_tenant(client, tenant)
        r = put_tenant_routing(c, tenant, profile_scopes=["global"])
        assert r.status_code == 200, r.text
        grant(tenant, FABLE_FAMILY, "global")

        resp = post_messages(c, model=FABLE_GLOBAL_ALIAS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The mocked Bedrock completion actually reached the caller -- this
        # is a real invocation, not merely "the reserve step didn't 403".
        text_blocks = [
            b.get("text") for b in body.get("content", []) if isinstance(b, dict)
        ]
        assert any("hi from the mock" in (t or "") for t in text_blocks), body

    def test_it_writes_a_usage_row_for_the_effective_bedrock_model_id(self, client, dynamodb_mock):
        """Observable behaviour beyond the HTTP response: the usage log this
        request produces must carry the EFFECTIVE model actually invoked
        (`global.anthropic.claude-fable-5`), not the alias, not the sibling
        `us.` entry, and not some unrelated fallback."""
        tenant = "c12-usage-row"
        c = _as_tenant(client, tenant)
        assert put_tenant_routing(c, tenant, profile_scopes=["global"]).status_code == 200
        grant(tenant, FABLE_FAMILY, "global")

        resp = post_messages(c, model=FABLE_GLOBAL_ALIAS)
        assert resp.status_code == 200, resp.text

        table = dynamodb_mock.Table("stratoclave-usage-logs")
        rows = table.query(
            KeyConditionExpression="tenant_id = :t",
            ExpressionAttributeValues={":t": tenant},
        ).get("Items", [])
        assert len(rows) == 1, f"expected exactly one usage row for {tenant}, got {rows!r}"
        assert rows[0].get("model_id") == FABLE_GLOBAL_BEDROCK_ID, (
            f"the usage row must record the EFFECTIVE model actually "
            f"invoked: expected {FABLE_GLOBAL_BEDROCK_ID!r}, got "
            f"{rows[0].get('model_id')!r}"
        )


class TestC12StillGatedNotAWorldOpenEntry:
    """Non-vacuous companions: C12 working for one tenant must not mean it
    works for anyone -- otherwise the success test above would pass on an
    entry that ignores eligibility entirely."""

    def test_ungranted_tenant_is_refused_model_not_entitled(self, client):
        tenant = "c12-ungranted"
        c = _as_tenant(client, tenant)
        assert put_tenant_routing(c, tenant, profile_scopes=["global"]).status_code == 200
        # No grant.
        resp = post_messages(c, model=FABLE_GLOBAL_ALIAS)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["reason"] == "model_not_entitled", resp.text

    def test_granted_but_out_of_scope_tenant_is_refused_scope_not_allowed(self, client):
        tenant = "c12-wrong-scope"
        c = _as_tenant(client, tenant)
        assert put_tenant_routing(c, tenant, profile_scopes=["us"]).status_code == 200
        grant(tenant, FABLE_FAMILY, "global")  # entitled, but the tenant is us-only
        resp = post_messages(c, model=FABLE_GLOBAL_ALIAS)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["reason"] == "scope_not_allowed", resp.text

    def test_default_empty_allowlist_tenant_with_no_grant_and_no_scope_write_is_refused(self, client):
        """The dangerous baseline the handoff's own Ordering section warns
        about: a tenant that has configured NOTHING (empty allowlist,
        absent profile_scopes) must still be refused for C12's entry, since
        `access=entitlement_required` -- never `general` -- is what keeps an
        empty-allowlist tenant from reaching it the moment it lands."""
        tenant = "c12-default-tenant"
        c = _as_tenant(client, tenant)
        # No routing config write at all, no grant.
        resp = post_messages(c, model=FABLE_GLOBAL_ALIAS)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["reason"] == "model_not_entitled", resp.text
