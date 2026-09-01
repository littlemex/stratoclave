"""Limit raises: the request, the approval, and the grant that expires.

A tenant refused at its money ceiling has one thing to do about it today, and it
is to ask a person. This module is that path: a requester files a raise, an
approver decides it, and an approval becomes a GRANT -- an amount added to
`pool_granted_microusd` with an expiry, so the raise ends by itself rather than
by somebody remembering.

WHY A GRANT IS A PURE `ADD` WHILE F1'S CEILING WRITERS TAKE A CAS. F1's writers
move the BASELINE, and a baseline delta is computed from values that can move
under them, so they must check those values are still what they read. A grant
sits OUTSIDE the baseline: `pool_limit = baseline + pool_granted`, so applying
+G moves the limit by exactly G whichever baseline is in force at the instant it
commits, and revoking -G reverses it the same way. There is nothing for a CAS to
protect. That asymmetry is deliberate and it is the reason a grant composes with
a concurrent hire, a concurrent operator set and a concurrent reserve instead of
racing all three.

THE CAP IS ABSENT BY DEFAULT, AND THIS IS THE TRADE THAT BUYS. `grant_cap_microusd`
caps what approvers may grant IN AGGREGATE, and an ABSENT cap means "derived from
the baseline, evaluated now" rather than a stored default. A materialised default
would freeze at the moment it was written, so a tenant that later hired would keep
a cap sized to the baseline it had then -- quietly wrong in the direction of
refusing legitimate approvals, with nothing saying so.

The cost, stated here rather than left to be discovered: a DynamoDB condition on
a MISSING attribute fails, so the apply guard cannot be a row-side condition on
the cap. The cap is resolved CALLER-SIDE from a read of the row, and the
transaction's condition then compares the row's LIVE `pool_granted_microusd`
against that figure -- which still catches a concurrent grant, because the value
the condition sees is not the value that was read. What it does NOT catch is a
concurrent BASELINE change: a hire or an operator set landing between the read
and the commit moves the derived cap, and this approval was checked against the
old one. `grant_cap_not_exceeded` in the daily reconciler closes that window a
day late, which is the same lateness class every other check here already
accepts.

TIME IS READ THROUGH ONE NAME. Everything in this module that needs the current
instant goes through `_now_epoch()`, which is the only site that calls
`datetime.now`. `datetime` is imported as a module-level NAME rather than through
its package, so a test substitutes that one symbol and controls every rule in
here at once -- the convention `backend/tests/test_sso_replay_failclosed.py`
already established. Without it the 300-second minimum window makes the lateness
rule untestable: no test can wait five minutes, so the boundary would ship as a
guess.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from botocore.exceptions import ClientError

from dynamo import TenantsRepository
from dynamo.quota_events import (
    GRANT_ACTIVE,
    GRANT_EXPIRED,
    GRANT_REVOKE_BLOCKED,
    GRANT_REVOKED,
    MAX_REVOKE_ATTEMPTS,
    QuotaEventsRepository,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_WITHDRAWN,
    slot_date_str,
    slot_reset_at,
)
from dynamo.tenant_budgets import (
    TenantBudgetsRepository,
    budget_sk,
    current_period,
    effective_grant_cap_for_row,
    granted_microusd,
    grant_cap_microusd,
)
from limits import MAX_POOL_BUDGET_USD_CENTS

from .authz import log_audit_event, require_permission
from .deps import AuthenticatedUser
from .reserve_limits import RESERVE_LIMITS, is_grantable_wall

#: The one wall a raise can be filed against. Named from `RESERVE_LIMITS` rather
#: than spelled here, so a rename of the limit kind cannot leave this module
#: pointing at a wall that no longer exists.
POOL_WALL = "tenant_dollar_pool"

#: R11's two bounds on a grant's window. The minimum exists because a grant that
#: expires before the requester can use it delivers nothing while consuming cap
#: headroom; the maximum exists because a raise is meant to end by itself.
MIN_GRANT_WINDOW_SECONDS = 300
MAX_GRANT_WINDOW_SECONDS = 7 * 24 * 3600

#: R35. Read at request time like every other operational flag here, so turning
#: it on does not need a deploy.
QUOTA_RAISES_DISABLED_ENV = "STRATOCLAVE_QUOTA_RAISES_DISABLED"

#: The reasons a requester may give. Served from here (see `GET /me/limit-raises`)
#: rather than restated by any surface: a second copy of an enum is a second thing
#: a console can offer that the backend then refuses.
RAISE_REASON_CODES: tuple[str, ...] = (
    "onboarding", "usage_spike", "migration", "incident_response", "other",
)

#: The widest amount any approval may carry, from the same maximum the pool
#: itself is bounded by. A grant larger than the pool's own maximum would put the
#: ceiling somewhere the creation path refuses to put it.
MAX_GRANT_MICROUSD = int(MAX_POOL_BUDGET_USD_CENTS) * 10_000

# Transaction item positions. The ORDER IS A CONTRACT: a cancellation is reported
# per item, so the only way to tell an authority failure from a cap failure is to
# know which index each one is at. Named rather than counted at the call site.
_TXN_AUTHORITY = 0
_TXN_REQUEST = 1
_TXN_GRANT = 2
_TXN_POOL = 3


def _now_epoch() -> int:
    """THE current-time site for this module. Nothing else here calls
    `datetime.now` or `time.time`; substituting the module-level `datetime`
    symbol therefore controls every rule in this file at once."""
    return int(datetime.now(timezone.utc).timestamp())


def _now_iso() -> str:
    return datetime.fromtimestamp(_now_epoch(), tz=timezone.utc).isoformat()


def quota_raises_disabled() -> bool:
    return str(os.getenv(QUOTA_RAISES_DISABLED_ENV, "")).strip().lower() in (
        "1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Failures, each one carrying the code a surface renders
# ---------------------------------------------------------------------------
class GrantError(Exception):
    """A refusal with the status and machine-readable code a caller acts on.

    One exception family with a status on it, rather than `HTTPException` raised
    from the service layer: `mvp/admin_tenants.py` calls into here from a path
    that has its own idea of what a 409 means, and a service layer that raises
    transport errors cannot be called from anywhere that is not a route.
    """

    status_code = 400
    code = "grant_error"

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra

    def as_detail(self) -> dict[str, Any]:
        return {"type": self.code, "message": self.message, **self.extra}

    def as_http(self) -> HTTPException:
        return HTTPException(status_code=self.status_code, detail=self.as_detail())


class RequestNotFound(GrantError):
    status_code = 404
    code = "limit_raise_not_found"


class RequestNotPending(GrantError):
    status_code = 409
    code = "limit_raise_not_pending"


class GrantNotFound(GrantError):
    status_code = 404
    code = "limit_grant_not_found"


class UnknownLimitKind(GrantError):
    status_code = 422
    code = "unknown_limit_kind"


class UnknownReasonCode(GrantError):
    status_code = 422
    code = "unknown_reason_code"


class GrantAmountInvalid(GrantError):
    status_code = 422
    code = "grant_amount_invalid"


class GrantWindowTooShort(GrantError):
    status_code = 422
    code = "grant_window_too_short"


class GrantCapExceeded(GrantError):
    status_code = 422
    code = "grant_cap_exceeded"


class DecisionCommentRequired(GrantError):
    status_code = 422
    code = "decision_comment_required"


class PoolSuspended(GrantError):
    status_code = 422
    code = "pool_suspended"


class PoolRowMissing(GrantError):
    status_code = 422
    code = "pool_period_row_missing"


class AuthorityDenied(GrantError):
    status_code = 403
    code = "authority_denied"


class SelfApprovalRefused(GrantError):
    status_code = 403
    code = "self_approval_refused"


class KillSwitchActive(GrantError):
    status_code = 403
    code = "limit_raises_disabled"


class DailySlotOccupied(GrantError):
    status_code = 409
    code = "limit_raise_daily_slot_occupied"


class ActiveGrantsRemain(GrantError):
    status_code = 409
    code = "active_grants_remain"


# ---------------------------------------------------------------------------
# The rules, as pure functions
# ---------------------------------------------------------------------------
def period_end_epoch(period: str) -> int:
    """The last second belonging to `period` ("2026-07" -> 2026-07-31T23:59:59Z).

    Calendar arithmetic on the string alone -- no DynamoDB read, no dependence on
    the current instant -- so a surface rendering the latest permissible expiry
    computes the same number the refusal does.
    """
    year, month = (int(x) for x in period.split("-"))
    if month == 12:
        first_of_next = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        first_of_next = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return int(first_of_next.timestamp()) - 1


def latest_permissible_expiry_for_period(now_epoch: int, period: str) -> int:
    """The latest instant an approval may set a grant to expire at.

    `min(now + 7d, period end)`, and the SECOND term is load-bearing for F1
    rather than for tidiness. The period rollover resets `pool_granted_microusd`
    by omission, which is safe ONLY because no grant outlives the period it was
    granted in: a grant allowed past the boundary would have its capacity
    destroyed by the reset on the 1st, silently, on every granted row at once.
    If this pin is ever loosened, `dynamo.pool_row_schema`'s classification of
    `pool_granted_microusd` has to change with it.

    Expressed once, here, and called both by the refusal and by anything that
    displays the bound -- two independent calendar computations of one rule drift.
    """
    return min(int(now_epoch) + MAX_GRANT_WINDOW_SECONDS, period_end_epoch(period))


def is_capacity_bearing(status: str) -> bool:
    """Does a grant in `status` currently contribute to `pool_granted_microusd`?

    THE ONE DEFINITION. F1's reconciler and the grant inventory both call this
    rather than restating the rule, and it is deliberately not re-exported from
    `dynamo.quota_events`: three independent statements of a lifecycle rule drift,
    and a second import path for a predicate whose whole job is to be the single
    source of a fact is the same defect as a second copy of it.

    `REVOKE_BLOCKED` counts, and that is the part worth stating. Its subtraction
    never committed, so the pool row is still carrying its amount -- honestly. A
    predicate that excluded it would make every row holding a blocked grant read
    as drifting, for as long as the fault lasted, which is exactly when an
    operator needs the reconciler to be quiet about everything else.
    """
    return str(status) in (GRANT_ACTIVE, GRANT_REVOKE_BLOCKED)


# --- the two wall vocabularies, and the total function between them ---------
#
# `RESERVE_LIMITS` names the walls the money path enforces, and those names are
# shipped: they sit on the admission path and renaming them is not this change's
# business. A refusal, though, is read by a client, and a client needs to know
# WHICH per-model quota refused -- the tenant's or the user's -- because those
# have different answers. So the public name is a projection of the internal one,
# and `per_model_quota` projects to more than one value.
#
# The projection is a FUNCTION rather than a table copied on both sides, and it is
# TOTAL over `RESERVE_LIMITS`: a fourth limit kind added later with no blocker
# raises here rather than producing a refusal with a blank or invented one.
_BLOCKER_BY_WALL: dict[str, str] = {
    "tenant_dollar_pool": "tenant_pool",
    "user_token_quota": "personal_budget",
}

#: The per-model wall's two public names, chosen by which scope actually refused.
_PER_MODEL_BLOCKER_BY_SCOPE: dict[str, str] = {
    "tenant_quota": "per_model_tenant",
    "user_quota": "per_model_user",
}

#: What the per-model wall projects to when the refusal cannot say which scope it
#: was. Reachable in exactly one situation: a cascade in which different
#: candidates were refused by different scopes, so there is no single true answer.
#: A distinct value rather than a guess, because naming the wrong scope sends a
#: reader to the wrong quota row, and collapsing the two would hide that the
#: gateway did not know.
PER_MODEL_BLOCKER_UNKNOWN_SCOPE = "per_model_scope_unknown"


def blocker_for_wall(wall: str, *, quota_scope: Optional[str] = None) -> str:
    """The public name of the wall that refused.

    Total over `RESERVE_LIMITS` by construction: an unmapped wall raises, so a
    limit kind added without a public name fails at the refusal it would
    otherwise describe wrongly.
    """
    if wall == "per_model_quota":
        if quota_scope is None:
            return PER_MODEL_BLOCKER_UNKNOWN_SCOPE
        try:
            return _PER_MODEL_BLOCKER_BY_SCOPE[str(quota_scope)]
        except KeyError:
            raise KeyError(
                f"per-model quota scope {quota_scope!r} has no public blocker "
                f"name; the scopes are {sorted(_PER_MODEL_BLOCKER_BY_SCOPE)}"
            ) from None
    try:
        return _BLOCKER_BY_WALL[wall]
    except KeyError:
        raise KeyError(
            f"wall {wall!r} has no public blocker name. Every entry in "
            f"mvp.reserve_limits.RESERVE_LIMITS must have one, or a refusal "
            f"describes itself to a client with a name the client cannot read."
        ) from None


def blocker_names() -> frozenset[str]:
    """Every public blocker name this deployment can emit, derived from the
    mapping rather than listed, so the set cannot fall behind it."""
    out = set(_BLOCKER_BY_WALL.values()) | set(_PER_MODEL_BLOCKER_BY_SCOPE.values())
    out.add(PER_MODEL_BLOCKER_UNKNOWN_SCOPE)
    return frozenset(out)


def unmapped_walls() -> tuple[str, ...]:
    """Every wall in `RESERVE_LIMITS` with no public blocker name.

    The closed-world half of the mapping, in the shape a check can ask: non-empty
    means a refusal exists that cannot name itself to a client.
    """
    missing: list[str] = []
    for kind in RESERVE_LIMITS:
        try:
            blocker_for_wall(kind.name)
        except KeyError:
            missing.append(kind.name)
    return tuple(sorted(missing))


# ---------------------------------------------------------------------------
# The hint a 402 carries, in its FINAL shape with degenerate content
# ---------------------------------------------------------------------------
class RaiseHintCandidate(BaseModel):
    """One wall a raise could address.

    Under this change the list holds exactly ONE element -- the wall that
    actually refused -- and `shortfall_microusd` is left unset. That is not a
    stub: at the instant of a pool refusal exactly one candidate has been priced,
    because the routing cascade leaves on a pool refusal and the untried tail is
    priced only after a hold commits. A hint carrying four shortfalls would
    describe measurements nobody took.
    """

    model_config = ConfigDict(extra="forbid")

    blocker: str
    wall: str
    model: Optional[str] = None
    shortfall_microusd: Optional[int] = None


class RaiseHint(BaseModel):
    """What a 402 tells a client about asking for more.

    Shipped in its FINAL shape now, with one candidate, so that filling it later
    -- more candidates, populated pricing -- is an append rather than a second
    wire change. Two changes to one field across two releases, with clients in
    between, is not a thing this deployment does.
    """

    model_config = ConfigDict(extra="forbid")

    candidates: list[RaiseHintCandidate]
    #: What is left of the tenant's aggregate cap. Carried because without it a
    #: surface can pre-fill an amount no approver is permitted to grant, and the
    #: requester spends a day of latency discovering that from a `422`.
    remaining_cap_microusd: int
    #: The reasons the submit endpoint accepts, so a surface offering a raise
    #: does not have to carry its own copy of the enum.
    reason_codes: list[str] = Field(default_factory=lambda: list(RAISE_REASON_CODES))


# ---------------------------------------------------------------------------
# The cap, resolved
# ---------------------------------------------------------------------------
def effective_grant_cap_microusd(tenant_id: str, period: str) -> int:
    """The aggregate cap in force for `(tenant_id, period)`, evaluated NOW.

    The stored `grant_cap_microusd` when the row carries one, else the row's own
    baseline. Consistent read, never cached and never written back: a cached or
    materialised answer is a figure that stops tracking the thing it was derived
    from, which is the whole reason absence is the default.

    Zero when the tenant has no pool row for the period -- there is no baseline
    to derive from, so nothing may be granted. The refusing direction, and the
    approval path refuses earlier with a clearer reason.
    """
    row = TenantBudgetsRepository().get(tenant_id, period, consistent_read=True)
    if row is None:
        return 0
    return effective_grant_cap_for_row(row)


def raise_hint_for_pool_row(row: Optional[dict[str, Any]]) -> RaiseHint:
    """Build the hint for a pool refusal from the row the refusal already read.

    Takes the ROW rather than a tenant and a period, so the refusal path adds no
    DynamoDB call: the caller has the row in hand because it just failed a
    condition against it. `remaining_cap_microusd` is floored at zero, so a cap
    lowered below what is already granted reports no room rather than a negative
    figure a surface would render as an amount to ask for.
    """
    granted = granted_microusd(row or {})
    cap = effective_grant_cap_for_row(row or {})
    return RaiseHint(
        candidates=[RaiseHintCandidate(
            blocker=blocker_for_wall(POOL_WALL), wall=POOL_WALL)],
        remaining_cap_microusd=max(0, cap - granted),
    )


# ---------------------------------------------------------------------------
# Authority, as an item inside the transaction
# ---------------------------------------------------------------------------
def _authority_condition_check_item(
    *, actor: AuthenticatedUser, tenant_id: str, as_owner: bool
) -> dict[str, Any]:
    """The approver's authority, as a `ConditionCheck` in the same transaction as
    the money.

    A route dependency proves the caller held the permission when the request
    ARRIVED. This proves they still hold the authority at the instant the grant
    commits, which is a different claim and the one that matters: a permission
    revoked mid-flight cancels the whole transaction, so there is no window in
    which capacity is granted on authority somebody has already lost.

    The tenant is the one read from the request or grant ROW, never a value the
    caller supplied. A flat permission is deployment-global at the write path, so
    without this binding a permission-holder who owns tenant A could approve a
    raise for tenant B and the write path would have nothing to say about it.
    That is why this is security-critical rather than defensive.

    The table name comes from a `TenantsRepository` instance rather than from a
    second lookup, so the row the condition is evaluated against is by
    construction the row the authority was read from. (`dynamo/tenants.py`
    resolves its own name from an inline environment read and exposes no lookup
    beside `tenant_budgets_table_name()`; adding one would mean editing a module
    this change does not own, so this reaches the handle it already has.)
    """
    tenants_table = TenantsRepository()._table.name
    if as_owner:
        return {
            "ConditionCheck": {
                "TableName": tenants_table,
                "Key": {"tenant_id": {"S": tenant_id}},
                "ConditionExpression": "team_lead_user_id = :actor",
                "ExpressionAttributeValues": {":actor": {"S": actor.user_id}},
            }
        }
    # A global approver's authority is not tenant-scoped, so the check confirms
    # the tenant still exists. It is NOT a no-op: a tenant deleted between the
    # read and the commit would otherwise take a grant pinned to a row nobody
    # will ever reconcile.
    return {
        "ConditionCheck": {
            "TableName": tenants_table,
            "Key": {"tenant_id": {"S": tenant_id}},
            "ConditionExpression": "attribute_exists(tenant_id)",
        }
    }


def _cancellation_codes(exc: ClientError) -> list[str]:
    return [str(r.get("Code", ""))
            for r in (exc.response.get("CancellationReasons", []) or [])]


def _is_transaction_cancelled(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "TransactionCanceledException"


def _ccf_at(codes: list[str], index: int) -> bool:
    return len(codes) > index and codes[index] == "ConditionalCheckFailed"


# ---------------------------------------------------------------------------
# Submitting a raise, and the daily slot that is also the token's anchor
# ---------------------------------------------------------------------------
def _request_public(item: dict[str, Any]) -> dict[str, Any]:
    """A request row as a caller sees it.

    `comment` and the client token are DELIBERATELY ABSENT. The comment is the
    requester's own prose about their tenant's spending and the token is a
    caller-supplied key; neither belongs in a body a surface may log, and R13's
    rule is easier to keep by never putting them in the projection than by
    remembering to strip them at each sink.
    """
    out: dict[str, Any] = {
        "request_id": str(item.get("request_id") or ""),
        "tenant_id": str(item.get("tenant_id") or ""),
        "user_id": str(item.get("user_id") or ""),
        "status": str(item.get("status") or ""),
        "limit_kind": str(item.get("limit_kind") or ""),
        "reason_code": str(item.get("reason_code") or ""),
        "asked_amount_microusd": int(item.get("asked_amount_microusd", 0)),
        "created_at": str(item.get("created_at") or ""),
    }
    for name in ("decided_at", "decided_by", "grant_id"):
        if item.get(name) is not None:
            out[name] = str(item[name])
    # The DECISION's comment is returned, and the requester's is not. They are
    # different facts: a decision's reason is addressed TO the requester and is
    # the whole point of R26, while the request's own justification is hers
    # already and only adds a place for it to leak.
    if item.get("decision_comment") is not None:
        out["decision_comment"] = str(item["decision_comment"])
    if item.get("approved_amount_microusd") is not None:
        out["approved_amount_microusd"] = int(item["approved_amount_microusd"])
    if item.get("expires_at") is not None:
        out["expires_at"] = int(item["expires_at"])
    return out


def _grant_public(item: dict[str, Any]) -> dict[str, Any]:
    """A grant row as a caller sees it.

    The amount is `approved_amount_microusd` and there is no shorter synonym. The
    row records both what was asked and what was approved, and a reader of
    `amount_microusd` cannot tell which they have -- which is the requester's
    central grievance in this feature, since she is told APPROVED and then plans
    against her own figure.
    """
    out: dict[str, Any] = {
        "grant_id": str(item.get("grant_id") or ""),
        "tenant_id": str(item.get("tenant_id") or ""),
        "request_id": str(item.get("request_id") or ""),
        "status": str(item.get("status") or ""),
        "approved_amount_microusd": int(item.get("approved_amount_microusd", 0)),
        "expires_at": int(item.get("expires_at", 0)),
        "period": str(item.get("period") or ""),
        "target_pk": str(item.get("target_pk") or ""),
        "target_sk": str(item.get("target_sk") or ""),
        "approver_user_id": str(item.get("approver_user_id") or ""),
        "created_at": str(item.get("created_at") or ""),
        "capacity_bearing": is_capacity_bearing(str(item.get("status") or "")),
        # A per-grant flag and its reason, not only the metric. The metric is how
        # an operator learns THAT something is stuck; these are how they learn
        # WHICH, and a count cannot answer that. Written in the same update as the
        # status so the two cannot disagree.
        "revoke_blocked": bool(item.get("revoke_blocked", False)),
        "revoke_attempts": int(item.get("revoke_attempts", 0)),
    }
    for name in ("revoked_at", "revoked_by", "revoke_reason",
                 "revoke_blocked_reason", "blocked_at"):
        if item.get(name) is not None:
            out[name] = str(item[name])
    return out


def _slot_holder_is_still_holding(
    repo: QuotaEventsRepository, slot: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """The request today's slot is held by, IF it is still holding it.

    None means the slot is free to reclaim. Freeing is lazy rather than swept for
    a reason: a sweeper would have to visit every slot of every user every day to
    find the few that were decided, and the only caller that cares is the next
    submission, which is already reading the slot.

    Four fates free it -- withdrawn, rejected, a request that does not exist (a
    crash between claiming the slot and writing the row), and an approval whose
    grant has stopped bearing capacity. One does not: `REVOKE_BLOCKED` is a grant
    still holding its share of the tenant's ceiling, so treating it as finished
    would let a second raise stack on top of capacity nobody has given back.
    """
    holder_id = str(slot.get("request_id") or "")
    if not holder_id:
        return None
    request = repo.get_request(holder_id)
    if request is None:
        return None
    status = str(request.get("status") or "")
    if status in (STATUS_WITHDRAWN, STATUS_REJECTED):
        return None
    if status == STATUS_PENDING:
        return request
    if status == STATUS_APPROVED:
        grant_id = str(request.get("grant_id") or "")
        tenant_id = str(request.get("tenant_id") or "")
        if not grant_id:
            return None
        grant = repo.get_grant(tenant_id=tenant_id, grant_id=grant_id)
        if grant is None:
            return None
        return request if is_capacity_bearing(str(grant.get("status") or "")) else None
    return request


def submit_limit_raise(
    *, actor: AuthenticatedUser, asked_amount_microusd: int, reason_code: str,
    client_token: str, limit_kind: str = POOL_WALL, comment: Optional[str] = None,
) -> dict[str, Any]:
    """File a raise against the caller's own tenant's money ceiling.

    The tenant is the caller's, read from their session. It is not a body field:
    a requester asking for a raise on a tenant they are not in is not a request
    this endpoint has any way to authorise, and accepting the field would put
    that decision on the approver instead of on the gate.
    """
    if quota_raises_disabled():
        raise KillSwitchActive(
            "Limit raises are disabled on this deployment. Grants already live "
            "keep their capacity until they expire; no new request or approval is "
            "accepted while the switch is set.")

    wall = str(limit_kind)
    known = {k.name for k in RESERVE_LIMITS}
    if wall not in known or not is_grantable_wall(wall):
        # One code for both "no such wall" and "a wall that exists and cannot be
        # raised", because the answer a reader needs is the same: this is not a
        # thing a money raise can address. The body says which wall and whether it
        # is grantable, so the reader is not left guessing which of the two it hit.
        raise UnknownLimitKind(
            f"{wall!r} is not a limit a money raise can address. Only "
            f"{POOL_WALL} is grantable; the token quota and the per-model quota "
            f"are refused here rather than turned into a request no approver "
            f"could act on.",
            limit_kind=wall,
            grantable=wall in known and is_grantable_wall(wall),
            known=sorted(known),
        )

    if str(reason_code) not in RAISE_REASON_CODES:
        raise UnknownReasonCode(
            f"{reason_code!r} is not one of the accepted reasons.",
            reason_codes=list(RAISE_REASON_CODES))

    asked = int(asked_amount_microusd)
    if asked <= 0 or asked > MAX_GRANT_MICROUSD:
        raise GrantAmountInvalid(
            f"An asked amount must be greater than zero and no more than "
            f"{MAX_GRANT_MICROUSD} micro-USD.",
            asked_amount_microusd=asked, maximum_microusd=MAX_GRANT_MICROUSD)

    tenant_id = actor.org_id
    repo = QuotaEventsRepository()
    now = _now_epoch()
    date_str = slot_date_str(now)

    for _attempt in range(2):
        slot = repo.get_slot(
            user_id=actor.user_id, tenant_id=tenant_id, date_str=date_str)
        if slot is not None:
            if str(slot.get("client_token") or "") == str(client_token):
                # The same token twice is the same submission. Return what it
                # produced rather than a second request: the slot IS the record of
                # what was admitted today, so idempotency and the daily cap are
                # answered by one row instead of two that can disagree.
                existing = repo.get_request(str(slot.get("request_id") or ""))
                if existing is not None:
                    return _request_public(existing)
                # The slot points at nothing -- a crash between claiming it and
                # writing the row. Reclaim and let this call complete what the
                # last one started.
                repo.delete_slot(
                    user_id=actor.user_id, tenant_id=tenant_id, date_str=date_str)
                continue
            holder = _slot_holder_is_still_holding(repo, slot)
            if holder is not None:
                raise DailySlotOccupied(
                    "You have already filed a limit raise today and it has not "
                    "been resolved yet.",
                    holder_request_id=str(holder.get("request_id") or ""),
                    holder_status=str(holder.get("status") or ""),
                    reset_at=slot_reset_at(date_str))
            repo.delete_slot(
                user_id=actor.user_id, tenant_id=tenant_id, date_str=date_str)

        request_id = f"lr_{uuid.uuid4().hex}"
        if not repo.put_slot_if_absent(
                user_id=actor.user_id, tenant_id=tenant_id, date_str=date_str,
                client_token=str(client_token), request_id=request_id):
            continue  # somebody claimed it between the read and the write
        # The slot is claimed BEFORE the request row exists, and that order is
        # deliberate: the reverse leaves an orphan request row whenever the slot
        # claim loses, and an orphan request is a thing an approver can see and
        # act on. A slot pointing at a row that does not exist is self-healing --
        # the next submission reads the fate as "nothing is holding capacity".
        item = repo.put_request(
            request_id=request_id, tenant_id=tenant_id, user_id=actor.user_id,
            asked_amount_microusd=asked, reason_code=str(reason_code),
            comment=comment, limit_kind=wall, created_at=_now_iso())
        log_audit_event(
            event="limit_raise_requested", actor_id=actor.user_id,
            actor_email=actor.email, target_id=request_id,
            target_type="limit_raise", tenant_id=tenant_id,
            after={"asked_amount_microusd": asked, "reason_code": str(reason_code),
                   "limit_kind": wall})
        return _request_public(item)

    raise DailySlotOccupied(
        "Another submission claimed today's slot while this one was being "
        "written. Retry.",
        holder_request_id="", reset_at=slot_reset_at(date_str))


def withdraw_limit_raise(*, actor: AuthenticatedUser, request_id: str) -> dict[str, Any]:
    """Take back one's own PENDING request.

    Withdrawing frees the day's slot, through the same lazy reclaim every other
    decided fate uses: the slot is not deleted here, it simply stops being held,
    and the next submission is what notices. Deleting it here would work too and
    would be a second place that knows the freeing rule.
    """
    repo = QuotaEventsRepository()
    request = repo.get_request(request_id)
    if request is None or str(request.get("user_id") or "") != actor.user_id:
        # A request belonging to somebody else is reported as absent rather than
        # forbidden, on the same enumeration-defence convention every tenant-scoped
        # read in this codebase uses.
        raise RequestNotFound(f"No limit raise {request_id!r}.")
    if str(request.get("status") or "") != STATUS_PENDING:
        raise RequestNotPending(
            f"This request is {request.get('status')}, so there is nothing to "
            f"withdraw.", status=str(request.get("status") or ""))
    now_iso = _now_iso()
    try:
        repo.transact_write([repo.decide_request_txn_item(
            request_id=request_id, to_status=STATUS_WITHDRAWN,
            decided_by=actor.user_id, decided_at=now_iso,
            read_revision=int(request.get("revision", 1)))])
    except ClientError as exc:
        if _is_transaction_cancelled(exc) and _ccf_at(_cancellation_codes(exc), 0):
            raise RequestNotPending(
                "This request was decided while the withdrawal was in flight.",
                status="") from None
        raise
    log_audit_event(
        event="limit_raise_withdrawn", actor_id=actor.user_id,
        actor_email=actor.email, target_id=request_id,
        target_type="limit_raise", tenant_id=str(request.get("tenant_id") or ""))
    return _request_public(repo.get_request(request_id) or {})


# ---------------------------------------------------------------------------
# Deciding a raise
# ---------------------------------------------------------------------------
def _pending_request_for_decision(
    repo: QuotaEventsRepository, *, actor: AuthenticatedUser, request_id: str
) -> dict[str, Any]:
    request = repo.get_request(request_id)
    if request is None:
        raise RequestNotFound(f"No limit raise {request_id!r}.")
    if str(request.get("status") or "") != STATUS_PENDING:
        raise RequestNotPending(
            f"This request is already {request.get('status')}.",
            status=str(request.get("status") or ""))
    if str(request.get("user_id") or "") == actor.user_id:
        # A raise a person approves for themselves is not a control. It matters
        # most for the tenant owner, who holds the approval authority for their
        # own tenant and would otherwise be the one path in this feature that
        # never expires.
        raise SelfApprovalRefused(
            "A limit raise cannot be approved or rejected by the person who "
            "filed it.")
    return request


def approve_limit_raise(
    *, actor: AuthenticatedUser, request_id: str, approved_amount_microusd: int,
    expires_at: int, decision_comment: Optional[str] = None,
    as_owner: bool = False,
) -> dict[str, Any]:
    """Approve a raise: create a grant and apply it to the pool, atomically.

    Four writes commit together or not at all -- the approver's authority, the
    request's decision, the grant row, and the pool's three attributes. The
    reason it is one transaction rather than four ordered writes is that every
    partial outcome is worse than a refusal: capacity granted with no grant record
    is capacity nothing will ever revoke, and a grant record with no capacity is a
    requester told yes who is still refused.
    """
    if quota_raises_disabled():
        raise KillSwitchActive(
            "Limit raises are disabled on this deployment. Grants already live "
            "keep their capacity until they expire.")

    repo = QuotaEventsRepository()
    request = _pending_request_for_decision(
        repo, actor=actor, request_id=request_id)
    tenant_id = str(request.get("tenant_id") or "")
    asked = int(request.get("asked_amount_microusd", 0))

    amount = int(approved_amount_microusd)
    # R3 is a PYTHON guard and not a DynamoDB condition, and that is the whole
    # requirement rather than a style choice: `ADD` with a negative number is not
    # floored, so a negative amount reaching the transaction would LOWER
    # `pool_limit` and `pool_headroom` -- an approval that silently cut the
    # tenant's ceiling, recorded as a grant. There is no row-side condition that
    # catches it, so this is the only guard there is.
    if amount <= 0 or amount > MAX_GRANT_MICROUSD:
        raise GrantAmountInvalid(
            f"An approved amount must be greater than zero and no more than "
            f"{MAX_GRANT_MICROUSD} micro-USD. A zero or negative amount is not a "
            f"smaller grant: DynamoDB does not floor a negative ADD, so it would "
            f"lower the tenant's ceiling.",
            approved_amount_microusd=amount, maximum_microusd=MAX_GRANT_MICROUSD)
    if amount > asked:
        raise GrantAmountInvalid(
            "An approval cannot exceed the amount that was asked for.",
            approved_amount_microusd=amount, asked_amount_microusd=asked)

    # The period the grant will target, and therefore the period whose end bounds
    # its window. Resolved before anything is read so R11's refusal does not depend
    # on the pool row existing: a caller who asked for an impossible expiry has the
    # same thing to fix whether or not the tenant is pooled.
    period = current_period()

    now = _now_epoch()
    ceiling = latest_permissible_expiry_for_period(now, period)
    earliest = now + MIN_GRANT_WINDOW_SECONDS
    expires = int(expires_at)
    if ceiling < earliest:
        raise GrantWindowTooShort(
            f"There are fewer than {MIN_GRANT_WINDOW_SECONDS} seconds left in "
            f"{period}, and a grant may not outlive the period it was granted in "
            f"-- so no expiry is satisfiable for this request today.",
            earliest_permissible=earliest, latest_permissible=ceiling,
            period=period)
    if expires < earliest or expires > ceiling:
        raise GrantWindowTooShort(
            f"A grant must expire at least {MIN_GRANT_WINDOW_SECONDS} seconds "
            f"from now and no later than the end of {period}.",
            requested_expires_at=expires, earliest_permissible=earliest,
            latest_permissible=ceiling, period=period)

    # R26: a decision that gives less than was asked has to say why. The full
    # amount does not, because "yes" needs no explanation; anything less is a
    # number the requester did not choose and cannot plan against without one.
    #
    # ORDER MATTERS HERE and it is worth stating. Every guard above this point is a
    # property of the caller's own input; everything below it needs the tenant's
    # stored state. Checking them in that order means a caller fixing one refusal
    # at a time is never told about a stale figure while their own request is still
    # malformed -- and, more usefully, that the refusal a caller gets does not
    # depend on which of two unrelated mistakes the code happened to look at first.
    if amount < asked and not (decision_comment or "").strip():
        raise DecisionCommentRequired(
            "Approving for less than was asked requires a comment saying why, "
            "because the requester is otherwise given a figure she did not "
            "choose and no way to find out how it was arrived at.")

    budgets = TenantBudgetsRepository()
    # Lazily roll the period forward if the scheduled job has not reached this
    # tenant yet. F1 already owns this mechanism and calls it from the seat-delta
    # path for the same reason: an approval at five past midnight on the 1st
    # should not fail because a daily job has not run.
    row = budgets.ensure_current_period_row(tenant_id=tenant_id, period=period)
    if row is None:
        raise PoolRowMissing(
            f"Tenant {tenant_id} has no pool row for {period}, so there is no "
            f"ceiling to raise and no baseline to derive a cap from.",
            tenant_id=tenant_id, period=period)

    # B6/R28. A grant applied to a suspended pool ticks toward its expiry
    # delivering nothing -- every request is refused on `status` regardless of
    # headroom -- while consuming the tenant's cap headroom for the whole window.
    # The refusal is here, server-side, rather than left to a surface: a surface
    # that has not shipped yet refuses nothing.
    if str(row.get("status", "active")) != "active":
        raise PoolSuspended(
            f"Tenant {tenant_id}'s pool is {row.get('status')} for {period}. A "
            f"grant applied now would expire without admitting a single request "
            f"while holding the tenant's cap headroom.",
            tenant_id=tenant_id, period=period,
            status=str(row.get("status") or ""))

    # B1's caller-side cap. The condition sent to DynamoDB compares the row's
    # LIVE granted sum against `cap - amount`, so a concurrent approval that
    # already moved it is caught at commit even though the cap itself was resolved
    # from a read.
    cap = effective_grant_cap_for_row(row)
    granted_read = granted_microusd(row)
    cap_minus_amount = cap - amount
    if cap_minus_amount < 0:
        # This grant alone exceeds the whole cap, so no value of the live granted
        # sum could satisfy the condition. Refused here rather than sent, because
        # a condition that cannot be met is an error message worth writing.
        raise GrantCapExceeded(
            f"An approval of {amount} micro-USD exceeds this tenant's entire "
            f"aggregate grant cap of {cap}.",
            grant_cap_microusd=cap, pool_granted_microusd=granted_read,
            approved_amount_microusd=amount,
            remaining_cap_microusd=max(0, cap - granted_read),
            cap_is_derived=grant_cap_microusd(row) is None)

    grant_id = f"lg_{uuid.uuid4().hex}"
    now_iso = _now_iso()
    target_pk = str(row.get("tenant_id") or tenant_id)
    target_sk = str(row.get("sk") or budget_sk(period))
    items = [
        _authority_condition_check_item(
            actor=actor, tenant_id=tenant_id, as_owner=as_owner),
        repo.decide_request_txn_item(
            request_id=request_id, to_status=STATUS_APPROVED,
            decided_by=actor.user_id, decided_at=now_iso,
            read_revision=int(request.get("revision", 1)),
            decision_comment=decision_comment,
            approved_amount_microusd=amount, grant_id=grant_id,
            expires_at_epoch=expires),
        repo.grant_put_txn_item(
            tenant_id=tenant_id, grant_id=grant_id, request_id=request_id,
            approver_user_id=actor.user_id, approved_amount_microusd=amount,
            expires_at_epoch=expires, target_pk=target_pk, target_sk=target_sk,
            period=period, created_at=now_iso),
        budgets.grant_apply_txn_item(
            target_pk=target_pk, target_sk=target_sk,
            approved_amount_microusd=amount, cap_minus_amount=cap_minus_amount),
    ]
    try:
        repo.transact_write(items)
    except ClientError as exc:
        if not _is_transaction_cancelled(exc):
            raise
        codes = _cancellation_codes(exc)
        if _ccf_at(codes, _TXN_AUTHORITY):
            raise AuthorityDenied(
                "The authority to approve raises for this tenant was not held at "
                "the instant this grant would have committed.",
                tenant_id=tenant_id) from None
        if _ccf_at(codes, _TXN_REQUEST):
            raise RequestNotPending(
                "This request was decided by somebody else while this approval "
                "was in flight.", status="") from None
        if _ccf_at(codes, _TXN_POOL):
            # Re-read so the figures reported are the ones that refused it, not
            # the ones this call started from.
            live = budgets.get_by_key(target_pk, target_sk) or {}
            live_granted = granted_microusd(live)
            live_cap = effective_grant_cap_for_row(live)
            raise GrantCapExceeded(
                f"This tenant has {live_granted} micro-USD granted against an "
                f"aggregate cap of {live_cap}; approving {amount} more would "
                f"exceed it.",
                grant_cap_microusd=live_cap,
                pool_granted_microusd=live_granted,
                approved_amount_microusd=amount,
                remaining_cap_microusd=max(0, live_cap - live_granted),
                cap_is_derived=grant_cap_microusd(live) is None) from None
        raise

    log_audit_event(
        event="limit_raise_approved", actor_id=actor.user_id,
        actor_email=actor.email, target_id=request_id,
        target_type="limit_raise", tenant_id=tenant_id,
        after={"grant_id": grant_id, "approved_amount_microusd": amount,
               "asked_amount_microusd": asked, "expires_at": expires,
               "period": period, "target_sk": target_sk,
               "approved_for_less": amount < asked})
    return {
        "request": _request_public(repo.get_request(request_id) or {}),
        "grant": _grant_public(
            repo.get_grant(tenant_id=tenant_id, grant_id=grant_id) or {}),
    }


def reject_limit_raise(
    *, actor: AuthenticatedUser, request_id: str, decision_comment: str,
    as_owner: bool = False,
) -> dict[str, Any]:
    """Reject a raise, with a reason the requester is given.

    The comment is required and not optional, because a rejection with no reason
    is indistinguishable from the feature being broken -- and it frees the day's
    slot, so the requester's next move is to file a better request rather than to
    wait for one they cannot see the fate of.

    The authority check rides in the transaction here too, even though no money
    moves. A rejection consumes the requester's day and is recorded against the
    approver's name, so it is a decision somebody has to still be entitled to make
    at the instant it lands.
    """
    if quota_raises_disabled():
        raise KillSwitchActive(
            "Limit raises are disabled on this deployment.")
    if not (decision_comment or "").strip():
        raise DecisionCommentRequired(
            "A rejection requires a comment. Without one the requester cannot "
            "tell a considered refusal from a broken feature, and has nothing to "
            "act on either way.")
    repo = QuotaEventsRepository()
    request = _pending_request_for_decision(
        repo, actor=actor, request_id=request_id)
    tenant_id = str(request.get("tenant_id") or "")
    now_iso = _now_iso()
    items = [
        _authority_condition_check_item(
            actor=actor, tenant_id=tenant_id, as_owner=as_owner),
        repo.decide_request_txn_item(
            request_id=request_id, to_status=STATUS_REJECTED,
            decided_by=actor.user_id, decided_at=now_iso,
            read_revision=int(request.get("revision", 1)),
            decision_comment=decision_comment),
    ]
    try:
        repo.transact_write(items)
    except ClientError as exc:
        if not _is_transaction_cancelled(exc):
            raise
        codes = _cancellation_codes(exc)
        if _ccf_at(codes, _TXN_AUTHORITY):
            raise AuthorityDenied(
                "The authority to decide raises for this tenant was not held at "
                "the instant this rejection would have committed.",
                tenant_id=tenant_id) from None
        if _ccf_at(codes, _TXN_REQUEST):
            raise RequestNotPending(
                "This request was decided by somebody else while this rejection "
                "was in flight.", status="") from None
        raise
    log_audit_event(
        event="limit_raise_rejected", actor_id=actor.user_id,
        actor_email=actor.email, target_id=request_id,
        target_type="limit_raise", tenant_id=tenant_id)
    return _request_public(repo.get_request(request_id) or {})


# ---------------------------------------------------------------------------
# Ending a grant: early by a person, or on time by the sweep
# ---------------------------------------------------------------------------
def _revoke_txn_items(
    *, repo: QuotaEventsRepository, budgets: TenantBudgetsRepository,
    grant: dict[str, Any], to_status: str, revoked_by: str, revoked_at: str,
    revoke_reason: Optional[str] = None,
) -> list[dict[str, Any]]:
    """The two fragments that end a grant and give its capacity back.

    ONE builder for both endings, differing only in `to_status` and who did it.
    An early revoke and an expiry are the same money move made for different
    reasons, and two builders would be two places for the subtraction to be
    written -- which is how a revoke that moves two of the three attributes
    happens.

    The amount comes from the grant ROW, and the grant's own condition pins it:
    the pool is decremented by exactly the figure the row still holds, so a grant
    mutated between the read and the write cannot have a stale amount subtracted
    for it.
    """
    amount = int(grant.get("approved_amount_microusd", 0))
    return [
        repo.grant_terminal_txn_item(
            tenant_id=str(grant.get("tenant_id") or ""),
            grant_id=str(grant.get("grant_id") or ""),
            to_status=to_status, approved_amount_read=amount,
            revoked_by=revoked_by, revoked_at=revoked_at,
            revoke_reason=revoke_reason),
        budgets.grant_revoke_txn_item(
            target_pk=str(grant.get("target_pk") or ""),
            target_sk=str(grant.get("target_sk") or ""),
            approved_amount_microusd=amount),
    ]


#: Where the grant's own condition sits in `_revoke_txn_items`. A cancellation at
#: this index means somebody else already ended this grant, which is a SUCCESS
#: from the caller's point of view; anywhere else means nothing committed.
_REVOKE_GRANT_INDEX = 0
_REVOKE_POOL_INDEX = 1


def revoke_grant(
    *, actor: AuthenticatedUser, tenant_id: str, grant_id: str,
    reason: Optional[str] = None, as_owner: bool = False,
) -> dict[str, Any]:
    """End a live grant early, by the authority entitled to have approved it.

    Not gated by the kill switch, deliberately. The switch exists to stop new
    capacity being granted; this only ever gives capacity back, so refusing it
    while the switch is set would leave an operator unable to undo the thing they
    turned the switch on about.
    """
    repo = QuotaEventsRepository()
    budgets = TenantBudgetsRepository()
    grant = repo.get_grant(tenant_id=tenant_id, grant_id=grant_id)
    if grant is None:
        raise GrantNotFound(f"No grant {grant_id!r} for tenant {tenant_id!r}.")
    status = str(grant.get("status") or "")
    if status != GRANT_ACTIVE:
        if status == GRANT_REVOKE_BLOCKED:
            # A blocked grant is exactly what a person is sent here to repair, so
            # a retry is the point rather than an error. Clear the block and let
            # the ordinary path try again.
            repo.clear_revoke_block(tenant_id=tenant_id, grant_id=grant_id)
            grant = repo.get_grant(tenant_id=tenant_id, grant_id=grant_id) or grant
            status = str(grant.get("status") or "")
        if status != GRANT_ACTIVE:
            raise RequestNotPending(
                f"This grant is {status}; there is no live capacity to give back.",
                status=status)

    # The tenant bound into the authority check is the one read FROM THE GRANT ROW,
    # never the path or query value the caller supplied. A caller naming a tenant
    # they do not own simply finds no grant at that key; a caller naming one they
    # do own still has the check evaluated against the row's own tenant.
    row_tenant = str(grant.get("tenant_id") or tenant_id)
    now_iso = _now_iso()
    items = [
        _authority_condition_check_item(
            actor=actor, tenant_id=row_tenant, as_owner=as_owner),
        *_revoke_txn_items(
            repo=repo, budgets=budgets, grant=grant, to_status=GRANT_REVOKED,
            revoked_by=actor.user_id, revoked_at=now_iso, revoke_reason=reason),
    ]
    try:
        repo.transact_write(items)
    except ClientError as exc:
        if not _is_transaction_cancelled(exc):
            raise
        codes = _cancellation_codes(exc)
        if _ccf_at(codes, 0):
            raise AuthorityDenied(
                "The authority to revoke grants for this tenant was not held at "
                "the instant the revocation would have committed.",
                tenant_id=row_tenant) from None
        if _ccf_at(codes, 1 + _REVOKE_GRANT_INDEX):
            raise RequestNotPending(
                "This grant was ended by somebody else while the revocation was "
                "in flight. Its capacity was given back once, not twice.",
                status="") from None
        raise
    log_audit_event(
        event="limit_grant_revoked", actor_id=actor.user_id,
        actor_email=actor.email, target_id=grant_id, target_type="limit_grant",
        tenant_id=row_tenant,
        after={"approved_amount_microusd": int(
                   grant.get("approved_amount_microusd", 0)),
               "period": str(grant.get("period") or ""),
               "target_sk": str(grant.get("target_sk") or ""),
               "early": True})
    return _grant_public(
        repo.get_grant(tenant_id=row_tenant, grant_id=grant_id) or {})


def revoke_all_active_grants(
    *, tenant_id: str, actor: AuthenticatedUser
) -> dict[str, Any]:
    """Give back every live grant this tenant holds, before it is retired.

    Starts FROM GRANTS rather than from the tenant's pool rows, because a grant
    pinned to a period whose row was already removed is exactly the one a
    pool-row-first sweep cannot see. Returns what is left so the caller can refuse
    a deletion rather than complete one over live capacity.
    """
    repo = QuotaEventsRepository()
    budgets = TenantBudgetsRepository()
    now_iso = _now_iso()
    revoked: list[str] = []
    remaining: list[dict[str, Any]] = []
    for grant in repo.list_grants_for_tenant(tenant_id=tenant_id):
        status = str(grant.get("status") or "")
        if not is_capacity_bearing(status):
            continue
        if status != GRANT_ACTIVE:
            remaining.append(_grant_public(grant))
            continue
        try:
            repo.transact_write(_revoke_txn_items(
                repo=repo, budgets=budgets, grant=grant,
                to_status=GRANT_REVOKED, revoked_by=actor.user_id,
                revoked_at=now_iso, revoke_reason="tenant_retirement"))
            revoked.append(str(grant.get("grant_id") or ""))
        except ClientError as exc:
            if not _is_transaction_cancelled(exc):
                raise
            # Anything that did not commit leaves the grant bearing capacity, and
            # the caller must not delete over it. Reported rather than retried
            # here: the retirement path is not the place to run a retry budget.
            live = repo.get_grant(
                tenant_id=tenant_id, grant_id=str(grant.get("grant_id") or ""))
            if live is not None and is_capacity_bearing(
                    str(live.get("status") or "")):
                remaining.append(_grant_public(live))
    return {"tenant_id": tenant_id, "revoked": revoked,
            "revoked_count": len(revoked),
            "remaining": remaining, "remaining_count": len(remaining)}


def sweep_expired_grants(*, now_epoch: Optional[int] = None) -> dict[str, Any]:
    """Revoke every grant whose expiry has passed. One call per schedule tick.

    Idempotent: a call with nothing due does no writes and still emits
    `sweeper_ran`, because a heartbeat that only fires when there was work cannot
    distinguish "nothing expired" from "the sweeper stopped".

    THE HEARTBEAT IS EMITTED AFTER PAGINATION COMPLETES, and that is a
    requirement rather than an ordering preference. A sweep that fails on page two
    has left grants unrevoked; if it had already claimed to have run, the absence
    alarm -- the only thing that notices a sweeper that stopped -- would have been
    satisfied by a run that did not finish.

    Never gated by the kill switch. A live grant's expiry is a money-safety
    property, not a feature: pausing revocation would leave capacity granted
    indefinitely, which is the opposite of what an operator reaches for the switch
    to achieve.
    """
    repo = QuotaEventsRepository()
    budgets = TenantBudgetsRepository()
    now = int(now_epoch) if now_epoch is not None else _now_epoch()
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

    revoked = 0
    already = 0
    blocked: list[dict[str, str]] = []
    # Lateness is measured over grants this pass actually revoked. A grant that
    # ends up REVOKE_BLOCKED is deliberately NOT a lateness sample: its staleness
    # is unbounded and its own alarm is what reports it, so folding it in here
    # would make one stuck grant read as the whole fleet revoking late.
    late_seconds: list[int] = []
    pages = 0
    last_key: Optional[dict[str, Any]] = None
    while True:
        page, last_key = repo.list_active_grants_expiring(
            now_epoch=now, limit=25, exclusive_start_key=last_key)
        pages += 1
        for grant in page:
            tenant_id = str(grant.get("tenant_id") or "")
            grant_id = str(grant.get("grant_id") or "")
            try:
                repo.transact_write(_revoke_txn_items(
                    repo=repo, budgets=budgets, grant=grant,
                    to_status=GRANT_EXPIRED, revoked_by="sweeper",
                    revoked_at=now_iso, revoke_reason="expired"))
            except ClientError as exc:
                if not _is_transaction_cancelled(exc):
                    raise
                codes = _cancellation_codes(exc)
                if _ccf_at(codes, _REVOKE_GRANT_INDEX):
                    # Another sweep got there first, or the grant was revoked
                    # early. Exactly-once holds by this condition alone; there is
                    # nothing to retry and nothing has gone wrong.
                    already += 1
                    continue
                reason = ",".join(c for c in codes if c) or "unknown"
                attempts = repo.bump_revoke_attempts(
                    tenant_id=tenant_id, grant_id=grant_id)
                if attempts is not None and attempts >= MAX_REVOKE_ATTEMPTS:
                    if repo.mark_revoke_blocked(
                            tenant_id=tenant_id, grant_id=grant_id,
                            reason=reason):
                        blocked.append({"tenant_id": tenant_id,
                                        "grant_id": grant_id,
                                        "reason": reason})
                continue
            revoked += 1
            late_seconds.append(max(0, now - int(grant.get("expires_at", now))))
        if not last_key:
            break

    summary = {
        "job": "quota_grant_sweep",
        "now_epoch": now,
        "pages": pages,
        "grants_revoked": revoked,
        "grants_already_terminal": already,
        "revoke_blocked_grants": len(blocked),
        "grant_revocation_late_seconds": max(late_seconds) if late_seconds else 0,
        "blocked_detail": blocked[:50],
    }
    _emit_sweep_metrics(summary)
    return summary


def _emit_sweep_metrics(summary: dict[str, Any]) -> None:
    """One structured line for the run, and one per blocked grant.

    The run's line carries NO `tenant_id`: it names no single tenant, and a
    heartbeat that claimed one would be attributing a deployment-wide fact to
    whichever tenant happened to be last. The per-grant lines DO carry it, which
    is how an operator paged by an undimensioned alarm finds the tenant in one
    query -- the metric stays a single series and the log line says whose.

    Nothing here carries a decision comment or a client token. R13's rule is kept
    by those strings never entering this function rather than by remembering to
    strip them.
    """
    line = {
        "event": "sweeper_ran",
        "pages": summary["pages"],
        "sweeper_ran": 1,
        "grants_revoked": summary["grants_revoked"],
        "grant_revocation_late_seconds": summary["grant_revocation_late_seconds"],
        "revoke_blocked_grants": summary["revoke_blocked_grants"],
        "_aws": {
            "CloudWatchMetrics": [{
                "Namespace": os.getenv(
                    "STRATOCLAVE_METRIC_NAMESPACE", "Stratoclave/Grants"),
                # No dimensions, on the discipline this repository already
                # decided: a custom metric is billed per name and a per-tenant
                # label is the facet that discipline exists to refuse.
                "Dimensions": [[]],
                "Metrics": [
                    {"Name": "SweeperRan"},
                    {"Name": "GrantsRevoked"},
                    {"Name": "GrantRevocationLateSeconds"},
                    {"Name": "RevokeBlockedGrants"},
                ],
            }],
            # Always stamped: a scheduled event carries no timestamp of its own
            # and an EMF line without one is dropped, which would leave the
            # absence alarm with no data and permanently green.
            "Timestamp": int(_now_epoch() * 1000),
        },
        "SweeperRan": 1,
        "GrantsRevoked": summary["grants_revoked"],
        "GrantRevocationLateSeconds": summary["grant_revocation_late_seconds"],
        "RevokeBlockedGrants": summary["revoke_blocked_grants"],
    }
    print(json.dumps(line, default=str))
    for b in summary["blocked_detail"]:
        print(json.dumps({
            "event": "revoke_blocked_grants",
            "tenant_id": b["tenant_id"],
            "grant_id": b["grant_id"],
            "reason": b["reason"],
            "revoke_blocked_grants": 1,
        }, default=str))


def sweep_handler(event=None, context=None):  # noqa: ARG001 -- Lambda signature
    """Scheduled entry point. Returns the summary so a caller can gate on it."""
    return sweep_expired_grants()


# ---------------------------------------------------------------------------
# The daily checks, registered into F1's loop from F2's own file
# ---------------------------------------------------------------------------
# F1 ships the check registry and a closed-world classification of the pool row;
# the grant-aware checks belong to the part that owns grants, so they are
# registered HERE. A check that had to be added by editing the reconciler is a
# check the next part can forget to add, and F1's `missing_declared_checks()` is
# the other half of that bargain: the declaration names these two, so a pass in
# which this module was never imported reports them missing rather than reporting
# the fleet clean.
from .observability.quota_reconciler import (  # noqa: E402 -- registry, see above
    Finding,
    ReconcileContext,
    SEVERITY_DEFECT,
    register_check,
)

_GRANTS_BY_TENANT = "f2_grants_by_tenant"
_TARGET_EXISTS = "f2_target_row_exists"


def _tenant_grants(ctx: ReconcileContext, tenant_id: str) -> list[dict[str, Any]]:
    """This tenant's grants, fetched ONCE per pass however many of its period rows
    the pass visits. A check that fetched per row would make the reconciler cost
    more the more carefully it looked, which is the pressure that gets checks
    deleted."""
    cache = ctx.extra.setdefault(_GRANTS_BY_TENANT, {})
    if tenant_id not in cache:
        cache[tenant_id] = QuotaEventsRepository().list_grants_for_tenant(
            tenant_id=tenant_id)
    return cache[tenant_id]


def _capacity_bearing_sum_for_row(
    ctx: ReconcileContext, row: dict[str, Any]
) -> int:
    """The sum of capacity-bearing grants pinned to THIS row.

    Per target row and not per tenant, which is the whole point. Grants are
    pinned to `target_pk`/`target_sk`, so during a late sweep an expired-but-
    unrevoked grant still bears capacity on the PRIOR period's row -- and a
    tenant-wide sum compared against the current period's row would be wrong in
    both directions at once, over-counting here and under-counting there. That
    combination only appears when rollover, sweeper lateness and reconciliation
    are all present, which is to say in no single part's tests.
    """
    tenant_id = str(row.get("tenant_id") or "")
    sk = str(row.get("sk") or "")
    return sum(
        int(g.get("approved_amount_microusd", 0))
        for g in _tenant_grants(ctx, tenant_id)
        if is_capacity_bearing(str(g.get("status") or ""))
        and str(g.get("target_pk") or "") == tenant_id
        and str(g.get("target_sk") or "") == sk
    )


@register_check("pool_granted_matches_active_grants")
def _pool_granted_matches_active_grants(
        row: dict[str, Any], ctx: ReconcileContext) -> Iterator[Finding]:
    """`pool_granted_microusd` against the grants it is supposed to be the sum of.

    The source comparison for the granted term. An intra-row identity cannot see
    an apply that landed twice -- the limit and the granted sum both move, so
    every equation over the row still balances while the tenant admits an extra
    grant's worth of spend. Only the grant records can say.

    `REVOKE_BLOCKED` grants count, through `is_capacity_bearing`, because their
    subtraction never committed and the row is still carrying their amount. A
    check that excluded them would alarm continuously on a row holding a blocked
    grant, for exactly as long as the fault lasted.
    """
    from dynamo.tenant_budgets import granted_microusd as _granted

    stored = _granted(row)
    expected = _capacity_bearing_sum_for_row(ctx, row)
    if stored != expected:
        yield Finding(
            check="pool_granted_matches_active_grants", severity=SEVERITY_DEFECT,
            tenant_id=str(row.get("tenant_id") or ""), period=_period_of(row),
            observed=stored, expected=expected,
            detail=(f"the row carries {stored} micro-USD of granted capacity and "
                    f"the grants pinned to it sum to {expected}; an apply or a "
                    f"revoke moved one side and not the other, or a grant was "
                    f"applied twice -- which no equation over this row can see "
                    f"because the ceiling moved with it"),
        )


@register_check("grant_cap_not_exceeded")
def _grant_cap_not_exceeded(
        row: dict[str, Any], ctx: ReconcileContext) -> Iterator[Finding]:
    """The granted sum against the aggregate cap in force NOW.

    THIS is what closes the window the caller-side cap opens. The apply guard
    compares the live granted sum against a cap resolved from a read, so a
    concurrent baseline change -- a hire, an operator's figure -- can leave a row
    over a cap that was legal when it was checked. Nothing on the row disagrees,
    because the cap is derived rather than stored. This check is the only place
    that notices, a day late, which is the lateness the trade was made for.
    """
    from dynamo.tenant_budgets import granted_microusd as _granted

    granted = _granted(row)
    if granted == 0:
        return
    cap = effective_grant_cap_for_row(row)
    if granted > cap:
        provenance = ("stored on the row" if grant_cap_microusd(row) is not None
                      else "derived from the baseline, evaluated now")
        yield Finding(
            check="grant_cap_not_exceeded", severity=SEVERITY_DEFECT,
            tenant_id=str(row.get("tenant_id") or ""), period=_period_of(row),
            observed=granted, expected=cap,
            detail=(f"{granted} micro-USD is granted against an aggregate cap of "
                    f"{cap} ({provenance}); either a baseline change lowered the "
                    f"derived cap under live grants, or an approval was checked "
                    f"against a cap that had already moved"),
        )


@register_check("grant_target_row_exists")
def _grant_target_row_exists(
        row: dict[str, Any], ctx: ReconcileContext) -> Iterator[Finding]:
    """Every grant this tenant holds points at a pool row that exists.

    Starts FROM GRANTS, which is the requirement rather than an implementation
    detail: a sweep that starts from pool rows has no row to start at for a grant
    whose target is gone, so the one defect it most needs to find is the one it is
    structurally unable to see.

    Boundary, stated because it is real: F1's loop visits pool ROWS, so a tenant
    holding grants and no pool row at all is invisible to this check -- every one
    of its grants is an orphan and there is no row for the loop to notice them
    from. `reconcile_tenant_grants` covers that case when pointed at the tenant.
    """
    tenant_id = str(row.get("tenant_id") or "")
    exists = ctx.extra.setdefault(_TARGET_EXISTS, {})
    budgets = TenantBudgetsRepository()
    for grant in _tenant_grants(ctx, tenant_id):
        if not is_capacity_bearing(str(grant.get("status") or "")):
            continue
        key = (str(grant.get("target_pk") or ""), str(grant.get("target_sk") or ""))
        if key == (tenant_id, str(row.get("sk") or "")):
            continue   # this is the row being visited; it plainly exists
        if key not in exists:
            exists[key] = budgets.get_by_key(*key) is not None
        if exists[key]:
            continue
        yield Finding(
            check="grant_target_row_exists", severity=SEVERITY_DEFECT,
            tenant_id=tenant_id, period=str(grant.get("period") or ""),
            observed=None, expected=list(key),
            detail=(f"grant {grant.get('grant_id')} bears "
                    f"{int(grant.get('approved_amount_microusd', 0))} micro-USD "
                    f"of capacity against {key[1]}, and no such pool row exists; "
                    f"its revocation can never move a ceiling and its amount is "
                    f"counted against the tenant's cap forever"),
        )


def _period_of(row: dict[str, Any]) -> str:
    sk = str(row.get("sk") or "")
    return sk.split("#", 1)[1] if "#" in sk else sk


def reconcile_tenant_grants(
    *, tenant_id: str, period: Optional[str] = None
) -> dict[str, Any]:
    """One tenant's grant reconciliation, reported per TARGET ROW.

    Two sums, not one, and the difference is R8b's. `active_only_sum_microusd` is
    the lateness denominator: a blocked grant stays in its own statistic rather
    than inflating another grant's. `capacity_bearing_sum_microusd` is the figure
    that must equal `pool_granted_microusd` for a clean reconcile, and it INCLUDES
    a blocked grant, because that grant's money was never given back and the row
    is right to still be counting it.
    """
    repo = QuotaEventsRepository()
    budgets = TenantBudgetsRepository()
    grants = repo.list_grants_for_tenant(tenant_id=tenant_id)
    by_target: dict[tuple[str, str], dict[str, Any]] = {}
    orphans: list[dict[str, Any]] = []
    for grant in grants:
        if period and str(grant.get("period") or "") != period:
            continue
        status = str(grant.get("status") or "")
        key = (str(grant.get("target_pk") or ""), str(grant.get("target_sk") or ""))
        amount = int(grant.get("approved_amount_microusd", 0))
        entry = by_target.setdefault(key, {
            "target_pk": key[0], "target_sk": key[1],
            "period": str(grant.get("period") or ""),
            "active_only_sum_microusd": 0,
            "capacity_bearing_sum_microusd": 0,
            "blocked_grant_ids": [],
        })
        if status == GRANT_ACTIVE:
            entry["active_only_sum_microusd"] += amount
        if is_capacity_bearing(status):
            entry["capacity_bearing_sum_microusd"] += amount
        if status == GRANT_REVOKE_BLOCKED:
            entry["blocked_grant_ids"].append(str(grant.get("grant_id") or ""))

    rows: list[dict[str, Any]] = []
    for key, entry in sorted(by_target.items()):
        row = budgets.get_by_key(*key)
        if row is None:
            orphans.append({**entry, "reason": "target pool row is missing"})
            continue
        stored = granted_microusd(row)
        cap = effective_grant_cap_for_row(row)
        rows.append({
            **entry,
            "pool_granted_microusd": stored,
            "drift_microusd": stored - entry["capacity_bearing_sum_microusd"],
            "grant_cap_microusd": grant_cap_microusd(row),
            "effective_grant_cap_microusd": cap,
            "cap_is_derived": grant_cap_microusd(row) is None,
            "remaining_cap_microusd": max(0, cap - stored),
            "cap_exceeded": stored > cap,
        })
    return {
        "reconciler": "tenant_grants",
        "tenant_id": tenant_id,
        "period": period,
        "rows": rows,
        "orphans": orphans,
        "clean": not orphans and all(
            r["drift_microusd"] == 0 and not r["cap_exceeded"] for r in rows),
    }


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/mvp", tags=["mvp-limit-raises"])


class SubmitLimitRaiseRequest(BaseModel):
    """The requester's side of the wire.

    `tenant_id` is deliberately absent: the tenant is the caller's own, read from
    their session. `limit_kind` defaults to the pool because it is the only
    grantable wall today, and is accepted explicitly so a client that has read a
    402's `wall` can echo it back and be told plainly when that wall is not one a
    money raise addresses.
    """

    model_config = ConfigDict(extra="forbid")

    asked_amount_microusd: int = Field(gt=0, le=MAX_GRANT_MICROUSD)
    reason_code: str = Field(min_length=1, max_length=64)
    client_token: str = Field(min_length=1, max_length=128)
    limit_kind: str = Field(default=POOL_WALL, min_length=1, max_length=64)
    comment: Optional[str] = Field(default=None, max_length=1024)


class ApproveLimitRaiseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_amount_microusd: int = Field(gt=0, le=MAX_GRANT_MICROUSD)
    #: Epoch seconds. Bounded by R11 in the service layer rather than here,
    #: because the bounds depend on the current instant and on the period's own
    #: calendar -- a schema constraint would have to be a constant.
    expires_at: int
    decision_comment: Optional[str] = Field(default=None, max_length=1024)


class RejectLimitRaiseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_comment: str = Field(min_length=1, max_length=1024)


class RevokeGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = Field(default=None, max_length=256)


def _guard(fn, *args, **kwargs):
    """Run a service call and translate its refusal into the transport's.

    ONE translation point. The service layer raises `GrantError`, which carries
    its own status and code, so a route cannot accidentally report a cap refusal
    as a 400 or a self-approval as a 404 -- and `mvp/admin_tenants.py` can call
    the same functions without inheriting an HTTP vocabulary it does not want.
    """
    try:
        return fn(*args, **kwargs)
    except GrantError as exc:
        raise exc.as_http() from None


# --- the requester -------------------------------------------------------
@router.post("/me/limit-raises", status_code=201)
def submit_own_limit_raise(
    body: SubmitLimitRaiseRequest,
    actor: AuthenticatedUser = Depends(require_permission("limits:raise-self")),
) -> dict[str, Any]:
    """File a raise against your own tenant's money ceiling.

    One per person per tenant per UTC day. The refusal names the request holding
    the day and when the day resets, WITH its zone, because a reset time read in
    the reader's own timezone is wrong for most of the world by up to a day -- and
    for a once-a-day allowance that is the whole allowance.
    """
    return _guard(
        submit_limit_raise, actor=actor,
        asked_amount_microusd=body.asked_amount_microusd,
        reason_code=body.reason_code, client_token=body.client_token,
        limit_kind=body.limit_kind, comment=body.comment)


@router.get("/me/limit-raises")
def list_own_limit_raises(
    actor: AuthenticatedUser = Depends(require_permission("limits:raise-self")),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Your raises for your current tenant, newest decision state included.

    `reason_codes` is served here rather than restated by any surface, so a
    console offering a reason cannot offer one the submit endpoint refuses.
    """
    repo = QuotaEventsRepository()
    mine = [
        _request_public(r)
        for r in repo.list_requests_for_tenant(tenant_id=actor.org_id, limit=limit)
        if str(r.get("user_id") or "") == actor.user_id
    ]
    return {"tenant_id": actor.org_id, "requests": mine,
            "reason_codes": list(RAISE_REASON_CODES)}


