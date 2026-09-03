"""F4 / R15 — the four DynamoDB facts the headroom/PENDING design rests on,
named as tests, and re-measured on real AWS (moto is looser about the fourth).

WHAT THIS PINS

`docs/design/ledger-hot-path.md` builds the headroom-ADD design and the
PENDING protocol on four DynamoDB semantics:

  1. `ADD` on a missing numeric attribute CREATES it (rather than failing) —
     load-bearing for pool-row creation and for the first reserve against a
     legacy row.
  2. A `ConditionExpression` referencing a MISSING attribute FAILS (rather
     than being vacuously true) — load-bearing for `attribute_not_exists`
     seeding and for the headroom gate ever refusing anything.
  3. A negative `ADD` result is NOT floored at zero — load-bearing for
     `set_pool_limit`'s "a lower limit can drive headroom negative, at which
     point new admissions are all correctly refused" (ledger-hot-path.md).
  4. `TransactWriteItems` across two tables (TenantBudgets + CreditLedger, or
     TenantBudgets + the HOLD row under the same PK) is atomic: a cancelled
     transaction leaves NEITHER write applied.

Each is exercised twice: an always-on `_moto` test (this repository's normal
per-commit gate) and a `@pytest.mark.live` counterpart, following this
repo's existing convention exactly
(`test_pricing_floor.py::test_no_floor_leg_undercuts_the_live_published_price`)
— skipped by default, opt-in via `STRATOCLAVE_LIVE_DYNAMODB_TESTS=1` plus real
credentials, because moto is "synchronously consistent and looser about
transactions" (CONTRACT-F4-claims (F4's contract document)) and the fourth fact specifically needs
real AWS's own internal locking to be exercised meaningfully rather than
moto's effectively-single-threaded execution. the F4 design note section 9 states,
per fact, what I expect to change (nothing, for facts 1-3; only the STRENGTH
of the atomicity claim moto can support, for fact 4) and why.

A `_live` table is created and torn down per test under the real account the
opt-in credentials resolve to, named with a `stratoclave-f4-live-` prefix and
a random suffix so a concurrent live run (or a crashed prior one) cannot
collide.
"""
from __future__ import annotations

import os
import uuid

import pytest

from tests.live_aws import real_session

_LIVE_FLAG = "STRATOCLAVE_LIVE_DYNAMODB_TESTS"


# --------------------------------------------------------------- moto fixture


