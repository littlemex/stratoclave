"""Contract 3.1 — one terminal arbiter per reservation.

Two mechanisms can end a hold, and they used to arbitrate on different cells: the
PENDING-protocol reconciler on the marker's phase (`pool_credit_back`), and the
settle/release path on the hold row's mere existence. Retiring a reclaimed hold
leaves the ROW in place with `status=RECLAIMED`, so a settle arriving afterwards
found `attribute_exists(sk)` true, passed, and returned the same reservation a
second time — enlarging the tenant's effective budget.

The reaper already gates on the status (`reclaim_hold_txn_item`). This pins that
the settle side gates on the same cell, so the two paths cannot both believe they
are the one ending this reservation.
"""
from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period, hold_sk


TENANT = "termination-org"


def _low_level():
    return boto3.client("dynamodb", region_name="us-east-1")


class TestTheSettleSideReadsTheSameCellTheReaperDoes:

    def test_the_hold_delete_is_gated_on_the_status(self):
        repo = TenantBudgetsRepository()
        item = repo.hold_delete_txn_item(tenant_id=TENANT, sk="HOLD#x")
        cond = item["Delete"]["ConditionExpression"]
        assert "attribute_exists(sk)" in cond
        assert "#st" in cond and "ACTIVE" in str(item["Delete"]["ExpressionAttributeValues"])
        assert item["Delete"]["ExpressionAttributeNames"]["#st"] == "status"

    def test_a_retired_hold_cannot_be_settled_again(self, dynamodb_mock):
        """`retire_reclaimed_best_effort` flips a fenced hold to RECLAIMED and
        leaves the row. The reservation has already been returned by
        `pool_credit_back` at that point, so a settle that deletes the row and
        decrements `pool_reserved` again returns it twice."""
        repo = TenantBudgetsRepository()
        period = current_period()
        repo.set_pool_limit(
            tenant_id=TENANT, period=period, pool_limit_microusd=1_000_000)

        sk = hold_sk(period, 1, "hold-retired")
        _low_level().put_item(
            TableName=repo._name,
            Item={
                "tenant_id": {"S": TENANT},
                "sk": {"S": sk},
                "amount_microusd": {"N": "5000"},
                "status": {"S": "RECLAIMED"},
            },
        )

        with pytest.raises(ClientError) as e:
            _low_level().transact_write_items(
                TransactItems=[repo.hold_delete_txn_item(tenant_id=TENANT, sk=sk)]
            )
        assert e.value.response["Error"]["Code"] == "TransactionCanceledException"

    def test_a_transactional_hold_with_no_status_still_settles(self, dynamodb_mock):
        """Pre-PENDING holds carry no status attribute at all. The gate must be
        inert for them, or every settle in the shipped configuration starts
        cancelling."""
        repo = TenantBudgetsRepository()
        period = current_period()
        repo.set_pool_limit(
            tenant_id=TENANT, period=period, pool_limit_microusd=1_000_000)

        sk = hold_sk(period, 1, "hold-plain")
        _low_level().put_item(
            TableName=repo._name,
            Item={
                "tenant_id": {"S": TENANT},
                "sk": {"S": sk},
                "amount_microusd": {"N": "5000"},
            },
        )
        _low_level().transact_write_items(
            TransactItems=[repo.hold_delete_txn_item(tenant_id=TENANT, sk=sk)]
        )
        assert _low_level().get_item(
            TableName=repo._name,
            Key={"tenant_id": {"S": TENANT}, "sk": {"S": sk}},
        ).get("Item") is None

    def test_an_active_hold_settles(self, dynamodb_mock):
        repo = TenantBudgetsRepository()
        period = current_period()
        repo.set_pool_limit(
            tenant_id=TENANT, period=period, pool_limit_microusd=1_000_000)

        sk = hold_sk(period, 1, "hold-active")
        _low_level().put_item(
            TableName=repo._name,
            Item={
                "tenant_id": {"S": TENANT},
                "sk": {"S": sk},
                "amount_microusd": {"N": "5000"},
                "status": {"S": "ACTIVE"},
            },
        )
        _low_level().transact_write_items(
            TransactItems=[repo.hold_delete_txn_item(tenant_id=TENANT, sk=sk)]
        )
        assert _low_level().get_item(
            TableName=repo._name,
            Key={"tenant_id": {"S": TENANT}, "sk": {"S": sk}},
        ).get("Item") is None
