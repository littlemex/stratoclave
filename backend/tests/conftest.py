"""Shared pytest fixtures for the Stratoclave backend test suite.

Design goals (from Team A-1 spec):
  - Never hit real AWS. All boto3 clients are redirected to moto.
  - DynamoDB tables match the production schema defined in
    `backend/dynamo/client.py` so repository code exercises real logic.
  - Fixtures are `function`-scoped so every test gets an isolated tree.

Only the minimum surface needed by the initial test set lives here; more
tables and helpers will be added as later tests land.
"""
from __future__ import annotations

import os
from typing import Iterator

import boto3
import pytest

# AWS credentials must be dummies *before* any boto3 import surface that
# might read the environment happens. Keep this at module load time.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
# Disable the lifespan seed so tests control state explicitly.
os.environ.setdefault("STRATOCLAVE_DISABLE_SEED", "true")
# Main.py treats env=production as strict (raises on missing required
# Cognito vars at import time). Override to development here so tests
# can import main without supplying a full prod env every time.
os.environ.setdefault("ENVIRONMENT", "development")
# Supply CORS_ORIGINS so _validate_cors_origins at import time passes.
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000")
# Table name prefix must match what repository code reads from env.
os.environ.setdefault("STRATOCLAVE_PREFIX", "stratoclave")

# Wire DynamoDB table names the way `backend/dynamo/client.py` expects.
_TABLE_ENVS = {
    "DYNAMODB_USERS_TABLE": "stratoclave-users",
    "DYNAMODB_USER_TENANTS_TABLE": "stratoclave-user-tenants",
    "DYNAMODB_USAGE_LOGS_TABLE": "stratoclave-usage-logs",
    "DYNAMODB_TENANTS_TABLE": "stratoclave-tenants",
    "DYNAMODB_PERMISSIONS_TABLE": "stratoclave-permissions",
    "DYNAMODB_API_KEYS_TABLE": "stratoclave-api-keys",
    "DYNAMODB_TRUSTED_ACCOUNTS_TABLE": "stratoclave-trusted-accounts",
    "DYNAMODB_SSO_PRE_REGISTRATIONS_TABLE": "stratoclave-sso-pre-registrations",
}
for k, v in _TABLE_ENVS.items():
    os.environ.setdefault(k, v)


@pytest.fixture(autouse=True)
def _aws_safety_net(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee AWS_PROFILE is not set during tests so the mock endpoints
    stay in effect even if the host has a real profile configured.
    """
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def dynamodb_mock() -> Iterator[boto3.resource]:
    """Start a moto mock_aws context and yield the DynamoDB resource with
    all Stratoclave tables created.

    The table schemas mirror production sufficiently for repository-level
    tests. GSIs are included where the repos query against them.
    """
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

        # UserTenants: PK user_id, SK tenant_id
        dynamodb.create_table(
            TableName=_TABLE_ENVS["DYNAMODB_USER_TENANTS_TABLE"],
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "tenant_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "tenant_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Tenants: PK tenant_id, with a GSI keyed on team_lead_user_id for
        # list_by_owner.
        dynamodb.create_table(
            TableName=_TABLE_ENVS["DYNAMODB_TENANTS_TABLE"],
            KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "tenant_id", "AttributeType": "S"},
                {"AttributeName": "team_lead_user_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "team-lead-index",
                    "KeySchema": [
                        {"AttributeName": "team_lead_user_id", "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # UsageLogs: PK tenant_id, SK timestamp_log_id
        dynamodb.create_table(
            TableName=_TABLE_ENVS["DYNAMODB_USAGE_LOGS_TABLE"],
            KeySchema=[
                {"AttributeName": "tenant_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp_log_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "tenant_id", "AttributeType": "S"},
                {"AttributeName": "timestamp_log_id", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "user-id-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "timestamp_log_id", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        yield dynamodb


@pytest.fixture
def seed_active_tenant(dynamodb_mock) -> dict:
    """Seed a single active UserTenants row with a known budget and yield
    the identifiers used by the tests.
    """
    user_id = "user-11111111-1111-1111-1111-111111111111"
    tenant_id = "default-org"

    from dynamo.user_tenants import UserTenantsRepository

    repo = UserTenantsRepository()
    repo.ensure(user_id=user_id, tenant_id=tenant_id, role="user", total_credit=10_000)
    return {"user_id": user_id, "tenant_id": tenant_id, "total_credit": 10_000}
