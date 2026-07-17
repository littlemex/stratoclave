"""Routing decision log (P0 — Layer 5 ↔ router bridge).

Records, append-only, WHAT the router chose and what it REJECTED, and — after
settle — the counterfactual cost delta against the ledger's frozen (measured)
charge. This makes "the router saved X" auditable rather than a black box.

Honesty contract (structural, three places): the recorded `savings_*` is a
COUNTERFACTUAL ESTIMATE, never a measured saving. It is
`estimate_cost(baseline, chosen's ACTUAL tokens) − actual(chosen from the
ledger)`, under a same-token / same-effort assumption stamped into the item as
`savings_basis`. It is NOT "billed savings". The provability we offer is:
"the router made this decision, the ledger measured this charge, and — from the
fields stored here alone — the difference under the recorded assumptions is this."

Storage: rides the existing routing-signals table under NEW sk namespaces
(`decision#…`, `outcome#…`), NOT the credit ledger (the money write path must not
carry variable-length, schema-evolving, best-effort data). No TTL attribute —
these are audit records, kept, unlike the short-TTL learning signals in the same
table. Two records per request, joined by (run_id, span_id):
  * decision#<run_id>#<span_id>  written at reserve (estimates only) — its
    existence BEFORE the outcome is the anti-post-hoc audit property;
  * outcome#<run_id>#<span_id>   written at settle (actuals + savings).

Both writes are fire-and-forget (never block/raise on the request path). Dropped
writes are handled by coverage reconciliation against the ledger: savings totals
are reported as a PARTIAL SUM over covered spans (NOT a lower bound — recorded
savings can be negative when the router escalated, and uncovered spans are
excluded), never as a complete figure.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from ..observability.store import _safe_key_token

SCHEMA_VERSION = 1

# reject reasons — a closed enum so the completeness property can validate it.
REJECT_REASONS = frozenset({
    "allowlist",         # not in the tenant allowlist
    "breaker-cap",       # filtered by the breaker's max cost tier
    "quota-exhausted",   # per-model quota was exhausted
    "not-servable",      # unresolvable / wrong wire protocol
    "fallback-order",    # servable but ranked below the chosen candidate
})

SAVINGS_BASIS = "counterfactual_estimate_at_actual_tokens"


def _decision_pk(tenant_id: str, day: str) -> str:
    # Decision/outcome share the tenant+day partition (day-scoped dumps); a
    # distinct prefix keeps them off the learning-signal (TENANT#…#CAT#…) keyspace.
    return f"DECISION#{_safe_key_token(tenant_id)}#D#{day}"


def decision_sk(run_id: str, span_id: str) -> str:
    return f"decision#{_safe_key_token(run_id)}#{_safe_key_token(span_id)}"


def outcome_sk(run_id: str, span_id: str) -> str:
    return f"outcome#{_safe_key_token(run_id)}#{_safe_key_token(span_id)}"


def _day(created_at_ms: int) -> str:
    return time.strftime("%Y%m%d", time.gmtime(created_at_ms / 1000.0))


def build_decision_item(
    *,
    tenant_id: str,
    run_id: str,
    span_id: str,
    group_id: Optional[str],
    requested_model: str,
    selection_reason: Optional[str],
    fallback_reason: Optional[str],
    chosen: dict,
    rejected: list[dict],
    estimate_inputs: dict,
    created_at_ms: int,
) -> dict[str, Any]:
    """PURE builder for the reserve-time decision record.

    `chosen` = {model, pricing_key, cost_tier, est_cost_microusd,
                pricing_version_at_decision}.
    `rejected` = [{model, pricing_key, cost_tier, reject_reason, servable,
                   est_cost_microusd|None}].
    No TTL attribute (audit record). Deterministic sk → an idempotent retry
    overwrites byte-identically.
    """
    return {
        "pk": _decision_pk(tenant_id, _day(created_at_ms)),
        "sk": decision_sk(run_id, span_id),
        "record_type": "decision",
        "schema_version": SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "workflow_run_id": run_id,
        "span_id": span_id,
        "group_id": group_id or "",
        "decided_at_ms": int(created_at_ms),
        "requested_model": requested_model,
        "selection_reason": selection_reason or "",
        "fallback_reason": fallback_reason or "",
        "chosen": chosen,
        "rejected": list(rejected),
        "estimate_inputs": dict(estimate_inputs),
    }


def build_outcome_item(
    *,
    tenant_id: str,
    run_id: str,
    span_id: str,
    settled_at_ms: int,
    actual_total_cost_microusd: int,
    actual_input_tokens: int,
    actual_output_tokens: int,
    effort: Optional[int],
    ledger_pricing_version: Optional[str],
    counterfactual_pricing_version: Optional[str],
    savings_vs_requested: Optional[int],
    savings_vs_max_servable: Optional[int],
    counterfactual_vs_requested_microusd: Optional[int],
    counterfactual_vs_max_servable_microusd: Optional[int],
) -> dict[str, Any]:
    """PURE builder for the settle-time outcome record.

    `actual_total_cost_microusd` is a COPY of the ledger's frozen rating total —
    the ledger remains the source of truth; on any mismatch the ledger wins.
    `savings_*` may be None (no comparable baseline) or negative (the router
    escalated to a pricier model) — never clamped. `savings_basis` and both
    pricing versions are stamped so the figure is reconstructable and its
    estimate-nature is explicit.
    """
    return {
        "pk": _decision_pk(tenant_id, _day(settled_at_ms)),
        "sk": outcome_sk(run_id, span_id),
        "record_type": "outcome",
        "schema_version": SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "workflow_run_id": run_id,
        "span_id": span_id,
        "settled_at_ms": int(settled_at_ms),
        # Ledger pointer + a copy of the measured charge (ledger is source of truth).
        "actual_total_cost_microusd": int(actual_total_cost_microusd),
        # The chosen request's ACTUAL usage + effort — persisted so the
        # counterfactual (tokens × baseline rate) is recomputable from THIS item
        # alone, without a ledger join (Fable RDL review High: provability).
        "actual_input_tokens": int(actual_input_tokens),
        "actual_output_tokens": int(actual_output_tokens),
        # effort is None when it could not be recovered from the decision facts;
        # in that case the counterfactuals/savings are also None (M2), so the
        # record is honestly "usage known, savings not computable".
        "effort": int(effort) if effort is not None else None,
        "ledger_pricing_version": ledger_pricing_version or "",
        "counterfactual_pricing_version": counterfactual_pricing_version or "",
        # Counterfactual (estimate) costs the baselines WOULD have cost at the
        # chosen request's actual token counts.
        "counterfactual_vs_requested_microusd": counterfactual_vs_requested_microusd,
        "counterfactual_vs_max_servable_microusd": counterfactual_vs_max_servable_microusd,
        # savings = counterfactual(baseline) − actual(chosen). Estimate; unclamped
        # (negative when the router escalated).
        "savings_vs_requested_microusd": savings_vs_requested,
        "savings_vs_max_servable_microusd": savings_vs_max_servable,
        "savings_basis": SAVINGS_BASIS,
    }


# ---- fire-and-forget emit (reuses the signals sink executor) ----


def emit_decision(item: dict) -> None:
    """Fire-and-forget write of a pre-built decision/outcome item. NEVER raises,
    NEVER blocks the event loop — same contract as emit_signal; a dropped write
    is caught by coverage reconciliation, not by failing the request."""
    from . import signals

    signals._submit(lambda: _put(item))


def record_decision_from_context(context) -> None:
    """Build + fire-and-forget the reserve-time decision record from a settled/
    reserved context. No-op (never raises) when the context lacks decision facts
    (single-candidate / no-config passthrough) or a run/span id. Called right
    after the reserve chokepoint returns, so the decision is on record BEFORE the
    outcome (the anti-post-hoc audit property)."""
    try:
        facts = getattr(context, "decision_facts", None)
        run_id = getattr(context, "workflow_run_id", None)
        span_id = getattr(context, "request_id", None) or getattr(context, "span_id", None)
        if not facts or not run_id or not span_id:
            return
        item = build_decision_item(
            tenant_id=getattr(context, "tenant_id", ""),
            run_id=run_id,
            span_id=span_id,
            group_id=getattr(context, "group_id", None),
            requested_model=getattr(context, "requested_model", "") or "",
            selection_reason=None,
            fallback_reason=None,
            chosen=facts["chosen"],
            rejected=facts["rejected"],
            estimate_inputs=facts.get("estimate_inputs", {}),
            created_at_ms=_now_ms(),
        )
        emit_decision(item)
    except Exception:  # noqa: BLE001 — decision logging must never break a request.
        pass


def _now_ms() -> int:
    import time as _t

    return int(_t.time() * 1000)


def record_outcome_from_context(
    context,
    *,
    actual_total_cost_microusd: int,
    actual_input_tokens: int,
    actual_output_tokens: int,
    ledger_pricing_version: Optional[str],
) -> None:
    """Build + fire-and-forget the settle-time outcome record. Recomputes the
    counterfactual cost of the baselines AT THE CHOSEN REQUEST'S ACTUAL TOKENS
    (the honest savings basis) and the two savings figures. No-op / never raises
    when decision facts or ids are missing. `actual_*` come from the ledger's
    frozen rating (the measured charge)."""
    try:
        facts = getattr(context, "decision_facts", None)
        run_id = getattr(context, "workflow_run_id", None)
        span_id = getattr(context, "request_id", None) or getattr(context, "span_id", None)
        if not facts or not run_id or not span_id:
            return

        from ..pricing import BUILTIN_VERSION, effective_rates, estimate_cost_microusd

        # effort MUST come through from the decision facts. If it is missing (e.g.
        # the estimate_inputs build was fenced at reserve), we must NOT silently
        # assume effort=1 — that would compute a too-low counterfactual and record
        # a self-consistent WRONG savings (Fable RDL review-2 M2). Propagate None
        # so no savings is fabricated.
        _ei = facts.get("estimate_inputs") or {}
        effort = _ei.get("effort")
        effort = int(effort) if effort is not None else None
        requested_model = getattr(context, "requested_model", "") or ""
        # The pricing version the counterfactual estimate below is computed under
        # (estimate_cost_microusd reads the live effective rates). Stamp the ACTUAL
        # version used, not the decision-time one, so the recorded counterfactual
        # is reconstructable (Fable RDL review Medium).
        cf_version = effective_rates()[0] or BUILTIN_VERSION

        def _cf(pricing_key: Optional[str]) -> Optional[int]:
            # Counterfactual: what the baseline WOULD have cost at the chosen
            # request's ACTUAL tokens + same effort. estimate (not measured).
            # None when the input is unusable (no key / unknown effort) or a single
            # candidate's pricing raises — the failure is confined to THAT baseline,
            # never dropping the whole outcome record (Fable review-2 M3).
            if not pricing_key or effort is None:
                return None
            try:
                return estimate_cost_microusd(
                    pricing_key=pricing_key,
                    input_tokens_est=int(actual_input_tokens),
                    max_output_tokens=int(actual_output_tokens),
                    effort_multiplier=effort,
                )
            except Exception:  # noqa: BLE001 — one bad key must not sink the record.
                return None

        # Baseline 1 (primary): the requested model — "if the router weren't here".
        # Find its pricing_key among chosen/rejected (it may BE the chosen).
        req_pk = None
        chosen = facts.get("chosen", {})
        if chosen.get("model") == requested_model:
            req_pk = chosen.get("pricing_key")
        else:
            for r in facts.get("rejected", []):
                if r.get("model") == requested_model:
                    req_pk = r.get("pricing_key")
                    break
        cf_requested = _cf(req_pk)
        savings_requested = (
            cf_requested - int(actual_total_cost_microusd) if cf_requested is not None else None
        )

        # Baseline 2 (reference): the most-expensive SERVABLE rejected candidate.
        # A candidate whose counterfactual is None (no pricing_key) is EXCLUDED,
        # not coerced to 0 — else a fabricated 0-cost baseline would produce a
        # bogus negative savings (Fable RDL review High: null≠0).
        servable = [r for r in facts.get("rejected", []) if r.get("servable")]
        cf_vals = [
            v for r in servable if (v := _cf(r.get("pricing_key"))) is not None
        ]
        cf_max = max(cf_vals) if cf_vals else None
        savings_max = (
            cf_max - int(actual_total_cost_microusd) if cf_max is not None else None
        )

        item = build_outcome_item(
            tenant_id=getattr(context, "tenant_id", ""),
            run_id=run_id,
            span_id=span_id,
            settled_at_ms=_now_ms(),
            actual_total_cost_microusd=int(actual_total_cost_microusd),
            actual_input_tokens=int(actual_input_tokens),
            actual_output_tokens=int(actual_output_tokens),
            effort=effort,
            ledger_pricing_version=ledger_pricing_version,
            counterfactual_pricing_version=cf_version,
            savings_vs_requested=savings_requested,
            savings_vs_max_servable=savings_max,
            counterfactual_vs_requested_microusd=cf_requested,
            counterfactual_vs_max_servable_microusd=cf_max,
        )
        emit_decision(item)
    except Exception:  # noqa: BLE001 — outcome logging must never break settle.
        pass


def _put(item: dict) -> None:
    """Low-level PutItem for a decision/outcome item (audit: no TTL). Bounded
    retry for transient throttling; never raises past the guarded submit."""
    from core.logging import get_logger
    from dynamo.client import get_dynamodb_resource

    logger = get_logger(__name__)
    table = get_dynamodb_resource().Table(signals_table_name())
    last = None
    for _ in range(2):
        try:
            table.put_item(Item=item)
            return
        except Exception as e:  # noqa: BLE001 — best-effort; retry then give up.
            last = e
    try:
        logger.warning("routing_decision_write_failed", error=str(last))
    except Exception:
        pass


def signals_table_name() -> str:
    from . import signals

    return signals._TABLE_NAME


def day_summary(*, tenant_id: str, day: str) -> dict:
    """Aggregate a (tenant, day) of decision/outcome records for the ops CLI.

    Returns decision/outcome counts, the SUMMED savings over outcome records
    that HAVE a savings figure (a PARTIAL SUM — NOT a lower bound: recorded
    savings can be negative and uncovered/null-baseline spans are excluded), and
    how many outcomes carried each baseline. Deliberately does NOT reconcile
    against the ledger here (that is a separate scan the CLI can layer on); this
    is a pure fold of what was
    recorded, so 'coverage' is expressed as counts, not a fabricated total."""
    rows = query_day(tenant_id=tenant_id, day=day)
    decisions = [r for r in rows if r.get("record_type") == "decision"]
    outcomes = [r for r in rows if r.get("record_type") == "outcome"]

    def _key(r: dict) -> tuple:
        return (str(r.get("workflow_run_id", "")), str(r.get("span_id", "")))

    # Coverage gap = the SET of (run, span) with a decision but no outcome in this
    # partition — a real join, not a count subtraction (Fable RDL review: a
    # count-subtraction lets an outcome that flowed in from a day-boundary-crossing
    # request cancel out a genuinely-missing one, under-reporting the gap).
    decision_keys = {_key(r) for r in decisions}
    outcome_keys = {_key(r) for r in outcomes}
    missing = decision_keys - outcome_keys
    # Outcomes whose decision is NOT in this partition — expected for requests
    # that crossed the UTC day boundary (decision landed in the previous day's
    # partition). Surfaced so a day-boundary artifact is observable rather than
    # silently inflating/deflating the gap (Fable RDL review-2 M4).
    orphan_outcomes = outcome_keys - decision_keys

    def _sum(field: str) -> tuple[int, int, int]:
        total = 0
        n = 0
        n_negative = 0
        for o in outcomes:
            v = o.get(field)
            if v is not None:
                total += int(v)
                n += 1
                if int(v) < 0:
                    n_negative += 1
        return total, n, n_negative

    sv_req, n_req, neg_req = _sum("savings_vs_requested_microusd")
    sv_max, n_max, neg_max = _sum("savings_vs_max_servable_microusd")
    return {
        "tenant_id": tenant_id,
        "day": day,
        "decision_count": len(decisions),
        "outcome_count": len(outcomes),
        # (run, span) with a decision but no outcome (failed/unsettled or dropped
        # write) — a coverage gap.
        "decisions_without_outcome": len(missing),
        # (run, span) with an outcome but no same-day decision — day-boundary
        # crossers (decision in the prior day's partition) or a dropped decision.
        "outcomes_without_decision": len(orphan_outcomes),
        # Summed savings over outcomes that HAVE a figure. This is a PARTIAL SUM
        # over `_sample` of the recorded spans, NOT a true lower bound: some
        # recorded savings are negative (router escalated) and some spans are
        # uncovered, so the population total could be either higher or lower.
        # `_negative_sample` surfaces how many recorded spans were negative.
        "savings_vs_requested_microusd_partial_sum": sv_req,
        "savings_vs_requested_sample": n_req,
        "savings_vs_requested_negative_sample": neg_req,
        "savings_vs_max_servable_microusd_partial_sum": sv_max,
        "savings_vs_max_servable_sample": n_max,
        "savings_vs_max_servable_negative_sample": neg_max,
        "basis": SAVINGS_BASIS,
    }


def query_day(*, tenant_id: str, day: str) -> list[dict]:
    """Read all decision + outcome records for one (tenant, day), paginated.

    Used by the coverage reconciliation and the internal ops CLI — not on any
    request path. `day` is 'YYYYMMDD'."""
    from boto3.dynamodb.conditions import Key

    from dynamo.client import get_dynamodb_resource

    table = get_dynamodb_resource().Table(signals_table_name())
    out: list[dict] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq(_decision_pk(tenant_id, day)),
    }
    while True:
        resp = table.query(**kwargs)
        out.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return out
        kwargs["ExclusiveStartKey"] = lek
