"""Tests for the model-registry additions (Opus 5 / Fable 5 / GPT-5.6 Sol+Terra /
Grok 4.6 / Gemma 4) and the token-limit clear/set/unlimited operations for Admin
and Team Lead.

Covers:
  * registry resolution + pricing-key coverage for every new model,
  * SetCreditRequest / SetMemberCreditRequest "exactly one action" validators,
  * the Team-Lead `PATCH /members/credit` endpoint authorization + effect
    (owner-only, own-tenant-only, plain-user-only, reset/set/unlimited).
"""
from __future__ import annotations

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dynamo.user_tenants import UNLIMITED_CREDIT


# ---------------------------------------------------------------------------
# 1. Model registry + pricing coverage
# ---------------------------------------------------------------------------
NEW_MODELS = {
    "claude-opus-5": ("us.anthropic.claude-opus-5", "us-east-1", "messages", "opus", "anthropic"),
    "claude-fable-5": ("us.anthropic.claude-fable-5", "us-east-1", "messages", "fable", "anthropic"),
    "gpt-5.6-sol": ("openai.gpt-5.6-sol", "us-east-2", "responses", "gpt-5.6-sol", "openai"),
    "gpt-5.6-terra": ("openai.gpt-5.6-terra", "us-east-2", "responses", "gpt-5.6-terra", "openai"),
    "grok-4.6": ("xai.grok-4.6", "us-west-2", "responses", "grok", "xai"),
    "gemma-4": ("google.gemma-4-31b", "us-east-2", "responses", "gemma", "google"),
}


@pytest.mark.parametrize("alias,expected", NEW_MODELS.items())
def test_new_models_resolve(alias, expected):
    from mvp.models import resolve_model

    entry = resolve_model(alias)
    model_id, region, wire, pk, provider = expected
    assert entry.bedrock_model_id == model_id
    assert entry.bedrock_region == region
    assert entry.wire_protocol == wire
    assert entry.pricing_key == pk
    assert entry.provider == provider


def test_bedrock_ids_roundtrip():
    from mvp.models import resolve_model

    for _, (model_id, *_rest) in NEW_MODELS.items():
        assert resolve_model(model_id).bedrock_model_id == model_id


def test_every_new_pricing_key_has_a_rate():
    """A registry entry whose pricing_key is missing from _DEFAULT_RATES would
    silently fall back to the Opus-priced 'default' — assert each new key is
    explicitly present so billing is intentional, not accidental."""
    from mvp.pricing import _DEFAULT_RATES

    for key in {"fable", "gpt-5.6-sol", "gpt-5.6-terra", "grok", "gemma"}:
        assert key in _DEFAULT_RATES, f"missing pricing rate for {key!r}"


def test_fable_priced_above_opus():
    from mvp.pricing import _DEFAULT_RATES

    assert _DEFAULT_RATES["fable"].input_per_mtok_microusd > _DEFAULT_RATES["opus"].input_per_mtok_microusd
    assert _DEFAULT_RATES["fable"].output_per_mtok_microusd > _DEFAULT_RATES["opus"].output_per_mtok_microusd


def test_gpt56_terra_cheaper_than_sol():
    from mvp.pricing import _DEFAULT_RATES

    assert _DEFAULT_RATES["gpt-5.6-terra"].output_per_mtok_microusd < _DEFAULT_RATES["gpt-5.6-sol"].output_per_mtok_microusd


def test_bogus_model_still_rejected():
    from mvp.models import resolve_model

    with pytest.raises(ValueError):
        resolve_model("no-such-model-xyz")


# ---------------------------------------------------------------------------
# 2. Request validators (pure, no DB)
# ---------------------------------------------------------------------------
def test_admin_set_credit_request_validators():
    from mvp.admin_users import SetCreditRequest

    # valid single actions
    assert SetCreditRequest(total_credit=1000).total_credit == 1000
    assert SetCreditRequest(unlimited=True).unlimited is True
    assert SetCreditRequest(reset_used=True).reset_used is True
    # both cap sources -> error
    with pytest.raises(ValidationError):
        SetCreditRequest(total_credit=1000, unlimited=True)
    # no action -> error
    with pytest.raises(ValidationError):
        SetCreditRequest()