@router.post("/me/limit-raises/{request_id}/withdraw")
def withdraw_own_limit_raise(
    request_id: str,
    actor: AuthenticatedUser = Depends(require_permission("limits:raise-self")),
) -> dict[str, Any]:
    """Take back your own pending raise, which frees the day's slot."""
    return _guard(withdraw_limit_raise, actor=actor, request_id=request_id)


# --- the approver, globally ---------------------------------------------
@router.get("/admin/limit-raises")
def admin_list_limit_raises(
    tenant_id: str = Query(..., min_length=1),
    status: Optional[str] = Query(default=None),
    limit: int = Query(100, ge=1, le=200),
    _actor: AuthenticatedUser = Depends(require_permission("limits:approve")),
) -> dict[str, Any]:
    """An approver's queue for one tenant.

    `tenant_id` is required rather than optional. Requests are partitioned by
    request id and indexed by tenant, so a fleet-wide queue would be a scan --
    and an approver's unit of work is a tenant, not a deployment.
    """
    repo = QuotaEventsRepository()
    return {
        "tenant_id": tenant_id,
        "requests": [
            _request_public(r) for r in repo.list_requests_for_tenant(
                tenant_id=tenant_id, status=status, limit=limit)],
        "reason_codes": list(RAISE_REASON_CODES),
    }


