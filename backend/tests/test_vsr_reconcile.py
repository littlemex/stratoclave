"""Offline VSR billing reconciliation (observability closer).

The VSR integration's decision log records — per request, keyed by span_id —
WHAT the VSR advised and how Stratoclave's trust boundary treated that advice
(hard-applied / prefer-applied / prefer-overridden / no-advice / timeout ...).
The UsageLogs table records — keyed by the same request id — the EFFECTIVE model
that was billed and its micro-USD cost.

This module JOINS the two, offline, to answer the only questions Stratoclave
owns at the boundary (NOT the VSR's own routing-quality metrics):

  1. billing reconciliation — for every VSR-acted request, what did it cost?
  2. enforcement integrity — was a HARD pin actually honored (advised == billed
     alias), or did something slip past the trust boundary (a `hard` decision
     whose committed model differs = a violation to surface)?
  3. coverage — VSR decisions with no matching usage row (request failed before
     settle, or a dropped write), and usage rows with no VSR decision.

It is a PURE fold over records + a thin DynamoDB reader. No request-path code,
no new table, no VSR-metric re-implementation.
"""
from __future__ import annotations

from mvp.learning import vsr_reconcile as vr


# --------------------------------------------------------------------------
# reconcile_join: PURE join of decision items + usage rows on span_id.
# --------------------------------------------------------------------------

def _decision(span, *, vsr, chosen_model, tenant="acme"):
    return {
        "record_type": "decision",
        "tenant_id": tenant,
        "workflow_run_id": "wf-1",
        "span_id": span,
        "requested_model": "claude-opus-4-7",
        "chosen": {"model": chosen_model},
        "vsr": vsr,
    }


def _usage(span, *, model_id, cost, tenant="acme"):
    # UsageLogs SK is "{iso}#{log_id}" where log_id == request_id == span_id.
    return {
        "tenant_id": tenant,
        "timestamp_log_id": f"2026-07-18T10:00:00Z#{span}",
        "model_id": model_id,
        "cost_microusd": cost,
        "input_tokens": 100,
        "output_tokens": 50,
    }


def test_join_matches_decision_to_usage_by_span():
    decisions = [_decision("sp-1", vsr={"decision": "hard-applied",
                                        "suggested_model": "claude-haiku-4-5",
                                        "mode": "hard"},
                           chosen_model="claude-haiku-4-5")]
    usages = [_usage("sp-1", model_id="haiku-4-5", cost=35)]
    rows = vr.reconcile_join(decisions, usages)
    assert len(rows) == 1
    r = rows[0]
    assert r["span_id"] == "sp-1"
    assert r["vsr_decision"] == "hard-applied"
    assert r["suggested_model"] == "claude-haiku-4-5"
    assert r["chosen_model"] == "claude-haiku-4-5"
    assert r["billed_model_id"] == "haiku-4-5"
    assert r["cost_microusd"] == 35
    assert r["matched"] is True


def test_hard_pin_honored_when_advice_equals_commit():
    decisions = [_decision("sp-1", vsr={"decision": "hard-applied",
                                        "suggested_model": "claude-haiku-4-5",
                                        "mode": "hard"},
                           chosen_model="claude-haiku-4-5")]
    rows = vr.reconcile_join(decisions, [_usage("sp-1", model_id="haiku", cost=10)])
    assert rows[0]["enforcement"] == vr.ENFORCE_HONORED


def test_hard_pin_violation_when_commit_differs_from_advice():
    # A `hard` decision whose committed alias differs from the advised alias =
    # the pin did NOT reach the money path = a trust-boundary violation to flag.
    decisions = [_decision("sp-1", vsr={"decision": "hard-applied",
                                        "suggested_model": "claude-haiku-4-5",
                                        "mode": "hard"},
                           chosen_model="claude-opus-4-7")]
    rows = vr.reconcile_join(decisions, [_usage("sp-1", model_id="opus", cost=500)])
    assert rows[0]["enforcement"] == vr.ENFORCE_VIOLATION


