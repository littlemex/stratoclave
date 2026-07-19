"""Property + unit tests for the counterfactual Savings Certificate engine.

Guards the honesty controls that make the number defensible (docs/design/
vsr-savings-certificate.md): net = gross - escalation exactly, escalation losses
are NEVER clipped away, every VSR-acted row lands in exactly one class, and
`followed` savings are not double-counted.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from mvp.learning.savings import counterfactual_row, summarize_savings

# A fake, monotonic price table so tests are pure (no live registry): micro-USD
# per token, cheap < mid < dear.
_RATE = {"cheap": 1, "mid": 5, "dear": 50}


def _price(pk, tin, tout):
    return (int(tin) + int(tout)) * _RATE[pk]


def _pkf(model):
    return {"m-cheap": "cheap", "m-mid": "mid", "m-dear": "dear",
            "b-cheap": "cheap", "b-mid": "mid", "b-dear": "dear"}.get(model)


def _row(decision="prefer", suggested="m-cheap", matched=True, billed="b-dear",
         cost=5000, tin=100, tout=100):
    return {"tenant_id": "t", "span_id": "s", "vsr_decision": decision,
            "suggested_model": suggested, "matched": matched,
            "billed_model_id": billed, "cost_microusd": cost,
            "input_tokens": tin, "output_tokens": tout}


# -------------------------------------------------------------- classification

def test_no_suggestion_is_out_of_base():
    cr = counterfactual_row(_row(decision="passthrough", suggested=None),
                            price=_price, pricing_key_for=_pkf)
    assert cr["class"] == "no_suggestion" and cr["saving_microusd"] == 0


def test_unmatched_is_coverage_gap():
    cr = counterfactual_row(_row(matched=False, billed=None, cost=None),
                            price=_price, pricing_key_for=_pkf)
    assert cr["class"] == "unmatched" and cr["saving_microusd"] == 0


def test_missing_tokens_is_data_gap():
    cr = counterfactual_row(_row(tin=None), price=_price, pricing_key_for=_pkf)
    assert cr["class"] == "no_tokens"


def test_unpriceable_suggested_model_is_gap():
    cr = counterfactual_row(_row(suggested="m-unknown"), price=_price, pricing_key_for=_pkf)
    assert cr["class"] == "unpriceable"


def test_followed_is_not_double_counted():
    # billed model already resolves to the suggested pricing key.
    cr = counterfactual_row(_row(suggested="m-cheap", billed="b-cheap", cost=200),
                            price=_price, pricing_key_for=_pkf)
    assert cr["class"] == "followed" and cr["saving_microusd"] == 0


def test_counterfactual_saving_when_vsr_cheaper():
    # suggested cheap (1/tok), billed dear (cost 5000 for 200 tok). cf = 200*1=200.
    cr = counterfactual_row(_row(suggested="m-cheap", billed="b-dear", cost=5000,
                                 tin=100, tout=100), price=_price, pricing_key_for=_pkf)
    assert cr["class"] == "counterfactual"
    assert cr["counterfactual_microusd"] == 200
    assert cr["saving_microusd"] == 4800   # 5000 billed - 200 counterfactual


def test_escalation_loss_is_surfaced_not_clipped():
    # VSR suggested DEAR, but a CHEAP model was actually billed -> following the
    # VSR would have cost MORE -> negative saving, must be surfaced.
    cr = counterfactual_row(_row(suggested="m-dear", billed="b-cheap", cost=200,
                                 tin=100, tout=100), price=_price, pricing_key_for=_pkf)
    assert cr["class"] == "counterfactual"
    assert cr["counterfactual_microusd"] == 10000   # 200 tok * 50
    assert cr["saving_microusd"] == -9800            # a LOSS, not clipped to 0


# -------------------------------------------------------------- summary honesty

def test_net_equals_gross_minus_escalation():
    win = _row(suggested="m-cheap", billed="b-dear", cost=5000)     # +4800
    loss = _row(suggested="m-dear", billed="b-cheap", cost=200)     # -9800
    s = summarize_savings([win, loss], price=_price, pricing_key_for=_pkf)
    assert s["gross_saving_microusd"] == 4800
    assert s["escalation_loss_microusd"] == 9800
    assert s["net_saving_microusd"] == 4800 - 9800   # == -5000, honestly negative


def test_quality_is_never_implied():
    s = summarize_savings([_row()], price=_price, pricing_key_for=_pkf)
    assert s["quality"]["measured"] is False


@given(
    rows=st.lists(
        st.fixed_dictionaries({
            "suggested": st.sampled_from(["m-cheap", "m-mid", "m-dear"]),
            "billed": st.sampled_from(["b-cheap", "b-mid", "b-dear"]),
            "tin": st.integers(min_value=0, max_value=100_000),
            "tout": st.integers(min_value=0, max_value=100_000),
            "cost": st.integers(min_value=0, max_value=10_000_000),
        }),
        min_size=0, max_size=40),
)
@settings(max_examples=300, deadline=None)
def test_summary_invariants_hold_over_random_rows(rows):
    joined = [_row(suggested=r["suggested"], billed=r["billed"], cost=r["cost"],
                   tin=r["tin"], tout=r["tout"]) for r in rows]
    s = summarize_savings(joined, price=_price, pricing_key_for=_pkf)
    # net == gross - escalation, exactly (no rounding drift, no clipping).
    assert s["net_saving_microusd"] == s["gross_saving_microusd"] - s["escalation_loss_microusd"]
    # gross and escalation are non-negative magnitudes.
    assert s["gross_saving_microusd"] >= 0 and s["escalation_loss_microusd"] >= 0
    # every row is classified into exactly one bucket (counts sum to input size).
    assert sum(s["class_counts"].values()) == len(joined)
    # priced base count == the "counterfactual" class count.
    assert s["priced_request_count"] == s["class_counts"].get("counterfactual", 0)
