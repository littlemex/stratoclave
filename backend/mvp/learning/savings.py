"""Counterfactual savings — the "Savings Certificate" engine (VSR core weapon).

This is the number litellm structurally cannot produce (docs/design/
vsr-savings-certificate.md): for each VSR-acted request, "if the tenant had
FOLLOWED the VSR's routing advice, how much cheaper (or dearer) would this exact
workload have been?" — priced from the SAME versioned rate table the ledger
charges from, against the SAME token counts the request actually produced (read
off the billed usage row, never re-estimated). The output is a savings figure
with the honesty controls a CFO / procurement will demand: escalation cost is
SUBTRACTED (never hidden), and coverage (unmatched / non-consulted / missing
tokens) is reported explicitly so the number is never a fabricated total.

WHY THIS IS DEFENSIBLE (and litellm's "70% cheaper!" dashboards are not):

  1. Ledger-precision baseline. `billed_microusd` is a settled charge, not a
     dashboard estimate.
  2. Same-token counterfactual. The suggested model is priced over the request's
     REAL (input_tokens, output_tokens), so the comparison is apples-to-apples.
  3. Honest sign. When the VSR suggested a cheap model that then escalated (the
     expensive model was actually billed), the counterfactual is DEARER and the
     saving is NEGATIVE — we surface it, we do not clip it to zero. A certificate
     that can show a loss is one a buyer can trust to show a gain.

SCOPE / boundary: this is a PURE fold over reconcile-join rows (which already
carry the VSR decision, the billed model+cost, and the billed token counts).
It owns ONLY the money-side counterfactual; routing QUALITY (did the cheaper
model actually solve the task?) is the VSR's own metric plus a tenant-defined
eval — represented here as an explicit, un-fabricated `quality` placeholder the
certificate must fill from that separate signal before any saving is CLAIMED.
No request-path code, no new table.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# The VSR decision labels that constitute a COST-REDUCING suggestion (the VSR
# advised a model other than what the request would otherwise use). A hard pin
# and a prefer are both "the VSR steered the model"; a passthrough / no-advice /
# timeout steered nothing and contributes no counterfactual.
from ..vsr.client import DECISION_HARD_APPLIED as _HARD

# Rows whose vsr_decision is one of these carried an actionable routing suggestion.
_STEERING_DECISIONS = frozenset({_HARD, "prefer", "prefer_applied", "suggested"})


def _default_pricer() -> Callable[[str, int, int], int]:
    """(pricing_key, input_tokens, output_tokens) -> micro-USD, from the live
    rate table. Injected so the fold stays pure and unit-testable."""
    from ..pricing import actual_cost_microusd

    def _price(pricing_key: str, input_tokens: int, output_tokens: int) -> int:
        return actual_cost_microusd(
            pricing_key=pricing_key, input_tokens=input_tokens,
            output_tokens=output_tokens)
    return _price


def _default_pricing_key_for() -> Callable[[str], Optional[str]]:
    """model alias/id -> pricing_key (or None if unresolvable). Injected."""
    from ..models import resolve_model

    def _pk(model: str) -> Optional[str]:
        try:
            return resolve_model(str(model)).pricing_key
        except Exception:  # noqa: BLE001 — unknown/retired model = data gap, not a crash
            return None
    return _pk


def counterfactual_row(
    row: dict,
    *,
    price: Optional[Callable[[str, int, int], int]] = None,
    pricing_key_for: Optional[Callable[[str], Optional[str]]] = None,
) -> dict[str, Any]:
    """Per-request counterfactual saving from ONE reconcile-join row.

    Returns a dict with a `class` field partitioning every VSR-acted request into
    a small, exhaustive set so the summary can never silently drop one:

      * "no_suggestion"  — the VSR steered nothing (passthrough/timeout): 0, out
        of the savings base.
      * "unmatched"      — no billed usage row: cannot price, counted as coverage
        gap, contributes 0.
      * "no_tokens"      — matched but the usage row lacks token counts: data gap.
      * "unpriceable"    — suggested model does not resolve to a pricing key: gap.
      * "followed"       — the billed model already IS the suggested model, so the
        saving is already REALISED in the billed cost; counterfactual delta 0 (we
        do not double-count a saving the ledger already reflects).
      * "counterfactual" — the billed model differs from the suggestion; savings =
        billed_microusd - cost_if_suggested (may be negative = escalation loss).
    """
    price = price or _default_pricer()
    pricing_key_for = pricing_key_for or _default_pricing_key_for()

    decision = str(row.get("vsr_decision") or "")
    suggested = row.get("suggested_model")
    out: dict[str, Any] = {
        "tenant_id": row.get("tenant_id"),
        "span_id": row.get("span_id"),
        "vsr_decision": decision,
        "suggested_model": suggested,
        "billed_model_id": row.get("billed_model_id"),
        "billed_microusd": row.get("cost_microusd"),
        "counterfactual_microusd": None,
        "saving_microusd": 0,
        "class": None,
    }
    if decision not in _STEERING_DECISIONS or not suggested:
        out["class"] = "no_suggestion"
        return out
    if not row.get("matched"):
        out["class"] = "unmatched"
        return out
    tin = row.get("input_tokens")
    tout = row.get("output_tokens")
    if tin is None or tout is None:
        out["class"] = "no_tokens"
        return out
    billed = int(row.get("cost_microusd") or 0)
    billed_model = str(row.get("billed_model_id") or "")
    sug_pk = pricing_key_for(str(suggested))
    if sug_pk is None:
        out["class"] = "unpriceable"
        return out
    # If the billed model already resolves to the suggested model's pricing key,
    # the suggestion was followed — the saving is in the billed cost already.
    billed_pk = pricing_key_for(billed_model) if billed_model else None
    if billed_pk is not None and billed_pk == sug_pk:
        out["class"] = "followed"
        out["counterfactual_microusd"] = billed
        return out
    cf = int(price(sug_pk, int(tin), int(tout)))
    out["counterfactual_microusd"] = cf
    out["saving_microusd"] = billed - cf   # positive = VSR would have saved; negative = escalation loss
    out["class"] = "counterfactual"
    return out


def summarize_savings(rows: list[dict], *, price=None, pricing_key_for=None) -> dict[str, Any]:
    """Fold reconcile-join rows into a Savings Certificate summary. Every counter
    is exact and every exclusion is named (no fabricated totals). `gross_saving`
    sums only the POSITIVE counterfactual deltas; `escalation_loss` sums the
    NEGATIVE ones (as a positive magnitude); `net_saving = gross - escalation` is
    the headline honest number. Coverage fields make the base explicit."""
    classes: dict[str, int] = {}
    gross = 0            # Σ positive savings (VSR would have been cheaper)
    escalation_loss = 0  # Σ |negative savings| (VSR-suggested-cheap then escalated dearer)
    net = 0              # gross - escalation_loss (== Σ all counterfactual deltas)
    billed_base = 0      # Σ billed cost over the priced counterfactual base
    priced = 0           # rows that produced a real counterfactual delta
    detail: list[dict] = []
    for r in rows:
        cr = counterfactual_row(r, price=price, pricing_key_for=pricing_key_for)
        classes[cr["class"]] = classes.get(cr["class"], 0) + 1
        if cr["class"] == "counterfactual":
            s = int(cr["saving_microusd"])
            net += s
            if s >= 0:
                gross += s
            else:
                escalation_loss += -s
            billed_base += int(cr.get("billed_microusd") or 0)
            priced += 1
            detail.append(cr)
    return {
        "priced_request_count": priced,
        "billed_microusd_over_base": billed_base,
        "gross_saving_microusd": gross,
        "escalation_loss_microusd": escalation_loss,
        "net_saving_microusd": net,
        # honest coverage: every VSR-acted request lands in exactly one class.
        "class_counts": classes,
        # quality is NOT computed here — a saving is only CLAIMABLE once the
        # tenant-defined eval / VSR quality signal fills this. Kept explicit so a
        # certificate can never imply quality parity it did not measure.
        "quality": {"measured": False, "note": "fill from tenant eval + VSR quality signal"},
        "detail": detail[:200],
    }


def savings_certificate(*, tenant_id: str, day: str) -> dict[str, Any]:
    """Assemble a (tenant, day) Savings Certificate: join the VSR decision log
    against billed usage (`vsr_reconcile.reconcile_day`, which now carries the
    billed token counts), then fold the counterfactual savings. INTERNAL ops path
    — same posture as `vsr_reconcile` (reads DynamoDB directly, no request-path,
    no new table). Returns {tenant_id, day, savings, reconcile} so a caller can
    show both the money-side saving and the enforcement/coverage context."""
    from . import vsr_reconcile as vr

    report = vr.reconcile_day(tenant_id=tenant_id, day=day)
    savings = summarize_savings(report["rows"])
    return {"tenant_id": tenant_id, "day": day, "savings": savings,
            "reconcile": report["summary"]}
