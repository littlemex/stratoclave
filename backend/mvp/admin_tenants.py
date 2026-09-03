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
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from dynamo.tenant_budgets import (
    PoolLimitExceedsMaximumError,
    seat_pool_limit_microusd,
)
from limits import MAX_POOL_BUDGET_USD_CENTS, MAX_TOKEN_CREDIT

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
    default_credit: Optional[int] = Field(default=None, ge=0, le=MAX_TOKEN_CREDIT)


class UpdateTenantRequest(BaseModel):
    """team_lead_user_id is not accepted here (Critical C-C: immutability guarantee)."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    default_credit: Optional[int] = Field(default=None, ge=0, le=MAX_TOKEN_CREDIT)


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


class RetainedHoldItem(BaseModel):
    """One reservation being held back pending a decision about what it cost."""

    model_config = ConfigDict(extra="forbid")
    hold_id: str
    amount_microusd: int
    # The `sc_attempt_id` stamped into the provider call's request metadata. It is
    # the handle the provider's own invocation record can be found by, so it is what
    # makes the retention resolvable rather than merely visible.
    attempt_marker: Optional[str] = None
    model_id: Optional[str] = None
    provider_invoked_at: Optional[str] = None
    retained_at: Optional[str] = None


class RetainedHoldsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    period: str
    held_microusd: int
    items: list[RetainedHoldItem]


class ResolveRetainedRequest(BaseModel):
    """How a retained reservation ends.

    Exactly one of the two, because they are different claims and a caller must say
    which one they are making: `charge_microusd` asserts a figure the operator got
    from the provider's own record, and `release` asserts the provider's record shows
    no charge. There is deliberately no default — the gateway cannot pick, and a
    default would be a guess wearing an API's authority.
    """

    model_config = ConfigDict(extra="forbid")
    charge_microusd: Optional[int] = Field(default=None, ge=0)
    release: Optional[bool] = None
    # Free-text, stored on the audit event: where the figure came from.
    evidence: Optional[str] = Field(default=None, max_length=500)


class ResolveRetainedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    period: str
    hold_id: str
    terminal: str  # SETTLE | RELEASE
    charged_microusd: int


class SetPoolBudgetRequest(BaseModel):
    """Set a tenant's dollar pool budget for a period, or return it to the seats.

    The limit is given in whole USD cents for precision without floats; the
    repository stores it as integer micro-USD. `period` defaults to the
    current calendar month (UTC) when omitted.

    `{"follow_seats": true}` is the REVERSAL, and it is a separate field rather
    than a magic value because there is no number that could carry it: zero is a
    legal ceiling meaning every request refused, and every existing caller may
    already be sending it. Reading zero as "follow the seats" would have reversed
    the meaning of a request those callers have been making all along. Exactly one
    of `limit_usd_cents` and `follow_seats` may be given.
    """

    model_config = ConfigDict(extra="forbid")

    limit_usd_cents: Optional[int] = Field(
        default=None,
        ge=0,
        le=MAX_POOL_BUDGET_USD_CENTS,
        description="Pool ceiling for the period, in whole USD cents. Zero is a "
                    "figure and means every request is refused.",
    )
    follow_seats: Optional[bool] = Field(
        default=None,
        description="True clears the hand-set figure so the ceiling follows the "
                    "tenant's seat count again. Mutually exclusive with "
                    "limit_usd_cents.",
    )
    period: Optional[str] = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}$",
        description="Billing period YYYY-MM (UTC). Defaults to the current month.",
    )
    status: Literal["active", "suspended"] = "active"

    @model_validator(mode="after")
    def _exactly_one_intent(self) -> "SetPoolBudgetRequest":
        if self.follow_seats is False:
            # `false` would be a third meaning ("stay manual but change nothing"),
            # and a request whose meaning is "do nothing" is a request the caller
            # believes it made. Refuse rather than guess.
            raise ValueError(
                "follow_seats accepts only true; omit the field to set a figure")
        asked_figure = self.limit_usd_cents is not None
        asked_seats = self.follow_seats is True
        if asked_figure == asked_seats:
            raise ValueError(
                "give exactly one of limit_usd_cents (a figure) or "
                "follow_seats: true (return to the seat count)")
        return self


class PoolBudgetResponse(BaseModel):
    tenant_id: str
    period: str
    status: str
    pool_limit_microusd: int
    pool_reserved_microusd: int
    pool_settled_microusd: int
    # SIGNED and never clamped. A ceiling lowered below committed spend leaves a
    # deficit, and the amount of it is what an operator has to act on: clamping
    # it at zero reports "nothing left" for both "exactly nothing left" and
    # "already $400 over", which are different problems with different fixes.
    remaining_microusd: int
    over_ceiling_microusd: int
    # Convenience mirrors in USD cents for admin surfaces that prefer dollars.
    # Truncated toward ZERO, not floored, so a negative available reads as the
    # same magnitude the micro-USD figure carries.
    pool_limit_usd_cents: int
    remaining_usd_cents: int
    # --- the ceiling's composition, so the total beside it can be checked ---
    # The mode is a SENTENCE, not a field: `mode_sentence` is what a surface
    # renders, and the parts are here so a surface can build its own. A field
    # spelling "per_seat" told an operator the name of a state and nothing about
    # what it meant for them, which is why the state that ended seat tracking
    # could be entered without anyone noticing they had entered it.
    mode_sentence: str
    seat_tracked: bool
    seat_count: int
    seat_rate_microusd: int
    seat_entitlement_microusd: int
    # None exactly when the row follows seats: ABSENCE is the sentinel, and zero
    # is a figure meaning every request refused.
    manual_limit_microusd: Optional[int]
    # Zero until grants exist. Rendered anyway, so the composition printed beside
    # the limit always adds up to it and no interval shows an admin a total that
    # does not equal its parts.
    pool_granted_microusd: int
    baseline_microusd: int
    # The aggregate grant cap, with its absent-default made EXPLICIT. Three fields
    # because there are three facts and collapsing them loses the one that matters:
    # `grant_cap_microusd` is None exactly when nobody set a figure,
    # `effective_grant_cap_microusd` is the number in force either way, and
    # `grant_cap_is_derived` says which of those two a surface is looking at.
    # Without the third, a console showing a cap cannot tell an operator whether
    # that number will move when the tenant hires.
    grant_cap_microusd: Optional[int]
    effective_grant_cap_microusd: int
    grant_cap_is_derived: bool
    # What an approver still has room to grant. Rendered beside the ceiling because
    # an approval surface that pre-fills more than this asks for an amount
    # guaranteed to be refused, and the requester learns that a day later.
    remaining_grant_cap_microusd: int
    # True when a manual figure has been outgrown by what the seats now entitle
    # the tenant to -- the state an operator wants to know about, since the
    # figure they chose is now smaller than the default would have been.
    entitlement_exceeds_figure: bool
    # The action that undoes the latch, named on the read so a surface does not
    # have to know the request shape to offer it.
    resume_action: Optional[str]


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


def _cents_toward_zero(microusd: int) -> int:
    """Micro-USD to whole cents, truncated toward ZERO on the magnitude.

    Plain `//` floors, which on a NEGATIVE available balance reports a deficit one
    cent LARGER than the micro-USD figure says. The cent mirror must never
    disagree with the figure it mirrors, in either direction, so the truncation is
    on the magnitude and the sign is reapplied. Integer-only; matches the
    frontend's `fmtMicroUsd`.
    """
    m = int(microusd)
    return -((-m) // _MICRO_USD_PER_CENT) if m < 0 else m // _MICRO_USD_PER_CENT


def _usd_from_microusd(microusd: int) -> str:
    """`$1,234.56` from integer micro-USD, with NO float anywhere.

    Formatting is where float money creeps back in: `cents / 100` inside an f-string
    reads as harmless and is a float division on a money quantity. The dollars and the
    cents are separate integers here, and the sign is carried once.
    """
    cents = _cents_toward_zero(microusd)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def _mode_sentence(summary: dict) -> str:
    """The pool's mode as a sentence an operator can act on.

    A sentence rather than a field because the field was the defect: a row
    labelled `fixed` said nothing about what the tenant was entitled to, nothing
    about how the label got there, and nothing about how to undo it -- so the
    write that ended seat tracking was invisible to the role that made it.
    """
    seats = int(summary.get("seat_count", 0))
    entitlement = int(summary.get("seat_entitlement_microusd", 0))
    ent = _usd_from_microusd(entitlement)
    if summary.get("seat_tracked"):
        return (
            f"This pool follows the tenant's seat count: {seats} "
            f"{'seat' if seats == 1 else 'seats'} entitle it to {ent} a month, and it "
            f"moves by one seat's worth whenever somebody joins or leaves. Setting a "
            f"figure by hand stops that."
        )
    figure = int(summary.get("manual_limit_microusd") or 0)
    tail = (
        f"The seats would entitle it to {ent}, which is more than the figure, so the "
        f"figure is now the smaller of the two."
        if entitlement > figure else
        f"The seats would entitle it to {ent}."
    )
    return (
        f"This pool is held at {_usd_from_microusd(figure)}, a figure set by hand, and "
        f"no longer follows the tenant's seat count. {tail} Sending "
        f"{{\"follow_seats\": true}} to this endpoint returns it to the seat count."
    )


def _pool_response(tenant_id: str, period: str, summary: dict) -> "PoolBudgetResponse":
    limit = int(summary["pool_limit_microusd"])
    remaining = int(summary["remaining_microusd"])
    manual = summary.get("manual_limit_microusd")
    entitlement = int(summary.get("seat_entitlement_microusd", 0))
    seat_tracked = bool(summary.get("seat_tracked"))
    return PoolBudgetResponse(
        tenant_id=tenant_id,
        period=period,
        status=str(summary.get("status", "active")),
        pool_limit_microusd=limit,
        pool_reserved_microusd=int(summary["pool_reserved_microusd"]),
        pool_settled_microusd=int(summary["pool_settled_microusd"]),
        remaining_microusd=remaining,
        over_ceiling_microusd=int(summary.get("over_ceiling_microusd", 0)),
        pool_limit_usd_cents=_cents_toward_zero(limit),
        remaining_usd_cents=_cents_toward_zero(remaining),
        mode_sentence=_mode_sentence(summary),
        seat_tracked=seat_tracked,
        seat_count=int(summary.get("seat_count", 0)),
        seat_rate_microusd=int(summary.get("seat_rate_microusd", 0)),
        seat_entitlement_microusd=entitlement,
        manual_limit_microusd=None if manual is None else int(manual),
        pool_granted_microusd=int(summary.get("pool_granted_microusd", 0)),
        baseline_microusd=int(summary.get("baseline_microusd", limit)),
        # Read straight off the summary, which resolves the cap through the one
        # pure function every other reader of it uses. A second resolution here --
        # "the attribute, or else the baseline" spelled again -- would be a second
        # default that agrees until one of them is edited.
        grant_cap_microusd=(
            None if summary.get("grant_cap_microusd") is None
            else int(summary["grant_cap_microusd"])),
        effective_grant_cap_microusd=int(
            summary.get("effective_grant_cap_microusd", 0)),
        grant_cap_is_derived=bool(summary.get("grant_cap_is_derived", True)),
        remaining_grant_cap_microusd=int(
            summary.get("remaining_grant_cap_microusd", 0)),
        entitlement_exceeds_figure=(
            not seat_tracked and entitlement > int(manual or 0)),
        # Named on the read rather than only documented, so a surface can offer
        # the reversal without knowing the request shape.
        resume_action=None if seat_tracked else "follow_seats",
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


def _provision_seat_pool(
    tenant_id: str, *, pool_limit_microusd: int, actor: AuthenticatedUser
) -> None:
    """Write the tenant's default dollar pool at creation: a SEAT-TRACKED row for
    the current period, at ZERO seats. It reaches `seats x rate` through the same
    ±1-seat delta every membership change applies, so the ceiling equals the seat
    count at every moment rather than at creation only. Shared by both
    create_tenant routes (admin and team-lead) so the pool a tenant gets does not
    depend on which route created it.

    Seat-tracked means the row carries NO operator figure at all: absence is the
    sentinel, so nothing has to be written to say "follow the seats".

    `pool_limit_microusd` is computed and L8-validated by the CALLER before the
    Tenants row is written (see `create_tenant`), so a misconfigured seat rate
    refuses loudly with no tenant created at all — this function's own write is not
    where that check happens. At zero seats it is zero, which is why the row is
    seeded from the seat count rather than from that figure.
    """
    period = current_period()
    TenantBudgetsRepository().create_seat_tracked_pool(
        tenant_id=tenant_id,
        period=period,
        seat_count=0,
    )
    log_audit_event(
        event="tenant_pool_provisioned",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
        after={
            "period": period,
            "pool_limit_microusd": pool_limit_microusd,
            "seat_tracked": True,
            "seats": 0,
        },
    )


@router.post("", response_model=TenantItem, status_code=201)
def create_tenant(
    body: CreateTenantRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:create")),
) -> TenantItem:
    _verify_team_lead(body.team_lead_user_id)
    # L8: validate ONE seat against MAX_POOL_BUDGET_USD_CENTS before writing
    # anything, so a misconfigured SEAT_MONTHLY_USD refuses loudly rather than
    # creating a tenant with a silently-clamped pool.
    try:
        seat_pool_limit_microusd(1)
    except PoolLimitExceedsMaximumError as e:
        raise HTTPException(status_code=422, detail=f"seat_pool_limit_exceeds_maximum: {e}")
    # The pool is written at ZERO seats and reaches its size through the same
    # ±1-seat delta every later membership change uses. Writing one seat here
    # instead would count the owner twice: a fresh tenant has no memberships yet,
    # and the first `ensure` adds its own seat. Zero is also the honest ceiling
    # for a tenant nobody is a member of — there is no caller to admit.
    pool_limit_microusd = seat_pool_limit_microusd(0)
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
    _provision_seat_pool(item["tenant_id"], pool_limit_microusd=pool_limit_microusd, actor=actor)
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

    # Live grants are given back BEFORE the tenant goes, and the retirement is
    # refused while any remain. Archiving over a live grant leaves a grant row
    # pinned to a pool row nobody will look at again: the sweeper will still try to
    # revoke it, the reconciler will still count it against a cap for a tenant that
    # no longer exists, and its capacity is never released because release means
    # moving a ceiling on a retired tenant.
    #
    # The drain starts FROM GRANTS rather than from the tenant's pool rows, because
    # a grant pinned to a period whose row is already gone is exactly the one a
    # pool-row-first sweep has no row to start at.
    from . import grants as _grants

    drain = _grants.revoke_all_active_grants(tenant_id=tenant_id, actor=actor)
    if drain["remaining_count"]:
        # Refused rather than archived-anyway. A grant that could not be given back
        # is capacity still counted against this tenant's ceiling, and completing
        # the retirement would make that permanent and unobservable.
        raise HTTPException(
            status_code=409,
            detail={
                "type": "active_grants_remain",
                "message": (
                    f"{drain['remaining_count']} grant(s) still bear capacity on "
                    f"this tenant's pool and could not be revoked. Retiring the "
                    f"tenant now would leave that capacity granted with nothing "
                    f"able to release it. Repair or revoke them first."),
                "revoked_count": drain["revoked_count"],
                "remaining": drain["remaining"],
            })

    repo.archive(tenant_id)
    log_audit_event(
        event="tenant_archived",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
        # What the retirement gave back, on the retirement's own event. A revoke
        # recorded only under its own event name would leave a reader of the
        # archive unable to tell a tenant that held no grants from one whose
        # grants this request released.
        details={"grants_revoked": drain["revoked_count"],
                 "grant_ids": drain["revoked"]},
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


def apply_pool_budget_request(
    *, tenant_id: str, body: SetPoolBudgetRequest, actor: AuthenticatedUser
) -> "PoolBudgetResponse":
    """Apply a pool-budget PUT and audit it. Shared by the admin route and the
    team-lead route so the two cannot drift: both are writers of the ceiling, both
    latch the row off seat tracking, and the log must read the same either way.

    Two audit events, not one. `tenant_pool_budget_set` records the figure, as it
    always has. A `tenant_pool_mode_changed` event is emitted IN ADDITION whenever
    the row crosses between following the seats and holding a figure, because a
    figure change and a mode change are different facts and the second one is the
    consequential one: it is the write that ends seat tracking, and before this it
    was inferable only by comparing two figures in two log lines.
    """
    period = body.period or current_period()
    repo = TenantBudgetsRepository()
    before = repo.pool_summary(tenant_id, period)
    was_seat_tracked = bool((before or {}).get("seat_tracked", True))

    # A figure that EQUALS the ceiling currently in force, while part of that
    # ceiling is granted, is almost certainly a figure copied off the screen.
    # `set_manual_limit` treats it as the new BASELINE and moves `pool_limit`
    # by the delta against the OLD baseline only -- the granted term is never
    # touched -- so a figure that already includes the grant makes the grant
    # get added on top of itself again: the ceiling holds at the typed figure
    # PLUS the grant for as long as the grant stays open, one grant's worth
    # above what was on the screen. That excess is temporary, not permanent --
    # the sweep subtracts the grant once at expiry and the ceiling lands
    # exactly on the number the operator typed, never below it -- but it is a
    # window of extra capacity nobody asked for, and it is indistinguishable,
    # from the figure alone, from an operator who genuinely wants that number
    # as the new baseline (for whom the same jump-then-settle is correct): a
    # $950 figure set while $450 was granted holds the ceiling at $1,400 until
    # the grant expires, then $950 -- never below $950.
    #
    # Refused rather than reinterpreted, because both readings are plausible
    # and picking one silently would either erase money that should still be
    # there or open a free window that should not exist. The refusal names
    # the composition so the next request can be exact.
    granted_now = int((before or {}).get("pool_granted_microusd", 0))
    if (not body.follow_seats) and granted_now > 0:
        asked_microusd = int(body.limit_usd_cents or 0) * _MICRO_USD_PER_CENT
        if asked_microusd == int((before or {}).get("pool_limit_microusd", -1)):
            raise HTTPException(
                status_code=409,
                detail={
                    "type": "figure_includes_active_grant",
                    "message": (
                        "That figure is the ceiling currently in force, and part of "
                        "it is granted rather than baseline. Setting it would fold "
                        "the grant into the new baseline and then add the same "
                        "grant on top again, holding the ceiling at this figure "
                        "plus the grant until the grant expires. Send the baseline "
                        "you want instead."),
                    "figure_microusd": asked_microusd,
                    "pool_limit_microusd": int(
                        (before or {}).get("pool_limit_microusd", 0)),
                    "pool_granted_microusd": granted_now,
                    "baseline_microusd": int(
                        (before or {}).get("baseline_microusd", 0)),
                })

    if body.follow_seats:
        if before is None:
            # Nothing to clear, and creating a row here would invent a ceiling
            # nobody asked for. The tenant is unlimited at the pool level for this
            # period; that is what an absent row means and it is not this
            # endpoint's business to change it.
            raise HTTPException(
                status_code=404,
                detail=f"No pool budget set for tenant {tenant_id} period {period}")
        repo.clear_manual_limit(tenant_id=tenant_id, period=period)
        limit_microusd: Optional[int] = None
    else:
        limit_microusd = int(body.limit_usd_cents or 0) * _MICRO_USD_PER_CENT
        repo.set_manual_limit(
            tenant_id=tenant_id,
            period=period,
            manual_limit_microusd=limit_microusd,
            status=body.status,
        )

    summary = repo.pool_summary(tenant_id, period)
    assert summary is not None  # just written
    now_seat_tracked = bool(summary.get("seat_tracked"))

    log_audit_event(
        event="tenant_pool_budget_set",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
        before={
            "pool_limit_microusd": (before or {}).get("pool_limit_microusd"),
            "status": (before or {}).get("status"),
            "manual_limit_microusd": (before or {}).get("manual_limit_microusd"),
            "seat_tracked": was_seat_tracked,
        },
        after={
            "period": period,
            "pool_limit_microusd": int(summary["pool_limit_microusd"]),
            "manual_limit_microusd": summary.get("manual_limit_microusd"),
            "status": str(summary.get("status", "active")),
            "seat_tracked": now_seat_tracked,
        },
    )
    if was_seat_tracked != now_seat_tracked:
        log_audit_event(
            event="tenant_pool_mode_changed",
            actor_id=actor.user_id,
            actor_email=actor.email,
            target_id=tenant_id,
            target_type="tenant",
            before={"seat_tracked": was_seat_tracked},
            after={
                "period": period,
                "seat_tracked": now_seat_tracked,
                "seat_count": int(summary.get("seat_count", 0)),
                "seat_entitlement_microusd": int(
                    summary.get("seat_entitlement_microusd", 0)),
                "manual_limit_microusd": summary.get("manual_limit_microusd"),
            },
        )
    return _pool_response(tenant_id, period, summary)


@router.put("/{tenant_id}/pool-budget", response_model=PoolBudgetResponse)
def set_pool_budget(
    tenant_id: str,
    body: SetPoolBudgetRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:update")),
) -> PoolBudgetResponse:
    """Set the tenant's dollar pool budget for a period, or return it to the seats.

    The pool is enforced *before* every inference call in the credit pipeline:
    when a tenant has a pool for the current period, each request reserves its
    dollar cost from the pool atomically with the per-user token debit, so the
    tenant cannot overspend its budget even under concurrency. This is a
    control a credential broker cannot offer — there is no request-time choke
    point outside a gateway.

    Setting a figure stops the ceiling following the tenant's seat count, and
    `{"follow_seats": true}` starts it again. The reversal is what this endpoint
    lacked: a figure set once stopped seat tracking permanently, with no request
    that could undo it.
    """
    tenant = TenantsRepository().get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return apply_pool_budget_request(tenant_id=tenant_id, body=body, actor=actor)


def _attempt_marker(tenant_id: str, hold_id: str) -> Optional[str]:
    from .provider_outcome import attempt_request_metadata

    return attempt_request_metadata(hold_id, tenant_id).get("sc_attempt_id")


@router.get("/{tenant_id}/pool-retained", response_model=RetainedHoldsResponse)
def list_retained_holds(
    tenant_id: str,
    period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    _admin: AuthenticatedUser = Depends(require_permission("tenants:read-all")),
) -> RetainedHoldsResponse:
    """Reservations this tenant is holding budget for, pending a decision.

    A reservation is retained rather than returned when the request that made it
    vanished AFTER its provider call had departed: the gateway cannot know what that
    call cost, and handing the budget back asserts it cost nothing — which was
    measured to be false. Nothing resolves a retention on its own, on purpose, so
    this list is where an operator sees what is held and what handle to look it up by
    in the provider's own records.
    """
    from . import _pipeline

    if not TenantsRepository().get(tenant_id):
        raise HTTPException(status_code=404, detail="Tenant not found")
    resolved = period or current_period()
    items = _pipeline.list_retained_holds(tenant_id, resolved)
    return RetainedHoldsResponse(
        tenant_id=tenant_id,
        period=resolved,
        held_microusd=sum(int(h.get("amount_microusd", 0)) for h in items),
        items=[
            RetainedHoldItem(
                hold_id=str(h.get("hold_id", "")),
                amount_microusd=int(h.get("amount_microusd", 0)),
                # Derived through the same function that stamps the provider call,
                # so the handle an operator searches the provider's records by
                # cannot drift from the one that was actually sent.
                attempt_marker=_attempt_marker(tenant_id, str(h.get("hold_id", ""))),
                model_id=str(h.get("model_id") or "") or None,
                provider_invoked_at=str(h.get("provider_invoked_at") or "") or None,
                retained_at=str(h.get("retained_at") or "") or None,
            )
            for h in items
        ],
    )


@router.post("/{tenant_id}/pool-retained/{hold_id}/resolve",
             response_model=ResolveRetainedResponse)
def resolve_retained_hold(
    tenant_id: str,
    hold_id: str,
    body: ResolveRetainedRequest,
    actor: AuthenticatedUser = Depends(require_permission("tenants:update")),
    period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
) -> ResolveRetainedResponse:
    """End a retained reservation, at a figure the operator supplies or at nothing.

    The two outcomes are different claims and the caller states which: a figure
    asserts what the provider's own record shows was charged, and a release asserts
    that record shows no charge. The gateway supplies neither — being unable to is
    why the reservation was retained — so there is no default and nothing is
    inferred. The figure may not exceed what was reserved: the pool holds exactly
    that much, and a larger figure is an overrun, which is a different record.

    Both outcomes go through the same money primitives a request uses, so a
    resolution is not a second settle path. The audit event carries the evidence the
    operator cited, because a charge that arrives days late at a figure no request
    computed is one a reader will want to trace.
    """
    from . import _pipeline

    if not TenantsRepository().get(tenant_id):
        raise HTTPException(status_code=404, detail="Tenant not found")
    resolved_period = period or current_period()
    try:
        terminal, settled = _pipeline.resolve_retained_hold(
            tenant_id, resolved_period, hold_id,
            charge_microusd=body.charge_microusd,
            release=bool(body.release),
        )
    except _pipeline.RetainedHoldNotFound:
        raise HTTPException(status_code=404, detail="No retained hold with that id")
    except _pipeline.RetainedResolutionRaced:
        raise HTTPException(
            status_code=409,
            detail={"type": "retention_already_resolved",
                    "message": "This retention was resolved by another request."})

    log_audit_event(
        event="tenant_pool_retention_resolved",
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=tenant_id,
        target_type="tenant",
        before={"hold_id": hold_id, "state": "RETAINED"},
        after={"period": resolved_period, "terminal": terminal,
               "charged_microusd": settled, "evidence": body.evidence},
    )
    return ResolveRetainedResponse(
        tenant_id=tenant_id, period=resolved_period, hold_id=hold_id,
        terminal=terminal, charged_microusd=settled,
    )


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
    return _pool_response(
        tenant_id, resolved_period, summary
    )


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
