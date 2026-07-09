"""Admin Tenant API (Phase 2).

- GET    /api/mvp/admin/tenants            list tenants (cursor pagination)
- POST   /api/mvp/admin/tenants            create tenant (validates team_lead existence + role)
- GET    /api/mvp/admin/tenants/{id}       tenant detail
- PATCH  /api/mvp/admin/tenants/{id}       update name / default_credit
- DELETE /api/mvp/admin/tenants/{id}       soft-delete (status=archived)
- PUT    /api/mvp/admin/tenants/{id}/owner reassign team_lead_user_id (Critical C-C)
- GET    /api/mvp/admin/tenants/{id}/users list tenant members
- GET    /api/mvp/admin/tenants/{id}/usage per-tenant usage aggregation
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from boto3.dynamodb.conditions import Key as boto3_key
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from dynamo import (
    ADMIN_OWNED,
    TenantLimitExceededError,
    TenantNotFoundError,
    TenantsRepository,
    UsersRepository,
    UserTenantsRepository,
    UsageLogsRepository,
)

from .authz import log_audit_event, require_permission
from .deps import DEFAULT_ORG_ID, AuthenticatedUser


router = APIRouter(prefix="/api/mvp/admin/tenants", tags=["mvp-admin-tenants"])


# -----------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------
Role = Literal["admin", "team_lead", "user"]


class TenantItem(BaseModel):
    tenant_id: str
    name: str
    team_lead_user_id: str
    default_credit: int
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None


class TenantListResponse(BaseModel):
    tenants: list[TenantItem]
    next_cursor: Optional[str] = None


class CreateTenantRequest(BaseModel):
    """Admin tenant creation request. Validates team_lead_user_id existence and role (Critical C-E)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    team_lead_user_id: str = Field(
        min_length=1,
        max_length=64,
        description="sub of a user with team_lead role, or 'admin-owned'",
    )
    default_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)


