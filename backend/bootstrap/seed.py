"""Backend lifespan 起動時の idempotent seed.

OSS 利用者が clone → deploy → admin login を zero-touch で動かせるよう、
Backend 起動時に以下を DynamoDB へ idempotent に投入する:

1. Permissions (admin / team_lead / user の 3 role)
   - backend/permissions.json が真実源
   - 既存 version と一致すれば no-op、不一致なら上書き
2. Default Tenant (default-org)
   - tenants テーブルに attribute_not_exists で put
   - 既存があれば touch しない

不変条件:
- 2 回実行しても同じ状態 (idempotent)
- permissions.json が壊れていても Backend 起動は継続 (warn しつつ)
- 既存 permissions と version が同じなら DynamoDB への書き込みは発生しない

環境変数:
- DEFAULT_ORG_ID: default tenant の tenant_id (default "default-org")
- DEFAULT_TENANT_CREDIT: default_credit (default 100000、int)
- PERMISSIONS_SEED_FILE: permissions.json の path (default backend/permissions.json)
- STRATOCLAVE_DISABLE_SEED: "true" なら seed をスキップ (テスト用)
- STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL: 初回 admin の email (optional).
    設定されており、かつ Users テーブルに admin role のユーザが 1 人も存在しない場合、
    Cognito に admin ユーザを作成して Users テーブルに登録する (P0-1).
    bootstrap-admin.sh 経由の 401/422 を回避し、OSS fork 即死を防ぐ。
    一時パスワードは CloudWatch Logs に structured log として 1 回だけ INFO 出力する
    (平文だが Backend 内のログに閉じる; admin はログ確認後に自分でローテートする前提)。
- STRATOCLAVE_BOOTSTRAP_ADMIN_ORG_ID: 初回 admin の所属 tenant (default DEFAULT_ORG_ID)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.logging import get_logger
from dynamo import (
    PermissionsRepository,
    TenantsRepository,
    UsersRepository,
    UserTenantsRepository,
)


logger = get_logger(__name__)


# permissions.json の default path (backend/ ディレクトリ直下)
# このファイルは backend/bootstrap/seed.py なので、親の親が backend/
_DEFAULT_PERMISSIONS_FILE = Path(__file__).resolve().parent.parent / "permissions.json"


def _permissions_file_path() -> Path:
    override = os.getenv("PERMISSIONS_SEED_FILE")
    if override:
        return Path(override)
    return _DEFAULT_PERMISSIONS_FILE


def seed_permissions() -> dict[str, int]:
    """Permissions テーブルを permissions.json から idempotent に seed する.

    戻り値: PermissionsRepository.seed_from_file の結果
      {"total": N, "changed": M, "skipped": S}

    例外は呼び出し元で握り潰す (seed_all 経由) 方針。
    """
    path = _permissions_file_path()
    if not path.exists():
        logger.warning(
            "permissions_seed_file_missing",
            path=str(path),
            hint="Skipping permissions seed; admin/team_lead/user roles may not work",
        )
        return {"total": 0, "changed": 0, "skipped": 0}

    result = PermissionsRepository().seed_from_file(path)
    logger.info(
        "permissions_seeded",
        path=str(path),
        total=result["total"],
        changed=result["changed"],
        skipped=result["skipped"],
    )
    return result


def seed_default_tenant() -> dict[str, Any]:
    """Default Tenant (default-org) を idempotent put する.

    既存があれば touch しない。
    戻り値: {"tenant_id": str, "created": bool, "item": dict}
    """
    tenant_id = os.getenv("DEFAULT_ORG_ID", "default-org")
    default_credit_env = os.getenv("DEFAULT_TENANT_CREDIT")
    default_credit = int(default_credit_env) if default_credit_env else None

    result = TenantsRepository().seed_default(
        tenant_id=tenant_id,
        name="Default Organization",
        default_credit=default_credit,
        created_by="system-seed",
    )
    logger.info(
        "default_tenant_seeded",
        tenant_id=result["tenant_id"],
        created=result["created"],
    )
    return result


def _generate_temp_password(length: int = 20) -> str:
    """Cognito password policy (大小英数記号各 1+) を満たす乱数パスワード."""
    import secrets
    import string

    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits = string.digits
    symbols = "!@#$%^&*()-_=+[]{}"
    required = [
        secrets.choice(lower),
        secrets.choice(upper),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    pool = lower + upper + digits + symbols
    remain = [secrets.choice(pool) for _ in range(length - len(required))]
    chars = required + remain
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def seed_bootstrap_admin() -> dict[str, Any]:
    """Zero-state (Users テーブルに admin が 1 人もいない) 時に Cognito + DynamoDB を
    seed して OSS fork 即死を回避する.

    動作:
      - `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL` 未設定なら何もしない
      - 既に admin role を持つユーザが 1 人以上いれば何もしない
      - どちらも満たす場合:
          1. Cognito AdminCreateUser (lower(email), custom:org_id=tenant_id)
          2. ランダム永続パスワードを設定 (admin は事後に変更)
          3. Users テーブルに `roles=[admin, user]` で put
          4. UserTenants テーブルに role=admin で ensure
          5. 一時パスワードを CloudWatch Logs に INFO で 1 回だけ出力

    Cognito で AdminCreateUser が失敗 (UsernameExists 等) した場合は既存ユーザを
    そのまま採用して Users/UserTenants 側だけ整合する。
    """
    import boto3
    from botocore.exceptions import ClientError

    email_env = os.getenv("STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    if not email_env:
        return {"skipped": True, "reason": "STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL not set"}

    pool_id = os.getenv("COGNITO_USER_POOL_ID")
    if not pool_id:
        logger.warning(
            "bootstrap_admin_skipped",
            reason="COGNITO_USER_POOL_ID not set; cannot provision Cognito user",
        )
        return {"skipped": True, "reason": "COGNITO_USER_POOL_ID missing"}

    tenant_id = os.getenv("STRATOCLAVE_BOOTSTRAP_ADMIN_ORG_ID") or os.getenv(
        "DEFAULT_ORG_ID", "default-org"
    )

    users_repo = UsersRepository()
    # 既に admin が 1 人以上いるなら no-op (idempotent)
    existing_admins = users_repo.scan_admins(limit=1)
    if existing_admins:
        logger.info(
            "bootstrap_admin_skipped",
            reason="at_least_one_admin_exists",
            admin_count_sample=len(existing_admins),
        )
        return {"skipped": True, "reason": "admin exists"}

    region = os.getenv("COGNITO_REGION") or os.getenv("AWS_REGION", "us-east-1")
    cognito = boto3.client("cognito-idp", region_name=region)

    sub: str | None = None
    created_new = True
    try:
        resp = cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=email_env,
            UserAttributes=[
                {"Name": "email", "Value": email_env},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:org_id", "Value": tenant_id},
            ],
            DesiredDeliveryMediums=[],
            MessageAction="SUPPRESS",
        )
        for attr in resp.get("User", {}).get("Attributes", []):
            if attr.get("Name") == "sub":
                sub = attr.get("Value")
                break
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "UsernameExistsException":
            created_new = False
            try:
                ex = cognito.admin_get_user(UserPoolId=pool_id, Username=email_env)
                for attr in ex.get("UserAttributes", []):
                    if attr.get("Name") == "sub":
                        sub = attr.get("Value")
                        break
            except ClientError as ee:
                logger.error(
                    "bootstrap_admin_cognito_lookup_failed",
                    error=str(ee),
                    email=email_env,
                )
                return {"skipped": True, "reason": "cognito lookup failed"}
        else:
            logger.error(
                "bootstrap_admin_cognito_create_failed",
                error=str(e),
                email=email_env,
            )
            return {"skipped": True, "reason": f"cognito: {code}"}

    if not sub:
        logger.error("bootstrap_admin_no_sub", email=email_env)
        return {"skipped": True, "reason": "no sub resolved"}

    temp_password = _generate_temp_password()
    try:
        cognito.admin_set_user_password(
            UserPoolId=pool_id,
            Username=email_env,
            Password=temp_password,
            Permanent=True,
        )
    except ClientError as e:
        logger.error(
            "bootstrap_admin_set_password_failed",
            error=str(e),
            email=email_env,
        )
        # password 設定に失敗しても Users 側は書いておく (後で reset 可能)
        temp_password = ""

    # Users テーブル
    users_repo.put_user(
        user_id=sub,
        email=email_env,
        auth_provider="cognito",
        auth_provider_user_id=sub,
        org_id=tenant_id,
        roles=["admin", "user"],
    )
    # UserTenants テーブル (admin role で active)
    UserTenantsRepository().ensure(
        user_id=sub,
        tenant_id=tenant_id,
        role="admin",
    )

    # CloudWatch Logs に一時パスワードを 1 回だけ INFO 出力.
    # ここは平文だが Backend 内部ログに閉じており、HTTP response に出ないため P0-3 違反ではない。
    # Admin は初回 login 後に自分でパスワードを変更する前提。
    if temp_password:
        logger.info(
            "bootstrap_admin_created",
            email=email_env,
            user_id=sub,
            tenant_id=tenant_id,
            created_new=created_new,
            temporary_password=temp_password,
            instruction=(
                "This password is logged once. Login once and change it via Cognito. "
                "Rotate by: aws cognito-idp admin-set-user-password --user-pool-id "
                f"{pool_id} --username {email_env} --password <NEW> --permanent"
            ),
        )
    else:
        logger.warning(
            "bootstrap_admin_created_without_password",
            email=email_env,
            user_id=sub,
            tenant_id=tenant_id,
            instruction="Set password via aws cognito-idp admin-set-user-password",
        )

    return {
        "created": True,
        "email": email_env,
        "user_id": sub,
        "tenant_id": tenant_id,
        "created_new": created_new,
    }


def seed_all() -> dict[str, Any]:
    """Backend lifespan から呼ばれる top-level エントリ.

    各 seed 関数を呼び、一部が失敗しても他は続行する (best-effort).
    戻り値は summary dict。呼び出し元 (main.py lifespan) は戻り値を無視しても
    良い (全て logger に出力される)。

    環境変数 STRATOCLAVE_DISABLE_SEED=true の場合はスキップ。
    """
    if os.getenv("STRATOCLAVE_DISABLE_SEED", "false").lower() == "true":
        logger.info("seed_skipped", reason="STRATOCLAVE_DISABLE_SEED=true")
        return {"skipped": True}

    summary: dict[str, Any] = {}

    # 1. Permissions
    try:
        summary["permissions"] = seed_permissions()
    except Exception as exc:
        logger.error(
            "permissions_seed_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        summary["permissions"] = {"error": str(exc)}

    # 2. Default Tenant
    try:
        summary["default_tenant"] = seed_default_tenant()
    except Exception as exc:
        logger.error(
            "default_tenant_seed_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        summary["default_tenant"] = {"error": str(exc)}

    # 3. Bootstrap Admin (STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL が設定済み & admin 0 件の時のみ)
    try:
        summary["bootstrap_admin"] = seed_bootstrap_admin()
    except Exception as exc:
        logger.error(
            "bootstrap_admin_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        summary["bootstrap_admin"] = {"error": str(exc)}

    return summary
