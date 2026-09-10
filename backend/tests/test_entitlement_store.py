"""C4/C11 -- the entitlement store: grants as a separate item type in the
existing `stratoclave-user-tenants` table, plus the audit trail on grant and
revoke.

PR2 handoff, verbatim on the parts that are NOT guesses in this file:

  - "A grant is a separate item type in that same table. Use the key
    convention `user_id = "ENTITLEMENT#{model_family}#{profile_scope}"`,
    `tenant_id = {tenant_id}`" -- the table name, PK/SK attribute names and
    the exact `user_id` value template are the handoff's own words, not a
    guess, and are asserted against the RAW stored item.
  - "Granting the same triple twice is idempotent. Revoking a grant that does
    not exist succeeds. A grant naming a family or scope that no registry
    entry has is rejected... A grant on an entry whose `access` is `general`
    is rejected as meaningless."
  - "Writing the routing config must not touch grants, and writing a grant
    must not touch the routing config."
  - "Every grant and revoke emits an audit event after the write commits,
    carrying actor, tenant, the entry triple, and before/after. A failed
    audit write does not roll back the grant, and says so. Use the existing
    `mvp.authz.log_audit_event`."
  - Failure paths table: read of grants fails closed on a retryable 503 when
    the store is unreachable, naming the dependency.

SETTLED IN THE CONTRACT (was a flagged guess in this file's first version; the
handoff had not named a route for the store, only the DynamoDB key, so the
code author and this file each invented one -- the contract has since picked
this file's reading and named it explicitly):
  - module `mvp.admin_entitlements`, `router` mounted at the same
    `/api/mvp/admin/tenants` prefix `mvp.admin_routing` uses;
  - `PUT    /{tenant_id}/entitlements/{model_family}/{profile_scope}` to grant
    (gated on `entitlements:grant`);
  - `DELETE /{tenant_id}/entitlements/{model_family}/{profile_scope}` to
    revoke (also gated on `entitlements:grant`);
  - `GET    /{tenant_id}/entitlements` to list a tenant's grants (gated on
    `entitlements:read`).

RESPONSE BODIES, also now settled in the contract:
  - `GET    /{tenant_id}/entitlements` returns `{"grants": [...]}` -- a named
    key, not a bare list, so the shape can grow without breaking a client.
  - `PUT` returns the resulting grant view (a `{"model_family": ...,
    "profile_scope": ...}` object -- the same shape a `grants` list item
    takes, since "a grant view" naturally means the thing `GET` lists).
  - `DELETE` returns **204 with no body**. Returning "the resulting grant"
    after removing it would be incoherent; 204 is what an idempotent delete
    says, whether or not a grant existed beforehand.

The audit event's kwargs are also settled: `target_type="entitlement"`,
`target_id="{model_family}#{profile_scope}"`, `tenant_id`, and `before`/
`after` -- pinned directly rather than searched for anywhere in the call, so
a triple hidden somewhere a reader would not look for it does not pass.

AUDIT-WRITE-FAILURE RESPONSE, settled (this file's first version guessed
`status_code >= 500`; that is wrong -- the grant/revoke already committed, so
a 5xx would tell the caller an operation failed that in fact succeeded, the
same lie the settle path refuses. A log line alone is also wrong: it leaves
the caller no way to know the audit trail is incomplete). The resolution
reuses a pattern already in this codebase, `mvp/task_tag.py`'s
`x-sc-task-tag-dropped` ("informational, never a status -- but the drop is
no longer silent to a caller that reads it"): the response keeps its normal
success status (200 for a grant, 204 for a revoke) and carries
`x-sc-audit-dropped` naming why, present ONLY when the audit write actually
failed -- asserted both ways, since an implementation that always sets the
header would pass a presence-only check.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.deps import get_current_user

TENANT = "acme-ent-eng"

GRANTABLE_FAMILY = "acme-widget"
GRANTABLE_SCOPE = "us"
GRANTABLE_SCOPE_2 = "global"  # second scope, same family (C10's own example shape)
GENERAL_FAMILY = "legacy-widget"
GENERAL_SCOPE = "us"


def _fixture_registry():
    from mvp.models import ModelEntry

    return (
        ModelEntry(
            provider="anthropic", bedrock_model_id="us.anthropic.acme-widget",
            bedrock_region="us-east-1", aliases=("acme-widget-us",),
            wire_protocol="messages", pricing_key="default",
            model_family=GRANTABLE_FAMILY, profile_scope=GRANTABLE_SCOPE,
            access="entitlement_required", jurisdiction_bounded=True, jurisdiction="us",
        ),
        ModelEntry(
            provider="anthropic", bedrock_model_id="global.anthropic.acme-widget",
            bedrock_region="us-east-1", aliases=("acme-widget-global",),
            wire_protocol="messages", pricing_key="default",
            model_family=GRANTABLE_FAMILY, profile_scope=GRANTABLE_SCOPE_2,
            access="entitlement_required", jurisdiction_bounded=False, jurisdiction=None,
        ),
        ModelEntry(
            provider="anthropic", bedrock_model_id="us.anthropic.legacy-widget",
            bedrock_region="us-east-1", aliases=("legacy-widget-us",),
            wire_protocol="messages", pricing_key="default",
            model_family=GENERAL_FAMILY, profile_scope=GENERAL_SCOPE,
            access="general", jurisdiction_bounded=True, jurisdiction="us",
        ),
    )


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
def registry(monkeypatch):
    monkeypatch.setattr("mvp.models._REGISTRY", _fixture_registry())


@pytest.fixture
def app_and_client(dynamodb_mock, registry, monkeypatch):
    """Permission checks bypassed -- this fixture is for the STORE's own
    behaviour (idempotency, rejection rules, key format, separation from the
    routing config). Permission gating itself is exercised separately, with
    the real evaluator, in `TestPermissionGate` below."""
    import mvp.authz as _authz
    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: True)

    from dynamo.tenants import TenantsRepository
    TenantsRepository().create(tenant_id=TENANT, team_lead_user_id="admin-1",
                               name="Acme Ent", created_by="admin-1")

    from mvp.admin_entitlements import router as entitlements_router
    app = FastAPI()
    app.include_router(entitlements_router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser()
    # raise_server_exceptions=False: an audit-write failure "not swallowed"
    # could surface as either a handled 5xx response or an unhandled
    # exception the framework converts to one -- this makes both show up as
    # a normal Response instead of the test itself blowing up on the latter.
    return app, TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(app_and_client):
    return app_and_client[1]


def _grant(client, family=GRANTABLE_FAMILY, scope=GRANTABLE_SCOPE, tenant=TENANT):
    return client.put(f"/api/mvp/admin/tenants/{tenant}/entitlements/{family}/{scope}")


def _revoke(client, family=GRANTABLE_FAMILY, scope=GRANTABLE_SCOPE, tenant=TENANT):
    return client.delete(f"/api/mvp/admin/tenants/{tenant}/entitlements/{family}/{scope}")


def _list(client, tenant=TENANT):
    return client.get(f"/api/mvp/admin/tenants/{tenant}/entitlements")


def _raw_grant_item(dynamodb_mock, family=GRANTABLE_FAMILY, scope=GRANTABLE_SCOPE, tenant=TENANT):
    from mvp.routing.config import _TABLE
    resp = dynamodb_mock.Table(_TABLE).get_item(
        Key={"user_id": f"ENTITLEMENT#{family}#{scope}", "tenant_id": tenant})
    return resp.get("Item")


# ---------------------------------------------------------------------------
# The stored item: exact key convention from the handoff.
# ---------------------------------------------------------------------------
class TestKeyConvention:
    def test_grant_creates_an_item_at_the_documented_key(self, client, dynamodb_mock):
        r = _grant(client)
        assert r.status_code in (200, 201), r.text
        item = _raw_grant_item(dynamodb_mock)
        assert item is not None, (
            "no item at user_id='ENTITLEMENT#{family}#{scope}', "
            f"tenant_id={TENANT!r} after a successful grant"
        )

    def test_put_returns_the_resulting_grant_view(self, client):
        """'PUT returns the resulting grant view' -- the caller sees the
        state it produced, identified by the same (model_family,
        profile_scope) pair GET's `grants` list items carry."""
        r = _grant(client)
        assert r.status_code in (200, 201), r.text
        body = r.json()
        assert body.get("model_family") == GRANTABLE_FAMILY, body
        assert body.get("profile_scope") == GRANTABLE_SCOPE, body

    def test_grant_lives_in_the_user_tenants_table_not_a_new_one(self, dynamodb_mock, client):
        """'No new table and no IaC' -- the item must be reachable through the
        SAME table name routing config already uses."""
        from mvp.routing.config import _TABLE
        assert _TABLE == "stratoclave-user-tenants"
        _grant(client)
        # A scan of the one table the fixture ever created finds it -- there
        # is no second table for the store to have used instead.
        items = dynamodb_mock.Table(_TABLE).scan().get("Items", [])
        assert any(
            i.get("tenant_id") == TENANT
            and i.get("user_id") == f"ENTITLEMENT#{GRANTABLE_FAMILY}#{GRANTABLE_SCOPE}"
            for i in items
        )


