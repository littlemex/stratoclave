"""Tenants table (Phase 2).

Table design (iac/lib/dynamodb-stack.ts):
  PK: tenant_id
  GSI team-lead-index: PK team_lead_user_id, SK created_at, ProjectionType ALL
  Attributes:
    tenant_id: str
    name: str
    team_lead_user_id: str  (Cognito sub; "admin-owned" when owned by an admin)
    default_credit: int
    status: "active" | "archived"
    created_at: str (ISO 8601)
    updated_at: str (ISO 8601)
    created_by: str
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .client import get_dynamodb_resource


ADMIN_OWNED = "admin-owned"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_credit_fallback() -> int:
    """Last-resort fallback value for Tenant.default_credit."""
    return int(os.getenv("DEFAULT_TENANT_CREDIT", "100000"))


def _tenants_table_name() -> str:
    return os.getenv("DYNAMODB_TENANTS_TABLE", "stratoclave-tenants")


class TenantNotFoundError(Exception):
    """Raised when the requested tenant does not exist."""


class TenantLimitExceededError(Exception):
    """Raised when a team lead exceeds the tenant creation limit."""


class TenantsRepository:
    """CRUD operations for the Tenants table.

    The team lead limit of 50 tenants (v2.1 §4.4) is enforced in `create`.
    """

    TEAM_LEAD_TENANT_LIMIT = 50

    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(
            table_name or _tenants_table_name()
        )

    # ----- read -----
    def get(self, tenant_id: str) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(Key={"tenant_id": tenant_id})
        item = resp.get("Item")
        if item and item.get("status") == "archived":
            return None
        return item

    def get_including_archived(self, tenant_id: str) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(Key={"tenant_id": tenant_id})
        return resp.get("Item")

    def list_by_owner(self, owner_user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        resp = self._table.query(
            IndexName="team-lead-index",
            KeyConditionExpression=Key("team_lead_user_id").eq(owner_user_id),
            Limit=min(limit, 100),
        )
        return [item for item in resp.get("Items", []) if item.get("status") != "archived"]

    def count_by_owner(self, owner_user_id: str) -> int:
        """Count *active* tenants owned by `owner_user_id`.

        A-04-tenant: archived tenants must NOT count toward the team-lead
        cap. Otherwise a team lead who archives a tenant cannot create a
        new one even though their visible footprint is below the limit,
        and the cap silently inflates over time as archives accumulate.

        Implementation note: DynamoDB COUNT-only queries cannot apply
        FilterExpression server-side without scanning attributes, so we
        fetch the items via the same projection the cap path needs and
        count active ones in Python. The team-lead-index entries per
        owner are bounded (the cap itself is the bound), so this stays
        O(limit) RCU.
        """
        resp = self._table.query(
            IndexName="team-lead-index",
            KeyConditionExpression=Key("team_lead_user_id").eq(owner_user_id),
            FilterExpression=Attr("status").ne("archived"),
            ProjectionExpression="tenant_id, #s",
            ExpressionAttributeNames={"#s": "status"},
        )
        return int(resp.get("Count", 0))

    def list_all(self, *, cursor: Optional[dict[str, Any]] = None, limit: int = 100) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
        """Fetch all tenants via Scan (admin only; limit<=100 enforced by the caller)."""
        kwargs: dict[str, Any] = {"Limit": min(limit, 100)}
        if cursor:
            kwargs["ExclusiveStartKey"] = cursor
        resp = self._table.scan(**kwargs)
        return resp.get("Items", []), resp.get("LastEvaluatedKey")

    # ----- write -----
    def create(
        self,
        *,
        name: str,
        team_lead_user_id: str,
        default_credit: Optional[int] = None,
        created_by: str,
        tenant_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a new tenant. Raises ConditionalCheckFailed if the tenant_id already exists."""
        # Check the team lead cap (admin-owned tenants are exempt).
        if team_lead_user_id != ADMIN_OWNED:
            existing = self.count_by_owner(team_lead_user_id)
            if existing >= self.TEAM_LEAD_TENANT_LIMIT:
                raise TenantLimitExceededError(
                    f"Team lead {team_lead_user_id} already owns {existing} tenants "
                    f"(limit={self.TEAM_LEAD_TENANT_LIMIT})"
                )

        tid = tenant_id or f"tenant-{uuid4()}"
        now = _now_iso()
        item: dict[str, Any] = {
            "tenant_id": tid,
            "name": name,
            "team_lead_user_id": team_lead_user_id,
            "default_credit": Decimal(default_credit or _default_credit_fallback()),
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "created_by": created_by,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(tenant_id)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"Tenant already exists: tenant_id={tid}")
            raise
        return item

    def update(
        self,
        *,
        tenant_id: str,
        name: Optional[str] = None,
        default_credit: Optional[int] = None,
    ) -> dict[str, Any]:
        """Update name and/or default_credit only. team_lead_user_id is updated via set_owner."""
        updates: list[str] = []
        values: dict[str, Any] = {":now": _now_iso(), ":active": "active"}
        expr_names: dict[str, str] = {"#s": "status"}
        if name is not None:
            updates.append("#n = :n")
            expr_names["#n"] = "name"
            values[":n"] = name
        if default_credit is not None:
            updates.append("default_credit = :dc")
            values[":dc"] = Decimal(default_credit)
        if not updates:
            existing = self.get(tenant_id)
            if not existing:
                raise TenantNotFoundError(tenant_id)
            return existing
        updates.append("updated_at = :now")

        try:
            resp = self._table.update_item(
                Key={"tenant_id": tenant_id},
                UpdateExpression="SET " + ", ".join(updates),
                ExpressionAttributeValues=values,
                ExpressionAttributeNames=expr_names,
                ConditionExpression="attribute_exists(tenant_id) AND #s = :active",
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise TenantNotFoundError(tenant_id)
            raise
        return resp.get("Attributes", {})

    def set_owner(self, *, tenant_id: str, new_owner_user_id: str) -> dict[str, Any]:
        """Reassign a tenant orphaned by Cognito user deletion/recreation (v2.1 C-C)."""
        try:
            resp = self._table.update_item(
                Key={"tenant_id": tenant_id},
                UpdateExpression="SET team_lead_user_id = :o, updated_at = :now",
                ExpressionAttributeValues={
                    ":o": new_owner_user_id,
                    ":now": _now_iso(),
                },
                ConditionExpression="attribute_exists(tenant_id)",
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise TenantNotFoundError(tenant_id)
            raise
        return resp.get("Attributes", {})

    def archive(self, tenant_id: str) -> None:
        """Archive a tenant (status=archived) and all its UserTenants rows.

        P2-2 regression: archiving a Tenant used to leave UserTenants
        rows with ``status=active``, which meant ``reserve()`` and
        ``refund()`` against the archived tenant would still succeed and
        rack up Bedrock usage on a tenant that was "deleted". The
        user-facing `/v1/messages` call would happily drain the old
        budget until an admin noticed.

        Archival is now a two-phase operation:
          1. Scan the user_tenants table for rows targeting this tenant
             and flip each active one to status=archived.
          2. Flip the tenants row itself to status=archived.

        We intentionally do steps 1 → 2 (and not the other way around)
        so that if the scan fails we leave the tenant in a re-runnable
        state. The reverse order would leave the tenant dead but its
        members writable.
        """
        from boto3.dynamodb.conditions import Attr

        from .client import user_tenants_table_name, get_dynamodb_resource

        ut_table = get_dynamodb_resource().Table(user_tenants_table_name())
        now = _now_iso()

        # Scan is acceptable here — archival is a rare, admin-initiated
        # operation. For a high-tenant-count deployment this can be
        # upgraded to a GSI query later.
        last_evaluated: Optional[dict[str, Any]] = None
        while True:
            scan_kwargs: dict[str, Any] = {
                "FilterExpression": Attr("tenant_id").eq(tenant_id)
                & (Attr("status").eq("active") | Attr("status").not_exists()),
                "ProjectionExpression": "user_id, tenant_id",
            }
            if last_evaluated:
                scan_kwargs["ExclusiveStartKey"] = last_evaluated
            resp = ut_table.scan(**scan_kwargs)
            for row in resp.get("Items", []):
                ut_table.update_item(
                    Key={
                        "user_id": row["user_id"],
                        "tenant_id": row["tenant_id"],
                    },
                    UpdateExpression="SET #s = :archived, updated_at = :now",
                    ConditionExpression=(
                        "attribute_not_exists(#s) OR #s = :active"
                    ),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":archived": "archived",
                        ":active": "active",
                        ":now": now,
                    },
                )
            last_evaluated = resp.get("LastEvaluatedKey")
            if not last_evaluated:
                break

        # Finally flip the tenant itself.
        self._table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET #s = :archived, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":archived": "archived",
                ":now": now,
            },
        )

    # ------------------------------------------------------------------
    # Seed (idempotent)
    # ------------------------------------------------------------------
    def seed_default(
        self,
        *,
        tenant_id: str = "default-org",
        name: str = "Default Organization",
        default_credit: Optional[int] = None,
        created_by: str = "system-seed",
    ) -> dict[str, Any]:
        """Idempotently put the default tenant for OSS zero-touch startup.

        Idempotency: ConditionExpression='attribute_not_exists(tenant_id)' ensures
        no write occurs if the record already exists, and it is never touched.

        Returns: {"tenant_id": str, "created": bool, "item": dict}
          - created=True: newly created by this call
          - created=False: already existed (no-op)
        """
        # If a record exists (even archived), return it without touching it.
        existing = self.get_including_archived(tenant_id)
        if existing:
            return {"tenant_id": tenant_id, "created": False, "item": existing}

        now = _now_iso()
        item: dict[str, Any] = {
            "tenant_id": tenant_id,
            "name": name,
            # At the time of first seed, no users (including admins) exist yet,
            # so we use the ADMIN_OWNED sentinel (exempt from the cap, treated as admin-owned).
            "team_lead_user_id": ADMIN_OWNED,
            "default_credit": Decimal(default_credit or _default_credit_fallback()),
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "created_by": created_by,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(tenant_id)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Race condition: another process seeded first — no-op.
                existing = self.get_including_archived(tenant_id) or item
                return {"tenant_id": tenant_id, "created": False, "item": existing}
            raise
        return {"tenant_id": tenant_id, "created": True, "item": item}
