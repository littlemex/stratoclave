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
        from mvp._pipeline import IdempotencyKeyReuse, _idemp_identity_or_raise

        row = {
            "request_fingerprint": "fp-1",
            "idempotency_key": "invoice?a",
            "authorization_id": "a", "hold_id": "h", "hold_sk": "HOLD#x",
            "period": "2026-03", "amount_microusd": 1, "expires_at": 1,
        }
        with pytest.raises(IdempotencyKeyReuse):
            _idemp_identity_or_raise(row, "fp-1", idempotency_key="invoice/a")

    def test_the_same_key_replays(self):
        from mvp._pipeline import _idemp_identity_or_raise, _idemp_result

        row = {
            "request_fingerprint": "fp-1",
            "idempotency_key": KEY,
            "authorization_id": "a", "hold_id": "h", "hold_sk": "HOLD#x",
            "period": "2026-03", "amount_microusd": 1, "expires_at": 1,
        }
        _idemp_identity_or_raise(row, "fp-1", idempotency_key=KEY)
        out = _idemp_result(row)
        assert out.replayed is True and out.authorization_id == "a"

    def test_a_row_with_no_stored_key_is_not_replayed(self):
        """Every row this code writes carries the raw key; an absent one is a
        partial or foreign write, and replaying it would hand back an
        authorization nobody can show belongs to this key."""
        from mvp._pipeline import IdempotencyKeyReuse, _idemp_identity_or_raise

        row = {
            "request_fingerprint": "fp-1",
            "authorization_id": "a", "hold_id": "h", "hold_sk": "HOLD#x",
            "period": "2026-03", "amount_microusd": 1, "expires_at": 1,
        }
        with pytest.raises(IdempotencyKeyReuse):
            _idemp_identity_or_raise(row, "fp-1", idempotency_key=KEY)


# --------------------------------------------------------------------------- C5.4