@pytest.fixture
def facts_table(dynamodb_mock):
    """A throwaway table, same key shape as TenantBudgets (PK tenant_id, SK
    sk), under moto — deliberately NOT reusing the real TenantBudgets table
    fixture from conftest.py, since these are facts about DynamoDB itself,
    not about the repository class, and a dedicated table keeps that
    distinction visible in the test."""
    import boto3

    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="f4-l15-facts",
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
    dynamodb.create_table(
        TableName="f4-l15-facts-2",
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
    yield dynamodb.Table("f4-l15-facts"), dynamodb.Table("f4-l15-facts-2")


# ----------------------------------------------------------------- fact 1


def _assert_add_creates_missing_attribute(table) -> None:
    key = {"tenant_id": "l15", "sk": "fact1"}
    table.put_item(Item=dict(key, other="x"))
    table.update_item(
        Key=key,
        UpdateExpression="ADD pool_headroom_microusd :amt",
        ExpressionAttributeValues={":amt": 500},
    )
    item = table.get_item(Key=key, ConsistentRead=True)["Item"]
    assert int(item["pool_headroom_microusd"]) == 500, (
        "ADD on a missing numeric attribute did not create it at the added "
        "value — the headroom design's pool-row-creation path assumes this."
    )


def test_add_creates_a_missing_numeric_attribute_moto(facts_table):
    table, _ = facts_table
    _assert_add_creates_missing_attribute(table)


@pytest.mark.live
def test_add_creates_a_missing_numeric_attribute_live():
    if not os.getenv(_LIVE_FLAG):
        pytest.skip(f"set {_LIVE_FLAG}=1 (with AWS credentials) to re-measure the "
                    f"four DynamoDB facts on real AWS")
    import boto3
    session = real_session(boto3)
    if session is None:
        pytest.skip("no real AWS credentials available")
    table_name = f"stratoclave-f4-live-{uuid.uuid4().hex[:8]}"
    ddb = session.resource("dynamodb", region_name=os.getenv("STRATOCLAVE_REGION", "us-east-1"))
    ddb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "tenant_id", "AttributeType": "S"},
                               {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table = ddb.Table(table_name)
    table.wait_until_exists()
    try:
        _assert_add_creates_missing_attribute(table)
    finally:
        table.delete()


# ----------------------------------------------------------------- fact 2


def _assert_condition_on_missing_attribute_fails(table) -> None:
    from botocore.exceptions import ClientError

    key = {"tenant_id": "l15", "sk": "fact2"}
    table.put_item(Item=dict(key, other="x"))  # no pool_headroom_microusd at all
    with pytest.raises(ClientError) as excinfo:
        table.update_item(
            Key=key,
            UpdateExpression="ADD pool_headroom_microusd :neg",
            ConditionExpression="pool_headroom_microusd >= :amt",
            ExpressionAttributeValues={":neg": -1, ":amt": 0},
        )
    assert excinfo.value.response["Error"]["Code"] == "ConditionalCheckFailedException", (
        "a condition comparing a MISSING attribute did not fail — this is "
        "exactly the semantic the headroom gate depends on to refuse a "
        "reserve against a row that (somehow) has no headroom counter yet, "
        "rather than treating a missing counter as vacuously satisfying "
        '">= :amt".'
    )


def test_a_condition_on_a_missing_attribute_fails_moto(facts_table):
    table, _ = facts_table
    _assert_condition_on_missing_attribute_fails(table)


@pytest.mark.live
def test_a_condition_on_a_missing_attribute_fails_live():
    if not os.getenv(_LIVE_FLAG):
        pytest.skip(f"set {_LIVE_FLAG}=1 (with AWS credentials) to re-measure the "
                    f"four DynamoDB facts on real AWS")
    import boto3
    session = real_session(boto3)
    if session is None:
        pytest.skip("no real AWS credentials available")
    table_name = f"stratoclave-f4-live-{uuid.uuid4().hex[:8]}"
    ddb = session.resource("dynamodb", region_name=os.getenv("STRATOCLAVE_REGION", "us-east-1"))
    ddb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "tenant_id", "AttributeType": "S"},
                               {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table = ddb.Table(table_name)
    table.wait_until_exists()
    try:
        _assert_condition_on_missing_attribute_fails(table)
    finally:
        table.delete()


# ----------------------------------------------------------------- fact 3


def _assert_negative_add_is_not_floored(table) -> None:
    key = {"tenant_id": "l15", "sk": "fact3"}
    table.put_item(Item=dict(key, pool_headroom_microusd=100))
    table.update_item(
        Key=key,
        UpdateExpression="ADD pool_headroom_microusd :neg",
        ExpressionAttributeValues={":neg": -500},
    )
    item = table.get_item(Key=key, ConsistentRead=True)["Item"]
    assert int(item["pool_headroom_microusd"]) == -400, (
        "a negative ADD result was floored at zero rather than going "
        "negative — set_pool_limit's 'a lower limit can drive headroom "
        "negative, at which point new admissions are all correctly refused' "
        "(ledger-hot-path.md) depends on this NOT being floored."
    )


def test_a_negative_add_is_not_floored_at_zero_moto(facts_table):
    table, _ = facts_table
    _assert_negative_add_is_not_floored(table)


@pytest.mark.live
def test_a_negative_add_is_not_floored_at_zero_live():
    if not os.getenv(_LIVE_FLAG):
        pytest.skip(f"set {_LIVE_FLAG}=1 (with AWS credentials) to re-measure the "
                    f"four DynamoDB facts on real AWS")
    import boto3
    session = real_session(boto3)
    if session is None:
        pytest.skip("no real AWS credentials available")
    table_name = f"stratoclave-f4-live-{uuid.uuid4().hex[:8]}"
    ddb = session.resource("dynamodb", region_name=os.getenv("STRATOCLAVE_REGION", "us-east-1"))
    ddb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "tenant_id", "AttributeType": "S"},
                               {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table = ddb.Table(table_name)
    table.wait_until_exists()
    try:
        _assert_negative_add_is_not_floored(table)
    finally:
        table.delete()


# ----------------------------------------------------------------- fact 4


def _assert_cross_table_transact_write_is_atomic(session_or_boto3, table_a, table_b) -> None:
    from botocore.exceptions import ClientError

    key_a = {"tenant_id": "l15", "sk": "fact4a"}
    key_b = {"tenant_id": "l15", "sk": "fact4b"}
    table_a.put_item(Item=dict(key_a, pool_headroom_microusd=100))
    table_b.put_item(Item=dict(key_b, hold="present"))

    client = session_or_boto3.client(
        "dynamodb", region_name=table_a.meta.client.meta.region_name)

    # A transaction whose SECOND item's condition is guaranteed to fail (the HOLD
    # already exists under attribute_not_exists) — the FIRST item's headroom ADD
    # must NOT have been applied when the whole transaction cancels.
    with pytest.raises(ClientError) as excinfo:
        client.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": table_a.table_name,
                "Key": {"tenant_id": {"S": "l15"}, "sk": {"S": "fact4a"}},
                "UpdateExpression": "ADD pool_headroom_microusd :neg",
                "ExpressionAttributeValues": {":neg": {"N": "-100"}},
            }},
            {"Put": {
                "TableName": table_b.table_name,
                "Item": {"tenant_id": {"S": "l15"}, "sk": {"S": "fact4b"}, "hold": {"S": "x"}},
                "ConditionExpression": "attribute_not_exists(sk)",
            }},
        ])
    assert excinfo.value.response["Error"]["Code"] == "TransactionCanceledException"

    item_a = table_a.get_item(Key=key_a, ConsistentRead=True)["Item"]
    assert int(item_a["pool_headroom_microusd"]) == 100, (
        "a cancelled cross-table TransactWriteItems applied the FIRST item's "
        "write anyway — this is exactly the non-atomicity the headroom design "
        "(pool ADD + HOLD Put in one transaction) depends on NOT happening. "
        "moto is documented as looser about transactions than real AWS; this "
        "is the fact CONTRACT-F4-claims (F4's contract document) calls out as needing an approval-"
        "exercised real run, not just a bare pytest pass, because a single "
        "sequential run cannot induce the adversarial timing a partial-apply "
        "bug would need real concurrent writers to expose."
    )


