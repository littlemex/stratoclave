"""Property tests for the routing decision log (P0).

The money arithmetic is L5's proven estimate/rate functions; the risks HERE are
completeness (did we record every candidate exactly once, with a valid reason?)
and provability (can savings be recomputed from the stored item alone?). Those
are structural properties, so they are property-tested rather than Z3'd.
"""
from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from mvp._pipeline import _build_decision_facts
from mvp.learning import decision_log as dl


# a priced candidate = (model, pricing_key, est_cost_microusd)
_COST = st.integers(min_value=0, max_value=10_000_000)


_ALL_MODELS = ["opus", "sonnet", "haiku", "gpt-5"]


@st.composite
def _priced_chain(draw):
    # distinct model names (at most the 4 known) so chosen/rejected are unambiguous
    models = draw(
        st.lists(st.sampled_from(_ALL_MODELS), min_size=1, max_size=4, unique=True)
    )
    return [(m, m, draw(_COST)) for m in models]


def _facts_from(priced, chosen_idx):
    """Call the (post-H1-fix) _build_decision_facts by splitting a full priced
    chain at chosen_idx: candidates 0..chosen were TRIED (last = chosen), the tail
    was UNTRIED. `price` returns the pre-priced (pk, cost) for a tail model."""
    priced_tried = priced[: chosen_idx + 1]
    untried = [m for (m, _pk, _c) in priced[chosen_idx + 1:]]
    tail_price = {m: (pk, c) for (m, pk, c) in priced[chosen_idx + 1:]}
    exhausted = {priced[i][0] for i in range(chosen_idx)}
    return _build_decision_facts(
        priced_tried, untried, lambda m: tail_price[m], exhausted
    )


@given(priced=_priced_chain(), pick=st.floats(min_value=0, max_value=0.999))
def test_completeness_chosen_once_and_partition(priced, pick):
    """P1: chosen is exactly one; chosen ∉ rejected; chosen ∪ rejected == the full
    chain; every rejected has a valid enum reason."""
    chosen_idx = int(pick * len(priced))
    facts = _facts_from(priced, chosen_idx)

    chosen_model = facts["chosen"]["model"]
    rejected_models = [r["model"] for r in facts["rejected"]]

    assert chosen_model == priced[chosen_idx][0]
    assert chosen_model not in rejected_models
    # partition: chosen + rejected == the whole chain, no dup, no omission.
    assert {chosen_model, *rejected_models} == {p[0] for p in priced}
    assert len(rejected_models) == len(priced) - 1
    for r in facts["rejected"]:
        assert r["reject_reason"] in dl.REJECT_REASONS
        assert r["servable"] is True
        assert isinstance(r["est_cost_microusd"], int)


@given(priced=_priced_chain(), pick=st.floats(min_value=0, max_value=0.999))
def test_reject_reason_matches_position(priced, pick):
    """Candidates before the chosen (tried, quota gone) → quota-exhausted; after
    (never tried) → fallback-order."""
    chosen_idx = int(pick * len(priced))
    facts = _facts_from(priced, chosen_idx)
    reason_by_model = {r["model"]: r["reject_reason"] for r in facts["rejected"]}
    for i, (model, _pk, _c) in enumerate(priced):
        if i < chosen_idx:
            assert reason_by_model[model] == "quota-exhausted"
        elif i > chosen_idx:
            assert reason_by_model[model] == "fallback-order"


def _outcome(**over):
    base = dict(
        tenant_id="t", run_id="r", span_id="s", settled_at_ms=1_700_000_000_000,
        actual_total_cost_microusd=1000,
        actual_input_tokens=1000, actual_output_tokens=500, effort=1,
        ledger_pricing_version="v", counterfactual_pricing_version="v",
        savings_vs_requested=4000, savings_vs_max_servable=4000,
        counterfactual_vs_requested_microusd=5000,
        counterfactual_vs_max_servable_microusd=5000,
    )
    base.update(over)
    return dl.build_outcome_item(**base)


def test_savings_reconstructs_from_item():
    """P5 (provability): savings == counterfactual − actual, recomputable from the
    stored fields ALONE (no hidden state)."""
    o = _outcome(
        actual_total_cost_microusd=1200,
        counterfactual_vs_requested_microusd=5000,
        savings_vs_requested=3800,
    )
    recomputed = (
        o["counterfactual_vs_requested_microusd"] - o["actual_total_cost_microusd"]
    )
    assert recomputed == o["savings_vs_requested_microusd"] == 3800


