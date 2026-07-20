"""Admin write path for tenant/user routing config (closes the P0-11 gap).

Why a separate module (not admin_tenants.py):
  * validation imports the model registry AND routing.config -> cycle risk
    and domain mixing if colocated with tenant lifecycle CRUD;
  * the pure validators/serializers below are the unit under the Hypothesis
    seam tests and belong next to the parser they mirror.

Invariant this module owns: any item written here parses via
config._parse_tenant_config / _parse_user_config into exactly the config the
operator submitted (enforced by tests/test_routing_config_admin_property.py).
The money path is untouched -- we only write config the resolver reads.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

# NOTE: import paths mirror admin_tenants.py; adjust if the layout differs.
from .authz import log_audit_event, require_permission
from .deps import AuthenticatedUser
from dynamo.client import get_dynamodb_resource
from .models import resolve_model
from dynamo.tenants import TenantsRepository
from .routing import config as routing_config
from .routing.config import RoutingConfig

router = APIRouter(prefix="/api/mvp/admin/tenants", tags=["admin-routing-config"])

_TENANT_CONFIG_PK = "CONFIG#ROUTING"


def _user_config_pk(user_id: str) -> str:
    return f"CONFIG#ROUTING#USER#{user_id}"


# =============================================================================
# Request / response models
# =============================================================================
class ModelQuota(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Only "usd_micro" is accepted: P0-11 enforcement interprets `limit` as
    # monthly micro-USD REGARDLESS of unit, so accepting unit="tokens" would
    # silently reinterpret e.g. a 1_000_000-token intent as a $1 spend cap — a
    # ×10^6 misconfiguration (Fable rev2 F5). Reject until token quotas are
    # actually enforced. `period` is likewise monthly-only.
    unit: Literal["usd_micro"] = "usd_micro"
    limit: Optional[int] = Field(default=None, ge=0)  # micro-USD; None = unlimited
    period: Literal["monthly"] = "monthly"


class TenantRoutingConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowlist: list[str] = Field(default_factory=list)
    chain: list[str] = Field(default_factory=list)
    quotas: dict[str, ModelQuota] = Field(default_factory=dict)
    fallback_mode: Literal["loud", "silent"] = "loud"
    fallback_default: Literal["on", "off"] = "off"
    free_tier_model: Optional[str] = None
    # Shadow VSR — advisory ONLY: does NOT affect execution, billing, or routing.
    # It only controls whether the shadow judge records a potential-saving
    # advisory on the decision log (for the Savings Certificate). Tri-state:
    # true/false = explicit per-tenant, null = follow the global default.
    shadow_vsr: Optional[bool] = None


class UserRoutingConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferred_model: Optional[str] = None
    chain: Optional[list[str]] = None  # None = inherit tenant chain
    fallback: Optional[Literal["on", "off"]] = None  # None = inherit


class TenantRoutingConfigResponse(BaseModel):
    tenant_id: str
    configured: bool
    allowlist: list[str]
    chain: list[str]
    quotas: dict[str, ModelQuota]
    fallback_mode: str
    fallback_default: str
    free_tier_model: Optional[str] = None
    # advisory-only shadow toggle (see request). Tri-state; null = global default.
    shadow_vsr: Optional[bool] = None


class UserRoutingConfigResponse(BaseModel):
    tenant_id: str
    user_id: str
    configured: bool
    preferred_model: Optional[str] = None
    chain: Optional[list[str]] = None
    fallback: Optional[str] = None


# =============================================================================
# Pure validation (unit under Hypothesis seam tests)
# =============================================================================
class RoutingValidationError(ValueError):
    """Semantic config error -> mapped to HTTP 400 by the endpoints."""


def _resolve_or_raise(model_id: str, field: str):
    try:
        return resolve_model(model_id)
    except ValueError:
        raise RoutingValidationError(
            f"{field}: model '{model_id}' is not in the model registry"
        )


def _canon(model_id: str, field: str) -> str:
    """Canonical model id: the entry's primary client-facing alias. A stable
    STRING (not id() — that would break if the registry ever built entries per
    call; Fable rev1 F5) that collapses alias vs bedrock-id spellings of the
    same model, so quota keys / allowlist / chain are stored consistently and
    the enforcement layer's within-config lookups can never miss (rev1 F1)."""
    entry = _resolve_or_raise(model_id, field)
    # Fallback to the entry's OWN bedrock id (never the caller's input) for an
    # alias-less entry, so two input spellings of the same model still collapse
    # to one canonical string (Fable rev2 F1: input-spelling fallback could
    # re-introduce a mismatch if resolve_model is non-injective on inputs).
    return entry.aliases[0] if entry.aliases else entry.bedrock_model_id


