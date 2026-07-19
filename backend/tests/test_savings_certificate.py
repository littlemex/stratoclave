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

# real recompute of these models over 10k input / 2k output (see pricing table).
_OPUS_10K2K = 100_000
_HAIKU_10K2K = 20_000


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
