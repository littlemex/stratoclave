"""End-to-end Savings Certificate over moto (decision log + usage logs).

Proves the model-vs-model counterfactual is computed against the REAL billed
token counts and the live rate table (mvp.learning.savings.savings_certificate),
joining the same two tables vsr_reconcile does. Uses real registry model ids so
pricing_key + bedrock_model_id resolution is exercised, not mocked. Billed cost
is seeded to the model's real recompute so no basis_drift is triggered.
"""
from __future__ import annotations

from mvp.learning import decision_log as dl
from mvp.learning import savings as sv
from mvp.vsr.client import DECISION_PREFER_APPLIED, DECISION_HARD_APPLIED
from dynamo import UsageLogsRepository

def _recompute(pricing_key: str, *, input_tokens: int = 10_000,
              output_tokens: int = 2_000) -> int:
    """What the ledger charges for this many tokens at the built-in floor.

    Computed rather than pinned: the floor holds measured list prices, so a literal
    here would have to be edited every time AWS moves a rate, and the certificate's
    property under test is that the saving is `recompute(billed) - recompute(advised)`
    over the SAME tokens — not any particular dollar amount.
    """
    from mvp import pricing

    return pricing.actual_cost_microusd(
        pricing_key=pricing_key, input_tokens=input_tokens, output_tokens=output_tokens,
    )


# real recompute of these models over 10k input / 2k output.
_OPUS_10K2K = _recompute("opus")
_HAIKU_10K2K = _recompute("haiku")


def test_certificate_counts_counterfactual_saving(dynamodb_mock):
    """VSR suggested haiku (cheap); opus (expensive) was actually billed. Following
    the VSR would have been cheaper -> a positive net saving, priced model-vs-model
    over the request's real tokens at one snapshot."""
    now_ms = dl._now_ms()
    day = dl._day(now_ms)
    dl._put(dl.build_decision_item(
        tenant_id="acme", run_id="wf-1", span_id="req-save",
        group_id=None, requested_model="claude-opus-4-7",
        selection_reason=None, fallback_reason=None,
        chosen={"model": "claude-opus-4-7"}, rejected=[], estimate_inputs={},
        created_at_ms=now_ms,
        vsr={"decision": DECISION_PREFER_APPLIED, "suggested_model": "claude-haiku-4-5",
             "mode": "prefer", "config_version": "v-1"},
    ))
    UsageLogsRepository().record(
        tenant_id="acme", user_id="u1", user_email="a@b.c",
        model_id="us.anthropic.claude-opus-4-7",
        input_tokens=10_000, output_tokens=2_000,
        request_id="req-save", cost_microusd=_OPUS_10K2K,  # matches recompute (no drift)
    )
    cert = sv.savings_certificate(tenant_id="acme", day=day)
    s = cert["savings"]
    assert s["priced_request_count"] == 1
    assert s["class_counts"].get("counterfactual") == 1
    # saving = recompute(opus) - recompute(haiku), both over the same 10k/2k tokens.
    assert s["net_saving_microusd"] == _OPUS_10K2K - _HAIKU_10K2K
    assert s["decomposition"]["negative_deltas_microusd"] == 0
    assert cert["rate_version"]                      # stamped for reproducibility
    assert s["quality"]["measured"] is False
    assert cert["traffic"] == "real"                 # default provenance


def test_certificate_stamps_synthetic_provenance(dynamodb_mock):
    """A seeded/demo run stamps traffic=synthetic on the certificate itself so a
    sample number can never be mistaken for a real audited one (Fable review)."""
    day = dl._day(dl._now_ms())
    cert = sv.savings_certificate(tenant_id="nobody", day=day, traffic="synthetic")
    assert cert["traffic"] == "synthetic"


def test_certificate_surfaces_escalation_loss(dynamodb_mock):
    """VSR suggested opus (dear); haiku (cheap) was billed. Following the VSR would
    have cost MORE -> the certificate reports a net LOSS, not zero (honest sign)."""
    now_ms = dl._now_ms()
    day = dl._day(now_ms)
    dl._put(dl.build_decision_item(
        tenant_id="acme2", run_id="wf-2", span_id="req-loss",
        group_id=None, requested_model="claude-haiku-4-5",
        selection_reason=None, fallback_reason=None,
        chosen={"model": "claude-haiku-4-5"}, rejected=[], estimate_inputs={},
        created_at_ms=now_ms,
        vsr={"decision": DECISION_HARD_APPLIED, "suggested_model": "claude-opus-4-7",
             "mode": "hard", "config_version": "v-1"},
    ))
    UsageLogsRepository().record(
        tenant_id="acme2", user_id="u1", user_email="a@b.c",
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        input_tokens=10_000, output_tokens=2_000,
        request_id="req-loss", cost_microusd=_HAIKU_10K2K,  # matches recompute
    )
    cert = sv.savings_certificate(tenant_id="acme2", day=day)
    s = cert["savings"]
    assert s["priced_request_count"] == 1
    assert s["net_saving_microusd"] == _HAIKU_10K2K - _OPUS_10K2K   # negative loss
    assert s["decomposition"]["negative_deltas_microusd"] == _OPUS_10K2K - _HAIKU_10K2K
    assert s["decomposition"]["positive_deltas_microusd"] == 0


# ---------------------------------------------------------------- C2.3 replay