def test_team_lead_set_member_credit_request_validators():
    from mvp.team_lead import SetMemberCreditRequest

    assert SetMemberCreditRequest(email="a@b.co", total_credit=1000).total_credit == 1000
    assert SetMemberCreditRequest(email="a@b.co", unlimited=True).unlimited is True
    assert SetMemberCreditRequest(email="a@b.co", reset_used=True).reset_used is True
    with pytest.raises(ValidationError):
        SetMemberCreditRequest(email="a@b.co", total_credit=1000, unlimited=True)
    with pytest.raises(ValidationError):
        SetMemberCreditRequest(email="a@b.co")  # no action


# ---------------------------------------------------------------------------
# 3. Team-Lead PATCH /members/credit — authorization + effect
# ---------------------------------------------------------------------------
OWNED_TENANT = "t-owned"
OTHER_TENANT = "t-other"


def _create_users_table():
    """conftest.dynamodb_mock does not create the Users table (+email-index);
    the Team-Lead endpoint resolves a member by email, so create it here."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="stratoclave-users",
        KeySchema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "email", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "email-index",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def tl_client(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz

    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: True)

    _create_users_table()

    from dynamo import UsersRepository, UserTenantsRepository
    from dynamo.tenants import TenantsRepository

    tenants = TenantsRepository()
    users = UsersRepository()
    uts = UserTenantsRepository()

    # Owned tenant (owner = tl-1) and a foreign tenant (owner = tl-2).
    tenants.create(tenant_id=OWNED_TENANT, team_lead_user_id="tl-1", name="Owned", created_by="tl-1")
    tenants.create(tenant_id=OTHER_TENANT, team_lead_user_id="tl-2", name="Other", created_by="tl-2")

    # A plain user in the owned tenant with used=400 of 1000.
    users.put_user(user_id="u-user", email="user@ex.co", auth_provider="cognito",
                   auth_provider_user_id="c-user", org_id=OWNED_TENANT, roles=["user"])
    uts.ensure(user_id="u-user", tenant_id=OWNED_TENANT, role="user", total_credit=1000)
    uts.reserve(user_id="u-user", tenant_id=OWNED_TENANT, tokens=400)

    # A team_lead-role member in the owned tenant (privileged target).
    users.put_user(user_id="tl-1", email="lead@ex.co", auth_provider="cognito",
                   auth_provider_user_id="c-lead", org_id=OWNED_TENANT, roles=["team_lead"])
    uts.ensure(user_id="tl-1", tenant_id=OWNED_TENANT, role="team_lead", total_credit=1000)

    # A user who belongs to the OTHER tenant only.
    users.put_user(user_id="u-other", email="other@ex.co", auth_provider="cognito",
                   auth_provider_user_id="c-other", org_id=OTHER_TENANT, roles=["user"])
    uts.ensure(user_id="u-other", tenant_id=OTHER_TENANT, role="user", total_credit=1000)

    # A GLOBAL admin who is (mis)assigned into the owned tenant with membership
    # role="user" — the global-role guard must still refuse a Team Lead here.
    users.put_user(user_id="u-ga", email="ga@ex.co", auth_provider="cognito",
                   auth_provider_user_id="c-ga", org_id=OWNED_TENANT, roles=["admin"])
    uts.ensure(user_id="u-ga", tenant_id=OWNED_TENANT, role="user", total_credit=1000)

    from mvp.deps import get_current_user
    from mvp.team_lead import router as tl_router

    class _Actor:
        user_id = "tl-1"
        org_id = OWNED_TENANT
        email = "lead@ex.co"
        roles = ["team_lead"]
        auth_kind = "jwt"
        key_scopes = None

    app = FastAPI()
    app.include_router(tl_router)
    app.dependency_overrides[get_current_user] = lambda: _Actor()
    return TestClient(app)


def _patch(client, tenant, body):
    return client.patch(f"/api/mvp/team-lead/tenants/{tenant}/members/credit", json=body)


def test_owner_can_reset_used_keeping_cap(tl_client):
    r = _patch(tl_client, OWNED_TENANT, {"email": "user@ex.co", "reset_used": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credit_used"] == 0
    assert body["total_credit"] == 1000  # cap unchanged


def test_owner_can_set_cap(tl_client):
    r = _patch(tl_client, OWNED_TENANT, {"email": "user@ex.co", "total_credit": 5000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_credit"] == 5000
    assert body["credit_used"] == 400  # setting the cap does not reset usage
    assert body["unlimited"] is False


def test_owner_can_set_unlimited(tl_client):
    r = _patch(tl_client, OWNED_TENANT, {"email": "user@ex.co", "unlimited": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_credit"] == UNLIMITED_CREDIT
    assert body["credit_used"] == 400  # unlimited raises the cap, does not reset usage
    assert body["unlimited"] is True  # rendered as a flag, not the raw sentinel


def test_global_admin_target_403(tl_client):
    # ga@ex.co has membership role "user" but is a GLOBAL admin -> off-limits.
    r = _patch(tl_client, OWNED_TENANT, {"email": "ga@ex.co", "reset_used": True})
    assert r.status_code == 403


def test_non_owner_tenant_404(tl_client):
    # tl-1 does not own OTHER_TENANT -> unified 404 from _require_owner.
    r = _patch(tl_client, OTHER_TENANT, {"email": "other@ex.co", "reset_used": True})
    assert r.status_code == 404


def test_cross_tenant_member_404(tl_client):
    # other@ex.co is not an active member of the owned tenant -> 404.
    r = _patch(tl_client, OWNED_TENANT, {"email": "other@ex.co", "reset_used": True})
    assert r.status_code == 404


def test_unknown_email_404(tl_client):
    r = _patch(tl_client, OWNED_TENANT, {"email": "ghost@ex.co", "reset_used": True})
    assert r.status_code == 404


def test_privileged_target_403(tl_client):
    # lead@ex.co is a team_lead-role member -> a Team Lead may not touch it.
    r = _patch(tl_client, OWNED_TENANT, {"email": "lead@ex.co", "reset_used": True})
    assert r.status_code == 403


def test_no_action_422(tl_client):
    r = _patch(tl_client, OWNED_TENANT, {"email": "user@ex.co"})
    assert r.status_code == 422


def test_email_case_and_whitespace_normalized(tl_client):
    # "  User@Ex.Co " must still resolve to the member stored as "user@ex.co".
    r = _patch(tl_client, OWNED_TENANT, {"email": "  User@Ex.Co ", "reset_used": True})
    assert r.status_code == 200, r.text
    assert r.json()["credit_used"] == 0


# ---------------------------------------------------------------------------
# 4. Repository-level: partial update, role condition, unlimited enables reserve
# ---------------------------------------------------------------------------
def test_overwrite_credit_reset_only_is_partial(dynamodb_mock):
    """reset_used with total_credit=None must clear ONLY credit_used and leave the
    cap untouched (no read-modify-write => no lost update)."""
    from dynamo import UserTenantsRepository

    uts = UserTenantsRepository()
    uts.ensure(user_id="u1", tenant_id="t1", role="user", total_credit=1000)
    uts.reserve(user_id="u1", tenant_id="t1", tokens=400)
    uts.overwrite_credit(user_id="u1", tenant_id="t1", total_credit=None, reset_used=True)
    s = uts.credit_summary("u1", "t1")
    assert s == {"total_credit": 1000, "credit_used": 0, "remaining_credit": 1000}


def test_overwrite_credit_noop_raises(dynamodb_mock):
    from dynamo import UserTenantsRepository

    uts = UserTenantsRepository()
    uts.ensure(user_id="u1", tenant_id="t1", role="user", total_credit=1000)
    with pytest.raises(ValueError):
        uts.overwrite_credit(user_id="u1", tenant_id="t1", total_credit=None, reset_used=False)


def test_overwrite_credit_require_role_blocks_mismatch(dynamodb_mock):
    from dynamo import UserTenantsRepository
    from dynamo.user_tenants import CreditExhaustedError

    uts = UserTenantsRepository()
    uts.ensure(user_id="tl", tenant_id="t1", role="team_lead", total_credit=1000)
    with pytest.raises(CreditExhaustedError):
        uts.overwrite_credit(
            user_id="tl", tenant_id="t1", total_credit=5000, require_role="user"
        )


def test_unlimited_lets_a_large_reserve_succeed(dynamodb_mock):
    """The whole point of unlimited: a user at their cap can reserve again. The
    reserve path is UNCHANGED — it just never exhausts against the sentinel."""
    from dynamo import UserTenantsRepository

    uts = UserTenantsRepository()
    uts.ensure(user_id="u1", tenant_id="t1", role="user", total_credit=1000)
    uts.reserve(user_id="u1", tenant_id="t1", tokens=1000)  # exhaust
    with pytest.raises(Exception):
        uts.reserve(user_id="u1", tenant_id="t1", tokens=1)  # CreditExhaustedError
    uts.overwrite_credit(user_id="u1", tenant_id="t1", total_credit=UNLIMITED_CREDIT, reset_used=True)
    remaining = uts.reserve(user_id="u1", tenant_id="t1", tokens=5_000_000)  # large
    assert remaining > 0
    assert uts.credit_summary("u1", "t1")["credit_used"] == 5_000_000


# ---------------------------------------------------------------------------
# 5. Registry invariant + pricing conversion + permission wildcard
# ---------------------------------------------------------------------------
def test_messages_protocol_implies_anthropic_provider():
    """The Converse transport is verified only for Anthropic; chains.py filters on
    provider=="anthropic". Lock the invariant so a future non-anthropic Converse
    entry can't silently ship without revisiting that filter."""
    from mvp.models import registry_entries

    for e in registry_entries():
        if e.wire_protocol == "messages":
            assert e.provider == "anthropic", f"{e.bedrock_model_id} is messages but not anthropic"


