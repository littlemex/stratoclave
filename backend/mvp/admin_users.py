"""Admin API: ユーザー作成 (Phase 2 v2.1).

POST /api/mvp/admin/users
    入力: {
      "email": "user@example.com",
      "role": "user" | "team_lead" | "admin"  (optional, default "user"),
      "tenant_id": "tenant-xxx"  (optional, default DEFAULT_ORG_ID),
      "total_credit": int  (optional, Tenant の default_credit を override)
    }
    処理:
      1. `users:create` permission を require
      2. role=="admin" の場合は環境変数 `ALLOW_ADMIN_CREATION=true` ゲート (Critical C-D)
      3. Cognito AdminCreateUser (一時パスワード自動生成、メール送信は SUPPRESS)
      4. DynamoDB Users + UserTenants を指定 tenant で作成
         - total_credit 未指定 → Tenant.default_credit、無ければ DEFAULT_TENANT_CREDIT
      5. Audit log 出力 (admin 作成時は event=admin_created)
    レスポンス:
      { "email", "user_id", "temporary_password", "user_pool_id", "org_id", "role" }
"""
from __future__ import annotations

import logging
import os
from typing import Any, Literal, Optional

import boto3
from boto3.dynamodb.conditions import Key as boto3_key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from dynamo import (
    TenantsRepository,
    UsersRepository,
    UserTenantsRepository,
)
from dynamo.client import get_dynamodb_resource

from .authz import (
    admin_creation_allowed,
    log_audit_event,
    require_permission,
    warn_if_admin_creation_enabled_in_production,
)
from .cognito_admin import delete_user as cognito_delete_user, global_sign_out, update_org_id
from .deps import DEFAULT_ORG_ID, AuthenticatedUser


router = APIRouter(prefix="/api/mvp/admin", tags=["mvp-admin"])
_log = logging.getLogger(__name__)


Role = Literal["admin", "team_lead", "user"]


