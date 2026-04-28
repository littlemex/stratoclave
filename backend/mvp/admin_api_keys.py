"""Admin-scope API key management (Phase C).

GET    /api/mvp/admin/api-keys                      list every key (cursor paging)
GET    /api/mvp/admin/users/{user_id}/api-keys      list one user's keys
POST   /api/mvp/admin/users/{user_id}/api-keys      issue a key on behalf of a user
DELETE /api/mvp/admin/api-keys/by-key-id/{key_id}   revoke any key (by masked id)
DELETE /api/mvp/admin/api-keys/{key_hash}           REMOVED — returns 410 Gone (P0-2')

Authorization:
  - list / issue / revoke all require `apikeys:*` (admin-only).
  - On behalf of a user, `scopes` must be a subset of the target user's role grants.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from dynamo import (
    ApiKeyLimitExceededError,
    ApiKeyNotFoundError,
    ApiKeysRepository,
    MAX_ACTIVE_KEYS_PER_USER,
    UsersRepository,
    api_key_to_public_dict,
)

from .authz import has_permission, log_audit_event, require_permission
from .deps import AuthenticatedUser
from .me_api_keys import (
    ApiKeySummary,
    CreateApiKeyResponse,
    DEFAULT_EXPIRES_DAYS,
    DEFAULT_SCOPES,
)


router = APIRouter(prefix="/api/mvp/admin", tags=["mvp-admin-api-keys"])
_log = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class AdminApiKeysListResponse(BaseModel):
    keys: list[ApiKeySummary]
    next_cursor: Optional[str] = None


class AdminCreateApiKeyRequest(BaseModel):
    """Admin が任意ユーザーに代理発行する際のリクエスト."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=64)
    scopes: Optional[list[str]] = Field(default=None, max_length=32)
    expires_in_days: Optional[int] = Field(default=DEFAULT_EXPIRES_DAYS, ge=0, le=3650)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
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
        raise HTTPException(status_code=400, detail="invalid cursor")


def _target_user_roles(user_id: str) -> list[str]:
    users_repo = UsersRepository()
    rec = users_repo.get_by_user_id(user_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"user not found: {user_id}")
    roles_raw = rec.get("roles") or []
    if isinstance(roles_raw, str):
        return [roles_raw]
    return [str(r) for r in roles_raw]


def _resolve_scopes_for(roles: list[str], requested: Optional[list[str]]) -> list[str]:
    final = list(requested) if requested else list(DEFAULT_SCOPES)
    final = [s.strip() for s in final if isinstance(s, str) and s.strip()]
    if not final:
        raise HTTPException(
            status_code=422, detail="scopes must contain at least one permission"
        )
    for scope in final:
        if ":" not in scope:
            raise HTTPException(
                status_code=422,
                detail=f"invalid scope: {scope!r} (must be 'resource:action')",
            )
        if not has_permission(roles, scope):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"scope {scope!r} is not within target user's roles "
                    f"({', '.join(roles)})"
                ),
            )
    return final


def _resolve_expires_at(expires_in_days: Optional[int]) -> Optional[str]:
    if expires_in_days is None or expires_in_days == 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat()


# ---------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------
@router.get("/api-keys", response_model=AdminApiKeysListResponse)
def list_all_api_keys(
    cursor: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    include_revoked: bool = False,
    _admin: AuthenticatedUser = Depends(require_permission("apikeys:read")),
) -> AdminApiKeysListResponse:
    repo = ApiKeysRepository()
    items, last_key = repo.list_all(cursor=_decode_cursor(cursor), limit=limit)
    if not include_revoked:
        items = [it for it in items if not it.get("revoked_at")]
    return AdminApiKeysListResponse(
        keys=[ApiKeySummary(**api_key_to_public_dict(it)) for it in items],
        next_cursor=_encode_cursor(last_key),
    )


@router.get("/users/{user_id}/api-keys", response_model=list[ApiKeySummary])
def list_user_api_keys(
    user_id: str,
    include_revoked: bool = False,
    _admin: AuthenticatedUser = Depends(require_permission("apikeys:read")),
) -> list[ApiKeySummary]:
    # 対象ユーザー存在確認 (404 統一)
    _target_user_roles(user_id)
    repo = ApiKeysRepository()
    items = repo.list_by_user(user_id, include_revoked=include_revoked)
    return [ApiKeySummary(**api_key_to_public_dict(it)) for it in items]


