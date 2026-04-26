"""Admin API: SSO Pre-Registrations (invite_only 専用) 管理 (Phase S).

- GET    /api/mvp/admin/sso-invites                一覧
- POST   /api/mvp/admin/sso-invites                追加
- DELETE /api/mvp/admin/sso-invites/{email}        削除

特記事項:
- IAM user を招待する場合は iam_user_name を指定 -> iam_user_lookup_key を DB に保存
- invited_role は "user" | "team_lead" (admin は SSO 経由での自動 provisioning 禁止)
"""
from __future__ import annotations

import base64
import json
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from dynamo import (
    SsoPreRegistrationsRepository,
    TrustedAccountsRepository,
)

from .authz import log_audit_event, require_permission
from .deps import AuthenticatedUser


router = APIRouter(prefix="/api/mvp/admin/sso-invites", tags=["mvp-admin-sso"])


InvitedRole = Literal["user", "team_lead"]


# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------
class SsoInviteItem(BaseModel):
    email: str
    account_id: str
    invited_role: InvitedRole
    tenant_id: Optional[str] = None
    total_credit: Optional[int] = None
    iam_user_name: Optional[str] = None
    invited_by: str
    invited_at: str
    consumed_at: Optional[str] = None


class SsoInvitesListResponse(BaseModel):
    invites: list[SsoInviteItem]
    next_cursor: Optional[str] = None


class CreateSsoInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    account_id: str = Field(min_length=12, max_length=12, pattern=r"^\d{12}$")
    invited_role: InvitedRole = "user"
    tenant_id: Optional[str] = Field(default=None, max_length=64)
    total_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    iam_user_name: Optional[str] = Field(default=None, max_length=64)


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


def _to_item(record: dict[str, Any]) -> SsoInviteItem:
    iam_user_name = None
    lookup = record.get("iam_user_lookup_key")
    if isinstance(lookup, str) and "#" in lookup:
        iam_user_name = lookup.split("#", 1)[1]
    total_credit = record.get("total_credit")
    return SsoInviteItem(
        email=str(record.get("email") or ""),
        account_id=str(record.get("account_id") or ""),
        invited_role=str(record.get("invited_role") or "user"),  # type: ignore[arg-type]
        tenant_id=record.get("tenant_id"),
        total_credit=int(total_credit) if total_credit is not None else None,
        iam_user_name=iam_user_name,
        invited_by=str(record.get("invited_by") or ""),
        invited_at=str(record.get("invited_at") or ""),
        consumed_at=record.get("consumed_at"),
    )


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------
@router.get("", response_model=SsoInvitesListResponse)
def list_invites(
    account_id: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    _admin: AuthenticatedUser = Depends(require_permission("accounts:read")),
) -> SsoInvitesListResponse:
    repo = SsoPreRegistrationsRepository()
    if account_id:
        # 特定 account で絞る場合は Scan+filter、cursor は利用しない (件数小想定)
        items = repo.list_by_account(account_id, limit=limit)
        return SsoInvitesListResponse(
            invites=[_to_item(it) for it in items],
            next_cursor=None,
        )
    items, last_key = repo.list_all(cursor=_decode_cursor(cursor), limit=limit)
    return SsoInvitesListResponse(
        invites=[_to_item(it) for it in items],
        next_cursor=_encode_cursor(last_key),
    )


@router.post("", response_model=SsoInviteItem, status_code=201)
def create_invite(
    body: CreateSsoInviteRequest,
    actor: AuthenticatedUser = Depends(require_permission("accounts:create")),
) -> SsoInviteItem:
    # 招待先 trusted_account が存在することを検証
    ta_repo = TrustedAccountsRepository()
    if not ta_repo.get(body.account_id):
        raise HTTPException(
            status_code=422,
            detail=f"Trusted account {body.account_id} is not registered",
        )
    repo = SsoPreRegistrationsRepository()
    existing = repo.get(body.email)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"An invite for {body.email} already exists",
        )
    try:
        item = repo.invite(
            email=body.email,
            account_id=body.account_id,
            invited_role=body.invited_role,
            tenant_id=body.tenant_id,
            total_credit=body.total_credit,
            iam_user_name=body.iam_user_name,
            invited_by=actor.user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    log_audit_event(
        event="sso_invite_created",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=body.email.lower(),
        target_type="sso_invite",
        details={
            "account_id": body.account_id,
            "invited_role": body.invited_role,
            "iam_user_name": body.iam_user_name,
        },
    )
    return _to_item(item)


@router.delete("/{email}")
def delete_invite(
    email: str,
    actor: AuthenticatedUser = Depends(require_permission("accounts:delete")),
) -> Response:
    repo = SsoPreRegistrationsRepository()
    existing = repo.get(email)
    if not existing:
        raise HTTPException(status_code=404, detail="Invite not found")
    repo.delete(email)
    log_audit_event(
        event="sso_invite_deleted",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=email.lower(),
        target_type="sso_invite",
    )
    return Response(status_code=204)
