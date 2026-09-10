"""C5/C6/C7 — the one eligibility predicate every reservation path must
consult, per concrete candidate, immediately before that candidate's
reservation is attempted.

`refusal_for` reads no DynamoDB and does no network I/O: `tenant_cfg`,
`user_cfg` and `grants` are the caller's already-read, in-memory values
(the routing-config loader's own dataclasses, and PR2's
`admin_entitlements.list_entitlements()` result, unmodified) — this module
never calls either loader itself, so a predicate that could reach the
network could not be truth-tabled. It DOES resolve a configured model
spelling to its registry entry (`mvp.models.resolve_model`) for the model
axis below; that is an in-memory dict lookup against the registry this
process already loaded once at import, not I/O, and it is required —
comparing spellings as strings instead would silently diverge from the
identity comparison `_validate_model_pin` already makes (see axis 1's
docstring for why that distinction is load-bearing).

The three call sites that must consult this — `mvp._pipeline
._reserve_over_candidates` (the direct request and every chain-fallback
candidate), `mvp._pipeline._validate_model_pin` (the VSR hard pin, as defence
in depth — the pin enters the same reservation loop as everything else, so
`_reserve_over_candidates` is the actual boundary), and the two model-listing
routes (`mvp.anthropic.list_models`, `mvp.openai_responses.list_openai_models`)
— all import the SAME `refusal_for` rather than each deciding eligibility on
its own terms. `mvp.registry_checks.check_eligibility_has_one_implementation`
(C5) is the mechanical guard for that: it fails the build if any of this
module's three refusal codes is spelled as a literal anywhere else under
`backend/mvp/`, which is what makes "one predicate" a checked fact rather than
a naming convention nobody enforces.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Optional

from .models import ModelEntry
from .routing.config import effective_profile_scopes

if TYPE_CHECKING:
    from .admin_entitlements import Entitlement
    from .routing.model_resolver import RoutingConfig, UserRoutingConfig

# The three refusal codes, in the precedence `refusal_for` checks them below.
# Named here, ONCE: `check_eligibility_has_one_implementation` fails the build
# if either spelling is written again, as a string literal, anywhere else
# under `backend/mvp/` — every other call site that needs to raise, log, or
# compare one of these codes imports the constant from here instead of
# retyping the string, which is what lets that check be a plain text scan
# rather than something that has to understand what "reimplements it" means.
MODEL_NOT_ALLOWED = "model_not_allowed"
MODEL_NOT_ENTITLED = "model_not_entitled"
SCOPE_NOT_ALLOWED = "scope_not_allowed"


def _resolves_to(spellings: "Iterable[str]", entry: ModelEntry) -> bool:
    """Whether any configured spelling in `spellings` resolves, through the
    registry, to THIS SAME `entry` object (identity, not string equality).

    A tenant's `allowlist`/`chain` and a user's `chain` are stored as
    admin-configured spellings (an alias, a dated alias, a raw Bedrock id) —
    strings the admin write path has already canonicalised, but strings all
    the same. Comparing one of them against `entry.aliases[0]` (a SINGLE
    designated spelling) would silently stop matching the moment the same
    model is configured under any OTHER of its own aliases; resolving each
    configured spelling and comparing the resulting `ModelEntry` by identity
    is what `_validate_model_pin`'s own policy-set loop already does, for the
    same reason, and this predicate reads that behaviour rather than a second,
    string-based idea of "the same model" that could disagree with it.

    A configured spelling that no longer resolves (a stale reference to a
    model since removed from the registry) simply cannot match anything here
    — it is skipped, never treated as a wildcard — which only ever makes this
    check MORE restrictive, never less.
    """
    from .models import resolve_model

    for spelling in spellings:
        try:
            if resolve_model(spelling) is entry:
                return True
        except ValueError:
            continue
    return False


def _model_axis_allows(
    entry: ModelEntry, tenant_cfg: "RoutingConfig", user_cfg: "Optional[UserRoutingConfig]",
) -> bool:
    """Axis 1 (permission, not availability): is `entry` inside the model
    policy the tenant — and, narrowing further, the user — configured.

    The policy set is `tenant_cfg.allowlist` when non-empty, else
    `tenant_cfg.chain` — exactly the `policy_set = tenant_cfg.allowlist or
    tenant_cfg.chain` `_validate_model_pin` already computes — further
    narrowed by `user_cfg.chain` when the user has one of its own (a user's
    chain is validated at write time to be a subsequence of the tenant's, so
    this can only narrow, never widen, what the tenant already allowed).
    An EMPTY policy set at either level (tenant absent both, or user absent
    a chain) does not restrict at that level — the existing, unchanged
    "no allowlist and no chain is a passthrough" reading.

    Chain ORDER and the breaker tier cap are availability, not permission,
    and are deliberately not read here — narrowing which model wins a
    cascade is a different question from whether the caller may reach it at
    all, and only the second is this predicate's job.
    """
    policy_set = tenant_cfg.allowlist or tenant_cfg.chain
    if policy_set and not _resolves_to(policy_set, entry):
        return False
    if user_cfg is not None and user_cfg.chain and not _resolves_to(user_cfg.chain, entry):
        return False
    return True


def refusal_for(
    entry: ModelEntry,
    *,
    tenant_cfg: "RoutingConfig",
    user_cfg: "Optional[UserRoutingConfig]",
    grants: "Iterable[Entitlement]",
) -> Optional[str]:
    """`None` when the caller may use `entry`; otherwise exactly one refusal
    code, in this precedence when more than one axis fails:

    1. `MODEL_NOT_ALLOWED` — `entry` fails the model policy axis; see
       `_model_axis_allows`. Existing code, existing meaning: this predicate
       computes the SAME permission decision `_resolve_candidate_chain`'s
       allowlist filter and `_validate_model_pin`'s policy-set check already
       make, just made per candidate, at reserve time, rather than once while
       building the candidate list.
    2. `MODEL_NOT_ENTITLED` — `entry.access == "entitlement_required"` and no
       member of `grants` names `(entry.model_family, entry.profile_scope)`.
       An entry the tenant's allowlist happens to name is not consent by
       itself — the grant is.
    3. `SCOPE_NOT_ALLOWED` — `entry.profile_scope` is outside
       `effective_profile_scopes(tenant_cfg, user_cfg)`, when that
       intersection is not `None`. `None` means unrestricted (the identity
       element PR2's own accessor already defines); this predicate reads that
       accessor rather than re-intersecting the two axes itself, so a tenant
       narrowing after a user's document was written is honoured identically
       here and at `routing.config`'s own read path.

    `grants` is exactly what `admin_entitlements.list_entitlements(tenant_id)`
    returns — an `Iterable[Entitlement]`, unmodified by the caller — never
    read here (see the module docstring for why this predicate never calls
    that loader itself). An empty iterable is always correct for a candidate
    whose `access` is `"general"`, because axis 2 is never consulted in that
    case; a caller may use that to skip the entitlement-store read entirely
    on a request no candidate of which needs it.
    """
    if not _model_axis_allows(entry, tenant_cfg, user_cfg):
        return MODEL_NOT_ALLOWED

    if entry.access == "entitlement_required":
        if not any(
            g.model_family == entry.model_family and g.profile_scope == entry.profile_scope
            for g in grants
        ):
            return MODEL_NOT_ENTITLED

    scopes = effective_profile_scopes(tenant_cfg, user_cfg)
    if scopes is not None and entry.profile_scope not in scopes:
        return SCOPE_NOT_ALLOWED

    return None
