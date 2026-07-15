"""User's own profile, credit balance, tenant name, and usage history (Phase 2)."""
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
    # deps.py should have already backfilled; re-fetch defensively.
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
# Phase D: own usage history and aggregation
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
    # P0-11: number of sampled requests served by a fallback model (requested
    # model canonically differs from the effective model). Legacy rows without
    # a recorded requested model are not counted (unknown, not a fallback).
    fallback_count: int = 0


class UsageHistoryEntry(BaseModel):
    tenant_id: str
    tenant_name: Optional[str] = None
    model_id: str  # the EFFECTIVE model the request was served by
    input_tokens: int
    output_tokens: int
    total_tokens: int
    recorded_at: str
    # P0-11 fallback visibility. `requested_model_id` is the client-requested
    # model (canonicalized); `fallback_occurred` is derived from the two ids at
    # read. Both None on legacy rows written before this field existed —
    # None means "unknown", NOT "no fallback" (and never True).
    requested_model_id: Optional[str] = None
    fallback_occurred: Optional[bool] = None


class UsageHistoryResponse(BaseModel):
    history: list[UsageHistoryEntry]
    next_cursor: Optional[str] = None


def _derive_fallback(requested: Optional[str], effective: str) -> Optional[bool]:
    """P0-11 fallback visibility, derived at read from the two model ids.

    Returns None when `requested` is absent (a legacy row written before the
    field existed) — "unknown", never False and never True.

    Both ids are stored in the SAME spelling space at write (their bedrock ids),
    so this is a plain string comparison with NO read-time canonicalization.
    That is deliberate (Fable #65 rev1 BUG 1): canonicalizing at read made the
    result depend on the live registry, so a model leaving the registry flipped
    historical non-fallback rows to a spurious True. Comparing the stored
    bedrock ids directly is stable forever — the stored bytes never change.
    """
    if not requested:
        return None
    return requested != effective


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
    """Aggregate the caller's own token usage by model and tenant.

    Scope is limited to the currently active tenant (user.org_id).
    Consumption recorded against previously archived tenants is excluded from by_tenant;
    it is accessible as a time series via /me/usage-history. This ensures the displayed
    "total consumed" and "remaining credit" match in scope (credit is tied to
    credit_used on the active tenant).
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
    fallback_count = 0
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
        # `by_model` keys on the EFFECTIVE model only — it is what actually
        # consumed capacity and generated cost (quota/pricing keyed on it).
        # Requested is display metadata, not an aggregation dimension.
        model = str(it.get("model_id") or "unknown")
        by_model[model] = by_model.get(model, 0) + tokens
        if _derive_fallback(it.get("requested_model_id"), model) is True:
            fallback_count += 1
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
        fallback_count=fallback_count,
    )


@router.get("/me/usage-history", response_model=UsageHistoryResponse)
def usage_history(
    since_days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=100),
    cursor: Optional[str] = None,
    user: AuthenticatedUser = Depends(require_permission("usage:read-self")),
) -> UsageHistoryResponse:
    """Return the caller's usage history in chronological order, including a resolved tenant_name."""
    since_iso = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    logs_repo = UsageLogsRepository()
    kwargs = {
        "IndexName": "user-id-index",
        "KeyConditionExpression": boto3_key("user_id").eq(user.user_id)
        & boto3_key("timestamp_log_id").gte(since_iso),
        "Limit": limit,
        "ScanIndexForward": False,  # newest first
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
            requested_model_id=(
                str(it["requested_model_id"]) if it.get("requested_model_id") else None
            ),
            fallback_occurred=_derive_fallback(
                it.get("requested_model_id"), str(it.get("model_id") or "")
            ),
        )
        for it in resp.get("Items", [])
    ]
    return UsageHistoryResponse(
        history=history,
        next_cursor=_encode_cursor(resp.get("LastEvaluatedKey")),
    )