def _canon_soft(model_id: str) -> str:
    """Canonical id for already-stored config; tolerate registry drift (an
    unresolvable stored id maps to itself so it simply never matches a live
    model rather than raising)."""
    try:
        entry = resolve_model(model_id)
        return entry.aliases[0] if entry.aliases else entry.bedrock_model_id
    except ValueError:
        return model_id


def _validate_model_list(models: list[str], field: str) -> list[str]:
    keys, seen = [], set()
    for m in models:
        k = _canon(m, field)
        if k in seen:
            raise RoutingValidationError(
                f"{field}: duplicate model '{m}' "
                "(different aliases of the same model count as duplicates)"
            )
        seen.add(k)
        keys.append(k)
    return keys


def _is_subsequence(sub: list, full: list) -> bool:
    it = iter(full)
    return all(any(s == f for f in it) for s in sub)


def validate_tenant_routing(body: TenantRoutingConfigRequest) -> None:
    chain_keys = _validate_model_list(body.chain, "chain")
    allow_keys = _validate_model_list(body.allowlist, "allowlist")
    _validate_model_list(list(body.quotas.keys()), "quotas")

    free_key = None
    if body.free_tier_model is not None:
        free_key = _canon(body.free_tier_model, "free_tier_model")

    if body.allowlist:  # empty allowlist = unrestricted (backward compat)
        allow_set = set(allow_keys)
        for m, k in zip(body.chain, chain_keys):
            if k not in allow_set:
                raise RoutingValidationError(
                    f"chain: model '{m}' is not in the non-empty allowlist; "
                    "it would never be servable"
                )
        if free_key is not None and free_key not in allow_set:
            raise RoutingValidationError(
                f"free_tier_model: '{body.free_tier_model}' is not in the "
                "non-empty allowlist"
            )


def validate_user_routing(body: UserRoutingConfigRequest, tenant: RoutingConfig) -> None:
    if body.preferred_model is not None:
        pref_key = _canon(body.preferred_model, "preferred_model")
        if tenant.allowlist:
            allow = {_canon_soft(a) for a in tenant.allowlist}
            if pref_key not in allow:
                raise RoutingValidationError(
                    f"preferred_model: '{body.preferred_model}' is not in the "
                    "tenant allowlist"
                )
    if body.chain is not None:
        if len(body.chain) == 0:
            raise RoutingValidationError(
                "chain: empty list is ambiguous; omit 'chain' (null) to "
                "inherit the tenant chain"
            )
        user_keys = _validate_model_list(body.chain, "chain")
        tenant_keys = [_canon_soft(m) for m in tenant.chain]
        if not _is_subsequence(user_keys, tenant_keys):
            raise RoutingValidationError(
                "chain: user chain must be an order-preserving subsequence of "
                f"the tenant chain {list(tenant.chain)}"
            )


# =============================================================================
# Serialization -- MUST mirror config._parse_tenant_config/_parse_user_config
# =============================================================================
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canon_or_self(model_id: str) -> str:
    """Canonical id for serialization; falls back to the input if unresolvable
    (validation already rejected unknown ids, so this is defensive)."""
    return _canon_soft(model_id)


def tenant_config_to_item(
    tenant_id: str, body: TenantRoutingConfigRequest, updated_by: Optional[str] = None
) -> dict:
    # Store CANONICAL model ids (Fable rev1 F1): the config's chain/allowlist/
    # quota keys must all spell the same model the same way, so the enforcement
    # layer's within-config lookups (quotas.get(chain_entry)) never miss because
    # an operator wrote an alias in one place and a bedrock id in another.
    item: dict = {
        "user_id": _TENANT_CONFIG_PK,
        "tenant_id": tenant_id,
        "allowlist": [_canon_or_self(m) for m in body.allowlist],
        "chain": [_canon_or_self(m) for m in body.chain],
        "quotas": {
            _canon_or_self(m): (
                {"unit": q.unit, "period": q.period}
                | ({"limit": int(q.limit)} if q.limit is not None else {})
            )
            for m, q in body.quotas.items()
        },
        "fallback_mode": body.fallback_mode,
        "fallback_default": body.fallback_default,
    }
    if body.free_tier_model is not None:
        item["free_tier"] = {"model": _canon_or_self(body.free_tier_model)}
    # Only persist shadow_vsr when explicitly set — absence stays absent so the
    # parsed config resolves to None (follow the global default). This keeps the
    # tri-state honest through the store round-trip.
    if body.shadow_vsr is not None:
        item["shadow_vsr"] = bool(body.shadow_vsr)
    if updated_by is not None:
        item["updated_by"] = updated_by
        item["updated_at"] = _now_iso()
    return item