@router.post(
    "/users/{user_id}/api-keys",
    response_model=CreateApiKeyResponse,
    status_code=201,
)
def create_api_key_on_behalf(
    user_id: str,
    body: AdminCreateApiKeyRequest,
    actor: AuthenticatedUser = Depends(require_permission("apikeys:create")),
) -> CreateApiKeyResponse:
    # API Key 自身での代理発行は禁止 (特権昇格防止)
    if actor.auth_kind == "api_key":
        raise HTTPException(
            status_code=403,
            detail="API keys cannot issue keys on behalf of users. Use Cognito login.",
        )

    target_roles = _target_user_roles(user_id)
    scopes = _resolve_scopes_for(target_roles, body.scopes)
    expires_at = _resolve_expires_at(body.expires_in_days)

    repo = ApiKeysRepository()
    try:
        item, plaintext = repo.create(
            user_id=user_id,
            name=body.name,
            scopes=scopes,
            expires_at=expires_at,
            created_by=actor.user_id,
        )
    except ApiKeyLimitExceededError as e:
        raise HTTPException(
            status_code=409,
            detail=f"active api key limit ({MAX_ACTIVE_KEYS_PER_USER}) reached: {e}",
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    log_audit_event(
        event="api_key_created",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=item["key_id"],
        target_type="api_key",
        details={
            "name": body.name,
            "scopes": scopes,
            "expires_at": expires_at,
            "on_behalf_of": user_id,
        },
    )
    return CreateApiKeyResponse(
        key_id=item["key_id"],
        plaintext_key=plaintext,
        name=item.get("name") or "",
        scopes=scopes,
        expires_at=expires_at,
        created_at=item["created_at"],
    )


@router.delete("/api-keys/by-key-id/{key_id:path}")
def revoke_any_api_key_by_key_id(
    key_id: str,
    actor: AuthenticatedUser = Depends(require_permission("apikeys:revoke")),
) -> Response:
    """Admin revoke-any-key using the masked `key_id`.

    P0-2' (2026-04 security review): the old path accepted the full
    SHA-256 `key_hash` in the URL, which persisted it in CloudFront /
    ALB access logs and made the endpoint itself a revoke oracle for
    anyone with log-read access. The short `sk-stratoclave-xxxx...yyyy`
    `key_id` is already shown in every admin listing, so using it as
    the identifier here does not trade away anything new while removing
    a long-lived enumeration surface.
    """
    repo = ApiKeysRepository()
    item = repo.find_any_by_key_id(key_id)
    if not item or item.get("revoked_at"):
        raise HTTPException(status_code=404, detail="api key not found")
    key_hash = str(item.get("key_hash") or "")
    if not key_hash:
        raise HTTPException(status_code=404, detail="api key not found")
    try:
        repo.revoke(key_hash, actor_user_id=actor.user_id)
    except ApiKeyNotFoundError:
        raise HTTPException(status_code=404, detail="api key not found")
    log_audit_event(
        event="api_key_revoked",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=item.get("key_id") or "(unknown)",
        target_type="api_key",
        details={"owner_user_id": item.get("user_id"), "by_admin": True},
    )
    return Response(status_code=204)


@router.delete(
    "/api-keys/{key_hash}",
    deprecated=True,
    include_in_schema=False,
)
def revoke_any_api_key_legacy(
    key_hash: str,  # noqa: ARG001
    _actor: AuthenticatedUser = Depends(require_permission("apikeys:revoke")),
) -> Response:
    """REMOVED. Use `DELETE /api/mvp/admin/api-keys/by-key-id/{key_id}`.

    P0-2' (2026-04 security review): this path exposed the SHA-256
    `key_hash` in the URL, which (a) left it in every CloudFront / ALB
    access log and (b) turned the endpoint into a revoke oracle for any
    principal with log-read access. Rather than patch it, we return
    410 Gone for every caller and keep the handler only to avoid a
    silent 405 for stale scripts. Clients already migrated to the
    `by-key-id` path above.
    """
    raise HTTPException(
        status_code=410,
        detail=(
            "This endpoint has been removed. Use "
            "`DELETE /api/mvp/admin/api-keys/by-key-id/{key_id}` instead."
        ),
    )
