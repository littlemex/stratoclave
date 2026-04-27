"""自分の API Keys 管理 (Phase C).

GET    /api/mvp/me/api-keys             自分の key 一覧 (plaintext は含まない)
POST   /api/mvp/me/api-keys             新規作成 (plaintext を作成時レスポンスで 1 回だけ返す)
DELETE /api/mvp/me/api-keys/{key_hash}  自分の key を revoke

スコープ制約:
  - リクエスト時に指定した `scopes` は、呼び出し元ユーザーの roles が持つ
    permissions の subset でなければならない (上位権限への横取りを防ぐ).
  - `scopes` を未指定 (空 or null) の場合は default ("messages:send", "usage:read-self")
    を付与.

有効期限:
  - `expires_in_days` で指定. 未指定時は 30 日. `0` or `null` を明示指定したら無期限.
  - 範囲: 1..3650 (10 年) + 無期限 (null).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from dynamo import (
    ApiKeyLimitExceededError,
    ApiKeyNotFoundError,
    ApiKeysRepository,
    MAX_ACTIVE_KEYS_PER_USER,
    api_key_to_public_dict,
)

from .authz import has_permission, log_audit_event, require_permission
from .deps import AuthenticatedUser


router = APIRouter(prefix="/api/mvp/me/api-keys", tags=["mvp-me-api-keys"])
_log = logging.getLogger(__name__)


DEFAULT_SCOPES = ["messages:send", "usage:read-self"]
DEFAULT_EXPIRES_DAYS = 30


# ---------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------
class ApiKeySummary(BaseModel):
    """plaintext を含まない安全な公開表現."""

    key_id: str
    name: str = ""
    user_id: str
    scopes: list[str] = []
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None
    last_used_at: Optional[str] = None
    created_by: Optional[str] = None


class ApiKeyList(BaseModel):
    keys: list[ApiKeySummary]
    active_count: int
    max_per_user: int


class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=64)
    scopes: Optional[list[str]] = Field(default=None, max_length=32)
    # None または 0 = 無期限、それ以外は日数指定 (1..3650)
    expires_in_days: Optional[int] = Field(default=DEFAULT_EXPIRES_DAYS, ge=0, le=3650)


class CreateApiKeyResponse(BaseModel):
    """作成直後のみ plaintext を含む特別なレスポンス."""

    key_id: str
    plaintext_key: str
    name: str = ""
    scopes: list[str]
    expires_at: Optional[str] = None
    created_at: str


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def _resolve_scopes(
    user: AuthenticatedUser, requested: Optional[list[str]]
) -> list[str]:
    """リクエストされた scopes が user の permissions の subset であることを検証."""
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
        if not has_permission(user.roles, scope):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"scope {scope!r} is not included in your roles "
                    f"({', '.join(user.roles)}); cannot escalate"
                ),
            )
    return final


def _resolve_expires_at(expires_in_days: Optional[int]) -> Optional[str]:
    """日数→ISO 8601. 0/None は無期限 (None)."""
    if expires_in_days is None or expires_in_days == 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat()


# ---------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------
@router.get("", response_model=ApiKeyList)
def list_my_api_keys(
    include_revoked: bool = False,
    user: AuthenticatedUser = Depends(require_permission("apikeys:read-self")),
) -> ApiKeyList:
    repo = ApiKeysRepository()
    items = repo.list_by_user(user.user_id, include_revoked=include_revoked)
    active_count = repo.count_active(user.user_id)
    return ApiKeyList(
        keys=[ApiKeySummary(**api_key_to_public_dict(it)) for it in items],
        active_count=active_count,
        max_per_user=MAX_ACTIVE_KEYS_PER_USER,
    )


@router.post("", response_model=CreateApiKeyResponse, status_code=201)
def create_my_api_key(
    body: CreateApiKeyRequest,
    user: AuthenticatedUser = Depends(require_permission("apikeys:create-self")),
) -> CreateApiKeyResponse:
    # API Key 自身からの API Key 作成は禁止 (特権昇格防止)
    if user.auth_kind == "api_key":
        raise HTTPException(
            status_code=403,
            detail="API keys cannot be used to create other API keys. Use Cognito login.",
        )

    scopes = _resolve_scopes(user, body.scopes)
    expires_at = _resolve_expires_at(body.expires_in_days)
    repo = ApiKeysRepository()
    try:
        item, plaintext = repo.create(
            user_id=user.user_id,
            name=body.name,
            scopes=scopes,
            expires_at=expires_at,
            created_by=user.user_id,
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
        actor_id=user.user_id,
        actor_email=user.email,
        target_id=item["key_id"],
        target_type="api_key",
        details={
            "name": body.name,
            "scopes": scopes,
            "expires_at": expires_at,
            "on_behalf_of": user.user_id,
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


@router.delete("/by-key-id/{key_id:path}")
def revoke_my_api_key_by_key_id(
    key_id: str,
    user: AuthenticatedUser = Depends(require_permission("apikeys:revoke-self")),
) -> Response:
    """Preferred revoke endpoint (P1-8).

    Accepts the masked `key_id` (`sk-stratoclave-XXXX...YYYY`) shown in
    `api-key list` instead of the SHA-256 hash. URL-path values end up in
    ALB / CloudFront access logs, and the hash is also DynamoDB's primary
    key — so leaking it via access logs is a long-lived enumeration
    material. The masked id is safe to log and is the only value the UI /
    CLI ever sees.
    """
    repo = ApiKeysRepository()
    item = repo.find_by_user_and_key_id(user.user_id, key_id)
    if not item:
        # Unify with 404 for non-owners (enumeration defense).
        raise HTTPException(status_code=404, detail="api key not found")

    key_hash = str(item["key_hash"])
    try:
        repo.revoke(key_hash, actor_user_id=user.user_id)
    except ApiKeyNotFoundError:
        raise HTTPException(status_code=404, detail="api key not found")

    log_audit_event(
        event="api_key_revoked",
        actor_id=user.user_id,
        actor_email=user.email,
        target_id=item.get("key_id") or "(unknown)",
        target_type="api_key",
        details={"owner_user_id": item.get("user_id")},
    )
    return Response(status_code=204)


@router.delete("/{key_hash}", deprecated=True)
def revoke_my_api_key_by_hash(
    key_hash: str,
    user: AuthenticatedUser = Depends(require_permission("apikeys:revoke-self")),
) -> Response:
    """Legacy revoke path kept for backward compatibility.

    Deprecated (P1-8): prefer `DELETE /api/mvp/me/api-keys/by-key-id/{key_id}`.
    Putting the SHA-256 hash in the URL leaves it in access logs. This
    route will be removed once CLI and UI migrate to the `by-key-id`
    endpoint.
    """
    repo = ApiKeysRepository()
    item = repo.get_by_hash(key_hash)
    if not item or str(item.get("user_id")) != user.user_id:
        raise HTTPException(status_code=404, detail="api key not found")

    try:
        repo.revoke(key_hash, actor_user_id=user.user_id)
    except ApiKeyNotFoundError:
        raise HTTPException(status_code=404, detail="api key not found")

    log_audit_event(
        event="api_key_revoked",
        actor_id=user.user_id,
        actor_email=user.email,
        target_id=item.get("key_id") or "(unknown)",
        target_type="api_key",
        details={"owner_user_id": item.get("user_id"), "via": "legacy-hash-path"},
    )
    return Response(status_code=204)
