"""Minimal LOCAL shadow VSR — the judgment-only router that makes the Savings
Certificate non-empty without a real external semantic router (docs/design/
vsr-savings-certificate.md; Fable "C + minimal shadow VSR" recommendation).

WHAT IT IS. A PURE, rule-based judge: given a request (the model the client asked
for + a few cheap features of the prompt), decide whether a CHEAPER model in the
same family would plausibly have sufficed, and if so emit a `shadow-advised`
suggestion. It NEVER steers execution — the client's pinned model is still what
runs and what is billed. The suggestion is recorded on the decision log so the
offline Savings Certificate can compute the POTENTIAL (not realized) saving of
having followed it.

WHY LOCAL + RULE-BASED. A real VSR (a hosted semantic classifier) is a separate,
heavier system. The wedge — "insert Stratoclave, get a savings report in week one"
— only needs a judge good enough to surface a defensible counterfactual, and
honesty about its provenance (rule id on every decision, potential kept separate
from realized, quality unmeasurable). A better judge is a drop-in replacement for
`propose()` later; the accounting boundary does not change.

TRUST BOUNDARY. This module has NO side effects and touches NO money: it returns a
suggestion or None. The caller logs it (observability) and NEVER changes routing on
it (fail-open preserved). That is the whole safety argument — a shadow judge cannot
overspend, mis-route, or corrupt the ledger because it does not act.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

# Price-tier ordering, cheapest first. A shadow downgrade only ever moves DOWN this
# ladder within reach of the requested tier, so the judge can never "advise" a
# dearer model (that would be an escalation, which a cost-saving shadow never
# proposes — the certificate can still show a loss for a REAL VSR, but this local
# judge is downgrade-only by construction).
_TIER_ORDER = ("haiku", "sonnet", "opus")


def shadow_enabled() -> bool:
    """Master switch. Dark by default — the judge is inert (propose() returns None)
    unless STRATOCLAVE_SHADOW_VSR=true, so it ships without touching any request."""
    return os.getenv("STRATOCLAVE_SHADOW_VSR", "false").lower() == "true"


@dataclass(frozen=True)
class ShadowSuggestion:
    """A judgment-only suggestion. `model` is a client-facing model id destined for
    the SAME allowlist as any pin (though it is never enacted in shadow). `rule_id`
    records WHY, for audit — every shadow saving in the certificate is traceable to
    the rule that proposed it (Fable shadow-label review)."""

    model: str
    rule_id: str


# --- request features (cheap, no model call) --------------------------------

@dataclass(frozen=True)
class RequestFeatures:
    """The minimal, cheaply-computable features the rule judge reads. Deliberately
    tiny — a heavier classifier replaces `propose` wholesale, not this shape."""

    approx_input_tokens: int
    has_tools: bool
    has_images: bool


# Below this prompt size, and with no tools/images, a request is "simple enough"
# that the cheapest tier is a defensible counterfactual. Conservative on purpose:
# a shadow suggestion that is obviously wrong destroys the certificate's
# credibility faster than a missed saving does.
_SIMPLE_MAX_INPUT_TOKENS = int(os.getenv("STRATOCLAVE_SHADOW_SIMPLE_MAX_TOKENS", "1500"))


def _tier_of(pricing_key: str) -> Optional[int]:
    try:
        return _TIER_ORDER.index(pricing_key)
    except ValueError:
        return None


def propose(
    *,
    requested_model: str,
    features: RequestFeatures,
    resolve: Optional[Callable[[str], Optional[dict]]] = None,
    cheapest_model_for_tier: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[ShadowSuggestion]:
    """The pure rule judge. Returns a `ShadowSuggestion` for a plausible cheaper
    model, or None to advise nothing. Injectable resolvers keep it unit-testable
    with no registry/pricing import.

    RULES (v1, deliberately few and conservative):
      R1  a request with tools or images is NOT downgraded (tool-use / vision are
          the capabilities that most often need the stronger model) -> None.
      R2  a SIMPLE request (small prompt, no tools/images) currently pinned ABOVE
          the cheapest tier is advised down to the cheapest tier -> shadow-advised.
      R3  everything else (already cheapest, or large prompt) -> None.
    The rule id is stamped so each shadow saving is auditable to its reason."""
    if not requested_model:
        return None
    resolve = resolve or _default_resolver()
    cheapest_model_for_tier = cheapest_model_for_tier or _default_cheapest_for_tier

    # R1: capability-bearing requests are never downgraded.
    if features.has_tools or features.has_images:
        return None

    ent = resolve(str(requested_model))
    if ent is None:
        return None
    cur_tier = _tier_of(ent["pricing_key"])
    if cur_tier is None:
        return None  # unpriced / non-Claude family — out of this judge's scope.

    # R3a: already at the cheapest tier -> nothing to advise.
    if cur_tier == 0:
        return None
    # R3b: not a simple request -> keep the stronger model (no advice).
    if features.approx_input_tokens > _SIMPLE_MAX_INPUT_TOKENS:
        return None

    # R2: simple + above cheapest -> advise the cheapest tier's model.
    target_key = _TIER_ORDER[0]
    target_model = cheapest_model_for_tier(target_key)
    if not target_model:
        return None
    # Guard: never "advise" the same model (would be a no-op counterfactual).
    tgt = resolve(str(target_model))
    if tgt is None or tgt["bedrock_model_id"] == ent["bedrock_model_id"]:
        return None
    return ShadowSuggestion(model=target_model, rule_id="R2-simple-downgrade")


# --- default resolvers (registry-backed) ------------------------------------

def _default_resolver() -> Callable[[str], Optional[dict]]:
    from ..models import resolve_model

    def _r(model: str) -> Optional[dict]:
        try:
            e = resolve_model(str(model))
            return {"pricing_key": e.pricing_key, "bedrock_model_id": e.bedrock_model_id}
        except Exception:  # noqa: BLE001 — unknown model = out of scope, advise nothing
            return None
    return _r


def _default_cheapest_for_tier(pricing_key: str) -> Optional[str]:
    """A stable client-facing alias for the cheapest model in a price tier. Reads
    the registry; picks the first entry whose pricing_key matches, preferring a
    short alias for a clean certificate. Bedrock-served only (a shadow suggestion
    must be a model the tenant could actually have used)."""
    from ..models import registry_entries

    for e in registry_entries():
        if e.pricing_key == pricing_key and getattr(e, "served_by", "bedrock") == "bedrock":
            return e.aliases[0] if e.aliases else e.bedrock_model_id
    return None
