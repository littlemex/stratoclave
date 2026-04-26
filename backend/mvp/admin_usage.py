"""Admin Usage Logs API (Phase 2).

GET /api/mvp/admin/usage-logs

フィルタ:
  - tenant_id: Tenant で絞る (PK Query 可)
  - user_id: User で絞る (user-id-index GSI Query)
  - since / until: ISO 8601 (timestamp_log_id は "{iso}#{uuid}" 形式なので SK Range 可)
  - limit + cursor (UsageLogs の LastEvaluatedKey)

tenant_id も user_id も無い場合は最新 Scan (limit 上限 100 で truncate)。
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
from .deps import AuthenticatedUser


router = APIRouter(prefix="/api/mvp/admin", tags=["mvp-admin-usage"])


class UsageLogEntry(BaseModel):
    tenant_id: str
    user_id: str
    user_email: Optional[str] = None
    model_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    recorded_at: str
    timestamp_log_id: str


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
    return UsageLogEntry(
        tenant_id=str(item.get("tenant_id") or ""),
        user_id=str(item.get("user_id") or ""),
        user_email=item.get("user_email"),
        model_id=str(item.get("model_id") or ""),
        input_tokens=int(item.get("input_tokens", 0)),
        output_tokens=int(item.get("output_tokens", 0)),
        total_tokens=int(item.get("total_tokens", 0)),
        recorded_at=str(item.get("recorded_at") or ""),
        timestamp_log_id=str(item.get("timestamp_log_id") or ""),
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

    # filter の優先順: tenant_id > user_id > scan
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
        # 全体 Scan (admin 専用、limit 100 で truncate)
        kwargs = {"Limit": limit}
        if decoded_cursor:
            kwargs["ExclusiveStartKey"] = decoded_cursor
        resp = repo._table.scan(**kwargs)

    items = [_to_entry(it) for it in resp.get("Items", [])]
    return UsageLogsResponse(
        logs=items,
        next_cursor=_encode_cursor(resp.get("LastEvaluatedKey")),
    )
