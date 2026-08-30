"""Contract 5 — one idempotency key means one authorization, for all time.

  C5.1 A retry that crosses a billing period, a deploy, or a protocol-mode change
       resolves to the SAME authorization.
  C5.2 The mapping from a client key to a stored row is injective.
  C5.3 A replay returns the original outcome; it never mints a second money move.

The record was stored inside the money partition (`TENANT#<t>#P#<period>`), so the
key's IDENTITY was period-scoped while the docstring called the mapping permanent.
A compensating read covered exactly one boundary, justified by the hold's 24h TTL —
but the hold's lifetime bounds the AUTHORIZATION, not the key. A retry two periods
later found nothing and minted a second hold, so one key could produce two charges
and the ledger could not answer "what did key K charge" with one number.

The identity is now period-independent: the row lives under a per-tenant partition
and carries the period as data, so a replay still addresses the right money
partition. Rows written before this change are still read where they were written.
"""
from __future__ import annotations

import boto3
import pytest

from dynamo.credit_ledger import (
    CreditLedgerRepository,
    idemp_pk,
    idemp_sk,
    ledger_pk,
)


TENANT = "idemp-org"
KEY = "invoice/2026-03/a"


def _put_via_txn_item(repo, *, period: str, key: str = KEY, auth_id="auth-1"):
    """Write the IDEMP row exactly as the reserve transaction writes it."""
    item = repo.idemp_txn_item(
        tenant_id=TENANT,
        period=period,
        idempotency_key=key,
        hold_id="hold-1",
        hold_sk="HOLD#x",
        authorization_id=auth_id,
        amount_microusd=50_000,
        expires_at_epoch=1_800_000_000,
        capture_mode="amount",
        request_fingerprint="fp-1",
    )
    boto3.client("dynamodb", region_name="us-east-1").transact_write_items(
        TransactItems=[item])


class TestTheKeysIdentityDoesNotExpireWithThePeriod:

    def test_the_record_is_not_stored_under_a_period(self, dynamodb_mock):
        repo = CreditLedgerRepository()
        item = repo.idemp_txn_item(
            tenant_id=TENANT, period="2026-03", idempotency_key=KEY,
            hold_id="h", hold_sk="HOLD#x", authorization_id="a",
            amount_microusd=1, expires_at_epoch=1, capture_mode="amount",
            request_fingerprint="fp",
        )
        pk = item["Put"]["Item"]["pk"]["S"]
        assert "2026-03" not in pk, (
            "the key's identity must not be scoped to a billing period")
        assert pk == idemp_pk(TENANT)
        # The period is still recorded, because a replay has to address the money
        # partition the authorization actually lives in.
        assert item["Put"]["Item"]["period"]["S"] == "2026-03"

    def test_a_retry_two_periods_later_finds_the_original(self, dynamodb_mock):
        repo = CreditLedgerRepository()
        _put_via_txn_item(repo, period="2026-03")
        row = repo.get_idemp(
            tenant_id=TENANT, period="2026-05", idempotency_key=KEY)
        assert row is not None, (
            "a retry two periods after the original minted a second authorization")
        assert str(row["authorization_id"]) == "auth-1"
        assert str(row["period"]) == "2026-03"

    def test_a_duplicate_key_still_cannot_write_twice(self, dynamodb_mock):
        from botocore.exceptions import ClientError

        repo = CreditLedgerRepository()
        _put_via_txn_item(repo, period="2026-03")
        with pytest.raises(ClientError) as e:
            _put_via_txn_item(repo, period="2026-05", auth_id="auth-2")
        assert e.value.response["Error"]["Code"] == "TransactionCanceledException", (
            "the condition that makes 'row exists ⟺ this reserve committed' must "
            "still refuse a second authorization for one key")

    def test_a_row_written_under_the_old_period_partition_is_still_found(
            self, dynamodb_mock):
        """Rows already in the wild live in the money partition. A deploy must not
        make them unreachable — that would mint a second hold for exactly the keys
        the change is meant to protect."""
        repo = CreditLedgerRepository()
        boto3.resource("dynamodb", region_name="us-east-1").Table(
            repo._name
        ).put_item(Item={
            "pk": ledger_pk(TENANT, "2026-03"),
            "sk": idemp_sk(KEY),
            "event_type": "IDEMP",
            "tenant_id": TENANT,
            "period": "2026-03",
            "idempotency_key": KEY,
            "hold_id": "hold-legacy",
            "hold_sk": "HOLD#legacy",
            "authorization_id": "auth-legacy",
            "amount_microusd": 50_000,
            "expires_at": 1_800_000_000,
            "capture_mode": "amount",
            "request_fingerprint": "fp-1",
        })
        row = repo.get_idemp(
            tenant_id=TENANT, period="2026-03", idempotency_key=KEY)
        assert row is not None and str(row["authorization_id"]) == "auth-legacy"


class TestAReplayVerifiesTheKeyItself:

    def test_a_stored_key_that_differs_is_not_replayed(self):
        """The digest is collision-free, but the pre-digest sanitised sort key was
        not: two distinct keys could land on one row. The row stores the raw key
        precisely so a replay can check it rather than trusting the address it was
        found at."""
        from mvp._pipeline import IdempotencyKeyReuse, _idemp_replay

        row = {
            "request_fingerprint": "fp-1",
            "idempotency_key": "invoice?a",
            "authorization_id": "a", "hold_id": "h", "hold_sk": "HOLD#x",
            "period": "2026-03", "amount_microusd": 1, "expires_at": 1,
        }
        with pytest.raises(IdempotencyKeyReuse):
            _idemp_replay(row, "fp-1", idempotency_key="invoice/a")

    def test_the_same_key_replays(self):
        from mvp._pipeline import _idemp_replay

        row = {
            "request_fingerprint": "fp-1",
            "idempotency_key": KEY,
            "authorization_id": "a", "hold_id": "h", "hold_sk": "HOLD#x",
            "period": "2026-03", "amount_microusd": 1, "expires_at": 1,
        }
        out = _idemp_replay(row, "fp-1", idempotency_key=KEY)
        assert out.replayed is True and out.authorization_id == "a"

    def test_a_row_with_no_stored_key_is_not_replayed(self):
        """Every row this code writes carries the raw key; an absent one is a
        partial or foreign write, and replaying it would hand back an
        authorization nobody can show belongs to this key."""
        from mvp._pipeline import IdempotencyKeyReuse, _idemp_replay

        row = {
            "request_fingerprint": "fp-1",
            "authorization_id": "a", "hold_id": "h", "hold_sk": "HOLD#x",
            "period": "2026-03", "amount_microusd": 1, "expires_at": 1,
        }
        with pytest.raises(IdempotencyKeyReuse):
            _idemp_replay(row, "fp-1", idempotency_key=KEY)
