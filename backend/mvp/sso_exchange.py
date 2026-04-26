"""Phase S: AWS SSO / STS 経由でのログインエンドポイント.

POST /api/mvp/auth/sso-exchange

入力: CLI が `sts:GetCallerIdentity` を sigv4 署名した presigned リクエスト
  {
    "method": "POST",
    "url": "https://sts.us-east-1.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
    "headers": { "Authorization": "AWS4-HMAC-SHA256 ...", "X-Amz-Date": "...", ... }
  }

処理:
  1. Backend が presigned URL を検証し STS を直接叩いて Arn/UserId/Account を取得
  2. identity_type (sso_user/federated_role/iam_user/instance_profile) に分類
  3. TrustedAccounts allowlist + role pattern + provisioning policy で 4 段階 Gate 判定
  4. Cognito User Pool に対応ユーザーを解決 or 作成 (auth_method=sso)
  5. 「random password 発行 → admin_initiate_auth → password 破棄」で access_token 発行

返却:
  { access_token, id_token, refresh_token, expires_in, token_type, email, user_id,
    roles, org_id, identity_type, new_user }
"""
from __future__ import annotations

import logging
import os
import secrets
import string
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from dynamo import (
    SsoPreRegistrationsRepository,
    TenantsRepository,
    UsersRepository,
    UserTenantsRepository,
)

from .authz import log_audit_event
from .sso_gate import validate_sso_identity
from .sso_sts import verify_and_call_sts


router = APIRouter(prefix="/api/mvp/auth", tags=["mvp-sso"])
_log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------
class SsoExchangeRequest(BaseModel):
    """CLI が送ってくる sigv4 presigned request."""

    model_config = ConfigDict(extra="forbid")
    method: str = Field(pattern="^(POST|post)$")
    url: str = Field(min_length=1, max_length=2048)
    headers: dict[str, str]
    # STS GetCallerIdentity では body は固定文字列だが、sigv4 署名に含まれるため
    # CLI からそのまま転送する必要がある.
    body: str = Field(default="", max_length=2048)


class SsoExchangeResponse(BaseModel):
    status: str = "authenticated"
    access_token: str
    id_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    token_type: Optional[str] = None
    email: str
    user_id: str
    roles: list[str]
    org_id: str
    identity_type: str
    new_user: bool = False


# ------------------------------------------------------------------
# Cognito helpers
# ------------------------------------------------------------------
def _cognito_client():
    region = os.getenv("COGNITO_REGION") or os.getenv("AWS_REGION", "us-east-1")
    return boto3.client("cognito-idp", region_name=region)


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise HTTPException(status_code=500, detail=f"{name} is not configured")
    return val


def _generate_temp_password() -> str:
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
    remain = [secrets.choice(remain_pool) for _ in range(20)]
    chars = required + remain
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _cognito_get_user_sub(pool_id: str, username: str) -> Optional[str]:
    cognito = _cognito_client()
    try:
        resp = cognito.admin_get_user(UserPoolId=pool_id, Username=username)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "UserNotFoundException":
            return None
        raise
    for attr in resp.get("UserAttributes", []):
        if attr.get("Name") == "sub":
            return attr.get("Value")
    return None


def _cognito_create_sso_user(
    pool_id: str, email: str, tenant_id: str
) -> str:
    """SSO 用に Cognito ユーザーを作成する. Permanent password を自動設定."""
    cognito = _cognito_client()
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
            # レースで同時作成された場合は get で sub を拾って続行
            sub = _cognito_get_user_sub(pool_id, email)
            if not sub:
                raise HTTPException(
                    status_code=502,
                    detail="Cognito user exists but sub not retrievable",
                )
            return sub
        raise HTTPException(
            status_code=502, detail=f"Cognito admin_create_user failed: {code}"
        )

    sub: Optional[str] = None
    for attr in resp.get("User", {}).get("Attributes", []):
        if attr.get("Name") == "sub":
            sub = attr.get("Value")
            break
    if not sub:
        raise HTTPException(status_code=502, detail="Cognito response missing sub")
    return sub