@router.post("/admin/limit-raises/{request_id}/approve")
def admin_approve_limit_raise(
    request_id: str,
    body: ApproveLimitRaiseRequest,
    actor: AuthenticatedUser = Depends(require_permission("limits:approve")),
) -> dict[str, Any]:
    return _guard(
        approve_limit_raise, actor=actor, request_id=request_id,
        approved_amount_microusd=body.approved_amount_microusd,
        expires_at=body.expires_at, decision_comment=body.decision_comment,
        as_owner=False)


@router.post("/admin/limit-raises/{request_id}/reject")
def admin_reject_limit_raise(
    request_id: str,
    body: RejectLimitRaiseRequest,
    actor: AuthenticatedUser = Depends(require_permission("limits:approve")),
) -> dict[str, Any]:
    return _guard(
        reject_limit_raise, actor=actor, request_id=request_id,
        decision_comment=body.decision_comment, as_owner=False)


@router.get("/admin/limit-grants")
def admin_list_limit_grants(
    tenant_id: str = Query(..., min_length=1),
    _actor: AuthenticatedUser = Depends(require_permission("limits:approve")),
) -> dict[str, Any]:
    """One tenant's grants, with the reconciliation beside them.

    The reconciliation is returned WITH the list rather than from a second
    endpoint, because the question an operator has about a grant inventory is
    always whether it adds up to what the pool row says -- and two endpoints would
    let them read a list that does not.
    """
    repo = QuotaEventsRepository()
    grants = [_grant_public(g)
              for g in repo.list_grants_for_tenant(tenant_id=tenant_id)]
    return {"tenant_id": tenant_id, "grants": grants,
            "reconciliation": reconcile_tenant_grants(tenant_id=tenant_id)}


