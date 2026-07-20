"""Counterfactual savings — the "Savings Certificate" engine (VSR core weapon).

The number LiteLLM structurally cannot produce (docs/design/
vsr-savings-certificate.md): for each VSR-acted request, "if the tenant had
FOLLOWED the VSR's routing advice, how much cheaper (or dearer) would this exact
workload have been?" — computed so the comparison is APPLES-TO-APPLES and the
bias can only fall to the CONSERVATIVE (VSR-unfavourable) side, because the whole
value proposition is that the number is a certificate, not a dashboard estimate.

FABLE REVIEW FIXES (why this is model-vs-model, not billed−cf):
The billed usage row records only (model_id, input_tokens, output_tokens,
cost_microusd) — NO pricing version and NO cache-token breakdown. So we CANNOT
reconstruct the billed model's historical, cache-inclusive charge. Naively doing
`billed_microusd − actual_cost(suggested, in, out)` mixes a past versioned,
cache-inclusive charge (billed) with a present, cache-free estimate (cf) — a
double asymmetry that both fall VSR-favourable (Fable findings 1 + 3).

Resolution: price BOTH the billed model AND the suggested model at ONE rate
snapshot, over the SAME (input, output) tokens. `saving = recompute(billed_model)
− recompute(suggested_model)`. Both legs use the identical rate basis and the
identical (cache-free) token treatment, so rate drift and cache asymmetry cancel
exactly. We ALSO recompute the billed model and compare it to the actual
`cost_microusd`: a large divergence (`basis_drift`) is surfaced, never silently
folded into savings. The rate snapshot version is stamped on the certificate so a
past (tenant, day) recomputes to the same number (audit reproducibility).

SCOPE: PURE fold over reconcile-join rows (VSR decision + billed model/cost +
billed tokens). Money side only; routing QUALITY is a separate tenant eval —
`quality.measured=false` until it fills, and no saving is CLAIMED before then.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# Single source of truth for the two decision-label sets (Fable findings d +
# shadow-label review): imported from vsr.client so a new label there can never
# silently fall to `no_suggestion` and shrink a base. STEERING = REALIZED savings
# (execution was steered); SHADOW = POTENTIAL savings (advice only, not enacted).
# These are kept as SEPARATE bases — never unioned — so a potential (hypothetical)
# saving can never be summed into the realized headline (Fable: "we don't count
# savings we didn't deliver").
from ..vsr.client import SHADOW_DECISIONS as _SHADOW_DECISIONS
from ..vsr.client import STEERING_DECISIONS as _STEERING_DECISIONS

# A row is in the savings computation iff its decision is in EITHER base; the
# `enacted` flag (set per row) records which side it belongs to.
_SAVINGS_DECISIONS = _STEERING_DECISIONS | _SHADOW_DECISIONS

# A recomputed-billed vs actual-billed divergence beyond this fraction is flagged
# as `basis_drift` (rate change since the charge, or cache-heavy billing the
# cache-free recompute can't see) rather than trusted as a counterfactual base.
_BASIS_DRIFT_TOLERANCE = 0.25


def _default_pricer() -> Callable[[str, int, int], int]:
    """(pricing_key, input_tokens, output_tokens) -> micro-USD at ONE rate
    snapshot. Injected so the fold stays pure and unit-testable."""
    from ..pricing import actual_cost_microusd

    def _price(pricing_key: str, input_tokens: int, output_tokens: int) -> int:
        return actual_cost_microusd(
            pricing_key=pricing_key, input_tokens=input_tokens,
            output_tokens=output_tokens)
    return _price


def _default_resolver() -> Callable[[str], Optional[dict]]:
    """model alias/id -> {'pricing_key', 'bedrock_model_id'} or None. Injected."""
    from ..models import resolve_model

    def _r(model: str) -> Optional[dict]:
        try:
            e = resolve_model(str(model))
            return {"pricing_key": e.pricing_key, "bedrock_model_id": e.bedrock_model_id}
        except Exception:  # noqa: BLE001 — unknown/retired model = data gap, not a crash
            return None
    return _r


def counterfactual_row(
    row: dict,
    *,
    price: Optional[Callable[[str, int, int], int]] = None,
    resolve: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict[str, Any]:
    """Per-request model-vs-model counterfactual from ONE reconcile-join row.

    Exhaustive, mutually-exclusive `class`:
      * no_suggestion — VSR steered nothing (out of base).
      * unmatched     — no billed usage row (coverage gap).
      * no_cost       — matched but no billed cost recorded (cannot cross-check).
      * no_tokens     — matched, cost present, but token counts missing/zero.
      * unpriceable   — suggested OR billed model has no pricing key (data gap).
      * followed      — billed model IS the suggested model (same bedrock id):
                        saving already realised in the bill, delta 0.
      * basis_drift   — recomputed billed diverges from the actual charge beyond
                        tolerance (rate change / cache-heavy bill) — EXCLUDED from
                        savings so a stale/asymmetric basis never inflates it.
      * counterfactual— priced both models at one snapshot; saving =
                        recompute(billed) − recompute(suggested) (signed).
    """
    price = price or _default_pricer()
    resolve = resolve or _default_resolver()

    decision = str(row.get("vsr_decision") or "")
    suggested = row.get("suggested_model")
    billed_model = row.get("billed_model_id")
    # `enacted` = did execution actually follow the advice this turn (realized) or
    # was it advice only (shadow, potential)? Recorded per row so summarize can
    # keep the two bases separate and never sum potential into realized.
    enacted = decision in _STEERING_DECISIONS
    out: dict[str, Any] = {
        "tenant_id": row.get("tenant_id"),
        "span_id": row.get("span_id"),
        "vsr_decision": decision,
        "enacted": enacted,
        "suggested_model": suggested,
        "billed_model_id": billed_model,
        "billed_microusd": row.get("cost_microusd"),
        "recompute_billed_microusd": None,
        "recompute_suggested_microusd": None,
        "saving_microusd": 0,
        "class": None,
    }

    def _fin(cls: str) -> dict:
        out["class"] = cls
        return out

    if decision not in _SAVINGS_DECISIONS or not suggested:
        return _fin("no_suggestion")
    if not row.get("matched"):
        return _fin("unmatched")
    if row.get("cost_microusd") is None:
        return _fin("no_cost")               # (Fable b) no billed cost -> not a fake loss
    tin, tout = row.get("input_tokens"), row.get("output_tokens")
    if tin is None or tout is None or (int(tin) == 0 and int(tout) == 0):
        return _fin("no_tokens")
    if not billed_model:
        return _fin("unpriceable")

    sug = resolve(str(suggested))
    bil = resolve(str(billed_model))
    if sug is None or bil is None:
        return _fin("unpriceable")

    # (Fable 2) followed = SAME bedrock model id, not merely same pricing key.
    if sug["bedrock_model_id"] == bil["bedrock_model_id"]:
        return _fin("followed")

    tin, tout = int(tin), int(tout)
    recompute_billed = int(price(bil["pricing_key"], tin, tout))
    recompute_sug = int(price(sug["pricing_key"], tin, tout))
    out["recompute_billed_microusd"] = recompute_billed
    out["recompute_suggested_microusd"] = recompute_sug

    # (Fable 1 + 3) cross-check the one-snapshot recompute of the BILLED model
    # against the ACTUAL charge. A large gap means the charge used a different
    # rate version or cache-heavy pricing this cache-free recompute cannot see;
    # its saving basis is untrustworthy -> exclude (never inflate silently).
    actual = int(row.get("cost_microusd"))
    if actual > 0:
        drift = abs(recompute_billed - actual) / actual
        if drift > _BASIS_DRIFT_TOLERANCE:
            out["basis_drift_fraction"] = round(drift, 4)
            return _fin("basis_drift")

    # model-vs-model at ONE snapshot, SAME tokens -> apples-to-apples, signed.
    out["saving_microusd"] = recompute_billed - recompute_sug
    return _fin("counterfactual")


def summarize_savings(rows: list[dict], *, price=None, resolve=None) -> dict[str, Any]:
    """Fold reconcile-join rows into a Savings Certificate summary. Every counter
    is exact and every exclusion is named with BOTH its count AND its billed
    micro-USD (Fable 5: a count-only class hides how much spend it represents).

    REALIZED vs POTENTIAL (Fable shadow-label review, case 2): the top-level
    headline `net_saving_microusd` is the REALIZED saving — counterfactual rows the
    VSR actually STEERED (`enacted=True`). Rows the shadow VSR only ADVISED
    (`enacted=False`, execution not changed) are aggregated SEPARATELY under
    `potential` and NEVER summed into the headline — a hypothetical saving must not
    be shown as delivered ("we don't count savings we didn't deliver"). Both use
    the identical model-vs-model recompute; `potential` carries an explicit caveat
    that it is an UPPER-BOUND estimate (a real switch could change output length or
    force retries, and quality is unmeasurable since the suggested model never ran).

    Headline decomposition is nested with deliberately un-promotable names (Fable
    4); `net` can be negative."""
    class_counts: dict[str, int] = {}
    class_billed: dict[str, int] = {}     # billed micro-USD per class (matched only)

    def _acc() -> dict:
        return {"net": 0, "positive": 0, "negative": 0, "billed_base": 0,
                "priced": 0, "detail": []}
    realized = _acc()      # enacted=True  (STEERING_DECISIONS)
    potential = _acc()     # enacted=False (SHADOW_DECISIONS, advice only)
    seen: set[tuple] = set()             # (Fable c) span-level dedup
    for r in rows:
        key = (r.get("tenant_id"), r.get("span_id"))
        if key in seen:
            _bump(class_counts, "duplicate")
            continue
        seen.add(key)
        cr = counterfactual_row(r, price=price, resolve=resolve)
        cls = cr["class"]
        _bump(class_counts, cls)
        if r.get("cost_microusd") is not None:
            class_billed[cls] = class_billed.get(cls, 0) + int(r.get("cost_microusd"))
        if cls == "counterfactual":
            acc = realized if cr.get("enacted") else potential
            s = int(cr["saving_microusd"])
            acc["net"] += s
            if s >= 0:
                acc["positive"] += s
            else:
                acc["negative"] += -s
            acc["billed_base"] += int(cr.get("billed_microusd") or 0)
            acc["priced"] += 1
            acc["detail"].append(cr)
    for acc in (realized, potential):
        # (Fable 4) audit-sort detail by |saving| desc so a large item never falls
        # off the truncation tail.
        acc["detail"].sort(key=lambda d: abs(int(d.get("saving_microusd") or 0)),
                           reverse=True)
    total_billed = sum(class_billed.values())
    return {
        # HEADLINE = REALIZED only (never includes potential/shadow).
        "net_saving_microusd": realized["net"],     # headline, top-level, can be negative
        "priced_request_count": realized["priced"],
        "billed_microusd_over_priced_base": realized["billed_base"],
        "total_billed_microusd_all_classes": total_billed,   # honest denominator
        "decomposition": {
            "positive_deltas_microusd": realized["positive"],   # NOT "gross saving"
            "negative_deltas_microusd": realized["negative"],   # dearer-if-followed magnitude
        },
        # SEPARATE section — advice the VSR did NOT enact. UPPER-BOUND estimate.
        "potential": {
            "net_saving_microusd": potential["net"],
            "priced_request_count": potential["priced"],
            "billed_microusd_over_priced_base": potential["billed_base"],
            "decomposition": {
                "positive_deltas_microusd": potential["positive"],
                "negative_deltas_microusd": potential["negative"],
            },
            "enacted": False,
            "note": ("UPPER-BOUND estimate of savings the VSR ADVISED but did NOT "
                     "enact (execution stayed on the client's model). A real switch "
                     "could change output length or force retries, and quality is "
                     "UNMEASURABLE since the suggested model never ran. Never summed "
                     "into the realized headline."),
            "detail": potential["detail"][:200],
        },
        "class_counts": class_counts,
        "class_billed_microusd": class_billed,       # (Fable 5) spend per class
        "quality": {"measured": False, "note": "fill from tenant eval + VSR quality signal"},
        "detail": realized["detail"][:200],
    }


def _bump(d: dict, k: str) -> None:
    d[k] = d.get(k, 0) + 1


def savings_certificate(*, tenant_id: str, day: str,
                        traffic: str = "real") -> dict[str, Any]:
    """Assemble a (tenant, day) Savings Certificate: join VSR decisions against
    billed usage (`vsr_reconcile.reconcile_day`, which carries billed tokens),
    then fold the model-vs-model counterfactual. Stamps the rate-table version
    used so a re-run reproduces the number (Fable 1 reproducibility). INTERNAL ops
    path — same posture as vsr_reconcile (reads DynamoDB directly, no request
    path, no new table).

    `traffic` is a PROVENANCE stamp carried on the certificate itself (Fable
    savings-certificate review): "real" for a genuine tenant's traffic, "synthetic"
    for a seeded demo/sample run. A product whose weapon is *honest proof* must
    never let a synthetic sample be mistaken for a real audited number — so the
    provenance lives in the certificate schema (and is surfaced by the CLI), not
    only in a caller's memory."""
    from . import vsr_reconcile as vr
    from ..pricing import effective_rates

    report = vr.reconcile_day(tenant_id=tenant_id, day=day)
    savings = summarize_savings(report["rows"])
    rate_version, _, _ = effective_rates()
    return {"tenant_id": tenant_id, "day": day,
            "traffic": traffic,
            "rate_version": rate_version or "builtin-defaults",
            "savings": savings, "reconcile": report["summary"]}
