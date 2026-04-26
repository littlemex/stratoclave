"""Permissions テーブル (Phase 2, RBAC 真実源).

テーブル設計 (iac/lib/dynamodb-stack.ts):
  PK: role (str)
  属性:
    role: str         e.g. "admin", "team_lead", "user"
    permissions: list[str]  e.g. ["users:*", "messages:send"]
    description: str
    updated_at: str (ISO 8601)
    version: str

seed: backend/permissions.json を backend lifespan 起動時に idempotent put で
      DynamoDB に同期する (bootstrap.seed.seed_all 経由)。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .client import get_dynamodb_resource


def _permissions_table_name() -> str:
    return os.getenv("DYNAMODB_PERMISSIONS_TABLE", "stratoclave-permissions")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PermissionsRepository:
    """Permissions テーブルへの read-mostly アクセス + idempotent seed."""

    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(
            table_name or _permissions_table_name()
        )

    def get(self, role: str) -> list[str]:
        """指定 role の permissions 一覧を返す。未登録なら空 list."""
        resp = self._table.get_item(Key={"role": role})
        item = resp.get("Item") or {}
        perms = item.get("permissions") or []
        # DynamoDB から戻る List<String> は list or set の可能性があるため list 化
        return [str(p) for p in perms]

    def get_record(self, role: str) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(Key={"role": role})
        return resp.get("Item")

    def list_all(self) -> list[dict[str, Any]]:
        resp = self._table.scan()
        return resp.get("Items", [])

    # ------------------------------------------------------------------
    # Seed (idempotent)
    # ------------------------------------------------------------------
    def seed_from_file(self, path: str | Path) -> dict[str, int]:
        """permissions.json を読み込んで idempotent に put する.

        冪等性の確保:
          1. まず GET で既存レコードを取得
          2. 既存 version == 新 version なら no-op (書き込みスキップ)
          3. 既存無し or version 不一致なら PUT で上書き

        戻り値: {"total": N, "changed": M, "skipped": S}
          - total: permissions.json 内の role 数
          - changed: 実際に PUT した数
          - skipped: version 一致で skip した数
        """
        file_path = Path(path)
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        version: str = str(data.get("version", "unknown"))
        roles: dict[str, Any] = data.get("roles") or {}

        total = len(roles)
        changed = 0
        skipped = 0
        now = _now_iso()

        for role_name, role_def in roles.items():
            permissions = role_def.get("permissions") or []
            description = role_def.get("description", "")

            existing = self.get_record(role_name) or {}
            existing_version = existing.get("version")

            if existing_version == version:
                # version 一致 → no-op
                skipped += 1
                continue

            item: dict[str, Any] = {
                "role": role_name,
                "permissions": [str(p) for p in permissions],
                "description": str(description),
                "version": version,
                "updated_at": now,
            }
            self._table.put_item(Item=item)
            changed += 1

        return {"total": total, "changed": changed, "skipped": skipped}
