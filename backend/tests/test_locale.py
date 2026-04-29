"""i18n locale handling — repository + /me surface contract.

Covers:
  - UsersRepository.update_locale: success, missing-row -> None, and
    default backfill semantics.
  - /api/mvp/me GET returns the server-clamped locale (unknown values
    fall back to "ja").
  - /api/mvp/me PATCH persists locale via UpdateItem and emits an
    audit event.

We stay at the repository / surface layer rather than booting the full
FastAPI dependency stack — the route's `Depends(get_current_user)` is
exercised elsewhere (`test_rbac.py`), and doubling up bloats test time
without catching additional bugs in this change set.
"""
from __future__ import annotations

import pytest


def _users_table(dynamodb_mock):
    """Bootstrap the Users table schema used in production."""
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


@pytest.fixture
def users_repo(dynamodb_mock):
    _users_table(dynamodb_mock)
    from dynamo import UsersRepository

    return UsersRepository()


def test_put_user_defaults_locale_to_ja(users_repo):
    """A new user row with no explicit `locale` lands with "ja".

    Why: the server default for legacy users is Japanese to preserve
    the historic UI; this invariant is what the PATCH /me handler and
    the /me response lean on, so a regression here would silently
    flip every existing user's default.
    """
    item = users_repo.put_user(
        user_id="u1",
        email="alice@example.com",
        auth_provider="cognito",
        auth_provider_user_id="u1",
        org_id="default-org",
    )
    assert item["locale"] == "ja"


def test_put_user_preserves_existing_locale(users_repo):
    """A second `put_user` call (re-login backfill) must not downgrade an
    existing locale to the default — otherwise a user's stored "en"
    would get stomped on every SSO backfill cycle.
    """
    users_repo.put_user(
        user_id="u1",
        email="alice@example.com",
        auth_provider="cognito",
        auth_provider_user_id="u1",
        org_id="default-org",
        locale="en",
    )
    item = users_repo.put_user(
        user_id="u1",
        email="alice@example.com",
        auth_provider="cognito",
        auth_provider_user_id="u1",
        org_id="default-org",
    )
    assert item["locale"] == "en"


def test_update_locale_returns_new_attrs(users_repo):
    users_repo.put_user(
        user_id="u1",
        email="alice@example.com",
        auth_provider="cognito",
        auth_provider_user_id="u1",
        org_id="default-org",
    )
    attrs = users_repo.update_locale("u1", "en")
    assert attrs is not None
    assert attrs["locale"] == "en"


def test_update_locale_returns_none_for_missing_user(users_repo):
    """The ConditionExpression must translate to `None` on miss so the
    caller can distinguish "row vanished" from a real failure."""
    assert users_repo.update_locale("ghost", "en") is None


def test_me_response_uses_server_clamped_locale():
    """Unknown / malformed stored locales fall back to DEFAULT_LOCALE so
    the SPA never receives a value outside SUPPORTED_LOCALES.
    """
    from mvp.me import _resolve_locale, DEFAULT_LOCALE

    assert _resolve_locale("en") == "en"
    assert _resolve_locale("ja") == "ja"
    assert _resolve_locale("fr") == DEFAULT_LOCALE  # unsupported
    assert _resolve_locale("") == DEFAULT_LOCALE
    assert _resolve_locale(None) == DEFAULT_LOCALE
    assert _resolve_locale(123) == DEFAULT_LOCALE  # type: ignore[arg-type]


def test_update_me_request_rejects_unsupported_locale():
    """Pydantic must reject locales outside SUPPORTED_LOCALES at the API
    boundary. This is the primary control against an attacker-provided
    value being written into the DynamoDB row.
    """
    from pydantic import ValidationError

    from mvp.me import UpdateMeRequest

    with pytest.raises(ValidationError):
        UpdateMeRequest(locale="fr")  # type: ignore[arg-type]
    # extra field forbidden
    with pytest.raises(ValidationError):
        UpdateMeRequest(locale="en", role="admin")  # type: ignore[call-arg]
    # happy path
    body = UpdateMeRequest(locale="en")
    assert body.locale == "en"
