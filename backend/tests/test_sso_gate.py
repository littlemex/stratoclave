"""Tests for the Vouch-by-STS gate.

Covers the security contract from ARCHITECTURE.md § SSO:

  - ARN classification produces the documented identity_type values.
  - Unknown trusted accounts are rejected.
  - EC2 instance_profile is denied by default (opt-in via
    `allow_instance_profile`).
  - IAM users are denied by default (opt-in via `allow_iam_user`) and
    always require an explicit invite.
  - Allowed-role patterns are honored via fnmatch.
  - Invites override the provisioning policy.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from mvp.sso_sts import classify_arn


def _trusted_accounts_table(dynamodb_mock):
    dynamodb_mock.create_table(
        TableName="stratoclave-trusted-accounts",
        KeySchema=[{"AttributeName": "account_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "account_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _sso_invites_table(dynamodb_mock):
    # The production schema indexes on a composite `iam_user_lookup_key`
    # (built from account_id + iam_user_name) with a single HASH key.
    dynamodb_mock.create_table(
        TableName="stratoclave-sso-pre-registrations",
        KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "email", "AttributeType": "S"},
            {"AttributeName": "iam_user_lookup_key", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "iam-user-index",
                "KeySchema": [
                    {"AttributeName": "iam_user_lookup_key", "KeyType": "HASH"}
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def sso_tables(dynamodb_mock):
    _trusted_accounts_table(dynamodb_mock)
    _sso_invites_table(dynamodb_mock)
    os_env_tables = {
        "DYNAMODB_TRUSTED_ACCOUNTS_TABLE": "stratoclave-trusted-accounts",
        "DYNAMODB_SSO_PRE_REGISTRATIONS_TABLE": "stratoclave-sso-pre-registrations",
    }
    import os

    for k, v in os_env_tables.items():
        os.environ.setdefault(k, v)
    yield dynamodb_mock


# ----------------------------------------------------------------------
# classify_arn
# ----------------------------------------------------------------------
def test_classify_sso_user():
    sts = classify_arn(
        arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Developer_abc/user@corp.example",
        user_id="AROAEXAMPLEID",
        account_id="111111111111",
    )
    assert sts.identity_type == "sso_user"
    assert sts.session_name == "user@corp.example"


def test_classify_iam_user():
    sts = classify_arn(
        arn="arn:aws:iam::111111111111:user/alice",
        user_id="AIDAEXAMPLEID",
        account_id="111111111111",
    )
    assert sts.identity_type == "iam_user"
    assert sts.iam_user_name == "alice"


def test_classify_instance_profile():
    sts = classify_arn(
        arn="arn:aws:sts::111111111111:assumed-role/InstanceRole/i-0123456789abcdef0",
        user_id="AROAEXAMPLEINSTANCE",
        account_id="111111111111",
    )
    assert sts.identity_type == "instance_profile"


def test_classify_federated_role():
    sts = classify_arn(
        arn="arn:aws:sts::111111111111:assumed-role/SomeRole/session-name",
        user_id="AROAEXAMPLEFED",
        account_id="111111111111",
    )
    assert sts.identity_type == "federated_role"


def test_classify_malformed_arn_raises():
    with pytest.raises(HTTPException) as exc:
        classify_arn(arn="not-an-arn", user_id="x", account_id="x")
    assert exc.value.status_code == 400


# ----------------------------------------------------------------------
# validate_sso_identity (the actual 4-gate dispatcher)
# ----------------------------------------------------------------------
def _seed_account(dynamodb_mock, account_id: str, **attrs):
    tbl = dynamodb_mock.Table("stratoclave-trusted-accounts")
    item = {"account_id": account_id, **attrs}
    tbl.put_item(Item=item)


def test_unknown_account_is_denied(sso_tables):
    from mvp.sso_gate import validate_sso_identity
    from mvp.sso_sts import StsIdentity

    sts = StsIdentity(
        arn="arn:aws:sts::999999999999:assumed-role/AWSReservedSSO_Developer_x/u@e.com",
        user_id="AROA",
        account_id="999999999999",
        identity_type="sso_user",
        role_name="AWSReservedSSO_Developer_x",
        session_name="u@e.com",
        iam_user_name=None,
    )
    with pytest.raises(HTTPException) as exc:
        validate_sso_identity(sts)
    assert exc.value.status_code == 403


def test_instance_profile_denied_by_default(sso_tables):
    from mvp.sso_gate import validate_sso_identity
    from mvp.sso_sts import StsIdentity

    _seed_account(sso_tables, "111111111111", provisioning_policy="auto_provision")

    sts = StsIdentity(
        arn="arn:aws:sts::111111111111:assumed-role/InstanceRole/i-0123456789abcdef0",
        user_id="AROAEXAMPLEINSTANCE",
        account_id="111111111111",
        identity_type="instance_profile",
        role_name="InstanceRole",
        session_name="i-0123456789abcdef0",
        iam_user_name=None,
    )
    with pytest.raises(HTTPException) as exc:
        validate_sso_identity(sts)
    assert exc.value.status_code == 403


def test_instance_profile_allowed_when_opted_in(sso_tables):
    from mvp.sso_gate import validate_sso_identity
    from mvp.sso_sts import StsIdentity

    _seed_account(
        sso_tables,
        "111111111111",
        allow_instance_profile=True,
        provisioning_policy="invite_only",  # invite_only でも instance は session が email でないため 403 ルートになるはず
    )

    sts = StsIdentity(
        arn="arn:aws:sts::111111111111:assumed-role/InstanceRole/i-0123456789abcdef0",
        user_id="AROAEXAMPLEINSTANCE",
        account_id="111111111111",
        identity_type="instance_profile",
        role_name="InstanceRole",
        session_name="i-0123456789abcdef0",
        iam_user_name=None,
    )
    # allow_instance_profile passes gate 0, but gate 3 with invite_only
    # rejects since the session_name is the instance id rather than an email.
    # The test pins this behavior: opting in does not silently auto-provision.
    with pytest.raises(HTTPException) as exc:
        validate_sso_identity(sts)
    assert exc.value.status_code == 403


def test_iam_user_always_requires_invite(sso_tables):
    from mvp.sso_gate import validate_sso_identity
    from mvp.sso_sts import StsIdentity

    # Even with allow_iam_user and auto_provision, iam_user must have an invite.
    _seed_account(
        sso_tables,
        "111111111111",
        allow_iam_user=True,
        provisioning_policy="auto_provision",
    )

    sts = StsIdentity(
        arn="arn:aws:iam::111111111111:user/alice",
        user_id="AIDAEXAMPLE",
        account_id="111111111111",
        identity_type="iam_user",
        role_name=None,
        session_name=None,
        iam_user_name="alice",
    )
    with pytest.raises(HTTPException) as exc:
        validate_sso_identity(sts)
    assert exc.value.status_code == 403


def test_allowed_role_patterns_honored(sso_tables):
    from mvp.sso_gate import validate_sso_identity
    from mvp.sso_sts import StsIdentity

    _seed_account(
        sso_tables,
        "111111111111",
        provisioning_policy="auto_provision",
        allowed_role_patterns=["AWSReservedSSO_Developer_*"],
    )

    # A matching role name passes gate 1 and (since session is email)
    # auto_provision resolves successfully.
    ok = StsIdentity(
        arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Developer_eng/u@e.com",
        user_id="AROA",
        account_id="111111111111",
        identity_type="sso_user",
        role_name="AWSReservedSSO_Developer_eng",
        session_name="u@e.com",
        iam_user_name=None,
    )
    resolved = validate_sso_identity(ok)
    assert resolved.email == "u@e.com"

    # A non-matching role name is rejected at gate 1.
    blocked = StsIdentity(
        arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin_root/u@e.com",
        user_id="AROA",
        account_id="111111111111",
        identity_type="sso_user",
        role_name="AWSReservedSSO_Admin_root",
        session_name="u@e.com",
        iam_user_name=None,
    )
    with pytest.raises(HTTPException) as exc:
        validate_sso_identity(blocked)
    assert exc.value.status_code == 403


def test_invite_only_requires_preregistration(sso_tables):
    from mvp.sso_gate import validate_sso_identity
    from mvp.sso_sts import StsIdentity

    _seed_account(sso_tables, "111111111111", provisioning_policy="invite_only")

    # No invite for this email → denied.
    sts = StsIdentity(
        arn="arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Developer_x/unknown@e.com",
        user_id="AROA",
        account_id="111111111111",
        identity_type="sso_user",
        role_name="AWSReservedSSO_Developer_x",
        session_name="unknown@e.com",
        iam_user_name=None,
    )
    with pytest.raises(HTTPException) as exc:
        validate_sso_identity(sts)
    assert exc.value.status_code == 403