# ---------------------------------------------------------------------------
# Idempotency.
# ---------------------------------------------------------------------------
class TestIdempotency:
    def test_granting_the_same_triple_twice_succeeds_both_times(self, client):
        first = _grant(client)
        second = _grant(client)
        assert first.status_code in (200, 201), first.text
        assert second.status_code in (200, 201), second.text

    def test_revoking_a_grant_that_does_not_exist_succeeds_with_204(self, client):
        """DELETE is 204 with no body regardless of prior existence --
        'returning the resulting grant' after removing it would be
        incoherent, and 204 is what an idempotent delete says."""
        r = _revoke(client)
        assert r.status_code == 204, r.text
        assert r.content == b"", f"DELETE must return no body, got {r.content!r}"

    def test_revoke_after_grant_removes_the_item_and_returns_204(self, client, dynamodb_mock):
        _grant(client)
        assert _raw_grant_item(dynamodb_mock) is not None
        r = _revoke(client)
        assert r.status_code == 204, r.text
        assert r.content == b""
        assert _raw_grant_item(dynamodb_mock) is None

    def test_revoke_is_itself_idempotent(self, client):
        _grant(client)
        first = _revoke(client)
        second = _revoke(client)
        assert first.status_code == 204, first.text
        assert second.status_code == 204, second.text


