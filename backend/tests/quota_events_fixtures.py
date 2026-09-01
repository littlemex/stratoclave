"""Shared test-only scaffolding for the F2 quota-events table.

Not a test file (no ``test_`` prefix, not collected by pytest). Mirrors the
pattern already used by ``test_pool_membership_delta_l4.py`` and friends:
extra tables are created LOCALLY by the test file that needs them, layered on
top of the shared ``dynamodb_mock`` fixture in ``conftest.py`` — this module
exists only so eight F2 test files do not each duplicate the same
``create_table`` call and the same raw-item seeding helpers.

Table shape is exactly ``CONTRACT-F2-grant.md``'s Interface section, plus the
two GSIs it names:

  * base table:  pk (HASH) / sk (RANGE)
  * ``tenant-status-index``:    tenant_id (HASH) / status_created_at (RANGE)
  * ``grant-expiry-index``:     grant_status (HASH) / expires_at (RANGE) — sparse
    by construction (only items that WRITE ``grant_status`` appear in it)

``design-F2.md`` Ambiguity #4: the table name is read from
``DYNAMODB_QUOTA_EVENTS_TABLE`` directly by ``dynamo.quota_events`` (inline,
no ``client.py`` change required), so this fixture sets that env var itself
rather than depending on ``conftest.py``'s ``_TABLE_ENVS``.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Optional

import boto3
import pytest

TABLE_NAME = "stratoclave-quota-events"
ENV_VAR = "DYNAMODB_QUOTA_EVENTS_TABLE"


@pytest.fixture
def quota_events_table(dynamodb_mock):
    """Create the quota-events table (+ both GSIs) inside the running moto
    mock, point the env var at it, and yield the boto3 Table resource."""
    os.environ[ENV_VAR] = TABLE_NAME
    dynamodb_mock.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "tenant_id", "AttributeType": "S"},
            {"AttributeName": "status_created_at", "AttributeType": "S"},
            {"AttributeName": "grant_status", "AttributeType": "S"},
            {"AttributeName": "expires_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "tenant-status-index",
                "KeySchema": [
                    {"AttributeName": "tenant_id", "KeyType": "HASH"},
                    {"AttributeName": "status_created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "grant-expiry-index",
                "KeySchema": [
                    {"AttributeName": "grant_status", "KeyType": "HASH"},
                    {"AttributeName": "expires_at", "KeyType": "RANGE"},
                ],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": [
                        "grant_id", "tenant_id", "approved_amount_microusd",
                        "target_pk", "target_sk", "period",
                    ],
                },
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    yield boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE_NAME)


def slot_pk(user_id: str) -> str:
    return f"USER#{user_id}"


def slot_sk(tenant_id: str, date_str: str) -> str:
    return f"SLOT#{tenant_id}#{date_str}"


def request_pk(request_id: str) -> str:
    return f"REQUEST#{request_id}"


def grant_pk(tenant_id: str) -> str:
    return f"TENANT#{tenant_id}"


def grant_sk(grant_id: str) -> str:
    return f"GRANT#{grant_id}"


def seed_slot(
    table, *, user_id: str, tenant_id: str, date_str: str, client_token: str,
    request_id: str, created_at: str = "2026-09-01T00:00:00+00:00",
) -> None:
    table.put_item(Item={
        "pk": slot_pk(user_id), "sk": slot_sk(tenant_id, date_str),
        "client_token": client_token, "request_id": request_id,
        "created_at": created_at,
    })


def seed_request(
    table, *, request_id: str, tenant_id: str, user_id: str,
    requested_amount_microusd: int, requested_expires_at: int,
    status: str = "PENDING", client_token: str = "tok-default",
    created_at: str = "2026-09-01T00:00:00+00:00",
    decision_comment: Optional[str] = None,
    revision: int = 1,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    item: dict[str, Any] = {
        "pk": request_pk(request_id), "sk": "REQUEST",
        "request_id": request_id, "tenant_id": tenant_id, "user_id": user_id,
        "requested_amount_microusd": Decimal(requested_amount_microusd),
        "requested_expires_at": Decimal(requested_expires_at),
        "status": status, "client_token": client_token,
        "created_at": created_at, "revision": Decimal(revision),
        "status_created_at": f"{status}#{created_at}",
    }
    if decision_comment is not None:
        item["decision_comment"] = decision_comment
    if extra:
        item.update(extra)
    table.put_item(Item=item)


def seed_grant(
    table, *, grant_id: str, tenant_id: str, request_id: str,
    approver_user_id: str, approved_amount_microusd: int,
    expires_at_epoch: int, target_pk: str, target_sk: str, period: str,
    status: str = "ACTIVE", created_at: str = "2026-09-01T00:00:00+00:00",
    revoke_attempts: int = 0,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    item: dict[str, Any] = {
        "pk": grant_pk(tenant_id), "sk": grant_sk(grant_id),
        "grant_id": grant_id, "tenant_id": tenant_id, "request_id": request_id,
        "approver_user_id": approver_user_id,
        "approved_amount_microusd": Decimal(approved_amount_microusd),
        "expires_at": Decimal(expires_at_epoch),
        "target_pk": target_pk, "target_sk": target_sk, "period": period,
        "status": status, "created_at": created_at,
        "revoke_attempts": Decimal(revoke_attempts),
        "status_created_at": f"{status}#{created_at}",
    }
    if status == "ACTIVE":
        item["grant_status"] = "ACTIVE"
    if extra:
        item.update(extra)
    table.put_item(Item=item)


def seed_pool_with_grant_fields(
    tenant_id: str, period: str, *, pool_limit_microusd: int,
    pool_granted_microusd: int = 0, grant_cap_microusd: Optional[int] = None,
) -> None:
    """Seed a `TenantBudgets` pool row carrying F2's two new attributes
    directly (bypassing F1's `set_pool_limit`, which does not know about
    them), per design-F2.md's opening paragraph: these tests do not depend on
    F1 landing first in this worktree."""
    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk

    repo = TenantBudgetsRepository()
    repo.set_pool_limit(
        tenant_id=tenant_id, period=period, pool_limit_microusd=pool_limit_microusd,
    )
    update_expr = "SET pool_granted_microusd = :g"
    values: dict[str, Any] = {":g": Decimal(pool_granted_microusd)}
    if grant_cap_microusd is not None:
        update_expr += ", grant_cap_microusd = :c"
        values[":c"] = Decimal(grant_cap_microusd)
    repo._table.update_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=values,
    )
