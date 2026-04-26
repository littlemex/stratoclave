"""Phase 2 RBAC / Tenant 認可ロジック.

設計書 §3 に従う:
- Permissions テーブル (DynamoDB) が真実源、permissions.json から seed
- Cognito Group は使わない、`cognito:groups` claim は無視
- ワイルドカードは resource 名完全一致のみ許可 (users:* は users:create をカバーするが
  users-admin:create はカバーしない。resource 名に - _ 禁止を前提)
- require_tenant_owner は admin 以外を 404 統一 (enumeration 防御)
- Admin 高リスク操作は CloudWatch に構造化 audit JSON 出力
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from fastapi import Depends, HTTPException

from dynamo import PermissionsRepository, TenantsRepository

from .deps import AuthenticatedUser, get_current_user


# -----------------------------------------------------------------
# Permissions キャッシュ (TTL 10 秒、ECS マルチタスク時の整合劣化を最小化)
# -----------------------------------------------------------------
_PERMS_CACHE: dict[str, tuple[list[str], float]] = {}
_PERMS_TTL = 10.0


def _get_permissions_for_role(role: str) -> list[str]:
    now = time.time()
    cached = _PERMS_CACHE.get(role)
    if cached and cached[1] > now:
        return cached[0]
    perms = PermissionsRepository().get(role) or []
    _PERMS_CACHE[role] = (perms, now + _PERMS_TTL)
    return perms


def _clear_permissions_cache() -> None:
    """主にテスト用."""
    _PERMS_CACHE.clear()


def _split_resource(permission: str) -> str:
    """permission 文字列から resource 部分 (最初のコロンより前) を抽出."""
    return permission.split(":", 1)[0]


def has_permission(roles: Iterable[str], permission: str) -> bool:
    """roles のいずれかが permission を保持するか判定.

    - ワイルドカード: "users:*" は "users:create", "users:read" をカバー。
      resource 名完全一致のみ (users-admin:* などには誤マッチしない)
    - 複数 role (例 ["admin", "team_lead"]) は和集合評価
    """
    target_resource = _split_resource(permission)
    for role in roles:
        perms = _get_permissions_for_role(role)
        for p in perms:
            if p == permission:
                return True
            if p.endswith(":*"):
                p_resource = _split_resource(p)
                if p_resource == target_resource:
                    return True
    return False


def _permission_matches(perms: Iterable[str], permission: str) -> bool:
    """permission 文字列のリストから判定 (role 解決なし).

    `has_permission(roles, ...)` と違い、roles → Permissions テーブル lookup を
    経由せず直接 permissions リストで判定する. API Key の scopes 用.
    """
    target_resource = _split_resource(permission)
    for p in perms:
        if p == permission:
            return True
        if p.endswith(":*"):
            p_resource = _split_resource(p)
            if p_resource == target_resource:
                return True
    return False


def user_has_permission(user: AuthenticatedUser, permission: str) -> bool:
    """JWT (Cognito) と API Key (sk-stratoclave) を統一的に判定.

    - JWT 認証: user.roles が permission を保持していれば true
    - API Key 認証: user.roles と user.key_scopes の **両方** が permission を持つ
      (= API Key scopes は User の roles の subset として有効化)
    """
    if user.auth_kind == "api_key" and user.key_scopes is not None:
        if not _permission_matches(user.key_scopes, permission):
            return False
        # さらに owner の roles 側にも permission があることを確認
        return has_permission(user.roles, permission)
    return has_permission(user.roles, permission)


# -----------------------------------------------------------------
# FastAPI dependency helpers
# -----------------------------------------------------------------
def require_permission(permission: str) -> Callable[..., AuthenticatedUser]:
    """指定 permission を持つユーザーのみ通過させる FastAPI Depends.

    API Key 認証の場合は user.roles と user.key_scopes の両方が permission を
    持つことを要求する (user_has_permission 経由).
    """

    def _dep(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if not user_has_permission(user, permission):
            raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")
        return user

    return _dep


def require_any_role(*allowed_roles: str) -> Callable[..., AuthenticatedUser]:
    """指定 role のいずれかを持つユーザーのみ通過 (permission より粗い判定)."""

    def _dep(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if not any(r in user.roles for r in allowed_roles):
            raise HTTPException(status_code=403, detail="Forbidden role")
        return user

    return _dep


def require_tenant_owner(tenant_id_param: str = "tenant_id") -> Callable[..., AuthenticatedUser]:
    """Tenant の所有者 (team_lead_user_id == user.user_id) のみ通過。

    admin は全 Tenant にアクセス可。
    非所有者・非存在は一律 404 に統一 (§3.7、enumeration 防御)。
    """

    def _dep(
        tenant_id: str,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        tenant = TenantsRepository().get(tenant_id)
        if "admin" in user.roles:
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")
            return user
        if not tenant or tenant.get("team_lead_user_id") != user.user_id:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return user

    # FastAPI が path param を inject できるよう、param 名が "tenant_id" の場合は
    # そのまま動作する。別名を使いたい場合はエンドポイント側で path param を tenant_id にすること。
    _dep.__name__ = f"require_tenant_owner_{tenant_id_param}"
    return _dep


# -----------------------------------------------------------------
# Admin 作成ゲート (Critical C-D)
# -----------------------------------------------------------------
def admin_creation_allowed() -> bool:
    """ALLOW_ADMIN_CREATION=true かつ production では warning を audit に記録."""
    allowed = os.getenv("ALLOW_ADMIN_CREATION", "false").lower() == "true"
    return allowed


def warn_if_admin_creation_enabled_in_production(logger: logging.Logger) -> None:
    env = os.getenv("ENVIRONMENT", "development")
    if env == "production" and admin_creation_allowed():
        logger.warning(
            "allow_admin_creation_enabled_in_production",
            extra={"event": "allow_admin_creation_warning", "environment": env},
        )


# -----------------------------------------------------------------
# Audit log (CloudWatch 構造化 JSON、Phase 3 で専用テーブルへ昇格)
# -----------------------------------------------------------------
_audit_logger = logging.getLogger("stratoclave.audit")


def log_audit_event(
    *,
    event: str,
    actor_id: str,
    actor_email: Optional[str] = None,
    target_id: Optional[str] = None,
    target_type: Optional[str] = None,
    tenant_id: Optional[str] = None,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """高リスク Admin 操作の audit log を CloudWatch に出力.

    `event` 例: "admin_created", "tenant_owner_changed", "user_tenant_switched",
              "credit_overwritten", "user_deleted"
    """
    payload: dict[str, Any] = {
        "event": event,
        "actor_id": actor_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if actor_email:
        payload["actor_email"] = actor_email
    if target_id:
        payload["target_id"] = target_id
    if target_type:
        payload["target_type"] = target_type
    if tenant_id:
        payload["tenant_id"] = tenant_id
    if before is not None:
        payload["before"] = before
    if after is not None:
        payload["after"] = after
    if details:
        payload["details"] = details

    # structlog を使っているが、audit ログは意図を伝えるため明示的な JSON 1 行を出す
    _audit_logger.info(json.dumps(payload, default=str, ensure_ascii=False))
