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


# Real registry ids (settle writes the resolved BEDROCK model id to UsageLogs,
# via resolve_bedrock_model(body.model)); the advised side is the alias the VSR
# returns. Enforcement compares the two AFTER normalizing both to the bedrock id.
_HAIKU_ALIAS = "claude-haiku-4-5"
_HAIKU_BEDROCK = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_OPUS_ALIAS = "claude-opus-4-7"
_OPUS_BEDROCK = "us.anthropic.claude-opus-4-7"
_SONNET_BEDROCK = "us.anthropic.claude-sonnet-4-6"


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
                                        "suggested_model": _HAIKU_ALIAS,
                                        "mode": "hard"},
                           chosen_model=_HAIKU_ALIAS)]
    usages = [_usage("sp-1", model_id=_HAIKU_BEDROCK, cost=35)]
    rows = vr.reconcile_join(decisions, usages)
    assert len(rows) == 1
    r = rows[0]
    assert r["span_id"] == "sp-1"
    assert r["vsr_decision"] == "hard-applied"
    assert r["suggested_model"] == _HAIKU_ALIAS
    assert r["chosen_model"] == _HAIKU_ALIAS
    assert r["billed_model_id"] == _HAIKU_BEDROCK
    assert r["cost_microusd"] == 35
    assert r["matched"] is True


def test_hard_pin_honored_when_advice_equals_billed():
    # advised alias and BILLED bedrock id normalize to the same registry id.
    decisions = [_decision("sp-1", vsr={"decision": "hard-applied",
                                        "suggested_model": _HAIKU_ALIAS,
                                        "mode": "hard"},
                           chosen_model=_HAIKU_ALIAS)]
    rows = vr.reconcile_join(decisions, [_usage("sp-1", model_id=_HAIKU_BEDROCK, cost=10)])
    assert rows[0]["enforcement"] == vr.ENFORCE_HONORED


def test_hard_pin_violation_when_billed_differs_from_advice():
    # A `hard` pin advised haiku but the money path BILLED opus = the pin did NOT
    # reach the money path = a trust-boundary violation, detected against BILLED
    # (not the decision's self-reported chosen).
    decisions = [_decision("sp-1", vsr={"decision": "hard-applied",
                                        "suggested_model": _HAIKU_ALIAS,
                                        "mode": "hard"},
                           chosen_model=_HAIKU_ALIAS)]  # decision SAYS haiku...
    rows = vr.reconcile_join(decisions, [_usage("sp-1", model_id=_OPUS_BEDROCK, cost=500)])
    # ...but opus was billed -> violation, even though chosen==advised on the record.
    assert rows[0]["enforcement"] == vr.ENFORCE_VIOLATION


def test_hard_pin_indeterminate_when_model_unresolvable():
    # A hard pin whose advised model can't be resolved is a DATA gap, not a breach.
    decisions = [_decision("sp-1", vsr={"decision": "hard-applied",
                                        "suggested_model": "totally-unknown-model",
                                        "mode": "hard"},
                           chosen_model="totally-unknown-model")]
    rows = vr.reconcile_join(decisions, [_usage("sp-1", model_id="also-unknown", cost=1)])
    assert rows[0]["enforcement"] == vr.ENFORCE_INDETERMINATE


def test_prefer_overridden_is_not_a_violation():
    # A local SAAR prefer legitimately outranks the VSR prefer: the billed model
    # differing from the suggestion is EXPECTED, never a violation.
    decisions = [_decision("sp-1", vsr={"decision": "prefer-overridden",
                                        "suggested_model": _HAIKU_ALIAS,
                                        "mode": "prefer"},
                           chosen_model="claude-sonnet-4-6")]
    rows = vr.reconcile_join(decisions, [_usage("sp-1", model_id=_SONNET_BEDROCK, cost=80)])
    assert rows[0]["enforcement"] == vr.ENFORCE_NA


def test_no_advice_and_timeout_are_na():
    decisions = [
        _decision("sp-1", vsr={"decision": "no-advice"}, chosen_model=_OPUS_ALIAS),
        _decision("sp-2", vsr={"decision": "timeout"}, chosen_model=_OPUS_ALIAS),
    ]
    usages = [_usage("sp-1", model_id=_OPUS_BEDROCK, cost=500),
              _usage("sp-2", model_id=_OPUS_BEDROCK, cost=500)]
    rows = {r["span_id"]: r for r in vr.reconcile_join(decisions, usages)}
    assert rows["sp-1"]["enforcement"] == vr.ENFORCE_NA
    assert rows["sp-2"]["enforcement"] == vr.ENFORCE_NA


def test_decision_without_usage_is_unsettled_coverage_gap():
    decisions = [_decision("sp-1", vsr={"decision": "hard-applied",
                                        "suggested_model": _HAIKU_ALIAS,
                                        "mode": "hard"},
                           chosen_model=_HAIKU_ALIAS)]
    rows = vr.reconcile_join(decisions, [])  # request failed before settle
    assert len(rows) == 1
    assert rows[0]["matched"] is False
    assert rows[0]["cost_microusd"] is None
    # An unmatched decision cannot prove enforcement (no billed model to compare).
    assert rows[0]["enforcement"] == vr.ENFORCE_UNSETTLED


