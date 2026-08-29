"""An assumed-role ARN is not a principal, and the login path must not treat it as one.

The defect these tests pin was reachable on the shipped SSO login path. Three
mistakes lined up:

1. `allowed_role_patterns` defaulted to empty, and an empty list meant "no
   restriction" — so any role in a trusted account could present an identity.
2. Every identity on that path is read out of the RoleSessionName: the invite is
   looked up by it, and `auto_provision` derives the email from it. Whoever calls
   `sts:AssumeRole` chooses that string.
3. The check meant to stop a second principal from logging in as an existing user
   compared the *ARN*. An assumed-role ARN is
   `arn:aws:sts::<acct>:assumed-role/<role>/<RoleSessionName>`, so a second caller
   of the same role who passed the same session name reproduced it byte for byte
   and passed.

Together: a principal able to assume any role in a trusted account could log in as
any invited email — and, because an existing user's roles are read from the
database rather than from the invite, as an administrator if one had ever been
elevated after being provisioned through SSO.

The fix is (1) an empty allowlist refuses, and (2) the binding compares the
AWS-assigned part of the STS UserId, which no caller can choose.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from mvp.sso_exchange import principal_matches
from mvp.sso_sts import classify_arn, stable_principal_id

ACCOUNT = "111111111111"
ROLE = "AWSReservedSSO_Developer_eng"
VICTIM_EMAIL = "victim@example.com"
ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/{ROLE}/{VICTIM_EMAIL}"


def _identity(user_id: str):
    """An STS identity as the vouch path builds it, for a chosen UserId."""
    return classify_arn(arn=ARN, user_id=user_id, account_id=ACCOUNT)


# ---------------------------------------------------------------------------
# the stable id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user_id,expected", [
    ("AROAEXAMPLEROLEID:victim@example.com", "AROAEXAMPLEROLEID"),
    ("AROAEXAMPLEROLEID:i-0123456789abcdef", "AROAEXAMPLEROLEID"),
    ("AIDAEXAMPLEUSERID", "AIDAEXAMPLEUSERID"),
    ("", ""),
])
def test_the_stable_principal_id_drops_the_session_part(user_id, expected):
    """The session part is caller-chosen; the prefix is assigned by AWS."""
    assert stable_principal_id(user_id) == expected


def test_the_identity_carries_the_stable_id_not_only_the_arn():
    sts = _identity("AROAEXAMPLEROLEID:victim@example.com")
    assert sts.principal_id == "AROAEXAMPLEROLEID"
    assert sts.arn.endswith(VICTIM_EMAIL), "the ARN still carries the session name"


# ---------------------------------------------------------------------------
# the binding
# ---------------------------------------------------------------------------


def test_the_bound_principal_logs_in():
    existing = {"sso_principal_id": "AROAEXAMPLEROLEID", "sso_principal_arn": ARN}
    assert principal_matches(existing, _identity("AROAEXAMPLEROLEID:victim@example.com"))


def test_a_reproduced_arn_from_a_different_role_is_refused():
    """The attack: same account, same session name, a role with a different id.

    The ARN alone cannot tell these apart when the role NAME is also reused, which
    is why the ARN comparison was not a control. Here the ids differ, so the
    identity is refused however the ARN was constructed.
    """
    existing = {"sso_principal_id": "AROAVICTIMROLEID", "sso_principal_arn": ARN}
    attacker = _identity("AROAATTACKERROLEID:victim@example.com")
    assert attacker.arn == existing["sso_principal_arn"], (
        "the premise of the test: the attacker reproduced the ARN exactly"
    )
    assert not principal_matches(existing, attacker)


def test_a_row_that_predates_the_stable_id_still_logs_in():
    """The upgrade path: compare the ARN, as this row has always been compared."""
    legacy = {"sso_principal_arn": ARN}
    assert principal_matches(legacy, _identity("AROAEXAMPLEROLEID:victim@example.com"))


def test_a_row_that_predates_the_stable_id_still_refuses_a_different_arn():
    legacy = {"sso_principal_arn": ARN}
    other = classify_arn(
        arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/OtherRole/{VICTIM_EMAIL}",
        user_id="AROAOTHER:victim@example.com",
        account_id=ACCOUNT,
    )
    assert not principal_matches(legacy, other)


def test_a_user_with_no_binding_at_all_is_not_refused():
    """A row with neither field is a user provisioned outside the SSO path; the
    binding cannot speak to it and must not invent an answer."""
    assert principal_matches({}, _identity("AROAEXAMPLEROLEID:victim@example.com"))


# ---------------------------------------------------------------------------
# the gate that makes the session name readable as an identity at all
# ---------------------------------------------------------------------------


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


def _seed_account(dynamodb_mock, account_id: str, **attrs):
    dynamodb_mock.Table("stratoclave-trusted-accounts").put_item(
        Item={"account_id": account_id, **attrs}
    )


def test_an_account_with_no_role_allowlist_refuses_an_assumed_role(sso_tables):
    """An empty allowlist used to mean "no restriction". What the allowlist bounds
    is whose RoleSessionName this deployment reads as an identity, so no restriction
    was the one setting it could not have."""
    from mvp.sso_gate import validate_sso_identity

    _seed_account(sso_tables, ACCOUNT, provisioning_policy="auto_provision")
    with pytest.raises(HTTPException) as exc:
        validate_sso_identity(_identity("AROAANYROLE:victim@example.com"))
    assert exc.value.status_code == 403
    assert "allowed role patterns" in str(exc.value.detail)


def test_an_arbitrary_role_cannot_claim_an_email_when_patterns_are_set(sso_tables):
    from mvp.sso_gate import validate_sso_identity

    _seed_account(
        sso_tables, ACCOUNT,
        provisioning_policy="auto_provision",
        allowed_role_patterns=["AWSReservedSSO_*"],
    )
    attacker = classify_arn(
        arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/AttackerRole/{VICTIM_EMAIL}",
        user_id="AROAATTACKER:victim@example.com",
        account_id=ACCOUNT,
    )
    with pytest.raises(HTTPException) as exc:
        validate_sso_identity(attacker)
    assert exc.value.status_code == 403


def test_an_identity_center_role_still_resolves(sso_tables):
    """The flow this is all in service of must keep working."""
    from mvp.sso_gate import validate_sso_identity

    _seed_account(
        sso_tables, ACCOUNT,
        provisioning_policy="auto_provision",
        allowed_role_patterns=["AWSReservedSSO_*"],
    )
    resolved = validate_sso_identity(_identity("AROAEXAMPLEROLEID:victim@example.com"))
    assert resolved.email == VICTIM_EMAIL
    assert resolved.target_role == "user"


# ---------------------------------------------------------------------------
# the action the signed request carries
# ---------------------------------------------------------------------------


def _headers():
    return {
        "Authorization": "AWS4-HMAC-SHA256 Credential=AKIA/20260830/us-east-1/sts/aws4_request",
        "X-Amz-Date": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ"),
    }


@pytest.mark.parametrize("url,body", [
    ("https://sts.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15", ""),
    ("https://sts.amazonaws.com/", "Action=GetCallerIdentity&Version=2011-06-15"),
])
def test_a_get_caller_identity_vouch_passes_validation(url, body):
    """Both shapes are legitimate: the action can be in the query or the signed form
    body, and a signer that presigns the POST form uses the latter."""
    from mvp.sso_sts import _validate_inputs

    _validate_inputs("POST", url, _headers(), body)


@pytest.mark.parametrize("url,body", [
    ("https://sts.amazonaws.com/?Action=GetSessionToken", ""),
    ("https://sts.amazonaws.com/", "Action=GetSessionToken&Version=2011-06-15"),
    ("https://sts.amazonaws.com/", ""),
])
def test_anything_other_than_get_caller_identity_is_refused(url, body):
    """The documented control — "Only Action=GetCallerIdentity is accepted" — was
    enforced on the query string only, so it did not exist for the signer whose
    action is in the body: the gateway would forward whatever action the presented
    credentials allowed. A request carrying no action at all is not a vouch either.
    """
    from mvp.sso_sts import _validate_inputs

    with pytest.raises(HTTPException) as exc:
        _validate_inputs("POST", url, _headers(), body)
    assert exc.value.status_code == 400
