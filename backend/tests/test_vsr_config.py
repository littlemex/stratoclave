"""Per-tenant VSR config: opaque-blob store + save-time validation proxy +
admin surface with tenant-scoped authz.

Proves the loose-coupling + blast-radius design:

  * Stratoclave NEVER parses the YAML — it stores opaque bytes and delegates the
    valid/invalid verdict to the pinned VSR's /validate;
  * a broken blob is REJECTED at save (422) and never reaches S3;
  * a VSR that is unreachable at save FAILS THE SAVE LOUDLY (503) — no
    unvalidated blob is stored;
  * size cap (413) is enforced before any network/validate call;
  * a path-traversal / bad tenant id can never escape the vsr-config/ prefix;
  * authz: admin edits any id incl. `default`; a tenant owner edits ONLY their
    own tenant; everyone else gets a unified 404; `default` is admin-only;
  * the feature is inert (404) with the flag off / no bucket.

S3 is moto; the VSR /validate endpoint is an httpx.MockTransport fake.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import boto3
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.deps import get_current_user
from mvp.vsr import config_store as store


# --------------------------------------------------------------------------
# Fixtures: moto S3 bucket, VSR /validate fake, flag/env, app client.
# --------------------------------------------------------------------------

_BUCKET = "test-vsr-config"


@pytest.fixture
def s3_bucket(monkeypatch):
    """A moto S3 bucket + the env the store reads. Requires the outer
    `dynamodb_mock`/moto context via the aws_credentials safety net."""
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        monkeypatch.setenv("VSR_CONFIG_BUCKET", _BUCKET)
        monkeypatch.setenv("EXTERNAL_VSR_ENABLED", "true")
        monkeypatch.setenv("VSR_BASE_URL", "http://vsr:8000")
        store.reset_for_test()
        yield s3
    store.reset_for_test()


def _install_validate(monkeypatch, handler):
    """Point the store's validate client at an httpx.MockTransport `handler`."""
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url="http://vsr:8000")
    monkeypatch.setattr(store, "_get_validate_client", lambda: client)
    return client


def _valid_handler(request):
    return httpx.Response(200, json={"valid": True})


def _reject_handler(request):
    return httpx.Response(422, json={"valid": False, "errors": ["bad field: foo"]})


def _unreachable_handler(request):
    raise httpx.ConnectError("refused")


# --------------------------------------------------------------------------
# Store-level: validation gate.
# --------------------------------------------------------------------------

def test_put_stores_only_after_vsr_validates(s3_bucket, monkeypatch):
    _install_validate(monkeypatch, _valid_handler)
    store.put_config("tenant-a", "routing:\n  decisions: []\n")
    # It is in S3, byte-for-byte, unparsed.
    got = store.get_config("tenant-a")
    assert got is not None
    text, _version_id = got
    assert text == "routing:\n  decisions: []\n"


def test_put_rejected_blob_never_reaches_s3(s3_bucket, monkeypatch):
    _install_validate(monkeypatch, _reject_handler)
    with pytest.raises(store.ConfigRejected) as ei:
        store.put_config("tenant-a", "garbage: [")
    assert "bad field: foo" in str(ei.value.errors)
    # Nothing was written.
    assert store.get_config("tenant-a") is None


def test_put_fails_loudly_when_validator_unreachable(s3_bucket, monkeypatch):
    _install_validate(monkeypatch, _unreachable_handler)
    with pytest.raises(store.ValidatorUnavailable):
        store.put_config("tenant-a", "routing: {}")
    assert store.get_config("tenant-a") is None


def test_put_size_cap_rejected_before_any_io(s3_bucket, monkeypatch):
    # Validator would say valid, but the cap fires first (no network call).
    called = {"n": 0}

    def _counting(request):
        called["n"] += 1
        return httpx.Response(200, json={"valid": True})

    _install_validate(monkeypatch, _counting)
    big = "x" * (store.MAX_BLOB_BYTES + 1)
    with pytest.raises(store.ConfigTooLarge):
        store.put_config("tenant-a", big)
    assert called["n"] == 0  # validator never consulted
    assert store.get_config("tenant-a") is None


@pytest.mark.parametrize("bad", ["../secrets", "a/b", "tenant space", "", "x" * 200])
def test_bad_tenant_id_cannot_escape_prefix(s3_bucket, monkeypatch, bad):
    _install_validate(monkeypatch, _valid_handler)
    with pytest.raises(store.VsrConfigError):
        store.put_config(bad, "routing: {}")


def test_default_is_a_legal_key(s3_bucket, monkeypatch):
    _install_validate(monkeypatch, _valid_handler)
    store.put_config("default", "routing: {}")
    assert store.get_config("default") is not None


def test_delete_reverts_tenant(s3_bucket, monkeypatch):
    _install_validate(monkeypatch, _valid_handler)
    store.put_config("tenant-a", "routing: {}")
    store.delete_config("tenant-a")
    assert store.get_config("tenant-a") is None


def test_missing_object_is_none(s3_bucket, monkeypatch):
    # A genuine NoSuchKey => None (tenant inherits default), not an error.
    assert store.get_config("never-configured") is None


def test_real_s3_error_is_not_swallowed_as_missing(s3_bucket, monkeypatch):
    # A NON-"not found" S3 ClientError (e.g. AccessDenied) must raise, NOT be
    # masked as an empty tenant config — otherwise an IAM/KMS/throttling fault
    # hides behind a "no config" UI state.
    from botocore.exceptions import ClientError

    class _DenyingS3:
        def get_object(self, **kw):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"},
                 "ResponseMetadata": {"HTTPStatusCode": 403}},
                "GetObject",
            )

    monkeypatch.setattr(store, "_get_s3", lambda: _DenyingS3())
    with pytest.raises(store.VsrConfigError):
        store.get_config("tenant-a")