class UpdateTenantRequest(BaseModel):
    """team_lead_user_id is not accepted here (Critical C-C: immutability guarantee)."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    default_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)


class SetOwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_lead_user_id: str = Field(min_length=1, max_length=64)


class TenantMember(BaseModel):
    user_id: str
    email: str
    role: str
    total_credit: int
    credit_used: int
    remaining_credit: int
    status: str


class TenantMembersResponse(BaseModel):
    tenant_id: str
    members: list[TenantMember]


class UsageBucket(BaseModel):
    tenant_id: str
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, int] = {}
    by_user: dict[str, int] = {}
    sample_size: int = 0


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
def _encode_cursor(last_key: Optional[dict]) -> Optional[str]:
    if not last_key:
        return None
    return base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode()


def _decode_cursor(cursor: Optional[str]) -> Optional[dict]:
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cursor")


def _to_tenant_item(item: dict[str, Any]) -> TenantItem:
    return TenantItem(
        tenant_id=str(item["tenant_id"]),
        name=str(item.get("name") or ""),
        team_lead_user_id=str(item.get("team_lead_user_id") or ADMIN_OWNED),
        default_credit=int(item.get("default_credit") or 0),
        status=str(item.get("status") or "active"),
        created_at=item.get("created_at"),
        updated_at=item.get("updated_at"),
        created_by=item.get("created_by"),
    )


def _verify_team_lead(team_lead_user_id: str) -> None:
    """Require that team_lead_user_id refers to an existing user whose roles include team_lead.

    Exception: validation is skipped when the value is `admin-owned`.
    """
    if team_lead_user_id == ADMIN_OWNED:
        return
    user = UsersRepository().get_by_user_id(team_lead_user_id)
    if not user:
        raise HTTPException(
            status_code=422,
            detail=f"team_lead_user_id not found: {team_lead_user_id}",
        )
    roles = user.get("roles") or []
    if "team_lead" not in roles:
        raise HTTPException(
            status_code=422,
            detail=f"user {team_lead_user_id} does not have team_lead role",
        )


# -----------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------
@router.get("", response_model=TenantListResponse)
def list_tenants(
    cursor: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    _admin: AuthenticatedUser = Depends(require_permission("tenants:read-all")),
) -> TenantListResponse:
    items, last_key = TenantsRepository().list_all(cursor=_decode_cursor(cursor), limit=limit)
    return TenantListResponse(
        tenants=[_to_tenant_item(it) for it in items if it.get("status") != "archived"],
        next_cursor=_encode_cursor(last_key),
    )


@router.post("", response_model=TenantItem, status_code=201)
def create_tenant(
    body: CreateTenantRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:create")),
) -> TenantItem:
    _verify_team_lead(body.team_lead_user_id)
    try:
        item = TenantsRepository().create(
            name=body.name,
            team_lead_user_id=body.team_lead_user_id,
            default_credit=body.default_credit,
            created_by=actor.user_id,
        )
    except TenantLimitExceededError as e:
        raise HTTPException(status_code=403, detail=str(e))
    log_audit_event(
        event="tenant_created",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=item["tenant_id"],
        target_type="tenant",
        details={"name": body.name, "team_lead_user_id": body.team_lead_user_id},
    )
    return _to_tenant_item(item)


@router.get("/{tenant_id}", response_model=TenantItem)
def get_tenant(
    tenant_id: str,
    _admin: AuthenticatedUser = Depends(require_permission("tenants:read-all")),
) -> TenantItem:
    item = TenantsRepository().get(tenant_id)
    if not item:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return _to_tenant_item(item)


@router.patch("/{tenant_id}", response_model=TenantItem)
def update_tenant(
    tenant_id: str,
    body: UpdateTenantRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:update")),
) -> TenantItem:
    try:
        item = TenantsRepository().update(
            tenant_id=tenant_id,
            name=body.name,
            default_credit=body.default_credit,
        )
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Tenant not found")
    log_audit_event(
        event="tenant_updated",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
        after={"name": body.name, "default_credit": body.default_credit},
    )
    return _to_tenant_item(item)


@router.delete("/{tenant_id}")
def archive_tenant(
    tenant_id: str,
    actor: AuthenticatedUser = Depends(require_permission("tenants:delete")),
) -> Response:
    if tenant_id == DEFAULT_ORG_ID:
        raise HTTPException(status_code=409, detail=f"{DEFAULT_ORG_ID} cannot be deleted")
    repo = TenantsRepository()
    item = repo.get(tenant_id)
    if not item:
        raise HTTPException(status_code=404, detail="Tenant not found")
    repo.archive(tenant_id)
    log_audit_event(
        event="tenant_archived",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
    )
    return Response(status_code=204)


@router.put("/{tenant_id}/owner", response_model=TenantItem)
def set_tenant_owner(
    tenant_id: str,
    body: SetOwnerRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:update")),
) -> TenantItem:
    """Reassign team_lead_user_id (Critical C-C: recovers tenants orphaned by Cognito delete-and-recreate)."""
    _verify_team_lead(body.team_lead_user_id)
    repo = TenantsRepository()
    before = repo.get(tenant_id)
    if not before:
        raise HTTPException(status_code=404, detail="Tenant not found")
    try:
        item = repo.set_owner(
            tenant_id=tenant_id,
            new_owner_user_id=body.team_lead_user_id,
        )
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Tenant not found")
    log_audit_event(
        event="tenant_owner_changed",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
        before={"team_lead_user_id": before.get("team_lead_user_id")},
        after={"team_lead_user_id": body.team_lead_user_id},
    )
    return _to_tenant_item(item)


@router.get("/{tenant_id}/users", response_model=TenantMembersResponse)
def list_tenant_users(
    tenant_id: str,
    _admin: AuthenticatedUser = Depends(require_permission("tenants:read-all")),
) -> TenantMembersResponse:
    """List members of a tenant (admin view, includes user_id)."""
    tenant = TenantsRepository().get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    user_tenants_repo = UserTenantsRepository()
    resp = user_tenants_repo._table.query(
        IndexName="tenant-id-index",
        KeyConditionExpression=boto3_key("tenant_id").eq(tenant_id),
    )
    members: list[TenantMember] = []
    users_repo = UsersRepository()
    for ut in resp.get("Items", []):
        if ut.get("status", "active") != "active":
            continue
        uid = str(ut["user_id"])
        user = users_repo.get_by_user_id(uid)
        email = str(user.get("email") if user else "") or ""
        total = int(ut.get("total_credit", 0))
        used = int(ut.get("credit_used", 0))
        members.append(
            TenantMember(
                user_id=uid,
                email=email,
                role=str(ut.get("role") or "user"),
                total_credit=total,
                credit_used=used,
                remaining_credit=max(total - used, 0),
                status=str(ut.get("status") or "active"),
            )
        )
    return TenantMembersResponse(tenant_id=tenant_id, members=members)


@router.get("/{tenant_id}/usage", response_model=UsageBucket)
def get_tenant_usage(
    tenant_id: str,
    since_days: int = Query(30, ge=1, le=365),
    _admin: AuthenticatedUser = Depends(require_permission("usage:read-all")),
) -> UsageBucket:
    """Query UsageLogs by tenant_id (PK) and aggregate by model and user in Python.

    Results are truncated at 1000 items (sufficient for MVP scale).
    """
    tenant = TenantsRepository().get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
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
        user_email = str(it.get("user_email") or it.get("user_id") or "unknown")
        bucket.by_user[user_email] = bucket.by_user.get(user_email, 0) + tokens
    return bucket