# ---------------------------------------------------------------------------
# Rejection rules -- non-vacuous because the SAME fixture registry has both a
# grantable (entitlement_required) entry and a general one at valid
# (family, scope) pairs, plus a pair matching no entry at all.
# ---------------------------------------------------------------------------
class TestRejectionRules:
    def test_grant_for_an_entitlement_required_entry_succeeds(self, client):
        assert _grant(client, GRANTABLE_FAMILY, GRANTABLE_SCOPE).status_code in (200, 201)

    def test_grant_for_a_family_scope_pair_with_no_registry_entry_is_rejected(self, client, dynamodb_mock):
        r = _grant(client, "no-such-family", "us")
        assert r.status_code == 400, r.text
        assert "no-such-family" in r.text
        assert _raw_grant_item(dynamodb_mock, "no-such-family", "us") is None

    def test_grant_for_a_known_family_at_an_ungranted_scope_is_rejected(self, client, dynamodb_mock):
        """The pair itself must match a real entry -- a real family at a
        scope IT does not have (rather than a wholly unknown family) is the
        sharper form of the same rule."""
        r = _grant(client, GRANTABLE_FAMILY, "jp")
        assert r.status_code == 400, r.text
        assert _raw_grant_item(dynamodb_mock, GRANTABLE_FAMILY, "jp") is None

    def test_grant_on_a_general_entry_is_rejected_as_meaningless(self, client, dynamodb_mock):
        r = _grant(client, GENERAL_FAMILY, GENERAL_SCOPE)
        assert r.status_code == 400, r.text
        assert _raw_grant_item(dynamodb_mock, GENERAL_FAMILY, GENERAL_SCOPE) is None

    def test_grant_for_the_second_scope_of_the_same_family_also_succeeds(self, client):
        """Two grants, same family, different (grantable) scopes -- proves
        the rejection above discriminates on the (family, scope) PAIR's own
        access level, not on the family alone."""
        assert _grant(client, GRANTABLE_FAMILY, GRANTABLE_SCOPE).status_code in (200, 201)
        assert _grant(client, GRANTABLE_FAMILY, GRANTABLE_SCOPE_2).status_code in (200, 201)