def test_pricing_conversion_gpt56_sol_exact():
    """Guard against a micro-USD digit slip: 1M input + 100k output at Sol's rate."""
    from mvp.pricing import snapshot_rates, rate_usage

    snap = snapshot_rates("gpt-5.6-sol")
    rec = rate_usage(snap, input_tokens=1_000_000, output_tokens=100_000)
    # input: 1M tok * 4.40 $/MTok = 4_400_000 ; output: 0.1M * 22.00 = 2_200_000
    assert rec.total_cost_microusd == 4_400_000 + 2_200_000


def test_users_star_grants_update_own_tenant_but_update_does_not():
    from mvp.authz import _grants

    assert _grants("users:*", "users:update-own-tenant") is True
    assert _grants("users:update", "users:update-own-tenant") is False
    assert _grants("users:update-own-tenant", "users:update") is False


def test_permissions_json_grants_scope_to_team_lead_not_user():
    """The seed source of truth: team_lead is granted the new scope, plain user is
    not, and admin holds it via the users:* wildcard. (Role→perm resolution at
    runtime reads the seeded DynamoDB Permissions table; here we assert the JSON
    the seed loads, which is deterministic and version-gated.)"""
    import json
    from pathlib import Path

    from mvp.authz import _grants

    perms = json.loads(
        (Path(__file__).resolve().parent.parent / "permissions.json").read_text()
    )["roles"]
    assert "users:update-own-tenant" in perms["team_lead"]["permissions"]
    assert "users:update-own-tenant" not in perms["user"]["permissions"]
    # admin holds users:* which grants it.
    assert any(_grants(p, "users:update-own-tenant") for p in perms["admin"]["permissions"])


def test_responses_alias_resolves_to_distinct_bedrock_id():
    """The Responses transport forwards `entry.bedrock_model_id` (not the client
    alias) to bedrock-mantle. That rewrite is load-bearing precisely because the
    alias and the id DIFFER for grok/gemma — mantle 404s on the bare alias. Lock
    the divergence so the rewrite can't be "simplified" away."""
    from mvp.models import resolve_model

    for alias, expected_id in [
        ("grok-4.6", "xai.grok-4.6"),
        ("gemma-4", "google.gemma-4-31b"),
    ]:
        e = resolve_model(alias)
        assert e.bedrock_model_id == expected_id
        assert e.bedrock_model_id != alias


def test_messages_route_rejects_a_responses_model():
    """grok is a responses-protocol model; the Anthropic Messages route's resolver
    must refuse it (400 upstream), never misroute it to Converse."""
    from mvp.models import resolve_bedrock_model

    with pytest.raises(ValueError):
        resolve_bedrock_model("grok-4.6")