@router.post("/admin/limit-grants/{grant_id}/revoke")
def admin_revoke_limit_grant(
    grant_id: str,
    body: RevokeGrantRequest,
    tenant_id: str = Query(..., min_length=1),
    actor: AuthenticatedUser = Depends(require_permission("limits:approve")),
) -> dict[str, Any]:
    """End a live grant early.

    `tenant_id` is a required query parameter because a grant row is partitioned
    by tenant and a point write needs its partition. It is NOT the value the
    authority is bound to: the check inside the transaction binds the tenant read
    from the grant ROW, so a caller naming a tenant they do not own finds no grant
    at that key and a caller naming one they do own is still checked against the
    row's own tenant.
    """
    return _guard(
        revoke_grant, actor=actor, tenant_id=tenant_id, grant_id=grant_id,
        reason=body.reason, as_owner=False)


# --- the approver, for their own tenants --------------------------------
def _require_owned(tenant_id: str, actor: AuthenticatedUser) -> None:
    """The same ownership check the rest of the team-lead surface uses, and the
    same unified 404 for a tenant that is not theirs -- a distinct 403 would tell
    a caller that a tenant they cannot see exists."""
    tenant = TenantsRepository().get(tenant_id)
    if not tenant or tenant.get("team_lead_user_id") != actor.user_id:
        raise HTTPException(status_code=404, detail="Tenant not found")


