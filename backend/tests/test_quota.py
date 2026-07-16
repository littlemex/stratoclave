"""Tests for per-model quota operations (single `used` counter design)."""
from __future__ import annotations

import boto3
import pytest

from mvp.routing.quota import (
    _TABLE,
    _pk_tenant,
    _pk_user,
    _sk,
    build_reserve_txn_items,
    release_quota,
    settle_quota,
    soft_check_exhausted,
)


@pytest.fixture
def quota_table(dynamodb_mock):
    """The model-quotas table (created by the shared dynamodb_mock fixture)."""
    return boto3.resource("dynamodb", region_name="us-east-1").Table(_TABLE)


class TestSoftCheck:
    def test_under_limit_returns_none(self, quota_table):
        assert soft_check_exhausted(
            tenant_id="acme", user_id=None, model="claude-sonnet-4-6",
            period="2026-07", amount=1000, tenant_limit=100000,
        ) is None

    def test_over_limit_returns_tenant_quota(self, quota_table):
        quota_table.put_item(Item={
            "pk": _pk_tenant("acme"), "sk": _sk("claude-sonnet-4-6", "2026-07"),
            "used": 99000,
        })
        assert soft_check_exhausted(
            tenant_id="acme", user_id=None, model="claude-sonnet-4-6",
            period="2026-07", amount=2000, tenant_limit=100000,
        ) == "tenant_quota"

    def test_missing_item_reads_as_zero(self, quota_table):
        # No init needed — a missing item is used=0.
        assert soft_check_exhausted(
            tenant_id="fresh", user_id=None, model="m", period="2026-07",
            amount=1, tenant_limit=10,
        ) is None


class TestBuildReserveTxnItems:
    def test_builds_tenant_and_user_items(self, quota_table):
        items = build_reserve_txn_items(
            tenant_id="acme", user_id="u1", model="claude-sonnet-4-6",
            period="2026-07", amount=5000, tenant_limit=1000000, user_limit=500000,
        )
        assert len(items) == 2
        assert items[0]["Update"]["Key"]["pk"]["S"] == "TENANT#acme"
        assert items[1]["Update"]["Key"]["pk"]["S"] == "TENANT#acme#USER#u1"
        # No cross-attribute arithmetic in the condition (DynamoDB forbids it).
        cond = items[0]["Update"]["ConditionExpression"]
        assert "+" not in cond
        assert "used <= :headroom" in cond

    def test_tenant_only_when_no_user_limit(self, quota_table):
        items = build_reserve_txn_items(
            tenant_id="acme", user_id="u1", model="m", period="2026-07",
            amount=5000, tenant_limit=1000000, user_limit=None,
        )
        assert len(items) == 1

    def test_empty_when_no_limits(self, quota_table):
        assert build_reserve_txn_items(
            tenant_id="acme", user_id="u1", model="m", period="2026-07",
            amount=5000, tenant_limit=None, user_limit=None,
        ) == []


class TestReserveEnforcement:
    """Execute the reserve txn against moto to exercise the real condition."""

    def _reserve(self, client, tenant_id, model, amount, limit):
        items = build_reserve_txn_items(
            tenant_id=tenant_id, user_id=None, model=model, period="2026-07",
            amount=amount, tenant_limit=limit,
        )
        client.transact_write_items(TransactItems=items)

    def test_reserve_accumulates_used_and_rejects_over_limit(self, quota_table):
        client = boto3.client("dynamodb", region_name="us-east-1")
        from botocore.exceptions import ClientError
        self._reserve(client, "acme", "m", 60, 100)   # used 0->60
        self._reserve(client, "acme", "m", 30, 100)   # used 60->90
        with pytest.raises(ClientError) as e:
            self._reserve(client, "acme", "m", 30, 100)  # 90->120 over → cancel
        assert e.value.response["Error"]["Code"] == "TransactionCanceledException"
        item = quota_table.get_item(
            Key={"pk": _pk_tenant("acme"), "sk": _sk("m", "2026-07")})["Item"]
        assert int(item["used"]) == 90  # never 120


class TestSettleRelease:
    def test_settle_adjusts_used_to_actual(self, quota_table):
        pk, sk = _pk_tenant("acme"), _sk("m", "2026-07")
        quota_table.put_item(Item={"pk": pk, "sk": sk, "used": 15000})  # 5000 reserved + 10000 prior
        settle_quota(tenant_id="acme", user_id=None, model="m", period="2026-07",
                     reserved_amount=5000, actual_amount=4200)
        # used += (4200 - 5000) = -800 → 14200
        assert int(quota_table.get_item(Key={"pk": pk, "sk": sk})["Item"]["used"]) == 14200

    def test_release_removes_reservation(self, quota_table):
        pk, sk = _pk_tenant("acme"), _sk("m", "2026-07")
        quota_table.put_item(Item={"pk": pk, "sk": sk, "used": 5000})
        release_quota(tenant_id="acme", user_id=None, model="m", period="2026-07",
                      reserved_amount=5000)
        assert int(quota_table.get_item(Key={"pk": pk, "sk": sk})["Item"]["used"]) == 0
