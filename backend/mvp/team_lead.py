"""Team Lead API (Phase 2).

Access isolation (v2.1 §2):
- A Team Lead may only view and manage the Tenants they own.
- Creating users and assigning users to Tenants is an Admin-only privilege.
- Requests for Tenants the caller does not own (or that do not exist) always
  return a unified 404 (enumeration defense).

Endpoints:
- POST   /api/mvp/team-lead/tenants            Create a Tenant owned by the caller
- GET    /api/mvp/team-lead/tenants            List the caller's owned Tenants
- GET    /api/mvp/team-lead/tenants/{id}       Tenant detail (owner only)
- PATCH  /api/mvp/team-lead/tenants/{id}       Update name / default_credit
- GET    /api/mvp/team-lead/tenants/{id}/members Members (user_id hidden; email only)
- GET    /api/mvp/team-lead/tenants/{id}/usage   Per-Tenant usage totals
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from boto3.dynamodb.conditions import Key as boto3_key
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from dynamo import (
    TenantBudgetsRepository,
    TenantLimitExceededError,
    TenantNotFoundError,
    TenantsRepository,
    UsersRepository,
    UserTenantsRepository,
    UsageLogsRepository,
    current_period,
)
from limits import MAX_TOKEN_CREDIT
from dynamo.tenant_budgets import PoolLimitExceedsMaximumError, seat_pool_limit_microusd
from dynamo.user_tenants import CreditExhaustedError, is_unlimited
from .admin_tenants import (
    PoolBudgetResponse,
    SetPoolBudgetRequest,
    _MICRO_USD_PER_CENT,
    _pool_response,
    _provision_seat_pool,
    apply_pool_budget_request,
)
from .credit_ops import CreditAction

from .authz import log_audit_event, require_permission
from .deps import AuthenticatedUser


router = APIRouter(prefix="/api/mvp/team-lead/tenants", tags=["mvp-team-lead"])


# -----------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------
class TenantItem(BaseModel):
    tenant_id: str
    name: str
    default_credit: int
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class TenantListResponse(BaseModel):
    tenants: list[TenantItem]


class CreateTenantTeamLeadRequest(BaseModel):
    """team_lead_user_id is set by the backend (not accepted from the caller) — Critical C-E."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    default_credit: Optional[int] = Field(default=None, ge=0, le=MAX_TOKEN_CREDIT)


