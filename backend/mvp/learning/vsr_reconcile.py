"""Offline VSR billing reconciliation (P0 — observability closer).

Joins, per (tenant, day), the routing **decision log** (what the external VSR
advised and how Stratoclave's trust boundary treated it — written at reserve,
keyed by span_id) against the **UsageLogs** table (the effective model billed
and its micro-USD cost — keyed by the same request id). It answers the three
questions Stratoclave owns at the boundary, and ONLY those (the VSR keeps its
own routing-quality metrics; see docs/VSR_CONFIG_CONTRACT.md §4):

  1. billing reconciliation — for each VSR-acted request, what did it cost;
  2. enforcement integrity — was a HARD pin actually honored (advised alias ==
     committed alias), or did a `hard` decision commit a different model — a
     trust-boundary violation to surface;
  3. coverage — VSR decisions with no matching usage row (request unsettled, or
     a dropped best-effort usage write), surfaced as an `unsettled_count`.

Coverage is one-directional by construction: the fold iterates VSR *decisions*
and looks up usage. A request whose *decision* write was dropped (the decision
log is fire-and-forget, like all learning signals) is NOT VSR-acted from this
module's view and cannot appear here at all — there is no VSR marker on the
usage row to reverse-join from. That drop is monitored elsewhere (the
`routing_decision_write_failed` warning + reserve-vs-decision-count comparison),
NOT by this job. This module is honest about the decision→usage direction only;
it does not claim to detect a lost decision.

This is a PURE fold (`reconcile_join` / `summarize`) plus a thin two-table
reader (`reconcile_day`). It is an INTERNAL ops path (no admin API/UI, no new
table, no request-path code) — reusing decision_log.query_day and the UsageLogs
table exactly as the admin usage API reads them.
"""
from __future__ import annotations

from typing import Any, Optional

from . import decision_log as dl
# Single source of truth for the hard-pin decision label — imported, NOT
# re-literaled, so a rename in vsr.client can never silently drift this module's
# enforcement check to n/a (which would make every violation invisible).
from ..vsr.client import DECISION_HARD_APPLIED as _HARD_DECISION

# enforcement verdicts — a closed set so the CLI/report can rely on them.
ENFORCE_HONORED = "honored"        # hard pin: advised alias == committed alias
ENFORCE_VIOLATION = "violation"    # hard pin: committed alias != advised alias
ENFORCE_NA = "n/a"                 # prefer/overridden/no-advice/timeout — nothing to enforce
ENFORCE_UNSETTLED = "unsettled"    # decision present, no billed usage row to compare


def _usage_span(item: dict) -> str:
    """The request id a UsageLogs row belongs to. The SK is '{iso}#{log_id}'
    where log_id == request_id == the decision's span_id (usage_logs.record:
    ``log_id = request_id or uuid4()``)."""
    tsl = str(item.get("timestamp_log_id") or "")
    return tsl.split("#", 1)[1] if "#" in tsl else ""


def _as_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def reconcile_join(decisions: list[dict], usages: list[dict]) -> list[dict[str, Any]]:
    """PURE join of decision items and usage rows on span_id.

    Only decisions that carry a `vsr` block are in scope (a plain routing
    decision is ignored). Each returned row records the advice, the committed
    alias, the billed model+cost (or None when unmatched), and an `enforcement`
    verdict. Unmatched VSR decisions are RETURNED (matched=False) so coverage
    gaps are counted, never dropped."""
    by_span: dict[str, dict] = {}
    for u in usages:
        span = _usage_span(u)
        if span:
            # First write wins; a request id maps to one usage row. If a retry
            # produced two, the earlier (settled) one is authoritative enough for
            # a reconciliation count — exact dedup is the ledger's job, not ours.
            by_span.setdefault(span, u)

    rows: list[dict[str, Any]] = []
    for d in decisions:
        vsr = d.get("vsr")
        if not vsr:
            continue  # not a VSR-acted request — out of scope.
        span = str(d.get("span_id") or "")
        decision = str(vsr.get("decision") or "")
        suggested = vsr.get("suggested_model")
        chosen_model = (d.get("chosen") or {}).get("model")
        u = by_span.get(span)
        matched = u is not None
        billed_model = str(u.get("model_id") or "") if matched else None
        cost = _as_int(u.get("cost_microusd")) if matched else None

        rows.append({
            "tenant_id": d.get("tenant_id", ""),
            "span_id": span,
            "vsr_decision": decision,
            "suggested_model": suggested,
            "mode": vsr.get("mode"),
            "config_version": vsr.get("config_version"),
            "chosen_model": chosen_model,
            "requested_model": d.get("requested_model", ""),
            "matched": matched,
            "billed_model_id": billed_model,
            "cost_microusd": cost,
            "enforcement": _enforcement(decision, suggested, chosen_model, matched),
        })
    return rows


