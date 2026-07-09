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
    TenantLimitExceededError,
    TenantNotFoundError,
    TenantsRepository,
    UsersRepository,
    UserTenantsRepository,
    UsageLogsRepository,
)

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
    default_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)


class UpdateTenantTeamLeadRequest(BaseModel):
    """team_lead_user_id is not accepted here (immutability guarantee)."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    default_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)


class TenantMemberPublic(BaseModel):
    """Team Lead member summary — does not include user_id (prevents cross-Tenant tracking)."""

    email: str
    role: str
    total_credit: int
    credit_used: int
    remaining_credit: int


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
            )
        )
    return TenantMembersResponse(tenant_id=tenant_id, members=members)


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