def user_config_to_item(
    tenant_id: str, user_id: str, body: UserRoutingConfigRequest,
    updated_by: Optional[str] = None,
) -> dict:
    item: dict = {"user_id": _user_config_pk(user_id), "tenant_id": tenant_id}
    if body.preferred_model is not None:
        item["preferred_model"] = _canon_or_self(body.preferred_model)
    if body.chain is not None:  # never []; validator forbids empty
        item["chain"] = [_canon_or_self(m) for m in body.chain]
    if body.fallback is not None:
        item["fallback"] = body.fallback
    if updated_by is not None:
        item["updated_by"] = updated_by
        item["updated_at"] = _now_iso()
    return item


# =============================================================================
# DynamoDB helpers (fresh, consistent reads -- never the 60s cache)
# =============================================================================
def _table():
    return get_dynamodb_resource().Table(routing_config._TABLE)


def _read_config_item(pk: str, tenant_id: str) -> Optional[dict]:
    resp = _table().get_item(
        Key={"user_id": pk, "tenant_id": tenant_id}, ConsistentRead=True
    )
    return resp.get("Item")


def provision_shadow_default_config(tenant_id: str, *, updated_by: str) -> None:
    """Write an EXPLICIT shadow_vsr=True routing-config record for a freshly
    created tenant so the Savings Certificate is populated from week one (the
    litellm-wedge value prop). It lives here (the routing-config write home)
    rather than in admin_tenants so the raw put stays in a module that only
    touches the ROUTING config item — never the budgets table. Advisory-only:
    shadow VSR never steers execution/routing/money, so this is money-neutral."""
    item = tenant_config_to_item(
        tenant_id, TenantRoutingConfigRequest(shadow_vsr=True), updated_by=updated_by
    )
    _table().put_item(Item=item)
    routing_config.invalidate_routing_cache(tenant_id)


def _require_tenant(tenant_id: str) -> None:
    if not TenantsRepository().get(tenant_id):
        raise HTTPException(status_code=404, detail="Tenant not found")


def _require_user_in_tenant(tenant_id: str, user_id: str) -> None:
    """404 when the user isn't a member of the tenant (Fable rev1 F3): without
    this, a typo'd user_id writes an orphan CONFIG#ROUTING#USER# item that GET
    reports as configured=true while the real user's enforcement is unchanged."""
    from dynamo import UserTenantsRepository

    if not UserTenantsRepository().get(user_id, tenant_id):
        raise HTTPException(
            status_code=404, detail=f"User {user_id} is not a member of tenant {tenant_id}"
        )


def _tenant_view(cfg: RoutingConfig) -> dict:
    return {
        "allowlist": list(cfg.allowlist),
        "chain": list(cfg.chain),
        "quotas": {
            m: {
                "unit": q.unit,
                "limit": int(q.limit) if q.limit is not None else None,  # Decimal-safe
                "period": q.period,
            }
            for m, q in cfg.quotas.items()
        },
        "fallback_mode": cfg.fallback_mode,
        "fallback_default": cfg.fallback_default,
        "free_tier_model": cfg.free_tier_model,
        "shadow_vsr": cfg.shadow_vsr,
    }


def _user_view(cfg: Optional[routing_config.UserRoutingConfig]) -> dict:
    if cfg is None:
        return {"preferred_model": None, "chain": None, "fallback": None}
    return {
        "preferred_model": cfg.preferred_model,
        "chain": list(cfg.chain) if cfg.chain else None,
        "fallback": cfg.fallback,
    }


# =============================================================================
# Endpoints
# =============================================================================
@router.get("/{tenant_id}/routing-config", response_model=TenantRoutingConfigResponse)
def get_tenant_routing(
    tenant_id: str,
    actor: AuthenticatedUser = Depends(require_permission("tenants:read-all")),
) -> TenantRoutingConfigResponse:
    """Current tenant routing config; defaults when unset (backward compat)."""
    _require_tenant(tenant_id)
    item = _read_config_item(_TENANT_CONFIG_PK, tenant_id)
    cfg = routing_config._parse_tenant_config(item) if item else RoutingConfig()
    return TenantRoutingConfigResponse(
        tenant_id=tenant_id, configured=item is not None, **_tenant_view(cfg)
    )