class UpdateTenantTeamLeadRequest(BaseModel):
    """team_lead_user_id is not accepted here (immutability guarantee)."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    default_credit: Optional[int] = Field(default=None, ge=0, le=MAX_TOKEN_CREDIT)


class TenantMemberPublic(BaseModel):
    """Team Lead member summary — does not include user_id (prevents cross-Tenant tracking)."""

    email: str
    role: str
    total_credit: int
    credit_used: int
    remaining_credit: int
    # True when total_credit is the effectively-unbounded sentinel; render
    # "unlimited" instead of the raw 1e15.
    unlimited: bool = False


class TenantMembersResponse(BaseModel):
    tenant_id: str
    members: list[TenantMemberPublic]


class UsageBucket(BaseModel):
    tenant_id: str
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, int] = {}
    by_user_email: dict[str, int] = {}
    sample_size: int = 0


# -----------------------------------------------------------------------
# Helpers: owner check returning 404 for non-owner / non-existent
# -----------------------------------------------------------------------
def _require_owner(tenant_id: str, actor: AuthenticatedUser) -> dict[str, Any]:
    """Allow only the owner (or an admin). Non-owners and non-existent tenants both return a unified 404."""
    tenant = TenantsRepository().get(tenant_id)
    if "admin" in actor.roles:
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return tenant
    if not tenant or tenant.get("team_lead_user_id") != actor.user_id:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


def _to_tenant_item(item: dict[str, Any]) -> TenantItem:
    return TenantItem(
        tenant_id=str(item["tenant_id"]),
        name=str(item.get("name") or ""),
        default_credit=int(item.get("default_credit") or 0),
        status=str(item.get("status") or "active"),
        created_at=item.get("created_at"),
        updated_at=item.get("updated_at"),
    )


# -----------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------
@router.post("", response_model=TenantItem, status_code=201)
def create_tenant(
    body: CreateTenantTeamLeadRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:create")),
) -> TenantItem:
    """Create a Tenant owned by the calling Team Lead. team_lead_user_id is forced to user.user_id."""
    # L8: validate seats (1, at creation) x SEAT_MONTHLY_USD against
    # MAX_POOL_BUDGET_USD_CENTS BEFORE writing anything (same gate the admin
    # route applies) — a misconfigured SEAT_MONTHLY_USD refuses loudly rather
    # than creating a tenant with no pool (or a silently-clamped one).
    try:
        seat_pool_limit_microusd(1)
    except PoolLimitExceedsMaximumError as e:
        raise HTTPException(status_code=422, detail=f"seat_pool_limit_exceeds_maximum: {e}")
    # Written at ZERO seats and grown by the same ±1-seat delta every membership
    # change applies, so the ceiling equals the seat count at every moment. One
    # seat here would count the owner twice.
    pool_limit_microusd = seat_pool_limit_microusd(0)
    try:
        item = TenantsRepository().create(
            name=body.name,
            team_lead_user_id=actor.user_id,
            default_credit=body.default_credit,
            created_by=actor.user_id,
        )
    except TenantLimitExceededError as e:
        raise HTTPException(
            status_code=403, detail=f"tenant_limit_exceeded: {e}"
        )
    log_audit_event(
        event="team_lead_tenant_created",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=item["tenant_id"],
        target_type="tenant",
        details={"name": body.name},
    )
    # L3: the same default dollar pool the admin route provisions, shared via
    # `admin_tenants._provision_seat_pool` so the pool a tenant gets does not
    # depend on which route created it.
    _provision_seat_pool(item["tenant_id"], pool_limit_microusd=pool_limit_microusd, actor=actor)
    return _to_tenant_item(item)


@router.get("", response_model=TenantListResponse)
def list_own_tenants(
    actor: AuthenticatedUser = Depends(require_permission("tenants:read-own")),
) -> TenantListResponse:
    """List Tenants owned by the caller (team-lead-index Query)."""
    if "admin" in actor.roles:
        # Admins should use the admin API, but since we got a request return all instead of empty.
        items, _ = TenantsRepository().list_all(limit=100)
    else:
        items = TenantsRepository().list_by_owner(actor.user_id)
    return TenantListResponse(tenants=[_to_tenant_item(it) for it in items])


@router.get("/{tenant_id}", response_model=TenantItem)
def get_own_tenant(
    tenant_id: str,
    actor: AuthenticatedUser = Depends(require_permission("tenants:read-own")),
) -> TenantItem:
    item = _require_owner(tenant_id, actor)
    return _to_tenant_item(item)


@router.patch("/{tenant_id}", response_model=TenantItem)
def update_own_tenant(
    tenant_id: str,
    body: UpdateTenantTeamLeadRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:read-own")),
) -> TenantItem:
    _require_owner(tenant_id, actor)
    try:
        item = TenantsRepository().update(
            tenant_id=tenant_id,
            name=body.name,
            default_credit=body.default_credit,
        )
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Tenant not found")
    log_audit_event(
        event="team_lead_tenant_updated",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
        after={"name": body.name, "default_credit": body.default_credit},
    )
    return _to_tenant_item(item)


@router.put("/{tenant_id}/pool-budget", response_model=PoolBudgetResponse)
def set_own_pool_budget(
    tenant_id: str,
    body: SetPoolBudgetRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:update-own")),
) -> PoolBudgetResponse:
    """Set the caller's own tenant's dollar pool budget for a period (L5), or
    return it to the seat count.

    The admin-only ``PUT /admin/tenants/{id}/pool-budget`` mirror: same request
    body, same semantics, same audit events, through the same shared
    `apply_pool_budget_request`, so a pool ceiling looks identical in the log
    regardless of which route moved it. Reuses `_require_owner` — the SAME
    ownership check every other team-lead-scoped write in this router uses — so a
    team lead may set only their OWN tenant's pool; another tenant's returns the
    same unified 404 as every other endpoint here.

    A team lead is therefore a WRITER of the ceiling, and this is the write that
    ends seat tracking. `{"follow_seats": true}` is the reversal, available to the
    same role that can make it necessary.
    """
    _require_owner(tenant_id, actor)
    return apply_pool_budget_request(tenant_id=tenant_id, body=body, actor=actor)


@router.get("/{tenant_id}/pool-budget", response_model=PoolBudgetResponse)
def get_own_pool_budget(
    tenant_id: str,
    period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    actor: AuthenticatedUser = Depends(require_permission("tenants:read-own")),
) -> PoolBudgetResponse:
    """The caller's own tenant's pool budget, its composition and its mode.

    A team lead can already SET this ceiling, and setting it is what ends seat
    tracking — so without a read, the one role that can silently leave seat
    tracking is the one role that cannot see it happened. The read is the same
    shape the admin route returns, mode sentence included, rather than a reduced
    one: a writer needs to see what it writes, and a second projection of the same
    row is a second thing that can disagree with it.

    404 when the tenant has no pool budget for the period, which is what an absent
    row means: unlimited at the pool level, with only per-user token budgets
    applying.
    """
    _require_owner(tenant_id, actor)
    resolved = period or current_period()
    summary = TenantBudgetsRepository().pool_summary(tenant_id, resolved)
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail=f"No pool budget set for tenant {tenant_id} period {resolved}")
    return _pool_response(tenant_id, resolved, summary)


@router.get("/{tenant_id}/members", response_model=TenantMembersResponse)
def list_members(
    tenant_id: str,
    actor: AuthenticatedUser = Depends(require_permission("tenants:read-own")),
) -> TenantMembersResponse:
    """List members of a tenant (email + credit only; user_id is not exposed)."""
    _require_owner(tenant_id, actor)
    user_tenants_repo = UserTenantsRepository()
    resp = user_tenants_repo._table.query(
        IndexName="tenant-id-index",
        KeyConditionExpression=boto3_key("tenant_id").eq(tenant_id),
    )
    users_repo = UsersRepository()
    members: list[TenantMemberPublic] = []
    for ut in resp.get("Items", []):
        if ut.get("status", "active") != "active":
            continue
        uid = str(ut["user_id"])
        user = users_repo.get_by_user_id(uid)
        email = str(user.get("email") if user else "") or ""
        total = int(ut.get("total_credit", 0))
        used = int(ut.get("credit_used", 0))
        members.append(
            TenantMemberPublic(
                email=email,
                role=str(ut.get("role") or "user"),
                total_credit=total,
                credit_used=used,
                remaining_credit=max(total - used, 0),
                unlimited=is_unlimited(total),
            )
        )
    return TenantMembersResponse(tenant_id=tenant_id, members=members)


class SetMemberCreditRequest(CreditAction):
    """Team-Lead set/clear of a member's per-user TOKEN quota. Cap semantics are
    shared with the Admin endpoint via `CreditAction`; the member is addressed by
    `email` (Team Leads never see user_id)."""

    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=1, max_length=320)


_PRIVILEGED_ROLES = {"admin", "team_lead"}


@router.patch("/{tenant_id}/members/credit", response_model=TenantMemberPublic)
def set_member_credit(
    tenant_id: str,
    body: SetMemberCreditRequest,
    actor: AuthenticatedUser = Depends(require_permission("users:update-own-tenant")),
) -> TenantMemberPublic:
    """Set/clear a member's per-user token quota within the caller's own tenant.

    Authorization is defense-in-depth: the `users:update-own-tenant` scope gate,
    plus `_require_owner` (the caller must own this tenant, or be an admin), plus
    a target guard that the member is an ACTIVE, plain-`user` member of THIS
    tenant — checked on BOTH the tenant-membership role AND the user's global
    roles, so a global admin who happens to be assigned here as `user` still
    cannot be touched. The role guard is also folded into the conditional write
    (`require_role="user"`) so a promote-during-request race cannot slip through.
    Non-owner / non-existent tenant and unknown / cross-tenant member all return a
    unified 404 (enumeration defense); a privileged target returns 403 and is
    audited (a denied escalation attempt is the signal worth keeping)."""
    _require_owner(tenant_id, actor)

    # Normalize the email for lookup: strip whitespace, and if the exact spelling
    # misses, retry lowercased (copy-paste and casing are the usual causes of a
    # "visible in `members` but 404" surprise).
    email = body.email.strip()
    users_repo = UsersRepository()
    user = users_repo.get_by_email(email) or users_repo.get_by_email(email.lower())
    if not user:
        raise HTTPException(status_code=404, detail="Member not found")
    user_id = str(user.get("user_id") or "")

    # Global-role guard (defense in depth vs the membership-role guard below): a
    # user who is admin/team_lead ANYWHERE is off-limits to a Team Lead.
    global_roles = user.get("roles") or []
    if isinstance(global_roles, str):
        global_roles = [global_roles]
    is_privileged_global = any(str(r) in _PRIVILEGED_ROLES for r in global_roles)

    user_tenants_repo = UserTenantsRepository()
    membership = user_tenants_repo.get(user_id, tenant_id)
    if not membership:
        # Not an active member of THIS tenant — unified 404 (do not reveal that
        # the email exists in another tenant).
        raise HTTPException(status_code=404, detail="Member not found")

    membership_role = str(membership.get("role") or "user")
    if membership_role != "user" or is_privileged_global:
        # A Team Lead may only adjust plain users, never another admin/team_lead.
        log_audit_event(
            event="team_lead_member_credit_denied",
            actor_id=actor.user_id,
            actor_email=actor.email,
            target_id=user_id,
            target_type="user",
            tenant_id=tenant_id,
            details={"reason": "privileged_target", "email": email},
        )
        raise HTTPException(
            status_code=403,
            detail="cannot modify the quota of a privileged member",
        )

    prev = user_tenants_repo.credit_summary(user_id, tenant_id)
    try:
        # require_role="user" closes the check-then-write TOCTOU atomically; a
        # target promoted between the read above and this write fails the
        # condition and is rejected rather than silently modified.
        attrs = user_tenants_repo.overwrite_credit(
            user_id=user_id,
            tenant_id=tenant_id,
            total_credit=body.resolved_total(),
            reset_used=body.reset_used,
            require_role="user",
        )
    except CreditExhaustedError:
        # Row not active, or role changed out from under us -> unified 404.
        raise HTTPException(status_code=404, detail="Member not found")

    log_audit_event(
        event="team_lead_member_credit_overwritten",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=user_id,
        target_type="user",
        tenant_id=tenant_id,
        before={**prev, "unlimited": is_unlimited(prev["total_credit"])},
        after={
            "total_credit": body.resolved_total(),
            "unlimited": body.unlimited,
            "reset_used": body.reset_used,
            "email": email,
        },
    )

    # Build the response from the write's returned attributes (avoids an
    # eventually-consistent read-after-write).
    total = int(attrs.get("total_credit", 0))
    used = int(attrs.get("credit_used", 0))
    return TenantMemberPublic(
        email=email,
        role=membership_role,
        total_credit=total,
        credit_used=used,
        remaining_credit=max(total - used, 0),
        unlimited=is_unlimited(total),
    )


@router.get("/{tenant_id}/usage", response_model=UsageBucket)
def get_own_tenant_usage(
    tenant_id: str,
    since_days: int = Query(30, ge=1, le=365),
    actor: AuthenticatedUser = Depends(require_permission("usage:read-own-tenant")),
) -> UsageBucket:
    _require_owner(tenant_id, actor)
    since_iso = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()

    repo = UsageLogsRepository()
    resp = repo._table.query(
        KeyConditionExpression=boto3_key("tenant_id").eq(tenant_id)
        & boto3_key("timestamp_log_id").gte(since_iso),
        Limit=1000,
    )
    items = resp.get("Items", [])
    bucket = UsageBucket(tenant_id=tenant_id, sample_size=len(items))
    for it in items:
        tokens = int(it.get("total_tokens", 0))
        input_tokens = int(it.get("input_tokens", 0))
        output_tokens = int(it.get("output_tokens", 0))
        bucket.total_tokens += tokens
        bucket.input_tokens += input_tokens
        bucket.output_tokens += output_tokens
        model = str(it.get("model_id") or "unknown")
        bucket.by_model[model] = bucket.by_model.get(model, 0) + tokens
        # Aggregate by user_email (user_id is not exposed to Team Leads).
        email = str(it.get("user_email") or "unknown")
        bucket.by_user_email[email] = bucket.by_user_email.get(email, 0) + tokens
    return bucket