class TestARetryCanTellCommittedFromNotCommitted:
    """C5.4, for both protocols.

    The transactional path writes the record inside the reserve transaction, so its
    presence means the debit landed. The PENDING protocol writes it BEFORE the
    commit point, so its presence means only that an attempt began — and the entry
    point used to replay any readable record as an authorization, which handed a
    live `authorization_id` back for a debit that had been REFUSED. There was no
    configuration of that protocol in which the clause held.

    The verdict now comes from `_commit_evidence`: a pool marker, an activated hold,
    a terminal event, or a RESERVE event. Any one is conclusive and each protocol
    leaves at least one, so the same resolver answers for both.
    """

    def _seed(self, tenant, limit):
        from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
        from dynamo.user_tenants import UserTenantsRepository

        period = current_period()
        UserTenantsRepository().ensure(user_id=f"u-{tenant}", tenant_id=tenant,
                                       role="user", total_credit=10 ** 9)
        TenantBudgetsRepository().set_manual_limit(
            tenant_id=tenant, period=period, manual_limit_microusd=limit)
        return period

    def _authorize(self, tenant, amount, key):
        from mvp import _pipeline
        from mvp.billing_authorize import encode_authorization_id

        return _pipeline.reserve_external_authorization(
            tenant_id=tenant, amount_microusd=amount, idempotency_key=key,
            request_fingerprint=f"fp-{key}",
            authorization_id_factory=lambda h, p, sk: encode_authorization_id(
                hold_id=h, period=p, hold_sk=sk),
            ttl_seconds=3600,
        )

    def test_a_refused_authorize_replays_as_refused_under_pending(self, dynamodb_mock,
                                                                 monkeypatch):
        """The failure this clause is about. The intent row exists because the
        attempt began; the debit was refused for want of headroom. A retry with the
        same key must get the same refusal, not an authorization for money the pool
        never held."""
        import fastapi
        from dynamo.tenant_budgets import TenantBudgetsRepository
        from mvp import _pipeline

        monkeypatch.setattr(_pipeline, "_RESERVE_PROTOCOL", "pending")
        tenant = "c54-refused"
        period = self._seed(tenant, limit=1_000)

        for attempt in range(2):
            with pytest.raises(fastapi.HTTPException) as ei:
                self._authorize(tenant, 500_000, "same-key")
            assert ei.value.status_code == 402, f"attempt {attempt}"
        # And no debit, on either attempt.
        pool = TenantBudgetsRepository().pool_summary(tenant, period)
        assert int(pool["pool_reserved_microusd"]) == 0
        assert int(pool["pool_settled_microusd"]) == 0

    def test_an_in_flight_attempt_answers_retry_rather_than_success(self, dynamodb_mock,
                                                                   monkeypatch):
        """An intent written and the commit not yet reached is the ambiguous state,
        and the honest answer is 503 with the same key — not a verdict either way.
        Reporting success here is the fail-open; reporting 402 would refuse a
        reservation that may be about to land."""
        import fastapi
        from mvp import _pipeline

        monkeypatch.setattr(_pipeline, "_RESERVE_PROTOCOL", "pending")
        tenant = "c54-inflight"
        self._seed(tenant, limit=10 ** 9)

        # Stop the attempt exactly between the intent and the commit point.
        def _die(*a, **kw):
            raise RuntimeError("task killed after the intent, before the commit")

        monkeypatch.setattr(_pipeline, "_pending_commit_transact", _die)
        with pytest.raises(RuntimeError):
            self._authorize(tenant, 400_000, "inflight-key")

        monkeypatch.undo()
        monkeypatch.setattr(_pipeline, "_RESERVE_PROTOCOL", "pending")
        with pytest.raises(fastapi.HTTPException) as ei:
            self._authorize(tenant, 400_000, "inflight-key")
        assert ei.value.status_code == 503
        assert ei.value.detail["reason"] == "pool_reservation_in_flight"

    def test_a_committed_reservation_still_replays_under_pending(self, dynamodb_mock,
                                                                monkeypatch):
        """The other direction, so the fix is not simply refusing everything: a
        debit that DID land replays the original authorization."""
        from mvp import _pipeline

        monkeypatch.setattr(_pipeline, "_RESERVE_PROTOCOL", "pending")
        tenant = "c54-committed"
        period = self._seed(tenant, limit=10 ** 9)
        first = self._authorize(tenant, 300_000, "ok-key")
        again = self._authorize(tenant, 300_000, "ok-key")
        assert again.replayed is True
        assert again.authorization_id == first.authorization_id
        from dynamo.tenant_budgets import TenantBudgetsRepository
        assert int(TenantBudgetsRepository().pool_summary(
            tenant, period)["pool_reserved_microusd"]) == 300_000

    def test_a_terminal_is_evidence_after_every_other_trace_is_gone(self, dynamodb_mock,
                                                                   monkeypatch):
        """An ending is proof the beginning happened. Once an authorization has been
        captured the hold is deleted and the marker settled, so an authorize retry
        arriving then has only the terminal to go on — and it is enough. Without
        this witness the retry would answer 404 for an authorization that was
        charged, which is the same defect wearing a different status code."""
        from mvp import _pipeline

        monkeypatch.setattr(_pipeline, "_RESERVE_PROTOCOL", "pending")
        tenant = "c54-terminal"
        self._seed(tenant, limit=10 ** 9)
        first = self._authorize(tenant, 200_000, "cap-key")

        ctx = _pipeline.rehydrate_reservation_context(
            tenant_id=tenant, period=first.period, hold_id=first.hold_id,
            hold_sk=first.hold_sk)
        assert ctx is not None
        from mvp.billing_authorize import _settle_external
        _settle_external(ctx, 150_000)

        again = self._authorize(tenant, 200_000, "cap-key")
        assert again.replayed is True
        assert again.authorization_id == first.authorization_id

    def test_the_transactional_path_is_unchanged(self, dynamodb_mock):
        """The default protocol writes the record inside the reserve transaction, so
        the resolver finds the RESERVE event it wrote there and replays. This is the
        regression guard on unifying the two resolvers."""
        tenant = "c54-txn"
        self._seed(tenant, limit=10 ** 9)
        first = self._authorize(tenant, 250_000, "txn-key")
        again = self._authorize(tenant, 250_000, "txn-key")
        assert again.replayed is True
        assert again.authorization_id == first.authorization_id