def test_a_cross_table_transact_write_is_atomic_moto(facts_table):
    import boto3

    table_a, table_b = facts_table
    _assert_cross_table_transact_write_is_atomic(boto3, table_a, table_b)


@pytest.mark.live
def test_a_cross_table_transact_write_is_atomic_live():
    if not os.getenv(_LIVE_FLAG):
        pytest.skip(f"set {_LIVE_FLAG}=1 (with AWS credentials) to re-measure the "
                    f"four DynamoDB facts on real AWS")
    import boto3
    session = real_session(boto3)
    if session is None:
        pytest.skip("no real AWS credentials available")
    region = os.getenv("STRATOCLAVE_REGION", "us-east-1")
    ddb = session.resource("dynamodb", region_name=region)
    suffix = uuid.uuid4().hex[:8]
    name_a = f"stratoclave-f4-live-a-{suffix}"
    name_b = f"stratoclave-f4-live-b-{suffix}"
    for name in (name_a, name_b):
        ddb.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "tenant_id", "AttributeType": "S"},
                                   {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    table_a, table_b = ddb.Table(name_a), ddb.Table(name_b)
    table_a.wait_until_exists()
    table_b.wait_until_exists()
    try:
        _assert_cross_table_transact_write_is_atomic(session, table_a, table_b)
    finally:
        table_a.delete()
        table_b.delete()