def test_prefer_overridden_is_not_a_violation():
    # A local SAAR prefer legitimately outranks the VSR prefer: the committed
    # model differing from the suggestion is EXPECTED, never a violation.
    decisions = [_decision("sp-1", vsr={"decision": "prefer-overridden",
                                        "suggested_model": "claude-haiku-4-5",
                                        "mode": "prefer"},
                           chosen_model="claude-sonnet-4-6")]
    rows = vr.reconcile_join(decisions, [_usage("sp-1", model_id="sonnet", cost=80)])
    assert rows[0]["enforcement"] == vr.ENFORCE_NA


def test_no_advice_and_timeout_are_na():
    decisions = [
        _decision("sp-1", vsr={"decision": "no-advice"}, chosen_model="claude-opus-4-7"),
        _decision("sp-2", vsr={"decision": "timeout"}, chosen_model="claude-opus-4-7"),
    ]
    usages = [_usage("sp-1", model_id="opus", cost=500),
              _usage("sp-2", model_id="opus", cost=500)]
    rows = {r["span_id"]: r for r in vr.reconcile_join(decisions, usages)}
    assert rows["sp-1"]["enforcement"] == vr.ENFORCE_NA
    assert rows["sp-2"]["enforcement"] == vr.ENFORCE_NA


def test_decision_without_usage_is_unsettled_coverage_gap():
    decisions = [_decision("sp-1", vsr={"decision": "hard-applied",
                                        "suggested_model": "claude-haiku-4-5",
                                        "mode": "hard"},
                           chosen_model="claude-haiku-4-5")]
    rows = vr.reconcile_join(decisions, [])  # request failed before settle
    assert len(rows) == 1
    assert rows[0]["matched"] is False
    assert rows[0]["cost_microusd"] is None
    # An unmatched decision cannot prove enforcement (no billed model to compare).
    assert rows[0]["enforcement"] == vr.ENFORCE_UNSETTLED


def test_non_vsr_decisions_are_ignored():
    # A decision record with no `vsr` block is a plain routing decision, out of
    # scope for VSR reconciliation — never joined, never counted.
    plain = {"record_type": "decision", "tenant_id": "acme", "span_id": "sp-9",
             "chosen": {"model": "claude-opus-4-7"}}
    rows = vr.reconcile_join([plain], [_usage("sp-9", model_id="opus", cost=500)])
    assert rows == []


# --------------------------------------------------------------------------
# summarize: fold the joined rows into the reconciliation report.
# --------------------------------------------------------------------------

def test_summarize_counts_and_totals():
    joined = [
        {"span_id": "sp-1", "vsr_decision": "hard-applied", "matched": True,
         "cost_microusd": 35, "enforcement": vr.ENFORCE_HONORED},
        {"span_id": "sp-2", "vsr_decision": "prefer-applied", "matched": True,
         "cost_microusd": 80, "enforcement": vr.ENFORCE_HONORED},
        {"span_id": "sp-3", "vsr_decision": "hard-applied", "matched": True,
         "cost_microusd": 500, "enforcement": vr.ENFORCE_VIOLATION},
        {"span_id": "sp-4", "vsr_decision": "hard-applied", "matched": False,
         "cost_microusd": None, "enforcement": vr.ENFORCE_UNSETTLED},
        {"span_id": "sp-5", "vsr_decision": "no-advice", "matched": True,
         "cost_microusd": 500, "enforcement": vr.ENFORCE_NA},
    ]
    s = vr.summarize(joined)
    assert s["vsr_acted_count"] == 5
    assert s["matched_count"] == 4
    assert s["unsettled_count"] == 1
    # Billed cost summed over MATCHED rows only (a partial sum, honest coverage).
    assert s["billed_microusd_matched_sum"] == 35 + 80 + 500 + 500
    assert s["enforcement_honored"] == 2
    assert s["enforcement_violation"] == 1
    assert s["enforcement_na"] == 1
    assert s["enforcement_unsettled"] == 1
    # The by-decision histogram is present for at-a-glance triage.
    assert s["by_decision"]["hard-applied"] == 3
    assert s["by_decision"]["prefer-applied"] == 1
    assert s["by_decision"]["no-advice"] == 1


def test_summarize_empty_is_all_zero():
    s = vr.summarize([])
    assert s["vsr_acted_count"] == 0
    assert s["billed_microusd_matched_sum"] == 0
    assert s["enforcement_violation"] == 0


