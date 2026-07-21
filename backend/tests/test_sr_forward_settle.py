"""S3+S4 money-path verification via the fake SR (no real hardware).

Proves, over every SR failure mode, the two invariants that make SR safe to ship:
  * no forward without a consumed reservation (type-enforced, checked here);
  * final_charge <= reserve_amount, ALWAYS (fail-closed ladder for out-of-snapshot
    / missing usage / missing replay).
"""
from __future__ import annotations

import pytest

from mvp.serving import semantic_router as srv
from mvp.serving.semantic_router import SrForwardError, SrForwardRequest, forward_to_sr
from mvp.sr.reservation import CandidatePool, PoolReservation, PricedCandidate
from mvp.sr.settle import settle_charge
from tests.fakes import fake_sr


@pytest.fixture(autouse=True)
def _reset():
    srv.reset_for_test()
    yield
    srv.reset_for_test()


def _pool():
    return CandidatePool(
        tenant_id="acme",
        models=(
            PricedCandidate("claude-haiku-4-5", 800_000, "v1"),
            PricedCandidate("claude-opus-4-7", 15_000_000, "v1"),
        ),
        pool_hash="hp", snapshot_at_ms=1,
    )


def _reservation(cap=1024):
    pool = _pool()
    amt = pool.max_unit_price() * cap // 1_000_000   # pool-max upper bound
    return PoolReservation._mint(reservation_id="r", pool=pool,
                                 max_tokens_cap=cap, reserve_amount_microusd=amt)


def _req(cap=1024):
    return SrForwardRequest(tenant_id="acme", span_id="span-1", logical_model="auto",
                            messages=(), max_tokens_cap=cap, pool_hash="hp")


def _normalize(raw):
    known = {"claude-haiku-4-5", "claude-opus-4-7"}
    return raw if raw in known else None


# ---------------------------------------------------------------- forward gating
def test_forward_requires_matching_cap():
    srv.set_transport_hook(fake_sr.normal())
    proof = _reservation(cap=1024).consume()
    # a request whose cap != the reserved cap is refused (upper-bound guard).
    with pytest.raises(SrForwardError):
        forward_to_sr(proof, _req(cap=2048))


def test_forward_propagates_span_id():
    captured: list = []
    srv.set_transport_hook(fake_sr.echoes_span_id(captured))
    proof = _reservation().consume()
    forward_to_sr(proof, _req())
    assert captured == ["span-1"]   # span_id reaches SR for replay join


def test_forward_timeout_raises_failopen():
    srv.set_transport_hook(fake_sr.timeout())
    proof = _reservation().consume()
    with pytest.raises(SrForwardError):
        forward_to_sr(proof, _req())


# ---------------------------------------------------------------- settle ladder
def test_settle_measured_within_reserve():
    proof = _reservation(cap=1024).consume()
    r = fake_sr.normal(chosen_model="claude-haiku-4-5", inp=100, out=50)(_req())
    charge = settle_charge(proof, billed_model_raw=r.chosen_model_raw,
                           normalize=_normalize,
                           input_tokens=r.usage_input_tokens,
                           output_tokens=r.usage_output_tokens)
    # haiku 0.8/Mtok × 150 tok = 120 microusd, well under the opus pool-max reserve.
    assert charge.basis == "measured"
    assert charge.billed_model == "claude-haiku-4-5"
    assert charge.charge_microusd == 800_000 * 150 // 1_000_000
    assert charge.charge_microusd <= charge.reserve_amount_microusd


def test_settle_out_of_snapshot_falls_back_to_reserve():
    proof = _reservation().consume()
    r = fake_sr.out_of_snapshot()(_req())
    charge = settle_charge(proof, billed_model_raw=r.chosen_model_raw,
                           normalize=_normalize,
                           input_tokens=r.usage_input_tokens,
                           output_tokens=r.usage_output_tokens)
    assert charge.basis == "reserve-fallback:unnormalizable"
    assert charge.charge_microusd == proof.reserve_amount_microusd


def test_settle_no_usage_falls_back_to_reserve():
    proof = _reservation().consume()
    charge = settle_charge(proof, billed_model_raw="claude-haiku-4-5",
                           normalize=_normalize, input_tokens=None, output_tokens=None)
    assert charge.basis == "reserve-fallback:no-usage"
    assert charge.charge_microusd == proof.reserve_amount_microusd


def test_settle_no_model_falls_back_to_reserve():
    proof = _reservation().consume()
    charge = settle_charge(proof, billed_model_raw=None, normalize=_normalize,
                           input_tokens=100, output_tokens=50)
    assert charge.charge_microusd == proof.reserve_amount_microusd


def test_settle_never_exceeds_reserve_even_on_overrun():
    # a pathological huge usage must clamp to the reserve (fail-closed), never bill above.
    proof = _reservation(cap=8).consume()   # tiny cap ⇒ tiny reserve
    charge = settle_charge(proof, billed_model_raw="claude-opus-4-7",
                           normalize=_normalize,
                           input_tokens=10_000_000, output_tokens=10_000_000)
    assert charge.charge_microusd == proof.reserve_amount_microusd
    assert charge.basis == "reserve-clamped"
