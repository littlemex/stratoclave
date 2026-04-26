"""Admin API: Trusted AWS Accounts 管理 (Phase S).

- GET    /api/mvp/admin/trusted-accounts        一覧
- POST   /api/mvp/admin/trusted-accounts        追加
- GET    /api/mvp/admin/trusted-accounts/{id}   詳細
- PATCH  /api/mvp/admin/trusted-accounts/{id}   更新
- DELETE /api/mvp/admin/trusted-accounts/{id}   削除
"""
from __future__ import annotations

import base64
import json
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from dynamo import (
    TrustedAccountNotFoundError,
    TrustedAccountsRepository,
)

from .authz import log_audit_event, require_permission
from .deps import AuthenticatedUser


router = APIRouter(prefix="/api/mvp/admin/trusted-accounts", tags=["mvp-admin-sso"])


ProvisioningPolicy = Literal["invite_only", "auto_provision"]


# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------
class TrustedAccountItem(BaseModel):
    account_id: str
    description: str = ""
    provisioning_policy: ProvisioningPolicy
    allowed_role_patterns: list[str] = []
    allow_iam_user: bool = False
    allow_instance_profile: bool = False
    default_tenant_id: Optional[str] = None
    default_credit: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None


class TrustedAccountsListResponse(BaseModel):
    accounts: list[TrustedAccountItem]
    next_cursor: Optional[str] = None


class CreateTrustedAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str = Field(min_length=12, max_length=12, pattern=r"^\d{12}$")
    description: str = Field(default="", max_length=256)
    provisioning_policy: ProvisioningPolicy = "invite_only"
    allowed_role_patterns: list[str] = Field(default_factory=list)
    allow_iam_user: bool = False
    allow_instance_profile: bool = False
    default_tenant_id: Optional[str] = Field(default=None, max_length=64)
    default_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)


class UpdateTrustedAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: Optional[str] = Field(default=None, max_length=256)
    provisioning_policy: Optional[ProvisioningPolicy] = None
    allowed_role_patterns: Optional[list[str]] = None
    allow_iam_user: Optional[bool] = None
    allow_instance_profile: Optional[bool] = None
    default_tenant_id: Optional[str] = Field(default=None, max_length=64)
    default_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
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


def _to_item(record: dict[str, Any]) -> TrustedAccountItem:
    default_credit = record.get("default_credit")
    return TrustedAccountItem(
        account_id=str(record["account_id"]),
        description=str(record.get("description") or ""),
        provisioning_policy=str(record.get("provisioning_policy") or "invite_only"),  # type: ignore[arg-type]
        allowed_role_patterns=list(record.get("allowed_role_patterns") or []),
        allow_iam_user=bool(record.get("allow_iam_user") or False),
        allow_instance_profile=bool(record.get("allow_instance_profile") or False),
        default_tenant_id=record.get("default_tenant_id"),
        default_credit=int(default_credit) if default_credit is not None else None,
        created_at=record.get("created_at"),
        updated_at=record.get("updated_at"),
        created_by=record.get("created_by"),
    )


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------
@router.get("", response_model=TrustedAccountsListResponse)
def list_trusted_accounts(
    cursor: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    _admin: AuthenticatedUser = Depends(require_permission("accounts:read")),
) -> TrustedAccountsListResponse:
    repo = TrustedAccountsRepository()
    items, last_key = repo.list_all(cursor=_decode_cursor(cursor), limit=limit)
    return TrustedAccountsListResponse(
        accounts=[_to_item(it) for it in items],
        next_cursor=_encode_cursor(last_key),
    )


@router.post("", response_model=TrustedAccountItem, status_code=201)
def create_trusted_account(
    body: CreateTrustedAccountRequest,
    actor: AuthenticatedUser = Depends(require_permission("accounts:create")),
) -> TrustedAccountItem:
    repo = TrustedAccountsRepository()
    existing = repo.get(body.account_id)
    if existing:
        raise HTTPException(status_code=409, detail="Trusted account already exists")
    try:
        item = repo.put(
            account_id=body.account_id,
            description=body.description,
            provisioning_policy=body.provisioning_policy,
            allowed_role_patterns=body.allowed_role_patterns,
            allow_iam_user=body.allow_iam_user,
            allow_instance_profile=body.allow_instance_profile,
            default_tenant_id=body.default_tenant_id,
            default_credit=body.default_credit,
            created_by=actor.user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    log_audit_event(
        event="trusted_account_created",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=body.account_id,
        target_type="trusted_account",
        details={
            "provisioning_policy": body.provisioning_policy,
            "allow_iam_user": body.allow_iam_user,
            "allow_instance_profile": body.allow_instance_profile,
            "allowed_role_patterns": body.allowed_role_patterns,
        },
    )
    return _to_item(item)


@router.get("/{account_id}", response_model=TrustedAccountItem)
def get_trusted_account(
    account_id: str,
    _admin: AuthenticatedUser = Depends(require_permission("accounts:read")),
) -> TrustedAccountItem:
    repo = TrustedAccountsRepository()
    item = repo.get(account_id)
    if not item:
        raise HTTPException(status_code=404, detail="Trusted account not found")
    return _to_item(item)


@router.patch("/{account_id}", response_model=TrustedAccountItem)
def update_trusted_account(
    account_id: str,
    body: UpdateTrustedAccountRequest,
    actor: AuthenticatedUser = Depends(require_permission("accounts:update")),
) -> TrustedAccountItem:
    repo = TrustedAccountsRepository()
    try:
        item = repo.update(
            account_id=account_id,
            description=body.description,
            provisioning_policy=body.provisioning_policy,
            allowed_role_patterns=body.allowed_role_patterns,
            allow_iam_user=body.allow_iam_user,
            allow_instance_profile=body.allow_instance_profile,
            default_tenant_id=body.default_tenant_id,
            default_credit=body.default_credit,
        )
    except TrustedAccountNotFoundError:
        raise HTTPException(status_code=404, detail="Trusted account not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    log_audit_event(
        event="trusted_account_updated",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=account_id,
        target_type="trusted_account",
        after=body.model_dump(exclude_none=True),
    )
    return _to_item(item)


@router.delete("/{account_id}")
def delete_trusted_account(
    account_id: str,
    actor: AuthenticatedUser = Depends(require_permission("accounts:delete")),
) -> Response:
    repo = TrustedAccountsRepository()
    existing = repo.get(account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Trusted account not found")
    repo.delete(account_id)
    log_audit_event(
        event="trusted_account_deleted",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=account_id,
        target_type="trusted_account",
    )
    return Response(status_code=204)
