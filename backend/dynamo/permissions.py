"""Permissions table (Phase 2, RBAC source of truth).

Table design (iac/lib/dynamodb-stack.ts):
  PK: role (str)
  Attributes:
    role: str               e.g. "admin", "team_lead", "user"
    permissions: list[str]  e.g. ["users:*", "messages:send"]
    description: str
    updated_at: str (ISO 8601)
    version: str

Seeded idempotently from backend/permissions.json at backend lifespan startup
via bootstrap.seed.seed_all.
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
    """Read-mostly access to the Permissions table, plus idempotent seed support."""

    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(
            table_name or _permissions_table_name()
        )

    def get(self, role: str) -> list[str]:
        """Return the permission list for the given role. Returns an empty list if the role is not registered."""
        resp = self._table.get_item(Key={"role": role})
        item = resp.get("Item") or {}
        perms = item.get("permissions") or []
        # The List<String> returned by DynamoDB may be a list or a set; normalise to list.
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
        """Load permissions.json and put records idempotently.

        Idempotency strategy:
          1. GET the existing record first.
          2. If the existing version matches the new version, no-op (skip write).
          3. If missing or version differs, PUT to overwrite.

        Returns: {"total": N, "changed": M, "skipped": S}
          - total: number of roles in permissions.json
          - changed: number of rows actually written
          - skipped: number of rows skipped due to matching version
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
                # Version matches → no-op.
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