# --------------------------------------------------------------------------
# reconcile_day: end-to-end over moto (decision log + usage logs).
# --------------------------------------------------------------------------

def test_reconcile_day_reads_both_tables(dynamodb_mock):
    from mvp.learning import decision_log as dl
    from dynamo import UsageLogsRepository

    # Both writes must land on the SAME UTC day (as they do in production, where
    # both fire at request time). UsageLogsRepository.record() stamps its SK with
    # the real "now", so the decision must use the same now for its day partition.
    now_ms = dl._now_ms()
    # A VSR-acted decision (hard pin honored) written to the routing-signals table.
    item = dl.build_decision_item(
        tenant_id="acme", run_id="wf-1", span_id="req-abc",
        group_id=None, requested_model="claude-opus-4-7",
        selection_reason=None, fallback_reason=None,
        chosen={"model": "claude-haiku-4-5"}, rejected=[], estimate_inputs={},
        created_at_ms=now_ms,
        vsr={"decision": "hard-applied", "suggested_model": "claude-haiku-4-5",
             "mode": "hard", "config_version": "v-1"},
    )
    dl._put(item)
    day = dl._day(now_ms)

    # The billed usage row for the SAME request id.
    UsageLogsRepository().record(
        tenant_id="acme", user_id="u1", user_email="a@b.c",
        model_id="haiku-4-5", input_tokens=100, output_tokens=50,
        request_id="req-abc", cost_microusd=35,
    )

    report = vr.reconcile_day(tenant_id="acme", day=day)
    assert report["summary"]["vsr_acted_count"] == 1
    assert report["summary"]["matched_count"] == 1
    assert report["summary"]["billed_microusd_matched_sum"] == 35
    assert report["summary"]["enforcement_honored"] == 1
    row = report["rows"][0]
    assert row["span_id"] == "req-abc"
    assert row["billed_model_id"] == "haiku-4-5"
    assert row["config_version"] == "v-1"


def test_reconcile_day_flags_unsettled_when_no_usage(dynamodb_mock):
    from mvp.learning import decision_log as dl

    now_ms = dl._now_ms()
    dl._put(dl.build_decision_item(
        tenant_id="acme", run_id="wf-1", span_id="req-dead",
        group_id=None, requested_model="claude-opus-4-7",
        selection_reason=None, fallback_reason=None,
        chosen={"model": "claude-haiku-4-5"}, rejected=[], estimate_inputs={},
        created_at_ms=now_ms,
        vsr={"decision": "hard-applied", "suggested_model": "claude-haiku-4-5",
             "mode": "hard"},
    ))
    report = vr.reconcile_day(tenant_id="acme", day=dl._day(now_ms))
    assert report["summary"]["unsettled_count"] == 1
    assert report["summary"]["matched_count"] == 0


# --------------------------------------------------------------------------
# CLI: fixed honesty notice + reconciliation over moto.
# --------------------------------------------------------------------------

def test_cli_prints_summary_and_notice(dynamodb_mock, capsys):
    from mvp.learning import decision_log as dl
    from mvp.learning import vsr_reconcile_cli as cli
    from dynamo import UsageLogsRepository

    now_ms = dl._now_ms()
    # A hard pin that the money path did NOT honor (committed != advised) — the
    # CLI must surface it as a violation, not hide it.
    dl._put(dl.build_decision_item(
        tenant_id="acme", run_id="wf-1", span_id="req-x",
        group_id=None, requested_model="claude-opus-4-7",
        selection_reason=None, fallback_reason=None,
        chosen={"model": "claude-opus-4-7"}, rejected=[], estimate_inputs={},
        created_at_ms=now_ms,
        vsr={"decision": "hard-applied", "suggested_model": "claude-haiku-4-5",
             "mode": "hard"},
    ))
    UsageLogsRepository().record(
        tenant_id="acme", user_id="u1", user_email="a@b.c",
        model_id="opus", input_tokens=100, output_tokens=50,
        request_id="req-x", cost_microusd=500,
    )
    rc = cli.main(["--tenant", "acme", "--day", dl._day(now_ms), "--rows"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "VSR billing reconciliation" in out
    assert "VIOLATION:  1" in out
    assert "PARTIAL SUM" in out
    # The advised->committed divergence is visible in the row listing.
    assert "req-x" in out
