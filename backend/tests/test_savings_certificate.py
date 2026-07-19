"""End-to-end Savings Certificate over moto (decision log + usage logs).

Proves the counterfactual saving is computed against the REAL billed token counts
and the live rate table (mvp.learning.savings.savings_certificate), joining the
same two tables vsr_reconcile does. Uses real registry model ids so pricing_key
resolution is exercised, not mocked.
"""
from __future__ import annotations

from mvp.learning import decision_log as dl
from mvp.learning import savings as sv
from dynamo import UsageLogsRepository


def test_certificate_counts_counterfactual_saving(dynamodb_mock):
    """VSR suggested a cheap model (haiku); an expensive model (opus) was actually
    billed. Following the VSR would have been cheaper -> a positive net saving,
    priced over the request's real tokens."""
    now_ms = dl._now_ms()
    day = dl._day(now_ms)
    dl._put(dl.build_decision_item(
        tenant_id="acme", run_id="wf-1", span_id="req-save",
        group_id=None, requested_model="claude-opus-4-7",
        selection_reason=None, fallback_reason=None,
        chosen={"model": "claude-opus-4-7"}, rejected=[], estimate_inputs={},
        created_at_ms=now_ms,
        vsr={"decision": "prefer", "suggested_model": "claude-haiku-4-5",
             "mode": "prefer", "config_version": "v-1"},
    ))
    # Billed on the EXPENSIVE opus model (the VSR's prefer was not followed).
    UsageLogsRepository().record(
        tenant_id="acme", user_id="u1", user_email="a@b.c",
        model_id="us.anthropic.claude-opus-4-7",
        input_tokens=10_000, output_tokens=2_000,
        request_id="req-save", cost_microusd=5_000_000,
    )

    cert = sv.savings_certificate(tenant_id="acme", day=day)
    s = cert["savings"]
    assert s["priced_request_count"] == 1
    assert s["class_counts"].get("counterfactual") == 1
    # haiku over the SAME 10k/2k tokens is far cheaper than the billed opus cost,
    # so following the VSR would have saved money: gross > 0, net > 0, no loss.
    assert s["gross_saving_microusd"] > 0
    assert s["escalation_loss_microusd"] == 0
    assert s["net_saving_microusd"] == s["gross_saving_microusd"]
    # the counterfactual is strictly below the billed cost.
    assert s["detail"][0]["counterfactual_microusd"] < 5_000_000
    # quality is never implied by the money-side certificate.
    assert s["quality"]["measured"] is False


def test_certificate_surfaces_escalation_loss(dynamodb_mock):
    """VSR suggested the EXPENSIVE model but a cheap model was billed: following
    the VSR would have cost MORE -> the certificate reports a net LOSS, not zero
    (honest sign)."""
    now_ms = dl._now_ms()
    day = dl._day(now_ms)
    dl._put(dl.build_decision_item(
        tenant_id="acme2", run_id="wf-2", span_id="req-loss",
        group_id=None, requested_model="claude-haiku-4-5",
        selection_reason=None, fallback_reason=None,
        chosen={"model": "claude-haiku-4-5"}, rejected=[], estimate_inputs={},
        created_at_ms=now_ms,
        vsr={"decision": "prefer", "suggested_model": "claude-opus-4-7",
             "mode": "prefer", "config_version": "v-1"},
    ))
    UsageLogsRepository().record(
        tenant_id="acme2", user_id="u1", user_email="a@b.c",
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        input_tokens=10_000, output_tokens=2_000,
        request_id="req-loss", cost_microusd=35_000,
    )
    cert = sv.savings_certificate(tenant_id="acme2", day=day)
    s = cert["savings"]
    assert s["priced_request_count"] == 1
    assert s["net_saving_microusd"] < 0            # a LOSS, surfaced not hidden
    assert s["escalation_loss_microusd"] > 0
    assert s["gross_saving_microusd"] == 0
