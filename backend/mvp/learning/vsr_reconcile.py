"""Offline VSR billing reconciliation (P0 — observability closer).

Joins, per (tenant, day), the routing **decision log** (what the external VSR
advised and how Stratoclave's trust boundary treated it — written at reserve,
keyed by span_id) against the **UsageLogs** table (the effective model billed
and its micro-USD cost — keyed by the same request id). It answers the three
questions Stratoclave owns at the boundary, and ONLY those (the VSR keeps its
own routing-quality metrics; see docs/VSR_CONFIG_CONTRACT.md §4):

  1. billing reconciliation — for each VSR-acted request, what did it cost;
  2. enforcement integrity — was a HARD pin actually honored (advised model ==
     the model that was ACTUALLY BILLED, both normalized to the registry's
     bedrock id), or did the money path bill a different model — a trust-boundary
     violation. Judged against the billed usage row, NOT the decision's own
     self-reported `chosen` (which is reserve-time and can't reveal a routing
     layer that recorded X then invoked Y);
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

# enforcement verdicts — a closed set (ENFORCE_VERDICTS) so the CLI/report and
# the property tests can rely on it exhaustively.
ENFORCE_HONORED = "honored"          # hard pin: advised model == BILLED model
ENFORCE_VIOLATION = "violation"      # hard pin: BILLED model != advised model
ENFORCE_NA = "n/a"                   # prefer/overridden/no-advice/timeout — nothing to enforce
ENFORCE_UNSETTLED = "unsettled"      # decision present, no billed usage row to compare
ENFORCE_INDETERMINATE = "indeterminate"  # hard pin but advised/billed model missing — data gap, NOT a breach
ENFORCE_VERDICTS = frozenset({
    ENFORCE_HONORED, ENFORCE_VIOLATION, ENFORCE_NA, ENFORCE_UNSETTLED,
    ENFORCE_INDETERMINATE,
})


def _usage_span(item: dict) -> str:
    """The request id a UsageLogs row belongs to. The SK is '{iso}#{log_id}'
    where log_id == request_id == the decision's span_id (usage_logs.record:
    ``log_id = request_id or uuid4()``). Robust to a missing/`#`-less SK and to
    a log_id that itself contains '#' (split once, keep the remainder)."""
    tsl = str(item.get("timestamp_log_id") or "")
    return tsl.split("#", 1)[1] if "#" in tsl else ""


def _as_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _norm_model(name, resolve) -> Optional[str]:
    """Normalize a model alias OR a billed bedrock model id to the SAME
    canonical token (the registry's bedrock_model_id) so an advised alias and a
    billed effective id can be compared as equals. Returns None when `name` is
    empty or unresolvable — the caller treats that as a data gap, never a match.

    `resolve` is injected (mvp.models.resolve_model by default) so this module
    can be unit-tested without the registry and so the reconciliation itself is
    a pure function of (records, resolver)."""
    if not name:
        return None
    try:
        entry = resolve(str(name))
    except Exception:  # noqa: BLE001 — an unknown/retired model is a data gap, not a crash.
        return None
    return getattr(entry, "bedrock_model_id", None) or str(name)


def _default_resolver():
    from ..models import resolve_model
    return resolve_model


def reconcile_join(
    decisions: list[dict],
    usages: list[dict],
    *,
    resolve=None,
) -> list[dict[str, Any]]:
    """PURE join of decision items and usage rows on (tenant_id, span_id).

    Only decisions that carry a `vsr` block are in scope (a plain routing
    decision is ignored). Each returned row records the advice, the committed
    alias (reserve-time intent), the BILLED model+cost (settle-time truth, or
    None when unmatched), and an `enforcement` verdict computed against the
    BILLED model — not the decision's own self-reported `chosen` (which is
    reserve-time and cannot reveal a routing layer that recorded X then invoked
    Y). Unmatched VSR decisions are RETURNED (matched=False) so coverage gaps
    are counted, never dropped.

    Duplicate decision records for the same (tenant, span) — the decision log is
    fire-and-forget and may be retried — are de-duplicated (first wins) so a
    replayed decision can never double-count its cost.

    `resolve` maps a model alias/id to a `ModelEntry` (default: the live
    registry); injected for testability and to keep the join pure."""
    if resolve is None:
        resolve = _default_resolver()

    by_span: dict[tuple[str, str], dict] = {}
    for u in usages:
        span = _usage_span(u)
        if span:
            # First write wins; a request id maps to one usage row. If a retry
            # produced two (same log_id, different timestamp SK), only one is
            # counted here — exact money dedup is the ledger's job, and counting
            # both would inflate billed_microusd_matched_sum.
            by_span.setdefault((str(u.get("tenant_id") or ""), span), u)

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for d in decisions:
        vsr = d.get("vsr")
        if not vsr:
            continue  # not a VSR-acted request — out of scope.
        tenant = str(d.get("tenant_id") or "")
        span = str(d.get("span_id") or "")
        key = (tenant, span)
        if key in seen:
            continue  # duplicate (retried) decision — count once.
        seen.add(key)
        decision = str(vsr.get("decision") or "")
        suggested = vsr.get("suggested_model")
        chosen_model = (d.get("chosen") or {}).get("model")
        u = by_span.get(key)
        matched = u is not None
        billed_model = str(u.get("model_id") or "") if matched else None
        cost = _as_int(u.get("cost_microusd")) if matched else None

        rows.append({
            "tenant_id": tenant,
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
            # Token counts from the billed usage row — the SAME tokens the request
            # actually produced, so a counterfactual "what would the suggested
            # model have cost" (savings.py) prices the identical workload, never a
            # re-estimate. None when unmatched (no usage row to read tokens from).
            "input_tokens": _as_int(u.get("input_tokens")) if matched else None,
            "output_tokens": _as_int(u.get("output_tokens")) if matched else None,
            "enforcement": _enforcement(decision, suggested, billed_model, matched, resolve),
        })
    return rows


def _enforcement(decision: str, suggested, billed_model, matched: bool, resolve) -> str:
    """Verdict for whether the VSR's advice was honored AT THE MONEY PATH.

    We can only *enforce* a HARD pin (a `prefer` is advisory — a local SAAR
    prefer legitimately overrides it, so a mismatch there is expected, never a
    violation). For a hard pin the check compares the advised model against the
    model that was ACTUALLY BILLED (settle-time truth), each normalized to the
    registry's bedrock id so an alias and an effective id compare as equals.
    This is the whole point of reconciliation: a routing layer that recorded the
    pin as `chosen` but then invoked a different model is invisible to any
    decision-internal check, but shows up here as a billed-model mismatch.

      * not a hard pin           -> n/a
      * hard, no usage row       -> unsettled (nothing billed to compare)
      * hard, advised or billed model missing/unresolvable -> indeterminate
        (a DATA gap, deliberately NOT flagged as a breach — avoids false alarms)
      * hard, advised == billed  -> honored
      * hard, advised != billed  -> violation
    """
    if decision != _HARD_DECISION:
        return ENFORCE_NA
    if not matched:
        return ENFORCE_UNSETTLED
    advised = _norm_model(suggested, resolve)
    billed = _norm_model(billed_model, resolve)
    if advised is None or billed is None:
        return ENFORCE_INDETERMINATE
    return ENFORCE_HONORED if advised == billed else ENFORCE_VIOLATION


def summarize(joined: list[dict]) -> dict[str, Any]:
    """Fold joined rows into the reconciliation report counters. Billed cost is
    summed over MATCHED rows only — an honest partial sum whose coverage is made
    explicit by matched_count / unsettled_count (never a fabricated total)."""
    by_decision: dict[str, int] = {}
    # Tally each enforcement verdict by its own name — NOT via an else fallback,
    # so an unexpected verdict string surfaces (in `enforcement_unknown`) instead
    # of being silently folded into n/a.
    enf_counts: dict[str, int] = {v: 0 for v in ENFORCE_VERDICTS}
    unknown = 0
    billed_sum = 0
    matched = unsettled = 0
    for r in joined:
        by_decision[r["vsr_decision"]] = by_decision.get(r["vsr_decision"], 0) + 1
        if r.get("matched"):
            matched += 1
            billed_sum += int(r.get("cost_microusd") or 0)
        else:
            unsettled += 1
        enf = r.get("enforcement")
        if enf in enf_counts:
            enf_counts[enf] += 1
        else:
            unknown += 1
    return {
        "vsr_acted_count": len(joined),
        "matched_count": matched,
        "unsettled_count": unsettled,
        "billed_microusd_matched_sum": billed_sum,
        "enforcement_honored": enf_counts[ENFORCE_HONORED],
        "enforcement_violation": enf_counts[ENFORCE_VIOLATION],
        "enforcement_na": enf_counts[ENFORCE_NA],
        "enforcement_unsettled": enf_counts[ENFORCE_UNSETTLED],
        "enforcement_indeterminate": enf_counts[ENFORCE_INDETERMINATE],
        "enforcement_unknown": unknown,
        "by_decision": by_decision,
    }


def _day_iso_bounds(day: str) -> tuple[str, str]:
    """('YYYYMMDD') → the [lo, hi] ISO-prefix bounds for a UsageLogs SK range
    query on that UTC calendar day. The SK is '{iso}#{log_id}'; every id in the
    day sorts within ['YYYY-MM-DD', 'YYYY-MM-DD\\uffff'].

    Validates the day is exactly 8 digits — a malformed day would otherwise
    build a garbage range that silently returns 0 rows (a false 'nothing that
    day' rather than a loud error)."""
    if len(day) != 8 or not day.isdigit():
        raise ValueError(f"day must be 'YYYYMMDD' (8 digits), got {day!r}")
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
    _day_iso_bounds(day)  # validate 'YYYYMMDD' loudly before any query.
    decisions = [
        r for r in dl.query_day(tenant_id=tenant_id, day=day)
        if r.get("record_type") == "decision"
    ]
    usages = _query_usage_day(tenant_id, day)
    rows = reconcile_join(decisions, usages)
    return {"summary": summarize(rows), "rows": rows}