# --------------------------------------------------------------------------
# Admin surface + tenant-scoped authz.
# --------------------------------------------------------------------------

@dataclass
class _User:
    user_id: str = "admin-1"
    org_id: str = "ops"
    email: str = "a@example.com"
    roles: list = field(default_factory=lambda: ["admin"])
    auth_kind: str = "jwt"
    key_scopes: list = None


class _FakeTenants:
    """Stand-in TenantsRepository: tenant-a is owned by lead-1."""

    def __init__(self, tenants):
        self._t = tenants

    def get(self, tid):
        return self._t.get(tid)


@pytest.fixture
def app_client(s3_bucket, monkeypatch):
    from mvp import admin_vsr_config as mod

    tenants = {"tenant-a": {"tenant_id": "tenant-a", "team_lead_user_id": "lead-1"}}
    monkeypatch.setattr(mod, "TenantsRepository", lambda: _FakeTenants(tenants))
    _install_validate(monkeypatch, _valid_handler)

    app = FastAPI()
    app.include_router(mod.router)

    def _as(user):
        app.dependency_overrides[get_current_user] = lambda: user

    return TestClient(app), _as


def test_admin_can_put_get_delete_any_tenant(app_client):
    client, _as = app_client
    _as(_User(roles=["admin"]))
    r = client.put("/api/mvp/admin/tenants/tenant-a/vsr-config",
                   content="routing: {}", headers={"Content-Type": "application/yaml"})
    assert r.status_code == 200, r.text
    r = client.get("/api/mvp/admin/tenants/tenant-a/vsr-config")
    assert r.status_code == 200
    assert r.text == "routing: {}"
    r = client.delete("/api/mvp/admin/tenants/tenant-a/vsr-config")
    assert r.status_code == 200


def test_admin_can_edit_default(app_client):
    client, _as = app_client
    _as(_User(roles=["admin"]))
    r = client.put("/api/mvp/admin/tenants/default/vsr-config", content="routing: {}")
    assert r.status_code == 200


def test_tenant_owner_edits_only_own_tenant(app_client):
    client, _as = app_client
    _as(_User(user_id="lead-1", roles=["team_lead"]))
    # Own tenant: OK.
    r = client.put("/api/mvp/admin/tenants/tenant-a/vsr-config", content="routing: {}")
    assert r.status_code == 200
    # A tenant they do not own: unified 404.
    r = client.get("/api/mvp/admin/tenants/tenant-b/vsr-config")
    assert r.status_code == 404


def test_non_owner_lead_cannot_touch_default(app_client):
    client, _as = app_client
    _as(_User(user_id="lead-1", roles=["team_lead"]))
    r = client.put("/api/mvp/admin/tenants/default/vsr-config", content="routing: {}")
    assert r.status_code == 404


def test_rejected_config_surfaces_422(app_client, monkeypatch):
    client, _as = app_client
    from mvp import admin_vsr_config as mod
    _install_validate(monkeypatch, _reject_handler)
    _as(_User(roles=["admin"]))
    r = client.put("/api/mvp/admin/tenants/tenant-a/vsr-config", content="bad: [")
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "vsr_rejected"


def test_validator_down_surfaces_503(app_client, monkeypatch):
    client, _as = app_client
    _install_validate(monkeypatch, _unreachable_handler)
    _as(_User(roles=["admin"]))
    r = client.put("/api/mvp/admin/tenants/tenant-a/vsr-config", content="routing: {}")
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "vsr_validate_unavailable"
    # The 503 must NOT leak the VSR internal URL/host (static message only).
    assert r.json()["detail"]["message"] == "vsr validator unreachable"


def test_invalid_utf8_body_rejected_400(app_client):
    client, _as = app_client
    _as(_User(roles=["admin"]))
    # An invalid UTF-8 byte sequence must be rejected, not silently mangled.
    r = client.put("/api/mvp/admin/tenants/tenant-a/vsr-config", content=b"\xff\xfe bad")
    assert r.status_code == 400
    assert "UTF-8" in r.json()["detail"]


def test_garbage_tenant_id_never_reaches_dynamo(app_client, monkeypatch):
    client, _as = app_client
    from mvp import admin_vsr_config as mod

    # If the id shape-guard works, TenantsRepository is never consulted for a
    # non-conforming id — it 404s first.
    def _boom():
        raise AssertionError("DynamoDB reached with a garbage id")

    monkeypatch.setattr(mod, "TenantsRepository", lambda: type("T", (), {"get": staticmethod(lambda tid: _boom())})())
    _as(_User(user_id="lead-1", roles=["team_lead"]))
    r = client.get("/api/mvp/admin/tenants/" + ("x" * 200) + "/vsr-config")
    assert r.status_code == 404


def test_validate_endpoint_is_dry_run(app_client):
    client, _as = app_client
    _as(_User(roles=["admin"]))
    r = client.post("/api/mvp/admin/tenants/tenant-a/vsr-config/validate",
                    content="routing: {}")
    assert r.status_code == 200
    assert r.json() == {"valid": True}
    # Dry run: nothing stored.
    r = client.get("/api/mvp/admin/tenants/tenant-a/vsr-config")
    assert r.status_code == 404


def test_surface_404_when_flag_off(app_client, monkeypatch):
    client, _as = app_client
    monkeypatch.setenv("EXTERNAL_VSR_ENABLED", "false")
    _as(_User(roles=["admin"]))
    r = client.get("/api/mvp/admin/tenants/tenant-a/vsr-config")
    assert r.status_code == 404
    r = client.put("/api/mvp/admin/tenants/tenant-a/vsr-config", content="routing: {}")
    assert r.status_code == 404
