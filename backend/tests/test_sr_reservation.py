"""S2 tests: PoolReservation / ConsumedProof type-enforcement + CandidatePool.

These pin the money-path safety spine: a reservation can only be minted (not
constructed), consumed exactly once, and a ConsumedProof (which the SR forward
requires) can only come from a consume(). pool-max pricing and the in-snapshot
price lookup close the reserve->forward TOCTOU.
"""
from __future__ import annotations

import threading

import pytest

from mvp.sr.reservation import (
    CandidatePool,
    ConsumedProof,
    PoolReservation,
    PricedCandidate,
    ReservationAlreadyConsumed,
)


def _pool():
    return CandidatePool(
        tenant_id="acme",
        models=(
            PricedCandidate("claude-haiku-4-5", 800_000, "v1"),
            PricedCandidate("claude-opus-4-7", 15_000_000, "v1"),
        ),
        pool_hash="deadbeef",
        snapshot_at_ms=1_700_000_000_000,
    )


def test_pool_max_is_the_dearest_member():
    assert _pool().max_unit_price() == 15_000_000


def test_pool_price_of_in_and_out_of_snapshot():
    p = _pool()
    assert p.price_of("claude-opus-4-7") == 15_000_000
    assert p.price_of("some-model-sr-invented") is None  # ⇒ settle at reserve amt


def test_empty_pool_has_no_max():
    empty = CandidatePool("acme", (), "0", 0)
    with pytest.raises(ValueError):
        empty.max_unit_price()


def test_pool_reservation_has_no_public_constructor():
    with pytest.raises(TypeError):
        PoolReservation(reservation_id="x")


def test_consumed_proof_has_no_public_constructor():
    with pytest.raises(TypeError):
        ConsumedProof()


def test_mint_then_consume_once_yields_proof():
    r = PoolReservation._mint(reservation_id="r1", pool=_pool(),
                              max_tokens_cap=1024, reserve_amount_microusd=999)
    proof = r.consume()
    assert isinstance(proof, ConsumedProof)
    assert proof.reservation_id == "r1"
    assert proof.max_tokens_cap == 1024
    assert proof.reserve_amount_microusd == 999
    assert proof.pool.pool_hash == "deadbeef"


def test_double_consume_raises():
    r = PoolReservation._mint(reservation_id="r2", pool=_pool(),
                              max_tokens_cap=512, reserve_amount_microusd=1)
    r.consume()
    with pytest.raises(ReservationAlreadyConsumed):
        r.consume()


# --------------------------------------------------------------- P2-3 from_rates
def test_from_rates_picks_conservative_max():
    pc = PricedCandidate.from_rates("m", input_per_mtok=800_000,
                                    output_per_mtok=4_000_000, price_version="v")
    assert pc.unit_price_microusd_per_mtok == 4_000_000  # output-heavy dominates


def test_from_rates_rejects_negative_rate():
    # a negative price would break the pool-max upper bound; refuse at construction.
    with pytest.raises(ValueError):
        PricedCandidate.from_rates("m", input_per_mtok=-1,
                                   output_per_mtok=100, price_version="v")


# --------------------------------------------------------------- P1-1 immutability
def test_reservation_is_immutable_after_mint():
    # P1-1: a minted reservation cannot have its money-bearing fields rewritten,
    # and a consumed latch cannot be reset to forge a second consume.
    r = PoolReservation._mint(reservation_id="rimm", pool=_pool(),
                              max_tokens_cap=1024, reserve_amount_microusd=999)
    with pytest.raises(AttributeError):
        r.reserve_amount_microusd = 0
    with pytest.raises(AttributeError):
        r.max_tokens_cap = 10 ** 9
    r.consume()
    with pytest.raises(AttributeError):
        r._consumed = False          # cannot "un-consume" to double-mint
    with pytest.raises(AttributeError):
        del r.reserve_amount_microusd


def test_consumed_proof_is_immutable():
    # P1-1: the proof the forward trusts cannot be tampered post-mint (cap/amount).
    proof = PoolReservation._mint(reservation_id="pimm", pool=_pool(),
                                  max_tokens_cap=8, reserve_amount_microusd=120).consume()
    with pytest.raises(AttributeError):
        proof.max_tokens_cap = 10 ** 9
    with pytest.raises(AttributeError):
        proof.reserve_amount_microusd = 0
    with pytest.raises(AttributeError):
        del proof.pool


# --------------------------------------------------------------- P1-2 thread-safe consume
def test_concurrent_consume_yields_exactly_one_proof():
    # P1-2: N threads race to consume the SAME reservation; exactly one must win a
    # ConsumedProof and the rest must raise. Without the lock the check-then-set
    # TOCTOU could mint two proofs (two forwards on one reserve = double spend).
    r = PoolReservation._mint(reservation_id="rrace", pool=_pool(),
                              max_tokens_cap=256, reserve_amount_microusd=42)
    proofs: list[ConsumedProof] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()               # maximize contention on the check-then-set
        try:
            proofs.append(r.consume())
        except ReservationAlreadyConsumed as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(proofs) == 1
    assert len(errors) == 15


def test_reserve_amount_covers_pool_max_times_cap():
    # the reserve amount a caller mints must be >= pool-max * cap so that
    # final_charge (<= pool_max * actual_tokens <= pool_max * cap) never exceeds
    # it — the money fail-closed upper bound.
    pool = _pool()
    cap = 1024
    amt = pool.max_unit_price() * cap // 1_000_000
    r = PoolReservation._mint(reservation_id="r3", pool=pool,
                              max_tokens_cap=cap, reserve_amount_microusd=amt)
    # a settle at the dearest model for the full cap equals the reserve; anything
    # cheaper or shorter is strictly less. Never above.
    assert r.reserve_amount_microusd == pool.price_of("claude-opus-4-7") * cap // 1_000_000