def _cognito_set_permanent_password(pool_id: str, email: str, password: str) -> None:
    cognito = _cognito_client()
    try:
        cognito.admin_set_user_password(
            UserPoolId=pool_id,
            Username=email,
            Password=password,
            Permanent=True,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        raise HTTPException(
            status_code=502,
            detail=f"Cognito admin_set_user_password failed: {code}",
        )


def _cognito_admin_password_auth(
    pool_id: str, client_id: str, email: str, password: str
) -> dict:
    cognito = _cognito_client()
    try:
        resp = cognito.admin_initiate_auth(
            UserPoolId=pool_id,
            ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        raise HTTPException(
            status_code=502, detail=f"Cognito admin_initiate_auth failed: {code}"
        )
    return resp.get("AuthenticationResult") or {}


# ------------------------------------------------------------------
# Endpoint
# ------------------------------------------------------------------
@router.post("/sso-exchange", response_model=SsoExchangeResponse)
def sso_exchange(body: SsoExchangeRequest) -> SsoExchangeResponse:
    # 1. STS 検証 + 身元抽出
    sts_identity = verify_and_call_sts(
        method=body.method,
        url=body.url,
        headers=dict(body.headers),
        body=body.body,
    )

    # 2. 4 Gate 判定
    try:
        trusted = validate_sso_identity(sts_identity)
    except HTTPException as e:
        log_audit_event(
            event="sso_login_denied",
            actor_id=f"sso:{sts_identity.account_id}",
            target_id=None,
            details={
                "reason": e.detail,
                "arn": sts_identity.arn,
                "account_id": sts_identity.account_id,
                "identity_type": sts_identity.identity_type,
            },
        )
        raise

    # 3. Cognito ユーザー解決 or 作成
    pool_id = _require_env("COGNITO_USER_POOL_ID")
    client_id = _require_env("COGNITO_CLIENT_ID")

    tenants_repo = TenantsRepository()
    tenant_id = trusted.target_tenant_id or os.getenv("DEFAULT_ORG_ID", "default-org")
    tenant_rec = tenants_repo.get(tenant_id)
    if not tenant_rec:
        raise HTTPException(
            status_code=422,
            detail=f"target tenant not found: {tenant_id}",
        )

    users_repo = UsersRepository()
    existing = users_repo.get_by_email(trusted.email)
    is_new_user = existing is None

    if is_new_user:
        # 既存 Cognito user が cognito auth method で居ないか確認 (U2 衝突チェック)
        existing_sub = _cognito_get_user_sub(pool_id, trusted.email)
        if existing_sub:
            # Cognito には居るが DynamoDB には居ない -> 他ルートで bootstrap された admin ユーザー等
            # 既存 Cognito user を SSO に convert はしない (安全側): 拒否
            log_audit_event(
                event="sso_login_denied",
                actor_id=f"sso:{sts_identity.account_id}",
                target_id=existing_sub,
                details={
                    "reason": "email already registered in Cognito under a different auth_method",
                    "email": trusted.email,
                },
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{trusted.email} already has a password-based Cognito account. "
                    "Ask an administrator to delete and re-invite."
                ),
            )

        sub = _cognito_create_sso_user(pool_id, trusted.email, tenant_id)
        users_repo.put_user(
            user_id=sub,
            email=trusted.email,
            auth_provider="cognito",
            auth_provider_user_id=sub,
            org_id=tenant_id,
            roles=[trusted.target_role],
            auth_method="sso",
            sso_account_id=sts_identity.account_id,
            sso_principal_arn=sts_identity.arn,
        )
        # UserTenants 初期化
        UserTenantsRepository().ensure(
            user_id=sub,
            tenant_id=tenant_id,
            role=trusted.target_role,
            total_credit=trusted.target_credit,
        )
        log_audit_event(
            event="sso_user_provisioned",
            actor_id=f"sso:{sts_identity.account_id}",
            target_id=sub,
            target_type="user",
            tenant_id=tenant_id,
            details={
                "email": trusted.email,
                "role": trusted.target_role,
                "identity_type": sts_identity.identity_type,
                "arn": sts_identity.arn,
            },
        )
        # 招待 consume
        invites_repo = SsoPreRegistrationsRepository()
        try:
            invite = invites_repo.get(trusted.email)
            if invite and not invite.get("consumed_at"):
                invites_repo.mark_consumed(trusted.email)
        except Exception as e:
            _log.warning("sso_invite_consume_failed: %s", e)
    else:
        sub = str(existing["user_id"])
        existing_method = str(existing.get("auth_method") or "cognito")
        if existing_method != "sso":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{trusted.email} exists with auth_method={existing_method}. "
                    "SSO login is not permitted for this user."
                ),
            )

    # 4. 毎回 random password 発行 → admin_initiate_auth → password 破棄
    temp_pw = _generate_temp_password()
    _cognito_set_permanent_password(pool_id, trusted.email, temp_pw)
    auth_result = _cognito_admin_password_auth(pool_id, client_id, trusted.email, temp_pw)
    # temp_pw はこの時点でスコープ外に出して再使用しない
    del temp_pw

    if not auth_result:
        raise HTTPException(status_code=502, detail="Cognito returned empty auth result")

    users_repo.record_sso_login(
        user_id=sub,
        sso_account_id=sts_identity.account_id,
        sso_principal_arn=sts_identity.arn,
    )

    log_audit_event(
        event="sso_login_success",
        actor_id=sub,
        actor_email=trusted.email,
        tenant_id=tenant_id,
        details={
            "account_id": sts_identity.account_id,
            "identity_type": sts_identity.identity_type,
            "arn": sts_identity.arn,
            "new_user": is_new_user,
        },
    )

    # ユーザー情報を再読込 (roles は provisioning 直後に決まるが、既存 user の場合は DB 既存を優先)
    fresh = users_repo.get_by_user_id(sub) or {}
    roles = _extract_roles(fresh)
    org_id = str(fresh.get("org_id") or tenant_id)

    return SsoExchangeResponse(
        access_token=auth_result.get("AccessToken") or "",
        id_token=auth_result.get("IdToken"),
        refresh_token=auth_result.get("RefreshToken"),
        expires_in=auth_result.get("ExpiresIn"),
        token_type=auth_result.get("TokenType"),
        email=trusted.email,
        user_id=sub,
        roles=roles,
        org_id=org_id,
        identity_type=sts_identity.identity_type,
        new_user=is_new_user,
    )


def _extract_roles(user: dict) -> list[str]:
    raw = user.get("roles") or []
    if isinstance(raw, str):
        return [raw]
    return [str(r) for r in raw]