@router.get("/team-lead/limit-raises")
def team_lead_list_limit_raises(
    tenant_id: str = Query(..., min_length=1),
    status: Optional[str] = Query(default=None),
    limit: int = Query(100, ge=1, le=200),
    actor: AuthenticatedUser = Depends(require_permission("limits:approve-own")),
) -> dict[str, Any]:
    _require_owned(tenant_id, actor)
    repo = QuotaEventsRepository()
    return {
        "tenant_id": tenant_id,
        "requests": [
            _request_public(r) for r in repo.list_requests_for_tenant(
                tenant_id=tenant_id, status=status, limit=limit)],
        "reason_codes": list(RAISE_REASON_CODES),
    }


@router.post("/team-lead/limit-raises/{request_id}/approve")
def team_lead_approve_limit_raise(
    request_id: str,
    body: ApproveLimitRaiseRequest,
    actor: AuthenticatedUser = Depends(require_permission("limits:approve-own")),
) -> dict[str, Any]:
    """Approve a raise for a tenant you own.

    `as_owner=True` selects the OWNERSHIP form of the in-transaction authority
    check, and the route decides that rather than the actor's roles. Sniffing
    roles would let a caller who happens to also be a global approver reach this
    route and have the weaker, tenant-agnostic check applied -- so the route that
    claims to be the ownership path would not be enforcing ownership.
    """
    return _guard(
        approve_limit_raise, actor=actor, request_id=request_id,
        approved_amount_microusd=body.approved_amount_microusd,
        expires_at=body.expires_at, decision_comment=body.decision_comment,
        as_owner=True)