class CreateUserRequest(BaseModel):
    """Pydantic Literal + extra=forbid で型制約 (Critical C-D / Security 4.1)."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    role: Role = "user"
    tenant_id: Optional[str] = Field(default=None, max_length=64)
    total_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)


class CreateUserResponse(BaseModel):
    email: str
    user_id: str
    # temporary_password はデフォルトでレスポンスから外す (P0-3).
    # access log / HAR / browser devtools 経由の漏洩を防ぐため。
    # 互換のために環境変数 EXPOSE_TEMPORARY_PASSWORD=true の時だけ含める。
    # 推奨: ForcePasswordReset + SES メール配信 / one-time secret link への移行 (P1).
    temporary_password: Optional[str] = None
    user_pool_id: str
    org_id: str
    role: Role


def _cognito_client():
    region = os.getenv("COGNITO_REGION") or os.getenv("AWS_REGION", "us-east-1")
    return boto3.client("cognito-idp", region_name=region)


def _require_user_pool_id() -> str:
    pool_id = os.getenv("COGNITO_USER_POOL_ID")
    if not pool_id:
        raise HTTPException(
            status_code=500,
            detail="COGNITO_USER_POOL_ID is not configured",
        )
    return pool_id


@router.post("/users", response_model=CreateUserResponse, status_code=201)
def create_user(
    body: CreateUserRequest,
    actor: AuthenticatedUser = Depends(require_permission("users:create")),
) -> CreateUserResponse:
    # Admin creation gate (Critical C-D, P1-A time-bounded in production).
    if body.role == "admin":
        if not admin_creation_allowed():
            raise HTTPException(
                status_code=403,
                detail=(
                    "admin role creation is disabled. In development set "
                    "ALLOW_ADMIN_CREATION=true. In production set "
                    "ALLOW_ADMIN_CREATION=true AND "
                    "ALLOW_ADMIN_CREATION_UNTIL=<future-epoch-seconds>; "
                    "the gate auto-closes once the epoch passes."
                ),
            )
        # Emit a per-request audit warning whenever the gate is
        # actively open in production so forgetting to close it is
        # impossible to miss in CloudWatch alerts.
        warn_if_admin_creation_enabled_in_production(_log)

    pool_id = _require_user_pool_id()
    email = body.email.lower().strip()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="invalid email format")

    # Tenant 解決: 指定あれば存在チェック、無ければ default-org
    tenant_id = body.tenant_id or DEFAULT_ORG_ID
    tenants_repo = TenantsRepository()
    tenant_rec = tenants_repo.get(tenant_id)
    if not tenant_rec:
        raise HTTPException(status_code=422, detail=f"tenant_id not found: {tenant_id}")

    cognito = _cognito_client()

    # Cognito AdminCreateUser
    try:
        resp = cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:org_id", "Value": tenant_id},
            ],
            DesiredDeliveryMediums=[],
            MessageAction="SUPPRESS",
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "UsernameExistsException":
            raise HTTPException(status_code=409, detail="User already exists")
        raise HTTPException(status_code=502, detail=f"Cognito error: {code}")

    cognito_user = resp["User"]
    sub: Optional[str] = None
    for attr in cognito_user.get("Attributes", []):
        if attr.get("Name") == "sub":
            sub = attr.get("Value")
            break
    if not sub:
        raise HTTPException(status_code=502, detail="Cognito response missing sub")

    # 一時パスワードを明示発行 (Permanent=False で NEW_PASSWORD_REQUIRED を発火)
    temp_password = _generate_temp_password()
    try:
        cognito.admin_set_user_password(
            UserPoolId=pool_id,
            Username=email,
            Password=temp_password,
            Permanent=False,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        raise HTTPException(status_code=502, detail=f"Cognito set_password error: {code}")

    # DynamoDB: Users
    users_repo = UsersRepository()
    user_tenants_repo = UserTenantsRepository()

    users_repo.put_user(
        user_id=sub,
        email=email,
        auth_provider="cognito",
        auth_provider_user_id=sub,
        org_id=tenant_id,
        roles=[body.role],
    )

    # DynamoDB: UserTenants (if total_credit is None, ensure() falls back to
    # the tenant default). Explicit admin re-add is the one path that is
    # allowed to resurrect an archived membership — `/api/mvp/me` must not
    # (P0-1, see dynamo/user_tenants.py).
    user_tenants_repo.ensure(
        user_id=sub,
        tenant_id=tenant_id,
        role=body.role,
        total_credit=body.total_credit,
        allow_resurrection=True,
    )

    # Audit log: admin 作成は常に、その他 role も記録
    log_audit_event(
        event="admin_created" if body.role == "admin" else "user_created",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=sub,
        target_type="user",
        tenant_id=tenant_id,
        details={"email": email, "role": body.role, "allow_admin_creation": admin_creation_allowed()},
    )

    # temporary_password のレスポンス露出は環境変数 EXPOSE_TEMPORARY_PASSWORD=true の時のみ。
    # デフォルトでは None にして、access log / HAR / browser devtools 経由の漏洩を防ぐ。
    # この場合、Admin は Cognito コンソール経由でパスワードリセットメールを送るか、
    # bootstrap-admin.sh の手動実行で取得する運用になる。
    expose_temp_password = (
        os.getenv("EXPOSE_TEMPORARY_PASSWORD", "false").lower() == "true"
    )
    return CreateUserResponse(
        email=email,
        user_id=sub,
        temporary_password=temp_password if expose_temp_password else None,
        user_pool_id=pool_id,
        org_id=tenant_id,
        role=body.role,
    )


def _generate_temp_password(length: int = 16) -> str:
    """Cognito のパスワードポリシー (大小英数記号各1以上) を満たす乱数パスワード."""
    import secrets
    import string

    alphabet_lower = string.ascii_lowercase
    alphabet_upper = string.ascii_uppercase
    digits = string.digits
    symbols = "!@#$%^&*()-_=+[]{}"

    required = [
        secrets.choice(alphabet_lower),
        secrets.choice(alphabet_upper),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    remain_pool = alphabet_lower + alphabet_upper + digits + symbols
    remain = [secrets.choice(remain_pool) for _ in range(max(length - len(required), 4))]
    chars = required + remain
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


# -----------------------------------------------------------------------
# Listing / inspection APIs
# -----------------------------------------------------------------------
class UserSummary(BaseModel):
    user_id: str
    email: str
    roles: list[str]
    org_id: str
    total_credit: int = 0
    credit_used: int = 0
    remaining_credit: int = 0
    created_at: Optional[str] = None
    # Phase S: SSO / auth method metadata
    auth_method: Optional[str] = None
    sso_account_id: Optional[str] = None
    sso_principal_arn: Optional[str] = None
    last_sso_login_at: Optional[str] = None


class UsersListResponse(BaseModel):
    users: list[UserSummary]
    next_cursor: Optional[str] = None


def _encode_cursor(last_key: Optional[dict]) -> Optional[str]:
    if not last_key:
        return None
    import base64
    import json
    return base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode()


def _decode_cursor(cursor: Optional[str]) -> Optional[dict]:
    if not cursor:
        return None
    import base64
    import json
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cursor")


def _enrich_user_with_credit(user_item: dict, repo: UserTenantsRepository) -> UserSummary:
    """Users item に active UserTenants の credit を付けて UserSummary 化."""
    org_id = str(user_item.get("org_id") or DEFAULT_ORG_ID)
    summary = repo.credit_summary(str(user_item["user_id"]), org_id)
    roles_raw = user_item.get("roles") or []
    if isinstance(roles_raw, str):
        roles = [roles_raw]
    else:
        roles = [str(r) for r in roles_raw]
    return UserSummary(
        user_id=str(user_item["user_id"]),
        email=str(user_item.get("email") or ""),
        roles=roles,
        org_id=org_id,
        total_credit=summary["total_credit"],
        credit_used=summary["credit_used"],
        remaining_credit=summary["remaining_credit"],
        created_at=user_item.get("created_at"),
        auth_method=user_item.get("auth_method"),
        sso_account_id=user_item.get("sso_account_id"),
        sso_principal_arn=user_item.get("sso_principal_arn"),
        last_sso_login_at=user_item.get("last_sso_login_at"),
    )


@router.get("/users", response_model=UsersListResponse)
def list_users(
    cursor: Optional[str] = None,
    limit: int = 50,
    role: Optional[Role] = None,
    tenant_id: Optional[str] = None,
    _admin: AuthenticatedUser = Depends(require_permission("users:read")),
) -> UsersListResponse:
    """全ユーザー一覧 (Scan + cursor pagination)。

    MVP の規模では Users は Scan で十分。role/tenant はサーバー側で filter する。
    limit は 100 を上限にクリップ (H2)。
    """
    users_repo = UsersRepository()
    user_tenants_repo = UserTenantsRepository()
    limit = max(1, min(limit, 100))

    scan_kwargs: dict[str, Any] = {"Limit": limit}
    decoded = _decode_cursor(cursor)
    if decoded:
        scan_kwargs["ExclusiveStartKey"] = decoded

    resp = users_repo._table.scan(**scan_kwargs)  # 単一テーブル Scan、PROFILE SK でフィルタは後段
    raw_items = resp.get("Items", [])
    next_cursor = _encode_cursor(resp.get("LastEvaluatedKey"))

    results: list[UserSummary] = []
    for item in raw_items:
        if item.get("sk") != users_repo.SK_PROFILE:
            continue
        roles_raw = item.get("roles") or []
        roles_list = [roles_raw] if isinstance(roles_raw, str) else [str(r) for r in roles_raw]
        if role and role not in roles_list:
            continue
        if tenant_id and str(item.get("org_id") or "") != tenant_id:
            continue
        results.append(_enrich_user_with_credit(item, user_tenants_repo))

    return UsersListResponse(users=results, next_cursor=next_cursor)


@router.get("/users/{user_id}", response_model=UserSummary)
def get_user(
    user_id: str,
    _admin: AuthenticatedUser = Depends(require_permission("users:read")),
) -> UserSummary:
    users_repo = UsersRepository()
    item = users_repo.get_by_user_id(user_id)
    if not item:
        raise HTTPException(status_code=404, detail="User not found")
    return _enrich_user_with_credit(item, UserTenantsRepository())


# -----------------------------------------------------------------------
# Delete (with last-admin protection)
# -----------------------------------------------------------------------
def _count_active_admins() -> int:
    """DynamoDB Users から `roles` に admin を含む active user 数を数える."""
    repo = UsersRepository()
    resp = repo._table.scan()
    count = 0
    for item in resp.get("Items", []):
        if item.get("sk") != repo.SK_PROFILE:
            continue
        roles = item.get("roles") or []
        if "admin" in roles:
            count += 1
    return count


@router.delete("/users/{user_id}")
def delete_user_endpoint(
    user_id: str,
    actor: AuthenticatedUser = Depends(require_permission("users:delete")),
) -> Response:
    """Cognito + DynamoDB からユーザーを削除。

    Last admin 削除防止: 対象が admin ロールを持ち、かつ active admin が 1 人しか居ない場合 409。
    UsageLogs は監査用に残す。
    """
    users_repo = UsersRepository()
    item = users_repo.get_by_user_id(user_id)
    if not item:
        raise HTTPException(status_code=404, detail="User not found")

    email = str(item.get("email") or "")
    roles = item.get("roles") or []
    roles_list = [roles] if isinstance(roles, str) else list(roles)

    if "admin" in roles_list and _count_active_admins() <= 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last admin user")

    # 自分自身削除も禁止 (操作事故防止)
    if user_id == actor.user_id:
        raise HTTPException(status_code=409, detail="Cannot delete yourself")

    # Cognito 側削除 (先に実行。失敗したら中断)
    if email:
        cognito_delete_user(email)

    # DynamoDB Users を削除
    users_repo._table.delete_item(Key={"user_id": user_id, "sk": users_repo.SK_PROFILE})

    # UserTenants を archived にする (履歴は残す)
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    user_tenants_repo = UserTenantsRepository()
    resp = user_tenants_repo._table.query(
        KeyConditionExpression=boto3_key("user_id").eq(user_id),
    )
    for ut in resp.get("Items", []):
        user_tenants_repo._table.update_item(
            Key={"user_id": user_id, "tenant_id": ut["tenant_id"]},
            UpdateExpression="SET #s = :archived, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":archived": "archived", ":now": now_iso},
        )

    log_audit_event(
        event="user_deleted",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=user_id,
        target_type="user",
        details={"email": email, "roles": roles_list},
    )
    return Response(status_code=204)


# -----------------------------------------------------------------------
# Tenant assignment (switch)
# -----------------------------------------------------------------------
class AssignTenantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=64)
    total_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    new_role: Role = "user"


@router.put("/users/{user_id}/tenant", response_model=UserSummary)
def assign_tenant(
    user_id: str,
    body: AssignTenantRequest,
    actor: AuthenticatedUser = Depends(require_permission("users:assign-tenant")),
) -> UserSummary:
    """User を指定 Tenant に切替 (TransactWriteItems + Cognito Saga)."""
    users_repo = UsersRepository()
    user_item = users_repo.get_by_user_id(user_id)
    if not user_item:
        raise HTTPException(status_code=404, detail="User not found")

    old_tenant_id = str(user_item.get("org_id") or DEFAULT_ORG_ID)
    new_tenant_id = body.tenant_id.strip()

    if old_tenant_id == new_tenant_id:
        raise HTTPException(status_code=409, detail="User is already assigned to this tenant")

    tenants_repo = TenantsRepository()
    new_tenant = tenants_repo.get(new_tenant_id)
    if not new_tenant:
        raise HTTPException(status_code=422, detail=f"tenant_id not found: {new_tenant_id}")

    user_tenants_repo = UserTenantsRepository()

    # (1) DynamoDB TransactWriteItems
    try:
        user_tenants_repo.switch_tenant(
            user_id=user_id,
            old_tenant_id=old_tenant_id,
            new_tenant_id=new_tenant_id,
            new_role=body.new_role,
            new_total_credit=body.total_credit,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=f"Tenant switch conflict: {e}")

    # (2) Cognito Saga: attribute 更新 → global sign out
    update_org_id(user_id, new_tenant_id)
    global_sign_out(user_id)

    log_audit_event(
        event="user_tenant_switched",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=user_id,
        target_type="user",
        tenant_id=new_tenant_id,
        before={"tenant_id": old_tenant_id},
        after={"tenant_id": new_tenant_id, "role": body.new_role, "total_credit": body.total_credit},
    )

    # 最新の Users を読み直して返す
    fresh = users_repo.get_by_user_id(user_id) or user_item
    return _enrich_user_with_credit(fresh, user_tenants_repo)


# -----------------------------------------------------------------------
# Credit overwrite
# -----------------------------------------------------------------------
class SetCreditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_credit: int = Field(ge=0, le=10_000_000)
    reset_used: bool = False


@router.patch("/users/{user_id}/credit", response_model=UserSummary)
def set_credit(
    user_id: str,
    body: SetCreditRequest,
    actor: AuthenticatedUser = Depends(require_permission("users:update")),
) -> UserSummary:
    users_repo = UsersRepository()
    user_item = users_repo.get_by_user_id(user_id)
    if not user_item:
        raise HTTPException(status_code=404, detail="User not found")
    tenant_id = str(user_item.get("org_id") or DEFAULT_ORG_ID)

    user_tenants_repo = UserTenantsRepository()
    prev = user_tenants_repo.credit_summary(user_id, tenant_id)
    try:
        user_tenants_repo.overwrite_credit(
            user_id=user_id,
            tenant_id=tenant_id,
            total_credit=body.total_credit,
            reset_used=body.reset_used,
        )
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"Credit update conflict: {e}")

    log_audit_event(
        event="credit_overwritten",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=user_id,
        target_type="user",
        tenant_id=tenant_id,
        before=prev,
        after={"total_credit": body.total_credit, "reset_used": body.reset_used},
    )

    return _enrich_user_with_credit(user_item, user_tenants_repo)
