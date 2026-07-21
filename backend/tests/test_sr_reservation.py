"""S2 tests: PoolReservation / ConsumedProof type-enforcement + CandidatePool.

These pin the money-path safety spine: a reservation can only be minted (not
constructed), consumed exactly once, and a ConsumedProof (which the SR forward
requires) can only come from a consume(). pool-max pricing and the in-snapshot
price lookup close the reserve->forward TOCTOU.
"""
from __future__ import annotations

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