def _seed_one_day(tenant: str):
    """One VSR-steered request whose billed cost matches its recompute exactly, so
    the row lands in the `counterfactual` class rather than `basis_drift`."""
    now_ms = dl._now_ms()
    day = dl._day(now_ms)
    dl._put(dl.build_decision_item(
        tenant_id=tenant, run_id="wf-r", span_id="req-replay",
        group_id=None, requested_model="claude-opus-4-7",
        selection_reason=None, fallback_reason=None,
        chosen={"model": "claude-opus-4-7"}, rejected=[], estimate_inputs={},
        created_at_ms=now_ms,
        vsr={"decision": DECISION_PREFER_APPLIED, "suggested_model": "claude-haiku-4-5",
             "mode": "prefer", "config_version": "v-1"},
    ))
    UsageLogsRepository().record(
        tenant_id=tenant, user_id="u1", user_email="a@b.c",
        model_id="us.anthropic.claude-opus-4-7",
        input_tokens=10_000, output_tokens=2_000,
        request_id="req-replay", cost_microusd=_OPUS_10K2K,
    )
    return day


def test_certificate_embeds_the_rates_and_models_it_priced_with(dynamodb_mock):
    """The artifact carries its own basis. Without this, "reproducible" meant
    "recomputable at whatever the table says when you ask again" — a different
    number with the same name on it."""
    day = _seed_one_day("acme-replay")
    cert = sv.savings_certificate(tenant_id="acme-replay", day=day)
    assert cert["replayed"] is False
    # Every pricing key the fold priced is present with all four legs.
    from mvp.rates import RATE_FIELDS
    assert cert["rate_table"], "a priced certificate with no recorded rates"
    for key, legs in cert["rate_table"].items():
        assert set(legs) == set(RATE_FIELDS), (key, legs)
    # Both models the comparison rests on are recorded, with what they resolved to.
    # The billed side is recorded under the provider id the usage row carried and
    # the suggested side under the alias the decision carried, because those are the
    # strings the fold actually asks about.
    assert set(cert["model_table"]) == {
        "us.anthropic.claude-opus-4-7", "claude-haiku-4-5"}
    for answer in cert["model_table"].values():
        assert answer and answer["pricing_key"] and answer["bedrock_model_id"]


def test_a_replay_holds_its_number_across_a_rate_change(dynamodb_mock, monkeypatch):
    """The differential that makes C2.3 enforced rather than argued: change the rate
    table underneath, then re-run both ways. The plain re-run moves (correctly — it
    is pricing at today's rates); the replay does not."""
    day = _seed_one_day("acme-replay")
    original = sv.savings_certificate(tenant_id="acme-replay", day=day)
    baseline = original["savings"]["net_saving_microusd"]
    assert baseline == _OPUS_10K2K - _HAIKU_10K2K

    # Double every rate the certificate priced at, at the same seam production
    # rate changes arrive through.
    from mvp import pricing
    from mvp.rates import RATE_FIELDS, Rate
    real_rate_for = pricing.rate_for

    # +10%, not +100%: a doubling would push the recomputed billed cost more than
    # 25% away from the actual charge, and the row would be excluded as
    # `basis_drift` — a re-run that returns 0 would "differ" for the wrong reason.
    def _dearer(pricing_key, repo=None):
        r = real_rate_for(pricing_key, repo)
        return Rate(**{f: getattr(r, f) * 11 // 10 for f in RATE_FIELDS})

    monkeypatch.setattr(pricing, "rate_for", _dearer)
    monkeypatch.setattr(pricing, "actual_cost_microusd", lambda **kw: (_ for _ in ()).throw(
        AssertionError("a recording/replaying certificate must not price through the "
                       "live-read helper")))

    repriced = sv.savings_certificate(tenant_id="acme-replay", day=day)
    assert repriced["savings"]["net_saving_microusd"] == baseline * 11 // 10, (
        "a plain re-run should price at the CURRENT table — if this holds equal, "
        "the test is not actually changing rates and the replay assertion below "
        "would pass for the wrong reason")

    replayed = sv.savings_certificate(
        tenant_id="acme-replay", day=day,
        rate_table=original["rate_table"], model_table=original["model_table"])
    assert replayed["replayed"] is True
    assert replayed["savings"]["net_saving_microusd"] == baseline
    # And the replay's own recorded basis is the one it was handed, so a chain of
    # replays cannot drift.
    assert replayed["rate_table"] == original["rate_table"]


def test_a_replay_refuses_rather_than_silently_repricing_a_missing_key(dynamodb_mock):
    """A partial embedded table is the dangerous case: falling back to the live rate
    for the missing key would produce a number that is part replay and part
    reprice, with nothing in the output distinguishing them."""
    import pytest

    day = _seed_one_day("acme-replay")
    cert = sv.savings_certificate(tenant_id="acme-replay", day=day)
    partial = dict(cert["rate_table"])
    partial.pop(next(iter(partial)))
    with pytest.raises(sv.UnpricedKeyOnReplay):
        sv.savings_certificate(tenant_id="acme-replay", day=day,
                               rate_table=partial, model_table=cert["model_table"])
    with pytest.raises(sv.UnresolvedModelOnReplay):
        sv.savings_certificate(tenant_id="acme-replay", day=day,
                               rate_table=cert["rate_table"], model_table={})
