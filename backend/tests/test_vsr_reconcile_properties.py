"""Property (formal) tests for the offline VSR billing reconciliation.

The arithmetic here is trivial; the RISKS are structural invariants that a few
hand-written examples can't cover exhaustively:

  * completeness       — every VSR-acted decision appears in exactly one joined
                         row; no non-VSR decision ever leaks in.
  * enforcement soundness — a `hard` pin is `violation` IFF (matched AND advised
                         alias != committed alias); it is NEVER honored on a
                         mismatch, never violation on a match, and a non-hard
                         decision is NEVER honored/violation.
  * coverage honesty   — billed_microusd_matched_sum counts matched rows ONLY
                         (an unsettled row can never inflate it), and the four
                         enforcement buckets partition the rows exactly.
  * join integrity     — a usage row is attributed to a decision IFF their
                         span_id / request-id match; a duplicate usage row for a
                         span never double-counts.

These are invariants over arbitrary decision/usage populations, so they are
property-tested rather than pinned by examples.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from mvp.learning import vsr_reconcile as vr

_ALIASES = ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7", "vllm-x"]
_DECISIONS = [
    vr._HARD_DECISION, "prefer-applied", "prefer-overridden", "no-advice", "timeout",
]


class _Entry:
    def __init__(self, bid):
        self.bedrock_model_id = bid


def _stub_resolve(name):
    """Deterministic identity resolver: each known alias maps to a distinct
    bedrock id; unknown -> ValueError (a data gap). Injected so the property
    tests are pure and independent of the live registry."""
    if name in _ALIASES:
        return _Entry(f"bedrock::{name}")
    raise ValueError(f"unknown model {name!r}")


def _join(decisions, usages):
    return vr.reconcile_join(decisions, usages, resolve=_stub_resolve)


@st.composite
def _decision(draw, span):
    dec = draw(st.sampled_from(_DECISIONS))
    suggested = draw(st.sampled_from(_ALIASES + [None]))
    chosen = draw(st.sampled_from(_ALIASES))
    return {
        "record_type": "decision",
        "tenant_id": "acme",
        "span_id": span,
        "requested_model": "claude-opus-4-7",
        "chosen": {"model": chosen},
        "vsr": {"decision": dec, "suggested_model": suggested, "mode": "hard"},
    }


# billed model ids the resolver understands (settle writes a bedrock id), plus a
# deliberately-unresolvable one so the INDETERMINATE path is exercised.
_BILLED = [f"bedrock::{a}" for a in _ALIASES] + ["unresolvable-billed-id"]


@st.composite
def _population(draw):
    """A set of VSR decisions (unique span ids) + a subset with usage rows +
    some noise: non-VSR decisions and orphan usage rows."""
    n = draw(st.integers(min_value=0, max_value=12))
    spans = [f"sp-{i}" for i in range(n)]
    decisions = [draw(_decision(s)) for s in spans]
    # a random subset gets a usage row; cost + billed model are arbitrary.
    settled = draw(st.lists(st.booleans(), min_size=n, max_size=n)) if n else []
    billed_of = {}
    usages = []
    for s, is_settled in zip(spans, settled):
        if is_settled:
            billed = draw(st.sampled_from(_BILLED))
            billed_of[s] = billed
            usages.append({
                "tenant_id": "acme",
                "timestamp_log_id": f"2026-07-18T10:00:00Z#{s}",
                "model_id": billed,
                "cost_microusd": draw(st.integers(min_value=0, max_value=9_999_999)),
            })
    # noise: a non-VSR decision (no `vsr` block) must be ignored entirely.
    if draw(st.booleans()):
        decisions.append({"record_type": "decision", "tenant_id": "acme",
                          "span_id": "noise-plain", "chosen": {"model": "claude-opus-4-7"}})
    # noise: an orphan usage row (no matching decision) must never be attributed.
    if draw(st.booleans()):
        usages.append({"tenant_id": "acme",
                       "timestamp_log_id": "2026-07-18T10:00:00Z#orphan-usage",
                       "model_id": "x", "cost_microusd": 123})
    settled_spans = {s for s, k in zip(spans, settled) if k}
    return decisions, usages, set(spans), settled_spans, billed_of


@given(_population())
@settings(max_examples=300)
def test_completeness_only_vsr_decisions_once(pop):
    decisions, usages, vsr_spans, _, _ = pop
    rows = _join(decisions, usages)
    row_spans = [r["span_id"] for r in rows]
    # exactly the VSR-acted spans, each exactly once — no plain decision leaks in.
    assert sorted(row_spans) == sorted(vsr_spans)
    assert len(row_spans) == len(set(row_spans))
    assert "noise-plain" not in row_spans


@given(_population())
@settings(max_examples=300)
def test_enforcement_soundness(pop):
    decisions, usages, _, settled_spans, billed_of = pop
    rows = _join(decisions, usages)
    by_span = {r["span_id"]: r for r in rows}
    for d in decisions:
        vsr = d.get("vsr")
        if not vsr:
            continue
        r = by_span[d["span_id"]]
        dec = vsr["decision"]
        matched = d["span_id"] in settled_spans
        enf = r["enforcement"]
        assert enf in vr.ENFORCE_VERDICTS  # closed set — never an unknown string.
        if dec != vr._HARD_DECISION:
            # non-hard is NEVER honored/violation/unsettled/indeterminate.
            assert enf == vr.ENFORCE_NA
        elif not matched:
            assert enf == vr.ENFORCE_UNSETTLED
        else:
            # enforcement is judged against the BILLED model, normalized.
            advised = vr._norm_model(vsr["suggested_model"], _stub_resolve)
            billed = vr._norm_model(billed_of[d["span_id"]], _stub_resolve)
            if advised is None or billed is None:
                assert enf == vr.ENFORCE_INDETERMINATE  # data gap, not a breach.
            else:
                assert enf == (vr.ENFORCE_HONORED if advised == billed
                               else vr.ENFORCE_VIOLATION)


@given(_population())
@settings(max_examples=300)
def test_coverage_partial_sum_and_bucket_partition(pop):
    decisions, usages, _, _, _ = pop
    rows = _join(decisions, usages)
    s = vr.summarize(rows)

    # matched/unsettled partition the rows exactly.
    assert s["matched_count"] + s["unsettled_count"] == s["vsr_acted_count"] == len(rows)

    # ALL enforcement buckets (closed set) + unknown partition the rows exactly.
    assert (s["enforcement_honored"] + s["enforcement_violation"]
            + s["enforcement_na"] + s["enforcement_unsettled"]
            + s["enforcement_indeterminate"] + s["enforcement_unknown"]) == len(rows)
    # no row ever lands in the unknown bucket (verdicts are all in the closed set).
    assert s["enforcement_unknown"] == 0

    # by_decision histogram is complete: it accounts for every row.
    assert sum(s["by_decision"].values()) == len(rows)

    # billed sum counts MATCHED rows only — an unsettled row (cost None) can never
    # contribute, and the sum equals the independent recomputation over matched.
    expected = sum(int(r["cost_microusd"] or 0) for r in rows if r["matched"])
    assert s["billed_microusd_matched_sum"] == expected
    # unsettled rows carry no cost.
    assert all(r["cost_microusd"] is None for r in rows if not r["matched"])


@given(st.lists(st.integers(min_value=0, max_value=1000), min_size=1, max_size=5))
@settings(max_examples=100)
def test_duplicate_records_never_double_count(costs):
    # BOTH a duplicated decision (retried fire-and-forget) AND duplicate usage
    # rows for the same span must yield ONE row with ONE cost — never a sum.
    span = "sp-dup"
    d = {
        "record_type": "decision", "tenant_id": "acme", "span_id": span,
        "chosen": {"model": "claude-haiku-4-5"},
        "vsr": {"decision": vr._HARD_DECISION,
                "suggested_model": "claude-haiku-4-5", "mode": "hard"},
    }
    usages = [{"tenant_id": "acme", "timestamp_log_id": f"2026-07-18T10:00:00Z#{span}",
               "model_id": "bedrock::claude-haiku-4-5", "cost_microusd": c} for c in costs]
    rows = _join([d, dict(d), dict(d)], usages)  # decision written 3x
    assert len(rows) == 1
    # exactly one contributing cost (the first usage), not the sum of duplicates.
    assert rows[0]["cost_microusd"] == costs[0]


@given(st.text(min_size=0, max_size=6), st.text(min_size=0, max_size=6))
@settings(max_examples=100)
def test_cross_tenant_span_never_misattributed(ta, tb):
    # Same span id under two DISTINCT tenants: neither decision may grab the
    # other tenant's usage row (join key is (tenant, span), not span alone).
    from hypothesis import assume
    assume(ta != tb)
    span = "shared-span"
    d = {"record_type": "decision", "tenant_id": ta, "span_id": span,
         "chosen": {"model": "claude-haiku-4-5"},
         "vsr": {"decision": vr._HARD_DECISION,
                 "suggested_model": "claude-haiku-4-5", "mode": "hard"}}
    u = {"tenant_id": tb, "timestamp_log_id": f"2026-07-18T10:00:00Z#{span}",
         "model_id": "bedrock::claude-haiku-4-5", "cost_microusd": 99}
    rows = _join([d], [u])
    assert len(rows) == 1
    assert rows[0]["matched"] is False  # tenant ta must not see tenant tb's usage.


@given(st.integers(min_value=2000, max_value=2999),
       st.integers(min_value=1, max_value=12),
       st.integers(min_value=2, max_value=27))
@settings(max_examples=100)
def test_day_iso_bounds_contains_only_that_day(y, m, d):
    day = f"{y:04d}{m:02d}{d:02d}"
    lo, hi = vr._day_iso_bounds(day)
    iso = f"{y:04d}-{m:02d}-{d:02d}"
    # every timestamp within that UTC day sorts inside [lo, hi].
    assert lo <= f"{iso}T00:00:00Z#anything" <= hi
    assert lo <= f"{iso}T23:59:59.999999Z#zzz" <= hi
    # neighbouring calendar days' ISO prefixes fall OUTSIDE the bounds.
    prev_iso = f"{y:04d}-{m:02d}-{d-1:02d}"
    next_iso = f"{y:04d}-{m:02d}-{d+1:02d}"
    assert f"{prev_iso}T12:00:00Z#x" < lo
    assert f"{next_iso}T12:00:00Z#x" > hi


def test_day_iso_bounds_rejects_malformed():
    import pytest as _pytest
    for bad in ["2026-07-18", "202607", "abcdefgh", "", "2026071"]:
        with _pytest.raises(ValueError):
            vr._day_iso_bounds(bad)