@router.post("/team-lead/limit-raises/{request_id}/reject")
def team_lead_reject_limit_raise(
    request_id: str,
    body: RejectLimitRaiseRequest,
    actor: AuthenticatedUser = Depends(require_permission("limits:approve-own")),
) -> dict[str, Any]:
    return _guard(
        reject_limit_raise, actor=actor, request_id=request_id,
        decision_comment=body.decision_comment, as_owner=True)


@router.get("/team-lead/limit-grants")
def team_lead_list_limit_grants(
    tenant_id: str = Query(..., min_length=1),
    actor: AuthenticatedUser = Depends(require_permission("limits:approve-own")),
) -> dict[str, Any]:
    _require_owned(tenant_id, actor)
    repo = QuotaEventsRepository()
    return {"tenant_id": tenant_id,
            "grants": [_grant_public(g)
                       for g in repo.list_grants_for_tenant(tenant_id=tenant_id)],
            "reconciliation": reconcile_tenant_grants(tenant_id=tenant_id)}


@router.post("/team-lead/limit-grants/{grant_id}/revoke")
def team_lead_revoke_limit_grant(
    grant_id: str,
    body: RevokeGrantRequest,
    tenant_id: str = Query(..., min_length=1),
    actor: AuthenticatedUser = Depends(require_permission("limits:approve-own")),
) -> dict[str, Any]:
    _require_owned(tenant_id, actor)
    return _guard(
        revoke_grant, actor=actor, tenant_id=tenant_id, grant_id=grant_id,
        reason=body.reason, as_owner=True)


#: The surface consumers bind to, named here rather than left to be guessed. A
#: consumer that guesses a name is a consumer that breaks at integration with an
#: `ImportError` that survives both parts shipping correctly.
#:
#: There is deliberately NO repository class in this layer. Grant rows and request
#: rows live in the `quota-events` table and `dynamo.quota_events.
#: QuotaEventsRepository` is its repository; a second one here would be two
#: data-access paths to one table, which is how two writers of one invariant
#: appear.
__all__ = [
    "router",
    "RaiseHint",
    "effective_grant_cap_microusd",
    "is_capacity_bearing",
    "latest_permissible_expiry_for_period",
    # Named by the contract's journey amendment: a behaviour described without a
    # callable is a behaviour no cross-part test can reach.
    "sweep_expired_grants",
]
