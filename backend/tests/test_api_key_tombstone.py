"""Z-1 regression (2026-04 third blind review).

Sweep-2 introduced soft-delete tombstones and a
``token_revoked_after`` watermark on the Users row. That protected
the Cognito access_token path in ``get_current_user`` but left
``_authenticate_api_key`` untouched — an attacker's long-lived
``sk-stratoclave-*`` key kept authenticating after the owner was
deleted or had their sessions forcibly revoked. This test suite
locks in the two new checks on the API-key path plus the
``ApiKeysRepository.revoke_all_for_user`` sweep we hook into the
admin delete / switch-tenant sagas.

We stay at the repository level where possible. The deps.py calls
that require JWKS / boto3 are exercised by stubbing the
repositories directly.
"""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException


@pytest.fixture
def tables(dynamodb_mock):
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
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb_mock.create_table(
        TableName="stratoclave-api-keys",
        KeySchema=[{"AttributeName": "key_hash", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "key_hash", "AttributeType": "S"},
            {"AttributeName": "user_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "user-id-index",
                "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    yield


@pytest.fixture
def api_keys_repo(tables):
    from dynamo import ApiKeysRepository
    return ApiKeysRepository()


@pytest.fixture
def users_repo(tables):
    from dynamo import UsersRepository
    return UsersRepository()


def _mint(api_keys_repo, users_repo, user_id="u-1"):
    users_repo.put_user(
        user_id=user_id,
        email=f"{user_id}@example.com",
        auth_provider="cognito",
        auth_provider_user_id=user_id,
        org_id="default-org",
    )
    item, plain = api_keys_repo.create(
        user_id=user_id,
        name="test",
        scopes=["messages:send"],
        expires_at=None,
        created_by=user_id,
    )
    return item, plain


class TestAuthenticateApiKeyRejectsDeletedOwner:
    def test_soft_deleted_owner_fails_auth_with_401(self, api_keys_repo, users_repo):
        from mvp.deps import _authenticate_api_key

        _, plain = _mint(api_keys_repo, users_repo)
        users_repo.mark_deleted("u-1")

        with pytest.raises(HTTPException) as exc:
            _authenticate_api_key(plain)
        assert exc.value.status_code == 401
        assert "deleted" in str(exc.value.detail).lower()

    def test_active_owner_passes(self, api_keys_repo, users_repo):
        from mvp.deps import _authenticate_api_key

        _, plain = _mint(api_keys_repo, users_repo)
        user = _authenticate_api_key(plain)
        assert user.user_id == "u-1"
        assert user.auth_kind == "api_key"


class TestAuthenticateApiKeyRejectsPreWatermark:
    def test_key_created_before_watermark_is_refused(self, api_keys_repo, users_repo):
        from mvp.deps import _authenticate_api_key

        _, plain = _mint(api_keys_repo, users_repo)
        # sleep so the watermark we're about to write is strictly
        # newer than the key's created_at (epoch-second granularity).
        time.sleep(1.1)
        users_repo.revoke_all_sessions("u-1")

        with pytest.raises(HTTPException) as exc:
            _authenticate_api_key(plain)
        assert exc.value.status_code == 401
        assert "predates" in str(exc.value.detail).lower()

    def test_key_created_after_watermark_still_works(self, api_keys_repo, users_repo):
        from mvp.deps import _authenticate_api_key

        users_repo.put_user(
            user_id="u-2",
            email="u2@example.com",
            auth_provider="cognito",
            auth_provider_user_id="u-2",
            org_id="default-org",
        )
        users_repo.revoke_all_sessions("u-2")
        time.sleep(1.1)
        _, plain = api_keys_repo.create(
            user_id="u-2",
            name="fresh",
            scopes=["messages:send"],
            expires_at=None,
            created_by="u-2",
        )
        user = _authenticate_api_key(plain)
        assert user.user_id == "u-2"


class TestRevokeAllForUser:
    def test_sweeps_active_keys_and_leaves_revoked_alone(
        self, api_keys_repo, users_repo
    ):
        _mint(api_keys_repo, users_repo, user_id="sweep-me")
        a2, _ = api_keys_repo.create(
            user_id="sweep-me",
            name="second",
            scopes=["messages:send"],
            expires_at=None,
            created_by="sweep-me",
        )
        # Pre-revoke one of the two to prove the sweep is idempotent
        # and does not re-revoke already-dead rows.
        api_keys_repo.revoke(a2["key_hash"], actor_user_id="admin-x")

        revoked = api_keys_repo.revoke_all_for_user(
            "sweep-me", actor_user_id="admin-x"
        )
        assert revoked == 1  # only the still-active row was transitioned

        rows = api_keys_repo.list_by_user("sweep-me", include_revoked=True)
        assert all(r.get("revoked_at") for r in rows)

    def test_unknown_user_returns_zero(self, api_keys_repo):
        assert api_keys_repo.revoke_all_for_user(
            "no-such-user", actor_user_id="admin-x"
        ) == 0
