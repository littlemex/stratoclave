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
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.logging import get_logger
from dynamo import PermissionsRepository, TenantsRepository


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
        # permissions.json が壊れている / DynamoDB 権限不足 / テーブル未存在 等
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

    return summary
