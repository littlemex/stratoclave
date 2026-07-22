"""SR settle: compute the charge from replay/usage evidence against the reserve
snapshot (Fable IMPLEMENTATION_PLAN §5). PURE — no ledger writes here; this
returns the micro-USD charge the caller hands to the existing
`settle_reservation_and_log`, so the atomic ledger path is untouched.

The one invariant this file exists to guarantee: **final_charge ≤ reserve_amount,
always.** Money is fail-closed — every ambiguous case (model not in the reserve
snapshot, missing usage, missing replay) settles at the reserve amount, never
above, never under-charging the tenant into a silent loss for the operator.

charge-of-record = ledger snapshot unit price × measured tokens. SR's own cost
figure is evidence only and never enters this computation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .reservation import ConsumedProof


@dataclass(frozen=True)
class SrCharge:
    """The computed charge + why. `charge_microusd ≤ reserve_amount` always.
    `basis` records how it was derived, for the decision-log evidence and the
    divergence metric (SR cost vs ledger charge)."""
    charge_microusd: int
    billed_model: Optional[str]     # normalized registry model, or None if unknown
    basis: str                       # "measured" | "reserve-fallback:<reason>"
    reserve_amount_microusd: int


def _measured(unit_price_per_mtok: int, input_tokens: int, output_tokens: int) -> int:
    # micro-USD = unit_price_per_mtok × tokens / 1e6, matching pricing._mtok_cost.
    toks = max(input_tokens, 0) + max(output_tokens, 0)
    return unit_price_per_mtok * toks // 1_000_000


def settle_charge(
    proof: ConsumedProof,
    *,
    billed_model_raw: Optional[str],
    normalize,                       # (raw:str) -> registry model_id | None
    input_tokens: Optional[int],
    output_tokens: Optional[int],
) -> SrCharge:
    """Compute the SR charge from the reserve snapshot + measured usage.

    Fail-closed ladder (each rung settles at the reserve amount):
      1. billed_model_raw missing/unnormalizable → reserve amount.
      2. model not in the reserve snapshot pool → reserve amount (SR executed a
         model outside the priced candidate set; quarantine handled by caller).
      3. usage missing OR only one side reported → reserve amount (cannot measure a
         complete bill; a partial usage would under-charge and silently eat the
         gap as operator loss — fail-closed to the reserve instead).
      4. otherwise → snapshot_unit_price(model) × tokens, CLAMPED to the reserve
         amount so a mis-set rate or token overrun can never exceed the hold.
    """
    reserve = proof.reserve_amount_microusd
    if not billed_model_raw:
        return SrCharge(reserve, None, "reserve-fallback:no-model", reserve)
    model = normalize(billed_model_raw)
    if not model:
        return SrCharge(reserve, None, "reserve-fallback:unnormalizable", reserve)
    unit = proof.pool.price_of(model)
    if unit is None:
        return SrCharge(reserve, model, "reserve-fallback:out-of-snapshot", reserve)
    # P1-3: fail-closed on ANY untrustworthy usage. All three rungs settle at the
    # reserve; the distinct basis labels exist only so the divergence metric can
    # tell the cases apart. Order matters for that labelling:
    #   1. invalid FIRST — any PRESENT side that is negative is a garbage report;
    #      check it before the None rungs so a (None, -5) pair is labelled
    #      "invalid", not hidden under "partial". (Money is identical either way.)
    #   2. no-usage — both sides absent (cannot measure at all).
    #   3. partial — exactly one side absent; must NOT be billed as the-other-side
    #      + 0, which would under-charge and make the operator eat the difference.
    if (input_tokens is not None and input_tokens < 0) or \
       (output_tokens is not None and output_tokens < 0):
        return SrCharge(reserve, model, "reserve-fallback:invalid-usage", reserve)
    if input_tokens is None and output_tokens is None:
        return SrCharge(reserve, model, "reserve-fallback:no-usage", reserve)
    if input_tokens is None or output_tokens is None:
        return SrCharge(reserve, model, "reserve-fallback:partial-usage", reserve)
    # both sides are now present and non-negative ints, so _measured sees clean
    # values (no None, no negatives); it applies max(...,0) purely defensively.
    measured = _measured(unit, input_tokens, output_tokens)
    # clamp: final ≤ reserve, always (pool-max makes this hold, but assert-by-clamp
    # so a rate/token anomaly degrades fail-closed instead of over-charging).
    charge = min(measured, reserve)
    basis = "measured" if measured <= reserve else "reserve-clamped"
    return SrCharge(charge, model, basis, reserve)
