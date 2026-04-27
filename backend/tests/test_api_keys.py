"""Tests for the long-lived API key repository and scope intersection.

These cover the contract documented in ARCHITECTURE.md § API keys:

  - Plaintext is never stored (only the SHA-256 hash is).
  - key_id shown in lists is a masked prefix/suffix of the plaintext.
  - The hash lookup round-trips exactly and is deterministic.
  - Effective scopes for authorization are `owner.roles ∩ key.scopes`.
  - Revoked keys cannot be found via the active lookup.
  - Active key count excludes revoked and expired entries.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3
import pytest

from dynamo.api_keys import (
    ApiKeysRepository,
    build_key_id,
    generate_plain_key,
    hash_key,
    is_api_key,
)


@pytest.fixture
def api_keys_table(dynamodb_mock):
    """Create the ApiKeys table matching production schema.

    PK: key_hash; GSI: user-id-index on (user_id, created_at).
    """
    dynamodb_mock.create_table(
        TableName="stratoclave-api-keys",
        KeySchema=[{"AttributeName": "key_hash", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "key_hash", "AttributeType": "S"},
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "user-id-index",
                "KeySchema": [
                    {"AttributeName": "user_id", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    yield


def test_plain_key_has_expected_prefix_and_entropy():
    k = generate_plain_key()
    assert k.startswith("sk-stratoclave-")
    # Base62-ish suffix length >= 20 to make brute force implausible.
    suffix = k.removeprefix("sk-stratoclave-")
    assert len(suffix) >= 20


def test_is_api_key_prefix_detection():
    assert is_api_key("sk-stratoclave-abc123") is True
    assert is_api_key("eyJhbGciOi.Xxxxxxx.Xxxx") is False
    assert is_api_key("") is False


def test_hash_is_deterministic_and_key_id_is_masked():
    plain = "sk-stratoclave-ABCDEFGHIJKLMNOPQRSTUVWX"
    assert hash_key(plain) == hash_key(plain)
    kid = build_key_id(plain)
    # The key_id must not reveal the middle bytes of the plaintext.
    assert "..." in kid
    assert kid.startswith("sk-stratoclave-")
    assert plain not in kid  # full plaintext must not survive in the mask


def test_repo_create_stores_only_hash(api_keys_table):
    repo = ApiKeysRepository()
    item, plaintext = repo.create(
        user_id="user-1",
        name="laptop",
        scopes=["messages:send", "usage:read-self"],
        expires_at=None,
        created_by="user-1",
    )
    # The record in DDB only has the hash, not the plaintext.
    assert item["key_hash"] == hash_key(plaintext)
    assert "plaintext" not in item
    assert plaintext.startswith("sk-stratoclave-")


def test_get_by_hash_roundtrip(api_keys_table):
    repo = ApiKeysRepository()
    _, plaintext = repo.create(
        user_id="user-1",
        name="hw",
        scopes=["messages:send"],
        expires_at=None,
        created_by="user-1",
    )
    hit = repo.get_by_hash(hash_key(plaintext))
    assert hit is not None
    assert hit["name"] == "hw"
    assert repo.get_by_hash("0" * 64) is None


def test_revoke_marks_key_inactive(api_keys_table):
    repo = ApiKeysRepository()
    item, plaintext = repo.create(
        user_id="user-1",
        name="to-revoke",
        scopes=["messages:send"],
        expires_at=None,
        created_by="user-1",
    )
    before = repo.count_active("user-1")
    assert before == 1

    repo.revoke(item["key_hash"], actor_user_id="user-1")
    after = repo.count_active("user-1")
    assert after == 0

    # The record still exists but is marked revoked — monitoring contract.
    hit = repo.get_by_hash(item["key_hash"])
    assert hit is not None
    assert hit.get("revoked_at")


def test_count_active_excludes_expired(api_keys_table):
    repo = ApiKeysRepository()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    repo.create(
        user_id="user-1",
        name="expired",
        scopes=["messages:send"],
        expires_at=past.isoformat(),
        created_by="user-1",
    )
    repo.create(
        user_id="user-1",
        name="alive",
        scopes=["messages:send"],
        expires_at=None,
        created_by="user-1",
    )
    assert repo.count_active("user-1") == 1


def test_list_by_user_returns_all_including_revoked(api_keys_table):
    repo = ApiKeysRepository()
    item_a, _ = repo.create(
        user_id="user-1",
        name="a",
        scopes=["messages:send"],
        expires_at=None,
        created_by="user-1",
    )
    repo.create(
        user_id="user-1",
        name="b",
        scopes=["messages:send"],
        expires_at=None,
        created_by="user-1",
    )
    repo.revoke(item_a["key_hash"], actor_user_id="user-1")

    # include_revoked=True returns everything; default returns only active.
    assert {i["name"] for i in repo.list_by_user("user-1", include_revoked=True)} == {
        "a",
        "b",
    }
    assert {i["name"] for i in repo.list_by_user("user-1")} == {"b"}


def _seed_permissions_table(dynamodb_mock):
    """Seed a minimal Permissions table so `has_permission()` finds role data."""
    dynamodb_mock.create_table(
        TableName="stratoclave-permissions",
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    tbl = dynamodb_mock.Table("stratoclave-permissions")
    tbl.put_item(
        Item={
            "role": "user",
            "permissions": ["messages:send", "usage:read-self", "apikeys:*-self"],
            "version": 1,
        }
    )
    tbl.put_item(
        Item={
            "role": "admin",
            "permissions": [
                "users:*",
                "tenants:*",
                "usage:*",
                "messages:send",
                "apikeys:*",
            ],
            "version": 1,
        }
    )


def test_scope_intersection_cannot_exceed_owner_roles(dynamodb_mock):
    """`user_has_permission` must return False when the key's scope is
    not also granted by one of the owner's roles. This is the
    defense-in-depth contract from ADMIN_GUIDE § API keys.
    """
    _seed_permissions_table(dynamodb_mock)
    from mvp.authz import _clear_permissions_cache, user_has_permission
    from mvp.deps import AuthenticatedUser

    _clear_permissions_cache()

    # Owner is merely `user` role.
    owner = AuthenticatedUser(
        user_id="u1",
        email="u1@example.com",
        roles=["user"],
        org_id="default-org",
        auth_kind="api_key",
        key_scopes=["messages:send", "users:create"],  # key claims more
    )

    # messages:send is in both role and scope → allowed.
    assert user_has_permission(owner, "messages:send") is True
    # users:create is in scope but NOT in `user` role → denied
    # (the scope intersection must clip to owner capabilities).
    assert user_has_permission(owner, "users:create") is False


def test_find_by_user_and_key_id_returns_only_owner_rows(api_keys_table):
    """P1-8 regression: the masked key_id resolver must only return rows
    owned by the caller, so a leaked key_id from another user cannot be
    used to revoke or otherwise touch that key.
    """
    repo = ApiKeysRepository()
    item_alice, _ = repo.create(
        user_id="alice",
        name="alice-key",
        scopes=["messages:send"],
        expires_at=None,
        created_by="alice",
    )
    item_bob, _ = repo.create(
        user_id="bob",
        name="bob-key",
        scopes=["messages:send"],
        expires_at=None,
        created_by="bob",
    )

    # Alice's own lookup returns her row.
    assert (
        repo.find_by_user_and_key_id("alice", item_alice["key_id"])["name"]
        == "alice-key"
    )
    # Alice cannot resolve Bob's key_id, even though the string is valid.
    assert repo.find_by_user_and_key_id("alice", item_bob["key_id"]) is None
    # Bogus key_id returns None.
    assert repo.find_by_user_and_key_id("alice", "sk-stratoclave-FAKE") is None


def test_scope_intersection_cannot_exceed_key_scopes(dynamodb_mock):
    """Even if the owner is admin, a narrow key must remain narrow."""
    _seed_permissions_table(dynamodb_mock)
    from mvp.authz import _clear_permissions_cache, user_has_permission
    from mvp.deps import AuthenticatedUser

    _clear_permissions_cache()

    admin_but_narrow = AuthenticatedUser(
        user_id="u1",
        email="u1@example.com",
        roles=["admin"],
        org_id="default-org",
        auth_kind="api_key",
        key_scopes=["messages:send"],  # admin capabilities, but key is narrow
    )

    # messages:send is within the key scope.
    assert user_has_permission(admin_but_narrow, "messages:send") is True
    # Even though admin role includes users:* wildcard, the key's narrow scope
    # blocks this — the intersection floor is the key's declared scopes.
    assert user_has_permission(admin_but_narrow, "users:create") is False
