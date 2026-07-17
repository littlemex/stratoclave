"""Routing decision log (P0) — item builders + sink round-trip.

The savings *semantics* (completeness, sign, reconstruct-from-item) are property-
tested in test_decision_log_properties.py. This file pins the item SHAPE and that
the sink writes/reads the routing-signals table under the decision#/outcome# sk
namespaces with no TTL.
"""
from __future__ import annotations

from mvp.learning import decision_log as dl


def _decision(**over):
    base = dict(
        tenant_id="acme",
        run_id="wf-1",
        span_id="sp-1",
        group_id=None,
        requested_model="opus",
        selection_reason="chain",
        fallback_reason=None,
        chosen={"model": "haiku", "pricing_key": "haiku", "cost_tier": 1,
                "est_cost_microusd": 500, "pricing_version_at_decision": "builtin"},
        rejected=[{"model": "opus", "pricing_key": "opus", "cost_tier": 3,
                   "reject_reason": "fallback-order", "servable": True,
                   "est_cost_microusd": 5000}],
        estimate_inputs={"input_est": 1000, "max_out": 500, "effort": 1},
        created_at_ms=1_700_000_000_000,
    )
    base.update(over)
    return dl.build_decision_item(**base)


def test_decision_item_shape_and_no_ttl():
    item = _decision()
    assert item["record_type"] == "decision"
    assert item["sk"] == "decision#wf-1#sp-1"
    assert item["pk"].startswith("DECISION#acme#D#")
    assert item["chosen"]["model"] == "haiku"
    assert item["rejected"][0]["reject_reason"] == "fallback-order"
    # Audit record: NO TTL attribute (unlike short-TTL learning signals).
    assert "expires_at" not in item and "ttl" not in item


def test_outcome_item_shape_and_basis():
    o = dl.build_outcome_item(
        tenant_id="acme", run_id="wf-1", span_id="sp-1",
        settled_at_ms=1_700_000_000_000,
        actual_total_cost_microusd=450,
        actual_input_tokens=1000, actual_output_tokens=500, effort=1,
        ledger_pricing_version="builtin", counterfactual_pricing_version="builtin",
        savings_vs_requested=4550, savings_vs_max_servable=4550,
        counterfactual_vs_requested_microusd=5000,
        counterfactual_vs_max_servable_microusd=5000,
    )
    assert o["record_type"] == "outcome"
    assert o["sk"] == "outcome#wf-1#sp-1"
    assert o["savings_basis"] == dl.SAVINGS_BASIS
    assert o["savings_vs_requested_microusd"] == 4550
    assert "expires_at" not in o


def test_sink_writes_and_query_reads_back(dynamodb_mock):
    """The low-level _put writes to the routing-signals table and query_day reads
    both records back for the (tenant, day)."""
    d = _decision()
    o = dl.build_outcome_item(
        tenant_id="acme", run_id="wf-1", span_id="sp-1",
        settled_at_ms=1_700_000_000_000, actual_total_cost_microusd=450,
        actual_input_tokens=1000, actual_output_tokens=500, effort=1,
        ledger_pricing_version="builtin", counterfactual_pricing_version="builtin",
        savings_vs_requested=4550, savings_vs_max_servable=4550,
        counterfactual_vs_requested_microusd=5000,
        counterfactual_vs_max_servable_microusd=5000,
    )
    dl._put(d)
    dl._put(o)
    rows = dl.query_day(tenant_id="acme", day="20231114")
    kinds = sorted(r["record_type"] for r in rows)
    assert kinds == ["decision", "outcome"]


def test_put_never_raises_when_table_missing(dynamodb_mock, monkeypatch):
    """A write failure must never escape (best-effort; coverage reconcile catches
    the gap, not a raised exception on the request path)."""
    monkeypatch.setattr(dl, "signals_table_name", lambda: "does-not-exist-table")
    dl._put(_decision())  # must not raise


def test_deterministic_sk_makes_retry_idempotent(dynamodb_mock):
    """Same (run_id, span_id) → same sk → a retry overwrites byte-identically
    rather than duplicating."""
    d = _decision()
    dl._put(d)
    dl._put(d)  # retry
    rows = [r for r in dl.query_day(tenant_id="acme", day="20231114")
            if r["record_type"] == "decision"]
    assert len(rows) == 1


def _outcome(span, savings_req, savings_max=None):
    return dl.build_outcome_item(
        tenant_id="acme", run_id="wf-1", span_id=span,
        settled_at_ms=1_700_000_000_000, actual_total_cost_microusd=1000,
        actual_input_tokens=1000, actual_output_tokens=500, effort=1,
        ledger_pricing_version="v", counterfactual_pricing_version="v",
        savings_vs_requested=savings_req, savings_vs_max_servable=savings_max,
        counterfactual_vs_requested_microusd=(1000 + savings_req) if savings_req is not None else None,
        counterfactual_vs_max_servable_microusd=None,
    )


def test_day_summary_lower_bound_and_coverage_gap(dynamodb_mock):
    """day_summary sums savings only over outcomes that HAVE a figure (lower
    bound), counts decisions without an outcome as a coverage gap, and never
    fabricates a total for null-baseline spans."""
    # two decisions, but only one outcome (one span failed before settle).
    dl._put(_decision(run_id="wf-1", span_id="sp-1"))
    dl._put(_decision(run_id="wf-1", span_id="sp-2"))
    dl._put(_outcome("sp-1", savings_req=3000, savings_max=None))  # max=null

    s = dl.day_summary(tenant_id="acme", day="20231114")
    assert s["decision_count"] == 2
    assert s["outcome_count"] == 1
    assert s["decisions_without_outcome"] == 1  # sp-2 never settled → coverage gap
    assert s["savings_vs_requested_microusd_partial_sum"] == 3000
    assert s["savings_vs_requested_sample"] == 1
    # null max-servable baseline is EXCLUDED, not counted as 0.
    assert s["savings_vs_max_servable_sample"] == 0
    assert s["savings_vs_max_servable_microusd_partial_sum"] == 0


def test_cli_prints_fixed_honesty_notice(dynamodb_mock, capsys):
    from mvp.learning import decision_log_cli as cli

    dl._put(_decision(run_id="wf-1", span_id="sp-1"))
    dl._put(_outcome("sp-1", savings_req=3000))
    rc = cli.main(["--tenant", "acme", "--day", "20231114"])
    assert rc == 0
    out = capsys.readouterr().out
    # The honesty notice is fixed text — never presents savings as billed, and
    # (post-Fable-review) does NOT claim a lower bound (negatives are possible).
    assert "ESTIMATED counterfactuals" in out
    assert "NOT billed savings" in out
    assert "PARTIAL SUM" in out
    assert "lower bound" not in out.lower() or "NOT a lower bound" in out