def _enforcement(decision: str, suggested, chosen_model, matched: bool) -> str:
    """Verdict for whether the VSR's advice was honored at the money path.

    We can only *enforce* a HARD pin (a `prefer` is advisory — a local SAAR
    prefer legitimately overrides it, so a mismatch there is expected, never a
    violation). For a hard pin, the check is advised alias == committed alias;
    both are model aliases (same unit) recorded on the decision item itself, so
    no billed-id ↔ alias translation is needed. An unmatched decision cannot be
    proven either way (no committed side to compare once the request never
    settled) → UNSETTLED."""
    if decision != _HARD_DECISION:
        return ENFORCE_NA
    if not matched:
        return ENFORCE_UNSETTLED
    if suggested and chosen_model and str(suggested) == str(chosen_model):
        return ENFORCE_HONORED
    return ENFORCE_VIOLATION


def summarize(joined: list[dict]) -> dict[str, Any]:
    """Fold joined rows into the reconciliation report counters. Billed cost is
    summed over MATCHED rows only — an honest partial sum whose coverage is made
    explicit by matched_count / unsettled_count (never a fabricated total)."""
    by_decision: dict[str, int] = {}
    billed_sum = 0
    matched = unsettled = 0
    honored = violation = na = unsettled_enf = 0
    for r in joined:
        by_decision[r["vsr_decision"]] = by_decision.get(r["vsr_decision"], 0) + 1
        if r.get("matched"):
            matched += 1
            billed_sum += int(r.get("cost_microusd") or 0)
        else:
            unsettled += 1
        enf = r.get("enforcement")
        if enf == ENFORCE_HONORED:
            honored += 1
        elif enf == ENFORCE_VIOLATION:
            violation += 1
        elif enf == ENFORCE_UNSETTLED:
            unsettled_enf += 1
        else:
            na += 1
    return {
        "vsr_acted_count": len(joined),
        "matched_count": matched,
        "unsettled_count": unsettled,
        "billed_microusd_matched_sum": billed_sum,
        "enforcement_honored": honored,
        "enforcement_violation": violation,
        "enforcement_na": na,
        "enforcement_unsettled": unsettled_enf,
        "by_decision": by_decision,
    }


def _day_iso_bounds(day: str) -> tuple[str, str]:
    """('YYYYMMDD') → the [lo, hi] ISO-prefix bounds for a UsageLogs SK range
    query on that UTC calendar day. The SK is '{iso}#{log_id}'; every id in the
    day sorts within ['YYYY-MM-DD', 'YYYY-MM-DD\\uffff']."""
    iso = f"{day[0:4]}-{day[4:6]}-{day[6:8]}"
    return iso, iso + "￿"


def _query_usage_day(tenant_id: str, day: str) -> list[dict]:
    """Read a (tenant, day) of UsageLogs rows, paginated. Mirrors the admin
    usage API's tenant+SK-range query — no new access pattern."""
    from boto3.dynamodb.conditions import Key

    from dynamo import UsageLogsRepository

    lo, hi = _day_iso_bounds(day)
    table = UsageLogsRepository()._table
    out: list[dict] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": (
            Key("tenant_id").eq(tenant_id)
            & Key("timestamp_log_id").between(lo, hi)
        ),
    }
    while True:
        resp = table.query(**kwargs)
        out.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return out
        kwargs["ExclusiveStartKey"] = lek


def reconcile_day(*, tenant_id: str, day: str) -> dict[str, Any]:
    """Join a (tenant, day) of VSR decisions against billed usage and return
    {'summary': {...}, 'rows': [...]}. INTERNAL ops path (no request-path use).

    `day` is 'YYYYMMDD' (UTC). Decision records are read from the routing-signals
    table (decision_log.query_day); usage rows from the UsageLogs table. A
    day-boundary crosser (decision and usage in different UTC days) shows up as
    an unsettled decision plus an unmatched usage row — the same honest coverage
    treatment decision_log.day_summary already applies."""
    decisions = [
        r for r in dl.query_day(tenant_id=tenant_id, day=day)
        if r.get("record_type") == "decision"
    ]
    usages = _query_usage_day(tenant_id, day)
    rows = reconcile_join(decisions, usages)
    return {"summary": summarize(rows), "rows": rows}
