"""Permissions テーブル (Phase 2, RBAC 真実源).

テーブル設計 (iac/lib/dynamodb-stack.ts):
  PK: role (str)
  属性:
    role: str         e.g. "admin", "team_lead", "user"
    permissions: list[str]  e.g. ["users:*", "messages:send"]
    description: str
    updated_at: str (ISO 8601)
    version: str

seed: backend/permissions.json を scripts/init-permissions.sh で DynamoDB に同期する.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from .client import get_dynamodb_resource


def _permissions_table_name() -> str:
    return os.getenv("DYNAMODB_PERMISSIONS_TABLE", "stratoclave-permissions")


class PermissionsRepository:
    """Permissions テーブルへの read-mostly アクセス."""

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
