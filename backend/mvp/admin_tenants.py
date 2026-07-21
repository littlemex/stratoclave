"""Admin Tenant API (Phase 2).

- GET    /api/mvp/admin/tenants            list tenants (cursor pagination)
- POST   /api/mvp/admin/tenants            create tenant (validates team_lead existence + role)
- GET    /api/mvp/admin/tenants/{id}       tenant detail
- PATCH  /api/mvp/admin/tenants/{id}       update name / default_credit
- DELETE /api/mvp/admin/tenants/{id}       soft-delete (status=archived)
- PUT    /api/mvp/admin/tenants/{id}/owner reassign team_lead_user_id (Critical C-C)
- GET    /api/mvp/admin/tenants/{id}/users list tenant members
- GET    /api/mvp/admin/tenants/{id}/usage per-tenant usage aggregation
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from boto3.dynamodb.conditions import Key as boto3_key
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from dynamo import (
    ADMIN_OWNED,
    TenantBudgetsRepository,
    TenantLimitExceededError,
    TenantNotFoundError,
    TenantsRepository,
    UsersRepository,
    UserTenantsRepository,
    UsageLogsRepository,
    current_period,
)

from .authz import log_audit_event, require_permission
from .deps import DEFAULT_ORG_ID, AuthenticatedUser


router = APIRouter(prefix="/api/mvp/admin/tenants", tags=["mvp-admin-tenants"])


# -----------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------
Role = Literal["admin", "team_lead", "user"]


class TenantItem(BaseModel):
    tenant_id: str
    name: str
    team_lead_user_id: str
    default_credit: int
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None


class TenantListResponse(BaseModel):
    tenants: list[TenantItem]
    next_cursor: Optional[str] = None


class CreateTenantRequest(BaseModel):
    """Admin tenant creation request. Validates team_lead_user_id existence and role (Critical C-E)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    team_lead_user_id: str = Field(
        min_length=1,
        max_length=64,
        description="sub of a user with team_lead role, or 'admin-owned'",
    )
    default_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)


