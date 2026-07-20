"""Property + unit tests for the counterfactual Savings Certificate engine.

Guards the honesty controls that make the number defensible (docs/design/
vsr-savings-certificate.md, Fable review): model-vs-model at ONE rate snapshot
(no rate-drift / cache asymmetry), net = positive - negative exactly with no
clipping, every row in exactly one class, `followed` = same bedrock id (not just
same pricing key), basis_drift excluded, no_cost never a fake loss.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from mvp.learning.savings import counterfactual_row, summarize_savings
from mvp.vsr.client import DECISION_PREFER_APPLIED as _PREFER
from mvp.vsr.client import DECISION_SHADOW_ADVISED as _SHADOW

# fake, monotonic rate table: micro-USD per token. cheap < mid < dear.
_RATE = {"cheap": 1, "mid": 5, "dear": 50}


def _price(pk, tin, tout):
    return (int(tin) + int(tout)) * _RATE[pk]


def _resolve(model):
    # model id -> {pricing_key, bedrock_model_id}. Distinct bedrock ids per model.
    table = {
        "m-cheap": ("cheap", "bedrock/cheap"), "b-cheap": ("cheap", "bedrock/cheap"),
        "m-mid": ("mid", "bedrock/mid"), "b-mid": ("mid", "bedrock/mid"),
        "m-dear": ("dear", "bedrock/dear"), "b-dear": ("dear", "bedrock/dear"),
        # same pricing key, DIFFERENT bedrock id (the followed-vs-equivalent case)
        "m-dear2": ("dear", "bedrock/dear-v2"),
    }
    if model not in table:
        return None
    pk, bid = table[model]
    return {"pricing_key": pk, "bedrock_model_id": bid}


def _row(decision=_PREFER, suggested="m-cheap", matched=True, billed="b-dear",
         cost=10_000, tin=100, tout=100):
    return {"tenant_id": "t", "span_id": "s", "vsr_decision": decision,
            "suggested_model": suggested, "matched": matched,
            "billed_model_id": billed, "cost_microusd": cost,
            "input_tokens": tin, "output_tokens": tout}


def _cr(**kw):
    return counterfactual_row(_row(**kw), price=_price, resolve=_resolve)


# -------------------------------------------------------------- classification

def test_no_suggestion_out_of_base():
    assert _cr(decision="passthrough", suggested=None)["class"] == "no_suggestion"


def test_unmatched_is_coverage_gap():
    assert _cr(matched=False, billed=None, cost=None)["class"] == "unmatched"


def test_no_cost_is_not_a_fake_loss():
    # matched but cost None -> no_cost (Fable b), never billed=0 -> fake -cf loss.
    assert _cr(cost=None)["class"] == "no_cost"


def test_missing_or_zero_tokens_is_data_gap():
    assert _cr(tin=None)["class"] == "no_tokens"
    assert _cr(tin=0, tout=0)["class"] == "no_tokens"


def test_unpriceable_model_is_gap():
    assert _cr(suggested="m-unknown")["class"] == "unpriceable"
    assert _cr(billed="b-unknown")["class"] == "unpriceable"


def test_followed_is_same_bedrock_id_not_just_pricing_key():
    # same pricing key ("dear") but DIFFERENT bedrock id -> NOT followed; it is a
    # real counterfactual (delta 0 because same rate, but classified honestly).
    cr = _cr(suggested="m-dear", billed="b-dear")   # same bedrock id -> followed
    assert cr["class"] == "followed"
    cr2 = _cr(suggested="m-dear2", billed="b-dear")  # same pk, diff bedrock id
    assert cr2["class"] == "counterfactual" and cr2["saving_microusd"] == 0


def test_counterfactual_saving_when_vsr_cheaper():
    # billed dear (50/tok), suggested cheap (1/tok), 200 tok. recompute both at
    # one snapshot: billed=10000, sug=200 -> saving 9800.
    cr = _cr(suggested="m-cheap", billed="b-dear", cost=10_000, tin=100, tout=100)
    assert cr["class"] == "counterfactual"
    assert cr["recompute_billed_microusd"] == 10_000
    assert cr["recompute_suggested_microusd"] == 200
    assert cr["saving_microusd"] == 9800


def test_escalation_loss_surfaced_not_clipped():
    # VSR suggested DEAR, cheap was billed -> following the VSR costs MORE -> loss.
    cr = _cr(suggested="m-dear", billed="b-cheap", cost=200, tin=100, tout=100)
    assert cr["class"] == "counterfactual"
    assert cr["saving_microusd"] == 200 - 10_000   # -9800, a loss


def test_basis_drift_excluded():
    # recompute of billed (dear, 200 tok = 10000) vs actual charge 1000 -> 900%
    # drift (rate change / cache-heavy bill) -> excluded, never inflates savings.
    cr = _cr(suggested="m-cheap", billed="b-dear", cost=1000, tin=100, tout=100)
    assert cr["class"] == "basis_drift"
    assert cr["saving_microusd"] == 0


# -------------------------------------------------------------- summary honesty

def test_net_equals_positive_minus_negative_and_nested():
    win = _row(suggested="m-cheap", billed="b-dear", cost=10_000)   # +9800
    loss = _row(suggested="m-dear", billed="b-cheap", cost=200)     # -9800
    loss["span_id"] = "s2"
    s = summarize_savings([win, loss], price=_price, resolve=_resolve)
    assert s["decomposition"]["positive_deltas_microusd"] == 9800
    assert s["decomposition"]["negative_deltas_microusd"] == 9800
    assert s["net_saving_microusd"] == 0            # top-level headline
    assert "gross_saving_microusd" not in s         # un-promotable naming


def test_class_billed_and_total_denominator():
    r = _row(suggested="m-cheap", billed="b-dear", cost=10_000)
    s = summarize_savings([r], price=_price, resolve=_resolve)
    assert s["class_billed_microusd"]["counterfactual"] == 10_000
    assert s["total_billed_microusd_all_classes"] == 10_000


def test_duplicate_span_is_deduped():
    r1 = _row(suggested="m-cheap", billed="b-dear", cost=10_000)
    r2 = _row(suggested="m-cheap", billed="b-dear", cost=10_000)  # same tenant+span
    s = summarize_savings([r1, r2], price=_price, resolve=_resolve)
    assert s["priced_request_count"] == 1              # counted once
    assert s["class_counts"].get("duplicate") == 1


def test_quality_never_implied():
    s = summarize_savings([_row()], price=_price, resolve=_resolve)
    assert s["quality"]["measured"] is False


# -------------------------------------------- realized vs potential (shadow)

def test_shadow_advised_row_is_counterfactual_but_not_enacted():
    cr = _cr(decision=_SHADOW)
    assert cr["class"] == "counterfactual"
    assert cr["enacted"] is False          # advice only, execution not steered
    # same model-vs-model recompute as a realized row.
    assert cr["saving_microusd"] == cr["recompute_billed_microusd"] - cr["recompute_suggested_microusd"]


def test_realized_row_is_enacted():
    assert _cr(decision=_PREFER)["enacted"] is True


def test_potential_never_summed_into_realized_headline():
    # one enacted saving + one shadow (potential) saving, distinct spans.
    r_real = _row(decision=_PREFER)
    r_real["span_id"] = "real"
    r_shadow = _row(decision=_SHADOW)
    r_shadow["span_id"] = "shadow"
    s = summarize_savings([r_real, r_shadow], price=_price, resolve=_resolve)
    one = _cr()["saving_microusd"]         # each row's saving is identical here
    # HEADLINE = realized ONLY (one saving), NOT two.
    assert s["net_saving_microusd"] == one
    assert s["priced_request_count"] == 1
    # potential is SEPARATE, carries the same magnitude, and is flagged not-enacted.
    assert s["potential"]["net_saving_microusd"] == one
    assert s["potential"]["priced_request_count"] == 1
    assert s["potential"]["enacted"] is False
    assert "UPPER-BOUND" in s["potential"]["note"]
    # both counted in class_counts as counterfactual (transparency).
    assert s["class_counts"]["counterfactual"] == 2


def test_all_shadow_leaves_realized_headline_zero():
    rows = []
    for i in range(3):
        r = _row(decision=_SHADOW)
        r["span_id"] = f"sh{i}"
        rows.append(r)
    s = summarize_savings(rows, price=_price, resolve=_resolve)
    assert s["net_saving_microusd"] == 0            # nothing was enacted
    assert s["priced_request_count"] == 0
    assert s["potential"]["priced_request_count"] == 3
    assert s["potential"]["net_saving_microusd"] > 0


@given(rows=st.lists(st.fixed_dictionaries({
    "suggested": st.sampled_from(["m-cheap", "m-mid", "m-dear"]),
    "billed": st.sampled_from(["b-cheap", "b-mid", "b-dear"]),
    "tin": st.integers(min_value=0, max_value=100_000),
    "tout": st.integers(min_value=0, max_value=100_000),
    "cost": st.integers(min_value=1, max_value=100_000_000),
}), min_size=0, max_size=40))
@settings(max_examples=300, deadline=None)
def test_summary_invariants(rows):
    joined = []
    for i, r in enumerate(rows):
        row = _row(suggested=r["suggested"], billed=r["billed"], cost=r["cost"],
                   tin=r["tin"], tout=r["tout"])
        row["span_id"] = f"s{i}"   # unique so dedup doesn't collapse the sample
        joined.append(row)
    s = summarize_savings(joined, price=_price, resolve=_resolve)
    d = s["decomposition"]
    # net == positive - negative, exactly (no clipping, no rounding drift).
    assert s["net_saving_microusd"] == d["positive_deltas_microusd"] - d["negative_deltas_microusd"]
    assert d["positive_deltas_microusd"] >= 0 and d["negative_deltas_microusd"] >= 0
    # every input row is classified into exactly one bucket.
    assert sum(s["class_counts"].values()) == len(joined)
    assert s["priced_request_count"] == s["class_counts"].get("counterfactual", 0)
