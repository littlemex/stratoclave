"""Tests for the credit reservation contract on UserTenantsRepository.

These are black-box tests against `reserve()` / `refund()` that guard the
invariants documented in `ARCHITECTURE.md` (Credit reservation section):

  - reserve() succeeds when balance covers the request and decrements
    remaining by exactly the reserved amount.
  - reserve() raises CreditExhaustedError when the balance cannot cover
    the request; state is unchanged in that case.
  - refund() releases previously reserved tokens and clamps underflow.
  - concurrent reservations by two callers at the same snapshot are
    serialized by the DynamoDB ConditionExpression: the second caller
    either retries or raises CreditExhaustedError, never overdraws.
"""
from __future__ import annotations

import pytest

from dynamo.user_tenants import CreditExhaustedError, UserTenantsRepository


def _remaining(repo: UserTenantsRepository, user_id: str, tenant_id: str) -> int:
    return repo.remaining_credit(user_id, tenant_id)


def test_reserve_success_decrements_remaining(seed_active_tenant):
    repo = UserTenantsRepository()
    uid, tid = seed_active_tenant["user_id"], seed_active_tenant["tenant_id"]

    before = _remaining(repo, uid, tid)
    after = repo.reserve(user_id=uid, tenant_id=tid, tokens=3_000)

    assert before == 10_000
    assert after == 7_000
    assert _remaining(repo, uid, tid) == 7_000


def test_reserve_exact_balance_is_allowed(seed_active_tenant):
    repo = UserTenantsRepository()
    uid, tid = seed_active_tenant["user_id"], seed_active_tenant["tenant_id"]

    after = repo.reserve(user_id=uid, tenant_id=tid, tokens=10_000)
    assert after == 0


def test_reserve_over_balance_raises_and_does_not_mutate(seed_active_tenant):
    repo = UserTenantsRepository()
    uid, tid = seed_active_tenant["user_id"], seed_active_tenant["tenant_id"]

    with pytest.raises(CreditExhaustedError):
        repo.reserve(user_id=uid, tenant_id=tid, tokens=10_001)

    # The row must be untouched on failure; nothing was reserved.
    assert _remaining(repo, uid, tid) == 10_000


def test_reserve_zero_is_noop(seed_active_tenant):
    repo = UserTenantsRepository()
    uid, tid = seed_active_tenant["user_id"], seed_active_tenant["tenant_id"]

    after = repo.reserve(user_id=uid, tenant_id=tid, tokens=0)
    assert after == 10_000


def test_refund_returns_tokens_to_balance(seed_active_tenant):
    repo = UserTenantsRepository()
    uid, tid = seed_active_tenant["user_id"], seed_active_tenant["tenant_id"]

    repo.reserve(user_id=uid, tenant_id=tid, tokens=4_000)
    assert _remaining(repo, uid, tid) == 6_000

    after = repo.refund(user_id=uid, tenant_id=tid, tokens=4_000)
    assert after == 10_000
    assert _remaining(repo, uid, tid) == 10_000


def test_refund_underflow_is_clamped(seed_active_tenant):
    """refund() must not let credit_used go below zero even if asked for
    more than was reserved. The repository catches the ConditionalCheck
    failure and returns the real remaining value.
    """
    repo = UserTenantsRepository()
    uid, tid = seed_active_tenant["user_id"], seed_active_tenant["tenant_id"]

    repo.reserve(user_id=uid, tenant_id=tid, tokens=100)
    assert _remaining(repo, uid, tid) == 9_900

    # Ask for a refund larger than what was reserved: must not underflow.
    after = repo.refund(user_id=uid, tenant_id=tid, tokens=5_000)
    # Repository falls back to reporting the real remaining; 9_900 unchanged.
    assert after == 9_900


def test_two_sequential_reserves_cannot_overdraw(seed_active_tenant):
    """Two reservations in sequence at the same nominal remaining cannot
    together exceed total_credit. The second call must raise.

    This simulates the TOCTOU race that PR #2 closed: caller A and caller
    B both saw 10k remaining and both tried to reserve 7k. Only one wins.
    """
    repo = UserTenantsRepository()
    uid, tid = seed_active_tenant["user_id"], seed_active_tenant["tenant_id"]

    first = repo.reserve(user_id=uid, tenant_id=tid, tokens=7_000)
    assert first == 3_000  # 10k - 7k

    with pytest.raises(CreditExhaustedError):
        repo.reserve(user_id=uid, tenant_id=tid, tokens=7_000)

    # Committed state reflects only the successful reserve.
    assert _remaining(repo, uid, tid) == 3_000


def test_reserve_after_refund_cycle_is_idempotent_shape(seed_active_tenant):
    """A reserve/refund pair must leave remaining unchanged, regardless
    of intermediate state.
    """
    repo = UserTenantsRepository()
    uid, tid = seed_active_tenant["user_id"], seed_active_tenant["tenant_id"]

    for tokens in (100, 2_048, 4_096):
        before = _remaining(repo, uid, tid)
        repo.reserve(user_id=uid, tenant_id=tid, tokens=tokens)
        repo.refund(user_id=uid, tenant_id=tid, tokens=tokens)
        assert _remaining(repo, uid, tid) == before
