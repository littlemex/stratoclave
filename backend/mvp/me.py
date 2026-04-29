"""ユーザー自身の情報 + クレジット残高 + 所属 Tenant 名 + 使用履歴 (Phase 2)."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from boto3.dynamodb.conditions import Key as boto3_key
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from dynamo import TenantsRepository, UsageLogsRepository, UsersRepository, UserTenantsRepository

from .authz import log_audit_event, require_permission
from .deps import AuthenticatedUser, get_current_user


router = APIRouter(prefix="/api/mvp", tags=["mvp-me"])


# i18n: supported UI locales. Kept small and explicit — a wildcard
# would let clients set arbitrary attacker-controlled strings in their
# DynamoDB row, which then flows back into /me and the SPA ships it
# into every DOM translator lookup. String whitelist here + Literal on
# the Pydantic layer is defence in depth.
SUPPORTED_LOCALES: tuple[str, ...] = ("en", "ja")
DEFAULT_LOCALE = "ja"
Locale = Literal["en", "ja"]


class TenantSummary(BaseModel):
    tenant_id: str
    name: Optional[str] = None


class MeResponse(BaseModel):
    user_id: str
    email: str
    org_id: str
    roles: list[str]
    total_credit: int
    credit_used: int
    remaining_credit: int
    currency: str = "tokens"
    tenant: Optional[TenantSummary] = None
    # i18n: SPA uses this as the authoritative source on bootstrap,
    # overriding any cached value in sessionStorage / navigator.language.
    locale: Locale = DEFAULT_LOCALE


class UpdateMeRequest(BaseModel):
    """Self-service update of mutable profile fields. Today the only
    mutable field is `locale`; more can be added later without
    breaking the API contract.
    """

    model_config = ConfigDict(extra="forbid")
    locale: Locale = Field(..., description="UI locale")


class UpdateMeResponse(BaseModel):
    locale: Locale


def _resolve_locale(raw: Optional[str]) -> Locale:
    """Clamp a stored locale to the supported set, defaulting to JA.

    Legacy rows (pre-i18n) won't have a `locale` field at all; new rows
    always land with a default. Any value we cannot understand (wrong
    case, stray "fr", operator typo) falls back to the default so the
    SPA never receives an unsupported code.
    """
    if isinstance(raw, str) and raw in SUPPORTED_LOCALES:
        return raw  # type: ignore[return-value]
    return DEFAULT_LOCALE


@router.get("/me", response_model=MeResponse)
def me(user: AuthenticatedUser = Depends(get_current_user)) -> MeResponse:
    users_repo = UsersRepository()
    # deps.py で backfill 済みだが念のため
    row = users_repo.get_by_user_id(user.user_id)
    if row is None:
        row = users_repo.put_user(
            user_id=user.user_id,
            email=user.email,
            auth_provider="cognito",
            auth_provider_user_id=user.user_id,
            org_id=user.org_id,
            roles=user.roles,
        )

    user_tenants_repo = UserTenantsRepository()
    user_tenants_repo.ensure(user_id=user.user_id, tenant_id=user.org_id)
    summary = user_tenants_repo.credit_summary(user.user_id, user.org_id)

    tenant = None
    tenant_rec = TenantsRepository().get(user.org_id)
    if tenant_rec:
        tenant = TenantSummary(
            tenant_id=tenant_rec["tenant_id"],
            name=tenant_rec.get("name"),
        )
    else:
        tenant = TenantSummary(tenant_id=user.org_id)

    return MeResponse(
        user_id=user.user_id,
        email=user.email,
        org_id=user.org_id,
        roles=user.roles,
        total_credit=summary["total_credit"],
        credit_used=summary["credit_used"],
        remaining_credit=summary["remaining_credit"],
        tenant=tenant,
        locale=_resolve_locale(row.get("locale") if row else None),
    )


@router.patch("/me", response_model=UpdateMeResponse)
def update_me(
    body: UpdateMeRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UpdateMeResponse:
    """Self-service profile update. Currently scope-limited to
    changing the UI locale. The target user is implicit — there is no
    path parameter — so this endpoint cannot be turned into a BOLA
    primitive by rewriting a URL.
    """
    repo = UsersRepository()
    attrs = repo.update_locale(user.user_id, body.locale)
    if attrs is None:
        # User row vanished between auth and PATCH. Recreate (rare;
        # e.g. admin deleted the user in parallel).
        repo.put_user(
            user_id=user.user_id,
            email=user.email,
            auth_provider="cognito",
            auth_provider_user_id=user.user_id,
            org_id=user.org_id,
            roles=user.roles,
            locale=body.locale,
        )

    log_audit_event(
        event="user_locale_updated",
        actor_id=user.user_id,
        actor_email=user.email,
        target_id=user.user_id,
        target_type="user",
        details={"locale": body.locale},
    )
    return UpdateMeResponse(locale=body.locale)


# ------------------------------------------------------------------
# Phase D: 自分の使用履歴・集計
# ------------------------------------------------------------------
class UsageSummaryResponse(BaseModel):
    tenant_id: str
    total_credit: int
    credit_used: int
    remaining_credit: int
    by_model: dict[str, int] = {}
    by_tenant: dict[str, int] = {}
    sample_size: int
    since_days: int


class UsageHistoryEntry(BaseModel):
    tenant_id: str
    tenant_name: Optional[str] = None
    model_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    recorded_at: str


class UsageHistoryResponse(BaseModel):
    history: list[UsageHistoryEntry]
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


@router.get("/me/usage-summary", response_model=UsageSummaryResponse)
def usage_summary(
    since_days: int = Query(30, ge=1, le=365),
    user: AuthenticatedUser = Depends(require_permission("usage:read-self")),
) -> UsageSummaryResponse:
    """自分の使用量を model 別 / tenant 別に集計。

    スコープは「現在 active な tenant (user.org_id) のみ」。
    過去に archived された tenant の消費は by_tenant に含めず、別エンドポイント
    (/me/usage-history) で時系列として参照する。これは画面上の
    「総消費」と「残クレジット」のスコープを揃えるため (クレジットは active tenant の
    credit_used に紐づく)。
    """
    user_tenants_repo = UserTenantsRepository()
    active = user_tenants_repo.get(user.user_id, user.org_id) or {}
    total = int(active.get("total_credit", 0))
    used = int(active.get("credit_used", 0))
    remaining = max(total - used, 0)

    since_iso = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    logs_repo = UsageLogsRepository()
    resp = logs_repo._table.query(
        IndexName="user-id-index",
        KeyConditionExpression=boto3_key("user_id").eq(user.user_id)
        & boto3_key("timestamp_log_id").gte(since_iso),
        Limit=1000,
    )
    items = resp.get("Items", [])

    # P2-1a: `by_tenant` previously leaked archived tenant ids the caller
    # no longer belongs to (and their names, via /me/usage-history).
    # Restrict all aggregations to the active tenant so the response mirrors
    # what `credit_used` actually represents.
    by_model: dict[str, int] = {}
    by_tenant: dict[str, int] = {}
    active_sample = 0
    for it in items:
        tid = str(it.get("tenant_id") or "unknown")
        if tid != user.org_id:
            # Skip consumption recorded against a tenant the caller has
            # since been archived out of. Still counted in the global
            # Admin usage view (`/admin/usage/show`), just hidden from
            # the user's own summary.
            continue
        tokens = int(it.get("total_tokens", 0))
        by_tenant[tid] = by_tenant.get(tid, 0) + tokens
        model = str(it.get("model_id") or "unknown")
        by_model[model] = by_model.get(model, 0) + tokens
        active_sample += 1

    return UsageSummaryResponse(
        tenant_id=user.org_id,
        total_credit=total,
        credit_used=used,
        remaining_credit=remaining,
        by_model=by_model,
        by_tenant=by_tenant,
        sample_size=active_sample,
        since_days=since_days,
    )


@router.get("/me/usage-history", response_model=UsageHistoryResponse)
def usage_history(
    since_days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=100),
    cursor: Optional[str] = None,
    user: AuthenticatedUser = Depends(require_permission("usage:read-self")),
) -> UsageHistoryResponse:
    """自分の使用履歴を時系列で返す (tenant_name も lookup して付与)."""
    since_iso = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    logs_repo = UsageLogsRepository()
    kwargs = {
        "IndexName": "user-id-index",
        "KeyConditionExpression": boto3_key("user_id").eq(user.user_id)
        & boto3_key("timestamp_log_id").gte(since_iso),
        "Limit": limit,
        "ScanIndexForward": False,  # 新しい順
    }
    decoded = _decode_cursor(cursor)
    if decoded:
        kwargs["ExclusiveStartKey"] = decoded
    resp = logs_repo._table.query(**kwargs)

    tenants_repo = TenantsRepository()
    tenant_cache: dict[str, Optional[str]] = {}

    def _tenant_name(tid: str) -> Optional[str]:
        if tid in tenant_cache:
            return tenant_cache[tid]
        t = tenants_repo.get_including_archived(tid)
        name = t.get("name") if t else None
        tenant_cache[tid] = name
        return name

    history = [
        UsageHistoryEntry(
            tenant_id=str(it.get("tenant_id") or ""),
            tenant_name=_tenant_name(str(it.get("tenant_id") or "")),
            model_id=str(it.get("model_id") or ""),
            input_tokens=int(it.get("input_tokens", 0)),
            output_tokens=int(it.get("output_tokens", 0)),
            total_tokens=int(it.get("total_tokens", 0)),
            recorded_at=str(it.get("recorded_at") or ""),
        )
        for it in resp.get("Items", [])
    ]
    return UsageHistoryResponse(
        history=history,
        next_cursor=_encode_cursor(resp.get("LastEvaluatedKey")),
    )