def test_duplicate_decision_counted_once():
    # The decision log is fire-and-forget and may be retried: two decision rows
    # for the same (tenant, span) must produce ONE joined row, not double-count.
    d = _decision("sp-dup", vsr={"decision": "hard-applied",
                                 "suggested_model": _HAIKU_ALIAS, "mode": "hard"},
                  chosen_model=_HAIKU_ALIAS)
    rows = vr.reconcile_join([d, dict(d)], [_usage("sp-dup", model_id=_HAIKU_BEDROCK, cost=35)])
    assert len(rows) == 1
    assert rows[0]["cost_microusd"] == 35


def test_cross_tenant_span_collision_not_misattributed():
    # Same span id under two tenants must not cross-join (join key is tenant+span).
    d = _decision("sp-x", vsr={"decision": "hard-applied",
                               "suggested_model": _HAIKU_ALIAS, "mode": "hard"},
                  chosen_model=_HAIKU_ALIAS, tenant="acme")
    # usage row belongs to a DIFFERENT tenant with the same span id.
    other = _usage("sp-x", model_id=_HAIKU_BEDROCK, cost=35, tenant="globex")
    rows = vr.reconcile_join([d], [other])
    assert len(rows) == 1
    assert rows[0]["matched"] is False  # acme's decision must NOT grab globex's usage.


def test_non_vsr_decisions_are_ignored():
    # A decision record with no `vsr` block is a plain routing decision, out of
    # scope for VSR reconciliation — never joined, never counted.
    plain = {"record_type": "decision", "tenant_id": "acme", "span_id": "sp-9",
             "chosen": {"model": _OPUS_ALIAS}}
    rows = vr.reconcile_join([plain], [_usage("sp-9", model_id=_OPUS_BEDROCK, cost=500)])
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
    assert s["enforcement_indeterminate"] == 0
    assert s["enforcement_unknown"] == 0
    # The by-decision histogram is present for at-a-glance triage.
    assert s["by_decision"]["hard-applied"] == 3
    assert s["by_decision"]["prefer-applied"] == 1
    assert s["by_decision"]["no-advice"] == 1


def test_summarize_flags_unknown_verdict():
    # A row carrying a verdict outside the closed set is surfaced (not folded
    # into n/a) so a future enum drift is caught, not hidden.
    s = vr.summarize([{"span_id": "z", "vsr_decision": "hard-applied",
                       "matched": True, "cost_microusd": 1, "enforcement": "bogus"}])
    assert s["enforcement_unknown"] == 1
    assert s["enforcement_na"] == 0


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

    # The billed usage row for the SAME request id. model_id is the resolved
    # BEDROCK id (settle writes resolve_bedrock_model(body.model)); it normalizes
    # to the same registry id as the advised alias -> honored.
    UsageLogsRepository().record(
        tenant_id="acme", user_id="u1", user_email="a@b.c",
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        input_tokens=100, output_tokens=50,
        request_id="req-abc", cost_microusd=35,
    )

    report = vr.reconcile_day(tenant_id="acme", day=day)
    assert report["summary"]["vsr_acted_count"] == 1
    assert report["summary"]["matched_count"] == 1
    assert report["summary"]["billed_microusd_matched_sum"] == 35
    assert report["summary"]["enforcement_honored"] == 1
    row = report["rows"][0]
    assert row["span_id"] == "req-abc"
    assert row["billed_model_id"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
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
    # A hard pin advised haiku, but the money path BILLED opus — the CLI must
    # surface it as a violation (detected against the billed model), not hide it.
    dl._put(dl.build_decision_item(
        tenant_id="acme", run_id="wf-1", span_id="req-x",
        group_id=None, requested_model="claude-opus-4-7",
        selection_reason=None, fallback_reason=None,
        chosen={"model": "claude-haiku-4-5"}, rejected=[], estimate_inputs={},
        created_at_ms=now_ms,
        vsr={"decision": "hard-applied", "suggested_model": "claude-haiku-4-5",
             "mode": "hard"},
    ))
    UsageLogsRepository().record(
        tenant_id="acme", user_id="u1", user_email="a@b.c",
        model_id="us.anthropic.claude-opus-4-7", input_tokens=100, output_tokens=50,
        request_id="req-x", cost_microusd=500,
    )
    rc = cli.main(["--tenant", "acme", "--day", dl._day(now_ms), "--rows"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "VSR billing reconciliation" in out
    assert "VIOLATION:  1" in out
    assert "PARTIAL SUM" in out
    # The full (untruncated) span id is visible in the row listing.
    assert "req-x" in out

    # --fail-on-violation makes the same violation a non-zero exit for CI/alarms.
    rc2 = cli.main(["--tenant", "acme", "--day", dl._day(now_ms), "--fail-on-violation"])
    assert rc2 == 2


def test_cli_bad_day_is_loud(dynamodb_mock):
    from mvp.learning import vsr_reconcile_cli as cli
    import pytest as _pytest
    # A malformed --day must raise (loud), not silently return an empty report.
    with _pytest.raises(ValueError):
        cli.main(["--tenant", "acme", "--day", "2026-07-18"])
