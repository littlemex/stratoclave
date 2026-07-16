"""Admin Usage Logs API (Phase 2).

GET /api/mvp/admin/usage-logs

Filters:
  - tenant_id: narrow by tenant (PK Query)
  - user_id: narrow by user (user-id-index GSI Query)
  - since / until: ISO 8601 (timestamp_log_id has "{iso}#{uuid}" format, enabling SK range queries)
  - limit + cursor (UsageLogs LastEvaluatedKey)

When neither tenant_id nor user_id is provided, falls back to a full Scan (truncated at limit 100).
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Optional

from boto3.dynamodb.conditions import Key as boto3_key
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from dynamo import UsageLogsRepository

from .authz import require_permission
from .me import _derive_fallback  # single source of truth for P0-11 fallback derivation
from .deps import AuthenticatedUser


router = APIRouter(prefix="/api/mvp/admin", tags=["mvp-admin-usage"])


class UsageLogEntry(BaseModel):
    tenant_id: str
    user_id: str
    # A-19-pii: surface the prefixed hash, not the plaintext email.
    # Operators correlate via user_id; UI lookups resolve display names
    # against the Users table on demand.
    user_email_hash: Optional[str] = None
    model_id: str  # the EFFECTIVE model the request was served by
    input_tokens: int
    output_tokens: int
    total_tokens: int
    recorded_at: str
    timestamp_log_id: str
    # P0-11 fallback visibility (see mvp.me._derive_fallback). None on legacy
    # rows = unknown, never True.
    requested_model_id: Optional[str] = None
    fallback_occurred: Optional[bool] = None


class UsageLogsResponse(BaseModel):
    logs: list[UsageLogEntry]
    next_cursor: Optional[str] = None


def _encode_cursor(last_key: Optional[dict]) -> Optional[str]:
    if not last_key:
        return None
    return base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode()


def _decode_cursor(cursor: Optional[str]) -> Optional[dict]:
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cursor")


def _to_entry(item: dict[str, Any]) -> UsageLogEntry:
    # Backwards-compat read path: legacy rows still carry ``user_email``
    # in plaintext. Surface only the hash to the API. If the row pre-
    # dates A-19-pii, hash the legacy field at read time so the API
    # contract is stable from day one.
    legacy_email = item.get("user_email")
    email_hash = item.get("user_email_hash")
    if email_hash is None and legacy_email:
        from dynamo.usage_logs import hash_user_email
        email_hash = hash_user_email(str(legacy_email))
    return UsageLogEntry(
        tenant_id=str(item.get("tenant_id") or ""),
        user_id=str(item.get("user_id") or ""),
        user_email_hash=email_hash,
        model_id=str(item.get("model_id") or ""),
        input_tokens=int(item.get("input_tokens", 0)),
        output_tokens=int(item.get("output_tokens", 0)),
        total_tokens=int(item.get("total_tokens", 0)),
        recorded_at=str(item.get("recorded_at") or ""),
        timestamp_log_id=str(item.get("timestamp_log_id") or ""),
        requested_model_id=(
            str(item["requested_model_id"]) if item.get("requested_model_id") else None
        ),
        fallback_occurred=_derive_fallback(
            item.get("requested_model_id"), str(item.get("model_id") or "")
        ),
    )


@router.get("/usage-logs", response_model=UsageLogsResponse)
def list_usage_logs(
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    since: Optional[str] = Query(None, description="ISO 8601"),
    until: Optional[str] = Query(None, description="ISO 8601"),
    cursor: Optional[str] = None,
    limit: int = Query(100, ge=1, le=100),
    _admin: AuthenticatedUser = Depends(require_permission("usage:read-all")),
) -> UsageLogsResponse:
    repo = UsageLogsRepository()
    decoded_cursor = _decode_cursor(cursor)

    # Filter priority: tenant_id > user_id > full scan.
    if tenant_id:
        key = boto3_key("tenant_id").eq(tenant_id)
        if since:
            key = key & boto3_key("timestamp_log_id").gte(since)
        if until:
            key = key & boto3_key("timestamp_log_id").lte(until + "￿")
        kwargs = {"KeyConditionExpression": key, "Limit": limit}
        if decoded_cursor:
            kwargs["ExclusiveStartKey"] = decoded_cursor
        resp = repo._table.query(**kwargs)
    elif user_id:
        key = boto3_key("user_id").eq(user_id)
        if since:
            key = key & boto3_key("timestamp_log_id").gte(since)
        if until:
            key = key & boto3_key("timestamp_log_id").lte(until + "￿")
        kwargs = {
            "IndexName": "user-id-index",
            "KeyConditionExpression": key,
            "Limit": limit,
        }
        if decoded_cursor:
            kwargs["ExclusiveStartKey"] = decoded_cursor
        resp = repo._table.query(**kwargs)
    else:
        # Full Scan (admin only, truncated at limit 100).
        kwargs = {"Limit": limit}
        if decoded_cursor:
            kwargs["ExclusiveStartKey"] = decoded_cursor
        resp = repo._table.scan(**kwargs)

    items = [_to_entry(it) for it in resp.get("Items", [])]
    return UsageLogsResponse(
        logs=items,
        next_cursor=_encode_cursor(resp.get("LastEvaluatedKey")),
    )
