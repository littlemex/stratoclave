"""ModelResolver: per-model quota selection + cascading fallback.

Sits ABOVE ChainResolver. Determines WHICH model family to use based on:
- Tenant allowlist
- User/tenant fallback chain ordering
- Per-model quota availability (soft check + atomic reserve)
- Staged breaker tier cap
- VSR HARD constraint (disables cascade)

The resolver iterates candidates in chain order, attempting to reserve
quota for each. The first model whose quota reserve succeeds is selected.
ChainResolver then maps it to infra targets (regions/retry).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ModelQuotaConfig:
    """Per-model quota from tenant or user config."""
    model: str
    unit: str = "tokens"  # "tokens" or "usd_micro"
    limit: Optional[int] = None  # None = unlimited
    period: str = "monthly"


@dataclass(frozen=True)
class RoutingConfig:
    """Tenant routing configuration (cached from DynamoDB)."""
    allowlist: tuple[str, ...] = ()
    chain: tuple[str, ...] = ()
    quotas: dict[str, ModelQuotaConfig] = field(default_factory=dict)
    fallback_mode: str = "loud"
    fallback_default: str = "off"
    free_tier_model: Optional[str] = None
    # SAAR (session-aware routing). When the SAAR_ENABLED flag is on, a tenant
    # can additionally opt into per-user session scoping: the routing-memory sk
    # is namespaced by the acting user so two users sharing a session id (or one
    # guessing another's) cannot perturb each other's continuity state. Default
    # False keeps the session shared across the tenant's users (the common
    # single-agent case). This field is inert when SAAR is globally disabled.
    saar_user_scoped: bool = False
    # Per-tenant shadow VSR toggle (advisory-only; NEVER affects execution,
    # billing, or routing — it only controls whether the shadow judge records a
    # potential-saving advisory on the decision log for the Savings Certificate).
    # Tri-state: True = on for this tenant, False = off, None = follow the global
    # env default (STRATOCLAVE_SHADOW_VSR, dark unless set). A tenant with no
    # routing-config item resolves to None, so existing tenants are unchanged.
    shadow_vsr: Optional[bool] = None
    # Per-tenant vLLM Semantic Router mode (SR migration). SR is an EXECUTING
    # gateway (no decision-only endpoint), so a request either flows through it
    # (reserve→forward→settle) or does not — there is no "observe without
    # executing" shadow. Three-state:
    #   "off"    — SR is not used for this tenant (direct Bedrock; the default).
    #   "canary" — a sampled fraction of this tenant's traffic goes through SR
    #              (full reserve→forward→settle); the rest uses the direct path.
    #   "active" — all of this tenant's eligible traffic goes through SR.
    # None = follow the global default (STRATOCLAVE_SR_MODE_DEFAULT, "off" unless
    # set). A tenant with no routing-config item resolves to None ⇒ unchanged.
    # Money is fail-closed in every mode (reserve precedes any SR forward); only
    # whether/how much traffic is routed through SR differs.
    sr_mode: Optional[str] = None


@dataclass(frozen=True)
class UserRoutingConfig:
    """User-level overrides (must be subsequence of tenant chain)."""
    preferred_model: Optional[str] = None
    chain: Optional[tuple[str, ...]] = None
    fallback: Optional[str] = None  # "on" | "off" | None (inherit)


@dataclass
class ModelSelection:
    """Result of model resolution."""
    selected_model: str
    requested_model: str
    fallback_occurred: bool = False
    fallback_reason: Optional[str] = None
    attempts: list[dict[str, str]] = field(default_factory=list)


def resolve_model(
    *,
    requested_model: str,
    tenant_config: RoutingConfig,
    user_config: Optional[UserRoutingConfig] = None,
    breaker_max_tier: Optional[int] = None,
    vsr_hard_model: Optional[str] = None,
    fallback_allowed: bool = False,
) -> ModelSelection:
    """Resolve which model to attempt, applying cascading fallback logic.

    This does NOT reserve quota (that's done by the caller with quota_lines).
    It determines the ordered candidate list and returns the first eligible model.

    In Phase 1+ this will integrate with DynamoDB quota counters for soft-check
    filtering. In Phase 0, it resolves purely from config (allowlist + chain).
    """
    # 1. VSR HARD pin — no cascade
    if vsr_hard_model:
        if tenant_config.allowlist and vsr_hard_model not in tenant_config.allowlist:
            raise ValueError(f"VSR HARD model {vsr_hard_model} not in tenant allowlist")
        return ModelSelection(
            selected_model=vsr_hard_model,
            requested_model=requested_model,
        )

    # 2. Build candidate chain
    chain = _resolve_chain(requested_model, tenant_config, user_config)

    # 2b. Allowlist enforcement (filter to allowed models only)
    if tenant_config.allowlist:
        chain = [m for m in chain if m in tenant_config.allowlist]
        if not chain:
            chain = [requested_model] if requested_model in tenant_config.allowlist else list(tenant_config.allowlist[:1])

    # 3. Apply breaker tier cap
    if breaker_max_tier is not None:
        from .chains import _tier_for
        filtered = [m for m in chain if _tier_for(m) <= breaker_max_tier]
        if filtered:
            chain = filtered

    # 4. If fallback not allowed, truncate to first candidate only
    if not fallback_allowed:
        chain = chain[:1]
    elif tenant_config.free_tier_model and tenant_config.free_tier_model not in chain:
        chain = list(chain) + [tenant_config.free_tier_model]

    # 5. Select first available (Phase 0: no quota check, just return first)
    if not chain:
        chain = [requested_model]

    selected = chain[0]
    # Compare canonically (Fable rev2 F3): serving the requested model under a
    # different spelling is NOT a fallback.
    fallback = _canonical_model_id(selected) != _canonical_model_id(requested_model)

    return ModelSelection(
        selected_model=selected,
        requested_model=requested_model,
        fallback_occurred=fallback,
        fallback_reason="breaker_downgrade" if fallback and breaker_max_tier else None,
    )


def _resolve_chain(
    requested: str,
    tenant: RoutingConfig,
    user: Optional[UserRoutingConfig],
) -> list[str]:
    """Build the ordered candidate chain from user/tenant config."""
    # User chain takes priority if set (must be subsequence of tenant chain)
    if user and user.chain:
        base_chain = list(user.chain)
    elif tenant.chain:
        base_chain = list(tenant.chain)
    else:
        return [requested]

    # Start from the requested model's position in the chain. Match on the
    # CANONICAL model id, not raw string equality (Fable rev2 F3): the admin
    # write path stores chain entries canonicalized (aliases[0]), so a client
    # requesting the same model under a different spelling (bedrock id, other
    # alias) must still locate its start position — otherwise it silently falls
    # back to the whole chain from the top.
    preferred = (user.preferred_model if user else None) or requested
    pref_key = _canonical_model_id(preferred)
    chain_keys = [_canonical_model_id(m) for m in base_chain]
    if pref_key in chain_keys:
        return base_chain[chain_keys.index(pref_key):]
    return base_chain


def _canonical_model_id(model_id: str) -> str:
    """Canonical id (registry primary alias) for spelling-insensitive matching;
    the raw id if it doesn't resolve (so an unknown id simply won't match)."""
    from ..models import resolve_model

    try:
        entry = resolve_model(model_id)
        return entry.aliases[0] if entry.aliases else entry.bedrock_model_id
    except ValueError:
        return model_id
