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

# Hypothesis profiles for the stateful billing tests. `ci` fixes the
# exploration so a CI run is deterministic (derandomize) and won't flake on a
# time-based deadline; `dev` (default) explores more per run. Select with
# HYPOTHESIS_PROFILE=ci. No-op if hypothesis isn't installed.
try:
    from hypothesis import HealthCheck, settings

    settings.register_profile(
        "ci",
        max_examples=200,
        stateful_step_count=50,
        deadline=None,
        derandomize=True,
        suppress_health_check=[HealthCheck.too_slow],
    )
    settings.register_profile("dev", max_examples=100, stateful_step_count=30, deadline=None)
    settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))
except Exception:  # pragma: no cover - hypothesis optional at import time
    pass

# AWS credentials must be dummies *before* any boto3 import surface that
# might read the environment happens. Keep this at module load time.
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_REGION"] = "us-east-1"
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
    "DYNAMODB_TENANT_BUDGETS_TABLE": "stratoclave-tenant-budgets",
    "DYNAMODB_PRICING_CONFIG_TABLE": "stratoclave-pricing-config",
    "DYNAMODB_RATE_LIMITS_TABLE": "stratoclave-rate-limits",
    "DYNAMODB_MODEL_QUOTAS_TABLE": "stratoclave-model-quotas",
    "DYNAMODB_OBSERVABILITY_TABLE": "stratoclave-observability",
    "DYNAMODB_ROUTING_SIGNALS_TABLE": "stratoclave-routing-signals",
}
for k, v in _TABLE_ENVS.items():
    os.environ.setdefault(k, v)


@pytest.fixture(autouse=True)
def _aws_safety_net(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee AWS_PROFILE is not set during tests so the mock endpoints
    stay in effect even if the host has a real profile configured.

    Also reset the rate limiter's cached low-level client between tests: it is
    lazily built on first use and cached in a module global, so a client built
    outside a moto mock (or in a prior test's mock) must not leak into the next
    test. Each test rebuilds it inside its own context.
    """
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    import core.rate_limit_ddb as _rl
    _rl._rl_client = None
    # Fresh in-process fallback counter per test (degraded-mode limiter state
    # must not leak across tests).
    _rl._local_fallback = _rl._LocalWindows()


@pytest.fixture
def dynamodb_mock() -> Iterator[boto3.resource]:
    """Start a moto mock_aws context and yield the DynamoDB resource with
    all Stratoclave tables created.

    The table schemas mirror production sufficiently for repository-level
    tests. GSIs are included where the repos query against them.
    """
    from dynamo.client import get_dynamodb_resource

    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        get_dynamodb_resource.cache_clear()
        # The rate limiter holds its own (short-timeout) DynamoDB client;
        # reset it so it is rebuilt inside this moto mock.
        import core.rate_limit_ddb as _rl
        _rl._rl_client = None
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

        # TenantBudgets: PK tenant_id, SK sk ("BUDGET#<period>")
        dynamodb.create_table(
            TableName=_TABLE_ENVS["DYNAMODB_TENANT_BUDGETS_TABLE"],
            KeySchema=[
                {"AttributeName": "tenant_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "tenant_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # PricingConfig: PK pk ("CONFIG#pricing"), SK sk
        dynamodb.create_table(
            TableName=_TABLE_ENVS["DYNAMODB_PRICING_CONFIG_TABLE"],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # RateLimits: PK pk ("RL#<scope>#<ip>#<window>"), TTL expires_at
        dynamodb.create_table(
            TableName=_TABLE_ENVS["DYNAMODB_RATE_LIMITS_TABLE"],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # ModelQuotas: PK pk ("TENANT#..." / "TENANT#...#USER#..."), SK sk
        # ("MQ#<model>#<period>"), TTL expires_at. One `used` counter per row.
        dynamodb.create_table(
            TableName=_TABLE_ENVS["DYNAMODB_MODEL_QUOTAS_TABLE"],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Observability (P0-13/14): span records + workflow_run rollups. PK pk
        # ("TENANT#<t>#RUN#<run>"), SK sk ("SPAN#..." | "ROLLUP"), TTL expires_at,
        # GSI1 (sparse, rollups only) on gsi1pk/gsi1sk.
        dynamodb.create_table(
            TableName=_TABLE_ENVS["DYNAMODB_OBSERVABILITY_TABLE"],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # RoutingSignals (P0-16): write-only append log. PK pk
        # ("TENANT#<t>#CAT#<c>#D#<yyyymmdd>#S#<shard>"), SK sk ("TS#<ms>#<span>"),
        # TTL expires_at. No GSI.
        dynamodb.create_table(
            TableName=_TABLE_ENVS["DYNAMODB_ROUTING_SIGNALS_TABLE"],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
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


@pytest.fixture
def seed_tenant_with_pool(dynamodb_mock) -> dict:
    """Seed an active UserTenants row plus a TenantBudgets pool for the current
    period, and yield the identifiers and limits used by the pool tests.

    The per-user token balance is generous (1e9) so pool tests exercise the
    dollar pool ceiling rather than the per-user token cap unless a test
    deliberately sets a tighter personal balance.
    """
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.user_tenants import UserTenantsRepository

    user_id = "user-22222222-2222-2222-2222-222222222222"
    tenant_id = "acme-eng"
    period = current_period()
    pool_limit_microusd = 5_000_000  # $5.00

    UserTenantsRepository().ensure(
        user_id=user_id, tenant_id=tenant_id, role="user", total_credit=1_000_000_000
    )
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=tenant_id, period=period, pool_limit_microusd=pool_limit_microusd
    )
    return {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "period": period,
        "pool_limit_microusd": pool_limit_microusd,
    }