def test_counterfactual_recomputable_from_persisted_tokens(dynamodb_mock):
    """Provability (Fable RDL review High): the counterfactual is recomputable from
    the outcome item ALONE — the actual tokens are persisted, so
    actual_cost(baseline_pricing_key, item tokens) reproduces the stored
    counterfactual without any ledger join.

    It is the CHARGING fold, not the reservation estimator, and effort is not an
    input. The estimator prices every input-side token at the worst input-side rate
    because at reserve time the provider has not chosen a leg yet; here it has, and
    the tokens are counted. Pricing a baseline at reservation rates, times an effort
    multiplier applied to output that was already generated, inflated the saving.
    """
    from mvp import pricing

    o = dl.build_outcome_item(
        tenant_id="t", run_id="r", span_id="s", settled_at_ms=1_700_000_000_000,
        actual_total_cost_microusd=500,
        actual_input_tokens=1_000_000, actual_output_tokens=1_000_000, effort=1,
        ledger_pricing_version="builtin", counterfactual_pricing_version="builtin",
        savings_vs_requested=None, savings_vs_max_servable=None,
        counterfactual_vs_requested_microusd=None,
        counterfactual_vs_max_servable_microusd=None,
    )
    # Recompute opus's counterfactual purely from the item's persisted tokens.
    pricing.reset_cache()
    pricing.reset_version_cache()
    recomputed = pricing.actual_cost_microusd(
        pricing_key="opus",
        input_tokens=o["actual_input_tokens"],
        output_tokens=o["actual_output_tokens"],
    )
    # One MTok on each side, so the counterfactual is the input rate plus the output
    # rate at the floor. Derived rather than pinned: the floor carries measured prices.
    opus = pricing.baseline_rates()["opus"]
    assert recomputed == opus.input_per_mtok_microusd + opus.output_per_mtok_microusd
    # And the reservation estimator, on the same tokens, is HIGHER — which is why it
    # is the wrong function here. It prices the input side at the cache-write rate.
    assert pricing.estimate_cost_microusd(
        pricing_key="opus",
        input_tokens_est=o["actual_input_tokens"],
        max_output_tokens=o["actual_output_tokens"],
        effort_multiplier=o["effort"],
    ) > recomputed


def test_savings_may_be_negative_on_escalation():
    """Sign is NOT clamped: if the router escalated to a pricier model than the
    requested baseline, savings is negative — a valid, recorded signal."""
    o = _outcome(
        actual_total_cost_microusd=5000,
        counterfactual_vs_requested_microusd=1000,  # requested was cheaper
        savings_vs_requested=1000 - 5000,
    )
    assert o["savings_vs_requested_microusd"] == -4000


def test_savings_survives_an_unknown_effort_because_effort_is_not_priced(
    dynamodb_mock, monkeypatch,
):
    """What replaced M2 (Fable RDL review-2).

    M2 said: if effort could not be recovered from the decision facts, the
    counterfactual must not be computed with an assumed effort=1, because that would
    record a self-consistent WRONG savings. The guard was right about the arithmetic
    of the day — the counterfactual priced `actual_output_tokens * effort` — and the
    arithmetic was the defect. A baseline is not charged for output that was never
    generated, so effort does not belong in the comparison at all.

    With effort out of the formula, a missing effort no longer makes the saving
    unknowable: it is recomputable from the persisted tokens alone. The record still
    carries `effort: None` honestly, so nothing is invented about the reservation.
    """
    class _Ctx:
        tenant_id = "t"
        workflow_run_id = "r"
        request_id = "s"
        requested_model = "opus"
        # decision facts WITHOUT estimate_inputs (effort unknown).
        decision_facts = {
            "chosen": {"model": "haiku", "pricing_key": "haiku",
                       "pricing_version_at_decision": "builtin"},
            "rejected": [{"model": "opus", "pricing_key": "opus",
                          "reject_reason": "fallback-order", "servable": True,
                          "est_cost_microusd": 5000}],
        }

    captured = {}
    monkeypatch.setattr(dl, "emit_decision", lambda item: captured.update(item))
    dl.record_outcome_from_context(
        _Ctx(), actual_total_cost_microusd=500,
        actual_input_tokens=1_000_000, actual_output_tokens=1_000_000,
        ledger_pricing_version="builtin",
    )
    # opus at 1 MTok in + 1 MTok out is the input rate plus the output rate at the
    # floor; the chosen model settled at 500, so the recorded saving is the difference
    # and not a null. Derived rather than pinned: the floor carries measured prices.
    from mvp import pricing as _pricing

    opus = _pricing.baseline_rates()["opus"]
    counterfactual = opus.input_per_mtok_microusd + opus.output_per_mtok_microusd
    assert captured.get("counterfactual_vs_requested_microusd") == counterfactual
    assert captured.get("savings_vs_requested_microusd") == counterfactual - 500
    assert captured.get("savings_vs_max_servable_microusd") == counterfactual - 500
    assert captured.get("effort") is None, (
        "effort must still be recorded as unknown rather than defaulted — it is "
        "evidence about the reservation, and only the counterfactual stopped needing it"
    )


def test_null_savings_when_no_baseline():
    """No comparable baseline → null (distinct from zero: 'no comparison' ≠ 'no
    difference')."""
    o = _outcome(
        savings_vs_max_servable=None,
        counterfactual_vs_max_servable_microusd=None,
    )
    assert o["savings_vs_max_servable_microusd"] is None
    assert o["savings_basis"] == dl.SAVINGS_BASIS


def test_build_decision_facts_single_candidate_no_rejected():
    """A one-element chain (pin / no fallback) records chosen with empty rejected
    — still a complete, valid partition."""
    facts = _build_decision_facts([("opus", "opus", 5000)], [], lambda m: (m, 0), set())
    assert facts["chosen"]["model"] == "opus"
    assert facts["rejected"] == []
