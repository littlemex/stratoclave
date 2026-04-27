"""Tenants and UserTenants isolation contract tests.

Guards the invariants ARCHITECTURE.md documents for tenant-scoped access:

  - `list_by_owner(user_id)` returns only tenants owned by that user.
  - Archived tenants are filtered out of standard reads (but remain in
    `_including_archived` for audit access).
  - `switch_tenant()` atomically archives the old UserTenants row and
    creates the new one, making `reserve()` against the old tenant fail.
  - Default tenant seeding is idempotent.
"""
from __future__ import annotations

import pytest

from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import CreditExhaustedError, UserTenantsRepository


def _make_users_table(dynamodb_mock):
    dynamodb_mock.create_table(
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


def test_list_by_owner_returns_only_that_owner(dynamodb_mock):
    tenants = TenantsRepository()
    # Seed two tenants owned by different team_leads.
    tenants.create(
        tenant_id="t-alpha",
        name="Alpha",
        team_lead_user_id="lead-1",
        default_credit=100_000,
        created_by="admin-1",
    )
    tenants.create(
        tenant_id="t-beta",
        name="Beta",
        team_lead_user_id="lead-2",
        default_credit=100_000,
        created_by="admin-1",
    )

    owned_by_lead1 = tenants.list_by_owner("lead-1")
    owned_by_lead2 = tenants.list_by_owner("lead-2")

    assert {t["tenant_id"] for t in owned_by_lead1} == {"t-alpha"}
    assert {t["tenant_id"] for t in owned_by_lead2} == {"t-beta"}


def test_archived_tenant_is_hidden_from_default_get(dynamodb_mock):
    tenants = TenantsRepository()
    tenants.create(
        tenant_id="t-archive-me",
        name="ToArchive",
        team_lead_user_id="lead-1",
        default_credit=100_000,
        created_by="admin-1",
    )
    assert tenants.get("t-archive-me") is not None

    tenants.archive("t-archive-me")

    assert tenants.get("t-archive-me") is None
    assert tenants.get_including_archived("t-archive-me") is not None


def test_list_by_owner_excludes_archived(dynamodb_mock):
    tenants = TenantsRepository()
    tenants.create(
        tenant_id="t-live",
        name="Live",
        team_lead_user_id="lead-1",
        default_credit=100_000,
        created_by="admin-1",
    )
    tenants.create(
        tenant_id="t-gone",
        name="Gone",
        team_lead_user_id="lead-1",
        default_credit=100_000,
        created_by="admin-1",
    )
    tenants.archive("t-gone")

    remaining = tenants.list_by_owner("lead-1")
    assert {t["tenant_id"] for t in remaining} == {"t-live"}


def test_seed_default_is_idempotent(dynamodb_mock):
    tenants = TenantsRepository()
    first = tenants.seed_default(
        tenant_id="default-org", name="Default Organization", default_credit=100_000
    )
    second = tenants.seed_default(
        tenant_id="default-org", name="Default Organization", default_credit=100_000
    )
    # The tenant exists after both calls and the payload is stable.
    assert first["tenant_id"] == "default-org"
    assert tenants.get("default-org") is not None
    # The second call is a no-op on an existing row (created=False).
    assert second["created"] is False


def test_switch_tenant_archives_old_ut_and_blocks_reserve(dynamodb_mock):
    """After switch_tenant(), the old UserTenants row is archived and
    reserve() against it must raise — there is no more budget there.
    """
    _make_users_table(dynamodb_mock)
    tenants = TenantsRepository()
    tenants.create(
        tenant_id="t-old",
        name="Old",
        team_lead_user_id="lead-old",
        default_credit=100_000,
        created_by="admin-1",
    )
    tenants.create(
        tenant_id="t-new",
        name="New",
        team_lead_user_id="lead-new",
        default_credit=100_000,
        created_by="admin-1",
    )

    ut = UserTenantsRepository()
    ut.ensure(user_id="wanderer", tenant_id="t-old", role="user", total_credit=1_000)
    assert ut.remaining_credit("wanderer", "t-old") == 1_000

    # Seed the user in the Users table so switch_tenant can update org_id.
    from dynamo.users import UsersRepository

    UsersRepository().put_user(
        user_id="wanderer",
        email="w@example.com",
        auth_provider="cognito",
        auth_provider_user_id="wanderer",
        org_id="t-old",
        roles=["user"],
    )

    ut.switch_tenant(
        user_id="wanderer",
        old_tenant_id="t-old",
        new_tenant_id="t-new",
    )

    # Old tenant row is gone from the active view.
    assert ut.get("wanderer", "t-old") is None
    # And reserving against it fails — the atomic guard blocks leakage.
    with pytest.raises(CreditExhaustedError):
        ut.reserve(user_id="wanderer", tenant_id="t-old", tokens=10)

    # New tenant is active with its own budget.
    assert ut.get("wanderer", "t-new") is not None