class UpdateTenantRequest(BaseModel):
    """team_lead_user_id is not accepted here (Critical C-C: immutability guarantee)."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    default_credit: Optional[int] = Field(default=None, ge=0, le=10_000_000)


class SetOwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_lead_user_id: str = Field(min_length=1, max_length=64)


class TenantMember(BaseModel):
    user_id: str
    email: str
    role: str
    total_credit: int
    credit_used: int
    remaining_credit: int
    status: str


class TenantMembersResponse(BaseModel):
    tenant_id: str
    members: list[TenantMember]


class UsageBucket(BaseModel):
    tenant_id: str
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, int] = {}
    by_user: dict[str, int] = {}
    sample_size: int = 0


class SetPoolBudgetRequest(BaseModel):
    """Set a tenant's dollar pool budget for a period.

    The limit is given in whole USD cents for precision without floats; the
    repository stores it as integer micro-USD. `period` defaults to the
    current calendar month (UTC) when omitted.
    """

    model_config = ConfigDict(extra="forbid")

    limit_usd_cents: int = Field(
        ge=0,
        le=100_000_000,  # up to $1,000,000.00 per period
        description="Pool ceiling for the period, in whole USD cents.",
    )
    period: Optional[str] = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}$",
        description="Billing period YYYY-MM (UTC). Defaults to the current month.",
    )
    status: Literal["active", "suspended"] = "active"


class PoolBudgetResponse(BaseModel):
    tenant_id: str
    period: str
    status: str
    pool_limit_microusd: int
    pool_reserved_microusd: int
    pool_settled_microusd: int
    remaining_microusd: int
    # Convenience mirrors in USD cents for admin surfaces that prefer dollars.
    pool_limit_usd_cents: int
    remaining_usd_cents: int


class PoolReconciliationResponse(BaseModel):
    """Counter-vs-ledger reconciliation for one tenant/period.

    The budget row's three counters are a materialized cache; the credit ledger
    is the append-only source of truth. `*_drift_microusd` is counter − ledger:
    a money source of truth tolerates NO drift, so any non-zero value is a defect
    to investigate (a metric filter alarms on the emitted `LedgerDrift*` events).
    `snapshot_stable` is False when the counters moved between the pre/post read
    (a concurrent txn) — the drift is then inconclusive and should be re-run.
    """
    tenant_id: str
    period: str
    counter_settled_microusd: int
    counter_reserved_microusd: int
    counter_reclaimed_microusd: int
    ledger_settled_microusd: int
    ledger_reserved_microusd: int
    ledger_reclaimed_microusd: int
    settled_drift_microusd: int
    reserved_drift_microusd: int
    reclaimed_drift_microusd: int
    snapshot_stable: bool
    in_sync: bool
    # True while the period still holds pre-Phase-2 terminals (no RESERVE event),
    # so the reserved/reclaimed axes are migration artifacts, not yet derivable.
    migrating: bool = False
    pre_p2_terminals: int = 0
    # Layer 5 replay audit: every frozen rating in the period recomputes to its
    # own total AND to the settled_delta (INV-R2/R3). False + a sample of the
    # offending holds when any rating fails to reproduce.
    rating_replay_ok: bool = True
    rating_replay_mismatches: list = Field(default_factory=list)


_MICRO_USD_PER_CENT = 10_000  # 1 cent = 10_000 micro-USD


def _pool_response(tenant_id: str, period: str, summary: dict) -> "PoolBudgetResponse":
    limit = int(summary["pool_limit_microusd"])
    remaining = int(summary["remaining_microusd"])
    return PoolBudgetResponse(
        tenant_id=tenant_id,
        period=period,
        status=str(summary.get("status", "active")),
        pool_limit_microusd=limit,
        pool_reserved_microusd=int(summary["pool_reserved_microusd"]),
        pool_settled_microusd=int(summary["pool_settled_microusd"]),
        remaining_microusd=remaining,
        pool_limit_usd_cents=limit // _MICRO_USD_PER_CENT,
        remaining_usd_cents=remaining // _MICRO_USD_PER_CENT,
    )


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
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


def _to_tenant_item(item: dict[str, Any]) -> TenantItem:
    return TenantItem(
        tenant_id=str(item["tenant_id"]),
        name=str(item.get("name") or ""),
        team_lead_user_id=str(item.get("team_lead_user_id") or ADMIN_OWNED),
        default_credit=int(item.get("default_credit") or 0),
        status=str(item.get("status") or "active"),
        created_at=item.get("created_at"),
        updated_at=item.get("updated_at"),
        created_by=item.get("created_by"),
    )


def _verify_team_lead(team_lead_user_id: str) -> None:
    """Require that team_lead_user_id refers to an existing user whose roles include team_lead.

    Exception: validation is skipped when the value is `admin-owned`.
    """
    if team_lead_user_id == ADMIN_OWNED:
        return
    user = UsersRepository().get_by_user_id(team_lead_user_id)
    if not user:
        raise HTTPException(
            status_code=422,
            detail=f"team_lead_user_id not found: {team_lead_user_id}",
        )
    roles = user.get("roles") or []
    if "team_lead" not in roles:
        raise HTTPException(
            status_code=422,
            detail=f"user {team_lead_user_id} does not have team_lead role",
        )


# -----------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------
@router.get("", response_model=TenantListResponse)
def list_tenants(
    cursor: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    _admin: AuthenticatedUser = Depends(require_permission("tenants:read-all")),
) -> TenantListResponse:
    items, last_key = TenantsRepository().list_all(cursor=_decode_cursor(cursor), limit=limit)
    return TenantListResponse(
        tenants=[_to_tenant_item(it) for it in items if it.get("status") != "archived"],
        next_cursor=_encode_cursor(last_key),
    )


def _provision_shadow_default(tenant_id: str, *, actor_id: str) -> None:
    """New tenants get shadow VSR ON by default so the Savings Certificate is
    populated from week one (the litellm-wedge value prop). This writes an
    EXPLICIT shadow_vsr=True routing-config record (not an implicit default), so
    existing tenants are untouched and the state is visible/auditable. An OSS
    operator can opt the default out with STRATOCLAVE_SHADOW_VSR_NEW_TENANT_DEFAULT
    =false. Best-effort + fenced: never fails tenant creation (shadow is advisory,
    money-neutral)."""
    import os

    # inverse-default (ON unless explicitly disabled): accept the common falsy
    # spellings symmetrically so an operator's "0"/"no"/"off" also opts out
    # (Fable per-tenant review-2 Low — do not regress to a literal "false").
    if os.getenv("STRATOCLAVE_SHADOW_VSR_NEW_TENANT_DEFAULT", "true").strip().lower() in (
            "false", "0", "no", "off"):
        return
    try:
        from . import admin_routing as _ar

        _ar.provision_shadow_default_config(tenant_id, updated_by=actor_id)
    except Exception as e:  # noqa: BLE001 — advisory default; never break creation.
        try:
            from core.logging import get_logger
            get_logger(__name__).warning("shadow_default_provision_failed",
                                         tenant_id=tenant_id, error=str(e))
        except Exception:
            pass


@router.post("", response_model=TenantItem, status_code=201)
def create_tenant(
    body: CreateTenantRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:create")),
) -> TenantItem:
    _verify_team_lead(body.team_lead_user_id)
    try:
        item = TenantsRepository().create(
            name=body.name,
            team_lead_user_id=body.team_lead_user_id,
            default_credit=body.default_credit,
            created_by=actor.user_id,
        )
    except TenantLimitExceededError as e:
        raise HTTPException(status_code=403, detail=str(e))
    log_audit_event(
        event="tenant_created",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=item["tenant_id"],
        target_type="tenant",
        details={"name": body.name, "team_lead_user_id": body.team_lead_user_id},
    )
    # after the create audit so the log reads create -> provision.
    _provision_shadow_default(item["tenant_id"], actor_id=actor.user_id)
    return _to_tenant_item(item)


@router.get("/{tenant_id}", response_model=TenantItem)
def get_tenant(
    tenant_id: str,
    _admin: AuthenticatedUser = Depends(require_permission("tenants:read-all")),
) -> TenantItem:
    item = TenantsRepository().get(tenant_id)
    if not item:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return _to_tenant_item(item)


@router.patch("/{tenant_id}", response_model=TenantItem)
def update_tenant(
    tenant_id: str,
    body: UpdateTenantRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:update")),
) -> TenantItem:
    try:
        item = TenantsRepository().update(
            tenant_id=tenant_id,
            name=body.name,
            default_credit=body.default_credit,
        )
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Tenant not found")
    log_audit_event(
        event="tenant_updated",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
        after={"name": body.name, "default_credit": body.default_credit},
    )
    return _to_tenant_item(item)


@router.delete("/{tenant_id}")
def archive_tenant(
    tenant_id: str,
    actor: AuthenticatedUser = Depends(require_permission("tenants:delete")),
) -> Response:
    if tenant_id == DEFAULT_ORG_ID:
        raise HTTPException(status_code=409, detail=f"{DEFAULT_ORG_ID} cannot be deleted")
    repo = TenantsRepository()
    item = repo.get(tenant_id)
    if not item:
        raise HTTPException(status_code=404, detail="Tenant not found")
    repo.archive(tenant_id)
    log_audit_event(
        event="tenant_archived",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
    )
    return Response(status_code=204)


@router.put("/{tenant_id}/owner", response_model=TenantItem)
def set_tenant_owner(
    tenant_id: str,
    body: SetOwnerRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:update")),
) -> TenantItem:
    """Reassign team_lead_user_id (Critical C-C: recovers tenants orphaned by Cognito delete-and-recreate)."""
    _verify_team_lead(body.team_lead_user_id)
    repo = TenantsRepository()
    before = repo.get(tenant_id)
    if not before:
        raise HTTPException(status_code=404, detail="Tenant not found")
    try:
        item = repo.set_owner(
            tenant_id=tenant_id,
            new_owner_user_id=body.team_lead_user_id,
        )
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Tenant not found")
    log_audit_event(
        event="tenant_owner_changed",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
        before={"team_lead_user_id": before.get("team_lead_user_id")},
        after={"team_lead_user_id": body.team_lead_user_id},
    )
    return _to_tenant_item(item)


@router.get("/{tenant_id}/users", response_model=TenantMembersResponse)
def list_tenant_users(
    tenant_id: str,
    _admin: AuthenticatedUser = Depends(require_permission("tenants:read-all")),
) -> TenantMembersResponse:
    """List members of a tenant (admin view, includes user_id)."""
    tenant = TenantsRepository().get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    user_tenants_repo = UserTenantsRepository()
    resp = user_tenants_repo._table.query(
        IndexName="tenant-id-index",
        KeyConditionExpression=boto3_key("tenant_id").eq(tenant_id),
    )
    members: list[TenantMember] = []
    users_repo = UsersRepository()
    for ut in resp.get("Items", []):
        if ut.get("status", "active") != "active":
            continue
        uid = str(ut["user_id"])
        user = users_repo.get_by_user_id(uid)
        email = str(user.get("email") if user else "") or ""
        total = int(ut.get("total_credit", 0))
        used = int(ut.get("credit_used", 0))
        members.append(
            TenantMember(
                user_id=uid,
                email=email,
                role=str(ut.get("role") or "user"),
                total_credit=total,
                credit_used=used,
                remaining_credit=max(total - used, 0),
                status=str(ut.get("status") or "active"),
            )
        )
    return TenantMembersResponse(tenant_id=tenant_id, members=members)


@router.get("/{tenant_id}/usage", response_model=UsageBucket)
def get_tenant_usage(
    tenant_id: str,
    since_days: int = Query(30, ge=1, le=365),
    _admin: AuthenticatedUser = Depends(require_permission("usage:read-all")),
) -> UsageBucket:
    """Query UsageLogs by tenant_id (PK) and aggregate by model and user in Python.

    Results are truncated at 1000 items (sufficient for MVP scale).
    """
    tenant = TenantsRepository().get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    since_iso = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()

    repo = UsageLogsRepository()
    resp = repo._table.query(
        KeyConditionExpression=boto3_key("tenant_id").eq(tenant_id)
        & boto3_key("timestamp_log_id").gte(since_iso),
        Limit=1000,
    )
    items = resp.get("Items", [])
    bucket = UsageBucket(tenant_id=tenant_id, sample_size=len(items))
    for it in items:
        tokens = int(it.get("total_tokens", 0))
        input_tokens = int(it.get("input_tokens", 0))
        output_tokens = int(it.get("output_tokens", 0))
        bucket.total_tokens += tokens
        bucket.input_tokens += input_tokens
        bucket.output_tokens += output_tokens
        model = str(it.get("model_id") or "unknown")
        bucket.by_model[model] = bucket.by_model.get(model, 0) + tokens
        user_email = str(it.get("user_email") or it.get("user_id") or "unknown")
        bucket.by_user[user_email] = bucket.by_user.get(user_email, 0) + tokens
    return bucket


@router.put("/{tenant_id}/pool-budget", response_model=PoolBudgetResponse)
def set_pool_budget(
    tenant_id: str,
    body: SetPoolBudgetRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:update")),
) -> PoolBudgetResponse:
    """Set (create or update) the tenant's dollar pool budget for a period.

    The pool is enforced *before* every inference call in the credit pipeline:
    when a tenant has a pool for the current period, each request reserves its
    dollar cost from the pool atomically with the per-user token debit, so the
    tenant cannot overspend its budget even under concurrency. This is a
    control a credential broker cannot offer — there is no request-time choke
    point outside a gateway.
    """
    tenant = TenantsRepository().get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    period = body.period or current_period()
    limit_microusd = int(body.limit_usd_cents) * _MICRO_USD_PER_CENT

    repo = TenantBudgetsRepository()
    before = repo.pool_summary(tenant_id, period)
    repo.set_pool_limit(
        tenant_id=tenant_id,
        period=period,
        pool_limit_microusd=limit_microusd,
        status=body.status,
    )
    summary = repo.pool_summary(tenant_id, period)
    assert summary is not None  # just written

    log_audit_event(
        event="tenant_pool_budget_set",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
        before={
            "pool_limit_microusd": (before or {}).get("pool_limit_microusd"),
            "status": (before or {}).get("status"),
        },
        after={
            "period": period,
            "pool_limit_microusd": limit_microusd,
            "status": body.status,
        },
    )
    return _pool_response(tenant_id, period, summary)


@router.get("/{tenant_id}/pool-budget", response_model=PoolBudgetResponse)
def get_pool_budget(
    tenant_id: str,
    period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    _admin: AuthenticatedUser = Depends(require_permission("tenants:read-all")),
) -> PoolBudgetResponse:
    """Return the tenant's pool budget and live usage for a period.

    404 when the tenant has no pool budget for the period (pool budgeting is
    opt-in; absence means the tenant is unlimited at the pool level and only
    per-user token budgets apply).
    """
    tenant = TenantsRepository().get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    resolved_period = period or current_period()
    summary = TenantBudgetsRepository().pool_summary(tenant_id, resolved_period)
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail=f"No pool budget set for tenant {tenant_id} period {resolved_period}",
        )
    return _pool_response(tenant_id, resolved_period, summary)


def _read_counters(repo: "TenantBudgetsRepository", tenant_id: str, period: str) -> dict:
    """Strongly-consistent read of the three budget counters (reclaimed is not in
    pool_summary, so read the row directly)."""
    row = repo.get(tenant_id, period, consistent_read=True) or {}
    return {
        "settled": int(row.get("pool_settled_microusd", 0)),
        "reserved": int(row.get("pool_reserved_microusd", 0)),
        "reclaimed": int(row.get("pool_reclaimed_microusd", 0)),
    }


@router.get(
    "/{tenant_id}/pool-reconciliation", response_model=PoolReconciliationResponse
)
def get_pool_reconciliation(
    tenant_id: str,
    period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    _admin: AuthenticatedUser = Depends(require_permission("tenants:read-all")),
) -> PoolReconciliationResponse:
    """Reconcile the budget counters (materialized cache) against the credit
    ledger (append-only source of truth) for a tenant/period.

    Reads counters (C1, consistent) → folds the ledger partition → re-reads
    counters (C2). When C1==C2 the drift is a true point-in-time comparison; a
    non-zero drift is a defect. When C1!=C2 a txn ran mid-fold, so the result is
    marked unstable (re-run). Any drift is logged as a `LedgerDrift*` event that a
    CloudWatch metric filter alarms on (see iac)."""
    from dynamo import CreditLedgerRepository

    tenant = TenantsRepository().get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    resolved_period = period or current_period()
    repo = TenantBudgetsRepository()
    if repo.get(tenant_id, resolved_period) is None:
        raise HTTPException(
            status_code=404,
            detail=f"No pool budget set for tenant {tenant_id} period {resolved_period}",
        )

    led_repo = CreditLedgerRepository()
    c1 = _read_counters(repo, tenant_id, resolved_period)
    ledger = led_repo.derived_totals(tenant_id=tenant_id, period=resolved_period)
    replay_mismatches = led_repo.rating_replay_mismatches(
        tenant_id=tenant_id, period=resolved_period
    )
    c2 = _read_counters(repo, tenant_id, resolved_period)
    stable = (
        c1["settled"] == c2["settled"]
        and c1["reserved"] == c2["reserved"]
        and c1["reclaimed"] == c2["reclaimed"]
    )

    settled_drift = c1["settled"] - ledger["settled_microusd"]
    reserved_drift = c1["reserved"] - ledger["reserved_microusd"]
    reclaimed_drift = c1["reclaimed"] - ledger["reclaimed_microusd"]

    # Migration gate (Fable P2 review-2 R2-6): while the period still holds
    # pre-Phase-2 terminals (SETTLE/RECLAIM written before RESERVE/RECLAIM ledger
    # events existed), the reserved/reclaimed axes are NOT fully ledger-derivable
    # — their "drift" is a migration artifact, not a defect. Suppress those two
    # axes from in_sync and from alarming until the pre-P2 tail has drained (the
    # period rolls over, or every legacy hold has finalized). Settled is valid
    # across the boundary (SETTLE terminals always carried settled_delta).
    migrating = int(ledger.get("pre_p2_terminals", 0)) > 0

    # M-1 (Fable P2 review-1): the fold is a paginated consistent read, not a
    # partition snapshot; a reserve+release pair straddling the fold cursor can
    # show a PHANTOM reserved drift that passes C1==C2 (settled/reclaimed are
    # monotonic so they can't). Re-fold once when a stable reserved drift shows
    # up: a straddle usually heals on the re-fold. NOTE (R2-5): this is a
    # mitigation, not a proof — a second independent straddle, or a uniform-price
    # tenant, can still reproduce the same phantom value; treat a persistent
    # reserved drift as "investigate", not "certain defect".
    if stable and not migrating and reserved_drift != 0:
        ledger2 = led_repo.derived_totals(tenant_id=tenant_id, period=resolved_period)
        c3 = _read_counters(repo, tenant_id, resolved_period)
        if c3["reserved"] == c1["reserved"]:
            reserved_drift = c1["reserved"] - ledger2["reserved_microusd"]
            ledger["reserved_microusd"] = ledger2["reserved_microusd"]
        else:
            # Counter moved during the re-fold → inconclusive; drop to unstable so
            # we neither report nor alarm on a moving target.
            stable = False

    # in_sync: settled is always meaningful; reserved/reclaimed only once the
    # migration tail has drained; and every frozen rating must replay (L5).
    in_sync = stable and settled_drift == 0 and not replay_mismatches
    if not migrating:
        in_sync = in_sync and reserved_drift == 0 and reclaimed_drift == 0

    # Emit drift metrics ONLY when stable. Settled always; reserved/reclaimed only
    # when NOT migrating (else every migrated tenant alarms on day 1 — R2-6).
    if stable:
        axes = [("Settled", settled_drift)]
        if not migrating:
            axes += [("Reserved", reserved_drift), ("Reclaimed", reclaimed_drift)]
        for axis, drift in axes:
            if drift != 0:
                # Event name is the metric-filter key (see iac dynamodb/ledger stack).
                log_audit_event(
                    event=f"LedgerDrift{axis}",
                    actor_id=_admin.user_id,
                    actor_email=_admin.email,
                    target_id=tenant_id,
                    target_type="tenant_pool",
                    after={"period": resolved_period, "drift_microusd": drift},
                )

    return PoolReconciliationResponse(
        tenant_id=tenant_id,
        period=resolved_period,
        counter_settled_microusd=c1["settled"],
        counter_reserved_microusd=c1["reserved"],
        counter_reclaimed_microusd=c1["reclaimed"],
        ledger_settled_microusd=ledger["settled_microusd"],
        ledger_reserved_microusd=ledger["reserved_microusd"],
        ledger_reclaimed_microusd=ledger["reclaimed_microusd"],
        settled_drift_microusd=settled_drift,
        reserved_drift_microusd=reserved_drift,
        reclaimed_drift_microusd=reclaimed_drift,
        snapshot_stable=stable,
        in_sync=in_sync,
        migrating=migrating,
        pre_p2_terminals=int(ledger.get("pre_p2_terminals", 0)),
        rating_replay_ok=not replay_mismatches,
        rating_replay_mismatches=replay_mismatches,
    )
