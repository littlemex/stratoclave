"""Tests for per-model quota operations."""
from __future__ import annotations

import pytest

from mvp.routing.quota import (
    _pk_tenant,
    _pk_user,
    _sk,
    build_reserve_txn_items,
    build_settle_updates,
    ensure_counter,
    soft_check_exhausted,
)


@pytest.fixture(autouse=True)
def _clear_ensured():
    """Reset the ensure cache between tests."""
    from mvp.routing import quota
    quota._ensured.clear()
    yield


class TestSoftCheck:
    def test_under_limit_returns_none(self, dynamodb_mock):
        from mvp.routing.quota import _table, _TABLE
        import boto3
        db = boto3.resource("dynamodb", region_name="us-east-1")
        db.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        result = soft_check_exhausted(
            tenant_id="acme",
            user_id=None,
            model="claude-sonnet-4-6",
            period="2026-07",
            amount=1000,
            tenant_limit=100000,
        )
        assert result is None

    def test_over_limit_returns_tenant_quota(self, dynamodb_mock):
        import boto3
        from mvp.routing.quota import _TABLE
        db = boto3.resource("dynamodb", region_name="us-east-1")
        db.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # Pre-seed with usage near limit
        table = db.Table(_TABLE)
        table.put_item(Item={
            "pk": _pk_tenant("acme"),
            "sk": _sk("claude-sonnet-4-6", "2026-07"),
            "reserved": 0,
            "settled": 99000,
        })

        result = soft_check_exhausted(
            tenant_id="acme",
            user_id=None,
            model="claude-sonnet-4-6",
            period="2026-07",
            amount=2000,
            tenant_limit=100000,
        )
        assert result == "tenant_quota"


class TestBuildReserveTxnItems:
    def test_builds_tenant_and_user_items(self, dynamodb_mock):
        import boto3
        from mvp.routing.quota import _TABLE
        db = boto3.resource("dynamodb", region_name="us-east-1")
        db.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        items = build_reserve_txn_items(
            tenant_id="acme",
            user_id="u1",
            model="claude-sonnet-4-6",
            period="2026-07",
            amount=5000,
            tenant_limit=1000000,
            user_limit=500000,
        )
        assert len(items) == 2
        assert items[0]["Update"]["Key"]["pk"]["S"] == "TENANT#acme"
        assert items[1]["Update"]["Key"]["pk"]["S"] == "TENANT#acme#USER#u1"

    def test_builds_tenant_only_when_no_user_limit(self, dynamodb_mock):
        import boto3
        from mvp.routing.quota import _TABLE
        db = boto3.resource("dynamodb", region_name="us-east-1")
        db.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        items = build_reserve_txn_items(
            tenant_id="acme",
            user_id="u1",
            model="claude-sonnet-4-6",
            period="2026-07",
            amount=5000,
            tenant_limit=1000000,
            user_limit=None,
        )
        assert len(items) == 1

    def test_empty_when_no_limits(self, dynamodb_mock):
        items = build_reserve_txn_items(
            tenant_id="acme",
            user_id="u1",
            model="claude-sonnet-4-6",
            period="2026-07",
            amount=5000,
            tenant_limit=None,
            user_limit=None,
        )
        assert items == []


class TestSettle:
    def test_settle_moves_reserved_to_settled(self, dynamodb_mock):
        import boto3
        from mvp.routing.quota import _TABLE
        db = boto3.resource("dynamodb", region_name="us-east-1")
        db.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        table = db.Table(_TABLE)
        pk = _pk_tenant("acme")
        sk = _sk("claude-sonnet-4-6", "2026-07")
        table.put_item(Item={"pk": pk, "sk": sk, "reserved": 5000, "settled": 10000})

        build_settle_updates(
            tenant_id="acme",
            user_id=None,
            model="claude-sonnet-4-6",
            period="2026-07",
            reserved_amount=5000,
            actual_amount=4200,
        )

        item = table.get_item(Key={"pk": pk, "sk": sk})["Item"]
        assert int(item["reserved"]) == 0
        assert int(item["settled"]) == 14200
