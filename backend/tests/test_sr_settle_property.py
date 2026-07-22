"""Property (Hypothesis) verification of the SR settle money invariant.

INV-MONEY: over the WHOLE input domain — any candidate pool, any billed model
(in or out of snapshot), any usage (including adversarial overruns), any
missing-usage/missing-model case — the SR charge NEVER exceeds the reserve
amount. This is the mathematical core of "money fail-closed for SR": whatever SR
does, the tenant is never billed above what was atomically reserved.

INV-DETERMINISM: settle_charge is a pure function of its inputs (same inputs →
same charge), so a replay recomputes an identical figure.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from mvp.sr.reservation import CandidatePool, PoolReservation, PricedCandidate
from mvp.sr.settle import settle_charge

_MODELS = ["m-haiku", "m-sonnet", "m-opus", "m-unknown-sr-invented"]
_KNOWN = {"m-haiku", "m-sonnet", "m-opus"}


def _normalize(raw):
    return raw if raw in _KNOWN else None


@st.composite
def _scenario(draw):
    # a non-empty priced pool drawn from the known models.
    n = draw(st.integers(min_value=1, max_value=3))
    chosen = draw(st.lists(st.sampled_from(sorted(_KNOWN)), min_size=n, max_size=n,
                           unique=True))
    prices = {m: draw(st.integers(min_value=1, max_value=100_000_000)) for m in chosen}
    pool = CandidatePool(
        tenant_id="t",
        models=tuple(PricedCandidate(m, prices[m], "v") for m in chosen),
        pool_hash="h", snapshot_at_ms=1,
    )
    cap = draw(st.integers(min_value=1, max_value=1_000_000))
    # reserve at pool-max × cap — the upper bound the reserve path always uses.
    reserve = pool.max_unit_price() * cap // 1_000_000
    billed = draw(st.one_of(st.none(), st.sampled_from(_MODELS)))
    inp = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=5_000_000)))
    out = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=5_000_000)))
    return pool, cap, reserve, billed, inp, out


@given(_scenario())
@settings(max_examples=400)
def test_charge_never_exceeds_reserve(scn):
    pool, cap, reserve, billed, inp, out = scn
    proof = PoolReservation._mint(reservation_id="r", pool=pool,
                                  max_tokens_cap=cap,
                                  reserve_amount_microusd=reserve).consume()
    charge = settle_charge(proof, billed_model_raw=billed, normalize=_normalize,
                           input_tokens=inp, output_tokens=out)
    # THE invariant: never bill above the atomic reservation.
    assert 0 <= charge.charge_microusd <= reserve


@given(_scenario())
@settings(max_examples=200)
def test_settle_is_deterministic(scn):
    pool, cap, reserve, billed, inp, out = scn

    def _charge():
        proof = PoolReservation._mint(reservation_id="r", pool=pool,
                                      max_tokens_cap=cap,
                                      reserve_amount_microusd=reserve).consume()
        return settle_charge(proof, billed_model_raw=billed, normalize=_normalize,
                             input_tokens=inp, output_tokens=out)

    a, b = _charge(), _charge()
    assert a.charge_microusd == b.charge_microusd
    assert a.basis == b.basis and a.billed_model == b.billed_model


@given(
    st.integers(min_value=0, max_value=100_000_000),   # input rate
    st.integers(min_value=0, max_value=100_000_000),   # output rate
    st.integers(min_value=0, max_value=5_000_000),      # input tokens
    st.integers(min_value=0, max_value=5_000_000),      # output tokens
)
@settings(max_examples=300)
def test_from_rates_single_rate_upper_bounds_two_column_cost(in_rate, out_rate, inp, out):
    # P2-3: the conservative single rate = max(input, output) must upper-bound the
    # true two-column cost for EVERY token split, so a single-rate pool-max reserve
    # can never under-estimate an output-heavy response into a silent operator loss.
    from mvp.sr.reservation import PricedCandidate
    pc = PricedCandidate.from_rates("m", input_per_mtok=in_rate,
                                    output_per_mtok=out_rate, price_version="v")
    single = pc.unit_price_microusd_per_mtok * (inp + out) // 1_000_000
    two_column = (inp * in_rate) // 1_000_000 + (out * out_rate) // 1_000_000
    assert single >= two_column
    assert pc.unit_price_microusd_per_mtok == max(in_rate, out_rate)


@given(_scenario())
@settings(max_examples=200)
def test_in_snapshot_measured_is_at_most_poolmax_times_cap(scn):
    # sanity: for an in-snapshot model with real usage, the measured figure is
    # bounded by pool-max × cap = reserve, so the clamp is a safety net, not the
    # normal path (proves the reserve is a genuine upper bound, not slack).
    pool, cap, reserve, _billed, _i, _o = scn
    proof = PoolReservation._mint(reservation_id="r", pool=pool,
                                  max_tokens_cap=cap,
                                  reserve_amount_microusd=reserve).consume()
    model = pool.models[0].model_id
    # usage within the reserved cap (the honest case: SR respected max_tokens).
    charge = settle_charge(proof, billed_model_raw=model, normalize=_normalize,
                           input_tokens=0, output_tokens=cap)
    assert charge.charge_microusd <= reserve
    assert charge.basis == "measured"