@router.put("/{tenant_id}/routing-config", response_model=TenantRoutingConfigResponse)
def put_tenant_routing(
    tenant_id: str,
    body: TenantRoutingConfigRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:update")),
) -> TenantRoutingConfigResponse:
    """Validate + write the CONFIG#ROUTING item that P0-11 enforcement reads.

    Full replace (PUT semantics): a partial body replaces the whole config, so
    callers must send the complete desired state (the UI pre-fills from GET and
    the CLI sends a full file). Model ids are stored CANONICALIZED so chain /
    allowlist / quota keys always spell a model the same way. Every model id
    must resolve in the registry; chain/allowlist/free-tier coherence is checked
    so the enforcement layer can never load a config where no chain model is
    servable by construction.

    Note (Fable rev1 F4): shrinking the tenant chain can leave an existing
    per-user override chain that is no longer a subsequence. We do NOT cascade-
    reject here; instead the enforcement layer treats user chains defensively
    (a user chain is only ever used as an ordering hint over the live tenant
    chain — same principle as the VSR pin), so an orphaned user chain degrades
    gracefully rather than enforcing a stale set.
    """
    _require_tenant(tenant_id)
    try:
        validate_tenant_routing(body)
    except RoutingValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    before_item = _read_config_item(_TENANT_CONFIG_PK, tenant_id)
    item = tenant_config_to_item(tenant_id, body, updated_by=actor.user_id)
    _table().put_item(Item=item)
    routing_config.invalidate_routing_cache(tenant_id)

    before_cfg = routing_config._parse_tenant_config(before_item) if before_item else None
    after_cfg = routing_config._parse_tenant_config(item)
    log_audit_event(
        event="tenant_routing_config_set",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
        before=_tenant_view(before_cfg) if before_cfg else None,
        after=_tenant_view(after_cfg),
    )
    return TenantRoutingConfigResponse(
        tenant_id=tenant_id, configured=True, **_tenant_view(after_cfg)
    )


@router.get(
    "/{tenant_id}/users/{user_id}/routing-config",
    response_model=UserRoutingConfigResponse,
)
def get_user_routing(
    tenant_id: str,
    user_id: str,
    actor: AuthenticatedUser = Depends(require_permission("tenants:read-all")),
) -> UserRoutingConfigResponse:
    _require_tenant(tenant_id)
    _require_user_in_tenant(tenant_id, user_id)
    item = _read_config_item(_user_config_pk(user_id), tenant_id)
    cfg = routing_config._parse_user_config(item) if item else None
    return UserRoutingConfigResponse(
        tenant_id=tenant_id, user_id=user_id, configured=item is not None,
        **_user_view(cfg),
    )


@router.put(
    "/{tenant_id}/users/{user_id}/routing-config",
    response_model=UserRoutingConfigResponse,
)
def put_user_routing(
    tenant_id: str,
    user_id: str,
    body: UserRoutingConfigRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:update")),
) -> UserRoutingConfigResponse:
    """Validate + write CONFIG#ROUTING#USER#<uid>. The user chain is checked
    (alias-aware, order-preserving) against a FRESH consistent read of the
    tenant chain -- never the 60s cache -- to minimize the validation window.
    """
    _require_tenant(tenant_id)
    _require_user_in_tenant(tenant_id, user_id)
    tenant_item = _read_config_item(_TENANT_CONFIG_PK, tenant_id)
    tenant_cfg = (
        routing_config._parse_tenant_config(tenant_item) if tenant_item else RoutingConfig()
    )
    try:
        validate_user_routing(body, tenant_cfg)
    except RoutingValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    before_item = _read_config_item(_user_config_pk(user_id), tenant_id)
    item = user_config_to_item(tenant_id, user_id, body, updated_by=actor.user_id)
    _table().put_item(Item=item)
    routing_config.invalidate_routing_cache(tenant_id, user_id=user_id)

    before_cfg = routing_config._parse_user_config(before_item) if before_item else None
    after_cfg = routing_config._parse_user_config(item)
    log_audit_event(
        event="user_routing_config_set",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=f"{tenant_id}/{user_id}",
        target_type="user",
        before=_user_view(before_cfg) if before_item else None,
        after=_user_view(after_cfg),
    )
    return UserRoutingConfigResponse(
        tenant_id=tenant_id, user_id=user_id, configured=True, **_user_view(after_cfg)
    )