# ---------------------------------------------------------------------------
# Read: fail-closed 503 when the store is unreachable (not 500, not a
# silent empty list).
# ---------------------------------------------------------------------------
class TestReadFailsClosed:
    def test_list_after_a_successful_grant_shows_it(self, client):
        _grant(client)
        r = _list(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "grants" in body, (
            f"list response must be a named-key object {{'grants': [...]}}, "
            f"not a bare list -- got {body!r}"
        )
        grants = body["grants"]
        found = any(
            g.get("model_family") == GRANTABLE_FAMILY and g.get("profile_scope") == GRANTABLE_SCOPE
            for g in grants
        )
        assert found, f"granted triple not present in list response: {body!r}"

    def test_list_with_no_grants_still_returns_the_named_key(self, client):
        """Non-vacuity for the named-key assertion above: an EMPTY list must
        still be wrapped in `{"grants": []}`, not e.g. a bare `[]` that only
        happens to look right once items are tolerated loosely."""
        r = _list(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("grants") == [], f"expected an empty grants list, got {body!r}"

    def test_read_503_when_the_table_is_unreachable(self, client, dynamodb_mock):
        from mvp.routing.config import _TABLE
        _grant(client)
        # Simulate "the entitlement store is unreachable" at the boto3/moto
        # boundary -- independent of which Python module holds the table
        # reference, unlike patching a specific imported name.
        dynamodb_mock.Table(_TABLE).delete()
        r = _list(client)
        assert r.status_code == 503, (
            f"a read failure on the entitlement store must be a retryable "
            f"503 (the caller may already be entitled -- a 403 tells them "
            f"to request access they hold), got {r.status_code}: {r.text!r}"
        )
        assert r.status_code != 200, "must not silently answer with an empty grant list"


# ---------------------------------------------------------------------------
# C4's own reason the store exists: routing config and grants must never
# touch each other's item through an unrelated write.
# ---------------------------------------------------------------------------
class TestSeparationFromRoutingConfig:
    def test_granting_does_not_touch_the_routing_config_item(self, client, dynamodb_mock):
        from mvp.routing.config import _TABLE
        _grant(client)
        resp = dynamodb_mock.Table(_TABLE).get_item(
            Key={"user_id": "CONFIG#ROUTING", "tenant_id": TENANT})
        assert resp.get("Item") is None, (
            "a grant write must not create/touch the CONFIG#ROUTING item -- "
            f"found {resp.get('Item')!r}"
        )

    def test_revoking_does_not_touch_the_routing_config_item(self, client, dynamodb_mock):
        from mvp.routing.config import _TABLE
        _grant(client)
        r = _revoke(client)
        assert r.status_code == 204, r.text
        resp = dynamodb_mock.Table(_TABLE).get_item(
            Key={"user_id": "CONFIG#ROUTING", "tenant_id": TENANT})
        assert resp.get("Item") is None


# ---------------------------------------------------------------------------
# C11 -- audit events.
# ---------------------------------------------------------------------------
class TestAudit:
    def test_grant_calls_log_audit_event_after_the_write_commits(self, client, dynamodb_mock, monkeypatch):
        calls = []

        def _capture(**kw):
            # The item must already be committed by the time the audit call
            # happens -- "emits an audit event AFTER the write commits".
            calls.append(kw)
            assert _raw_grant_item(dynamodb_mock) is not None, (
                "log_audit_event was called before the grant item was "
                "actually written"
            )

        monkeypatch.setattr("mvp.admin_entitlements.log_audit_event", _capture)
        r = _grant(client)
        assert r.status_code in (200, 201), r.text
        assert len(calls) == 1, f"expected exactly one audit call, got {calls!r}"
        call = calls[0]
        assert call.get("tenant_id") == TENANT
        assert call.get("actor_id") == "admin-1"
        # The entry triple's kwargs, pinned exactly rather than searched for
        # anywhere in the call -- a triple hidden in a key a reader would
        # never look at must not pass.
        assert call.get("target_type") == "entitlement", call
        assert call.get("target_id") == f"{GRANTABLE_FAMILY}#{GRANTABLE_SCOPE}", call

    def test_revoke_calls_log_audit_event_with_before_after(self, client, monkeypatch):
        _grant(client)
        calls = []
        monkeypatch.setattr("mvp.admin_entitlements.log_audit_event",
                             lambda **kw: calls.append(kw))
        r = _revoke(client)
        assert r.status_code == 204, r.text
        assert len(calls) == 1
        call = calls[0]
        assert call.get("tenant_id") == TENANT
        assert call.get("target_type") == "entitlement", call
        assert call.get("target_id") == f"{GRANTABLE_FAMILY}#{GRANTABLE_SCOPE}", call
        # before/after reflect the transition: something existed, then did not.
        assert call.get("before") not in (None, {}), (
            f"revoke's audit call before= does not reflect a prior grant: {call!r}"
        )
        assert call.get("after") in (None, {}), (
            f"revoke's audit call after= must reflect 'no grant', got {call.get('after')!r}"
        )

    def test_grant_audit_before_is_empty_and_after_reflects_the_grant(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr("mvp.admin_entitlements.log_audit_event",
                             lambda **kw: calls.append(kw))
        _grant(client)
        assert len(calls) == 1
        call = calls[0]
        assert call.get("before") in (None, {}), (
            f"a first-time grant's audit before= must reflect 'no prior "
            f"grant', got {call.get('before')!r}"
        )
        assert call.get("after") not in (None, {}), (
            f"a grant's audit after= must reflect the new grant, got "
            f"{call.get('after')!r}"
        )

    def test_audit_failure_does_not_roll_back_the_grant(self, client, dynamodb_mock, monkeypatch):
        """'A failed audit write does not roll back the grant, and says so.'
        The grant committed, so the response must not claim the operation
        failed (a 5xx here would be the same lie the settle path refuses --
        a response that contradicts the state it produced). It must also not
        be a silent 200 as if nothing went wrong: the drop is reported via
        the `x-sc-audit-dropped` response header, the same pattern
        `mvp/task_tag.py` already uses for `x-sc-task-tag-dropped`
        ("informational, never a status -- but the drop is no longer silent
        to a caller that reads it")."""
        def _boom(**kw):
            raise RuntimeError("audit sink unavailable")

        monkeypatch.setattr("mvp.admin_entitlements.log_audit_event", _boom)
        r = _grant(client)
        assert _raw_grant_item(dynamodb_mock) is not None, (
            "the grant must stand even though the audit write failed"
        )
        assert r.status_code == 200, (
            f"the grant committed -- the response must say so (200), not "
            f"report a failure that did not happen: got {r.status_code}"
        )
        header = r.headers.get("x-sc-audit-dropped")
        assert header, (
            "a committed grant whose audit write failed must carry "
            f"x-sc-audit-dropped naming why -- headers were {dict(r.headers)!r}"
        )

    def test_audit_dropped_header_is_absent_when_the_audit_write_succeeds(self, client):
        """Non-vacuous companion to the test above: an implementation that
        ALWAYS sets `x-sc-audit-dropped` (regardless of whether the audit
        write actually failed) would pass a presence-only check. The header
        must be absent on the ordinary, successful path."""
        r = _grant(client)
        assert r.status_code in (200, 201), r.text
        assert "x-sc-audit-dropped" not in r.headers, (
            f"x-sc-audit-dropped must be absent when the audit write "
            f"succeeded, got headers {dict(r.headers)!r}"
        )

    def test_revoke_audit_failure_does_not_roll_back_the_revoke(self, client, dynamodb_mock, monkeypatch):
        """Same shape as the grant case, for revoke: the row is gone (the
        revoke committed), the status stays the success code for revoke
        (204), and the drop is reported via the header instead."""
        _grant(client)

        def _boom(**kw):
            raise RuntimeError("audit sink unavailable")

        monkeypatch.setattr("mvp.admin_entitlements.log_audit_event", _boom)
        r = _revoke(client)
        assert _raw_grant_item(dynamodb_mock) is None, (
            "the revoke must stand even though the audit write failed"
        )
        assert r.status_code == 204, (
            f"the revoke committed -- the response must say so (204), not "
            f"report a failure that did not happen: got {r.status_code}"
        )
        header = r.headers.get("x-sc-audit-dropped")
        assert header, (
            "a committed revoke whose audit write failed must carry "
            f"x-sc-audit-dropped naming why -- headers were {dict(r.headers)!r}"
        )

    def test_audit_dropped_header_is_absent_on_a_successful_revoke(self, client):
        _grant(client)
        r = _revoke(client)
        assert r.status_code == 204, r.text
        assert "x-sc-audit-dropped" not in r.headers, (
            f"x-sc-audit-dropped must be absent when the audit write "
            f"succeeded, got headers {dict(r.headers)!r}"
        )

    def test_audit_uses_the_real_log_audit_event_and_masks_the_actor_email(self, client, caplog):
        """Ties the grant path to the ACTUAL production audit sink (not a
        mock) and to its documented behaviour ('replaces actor_email with a
        pii: hash') -- the handoff's own heads-up for this PR's author."""
        import logging
        caplog.set_level(logging.INFO, logger="stratoclave.audit")
        r = _grant(client)
        assert r.status_code in (200, 201), r.text
        audit_lines = [rec.message for rec in caplog.records
                       if rec.name == "stratoclave.audit"]
        assert audit_lines, "no record emitted on the real 'stratoclave.audit' logger"
        joined = " ".join(audit_lines)
        assert "admin@example.com" not in joined, (
            "the actor's raw email address must not appear in the audit log"
        )
        assert TENANT in joined


# ---------------------------------------------------------------------------
# Permission gate -- exercised with the REAL evaluator (permissions.json +
# PermissionsRepository), not the bypass used above.
# ---------------------------------------------------------------------------
class TestPermissionGate:
    def _client_as(self, dynamodb_mock, registry, roles):
        from pathlib import Path

        dynamodb_mock.create_table(
            TableName="stratoclave-permissions",
            KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        from dynamo.permissions import PermissionsRepository
        PermissionsRepository().seed_from_file(
            Path(__file__).resolve().parent.parent / "permissions.json")
        import mvp.authz as authz
        authz._clear_permissions_cache()

        from dynamo.tenants import TenantsRepository
        TenantsRepository().create(tenant_id=TENANT, team_lead_user_id="admin-1",
                                   name="Acme Ent", created_by="admin-1")

        from mvp.admin_entitlements import router as entitlements_router
        app = FastAPI()
        app.include_router(entitlements_router)
        app.dependency_overrides[get_current_user] = lambda: _AdminUser(roles=roles)
        return TestClient(app, raise_server_exceptions=False)

    def test_end_user_role_cannot_grant(self, dynamodb_mock, registry):
        client = self._client_as(dynamodb_mock, registry, ["user"])
        r = _grant(client)
        assert r.status_code == 403, r.text

    def test_end_user_role_cannot_read(self, dynamodb_mock, registry):
        client = self._client_as(dynamodb_mock, registry, ["user"])
        r = _list(client)
        assert r.status_code == 403, r.text
