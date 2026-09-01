"""Daily reconciliation of the tenant pool row against ITS SOURCES.

An intra-row identity cannot see a delta applied twice. If a membership change
lands twice, `seat_count` goes up by two and `pool_limit` goes up by two seats'
money -- CONSISTENTLY -- so every equation over the row still balances, and the
tenant is admitting one extra seat's worth of spend a month with nothing
disagreeing anywhere. The only way to see it is to compare the row to the thing
it is supposed to be derived FROM: the memberships themselves, and (once grants
exist) the grant records.

So this reconciler is a LOOP over a registry, not a function with a list of
comparisons in it. There are two reasons, and the second is the load-bearing one:

  * a check has a name, a severity and a source, so a finding says which
    comparison failed rather than that "the row is wrong";
  * the registry is what a later part registers INTO, from its own files. The
    grant-sum and cap checks belong to the part that owns grants, and a check
    that has to be added by editing this file is a check the next part can
    forget to add. The closed-world declaration is the other half of that: every
    check it names must be registered, so an attribute whose covering check went
    missing is a failure here rather than a comparison quietly not happening.

Read-only. Every finding is a signal to investigate, never a repair: a
reconciler that fixes what it finds destroys the evidence of how the row got
that way, and it can only guess which side of a disagreement is right.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

SEVERITY_DEFECT = "defect"    # the row disagrees with its source; investigate
SEVERITY_NOTICE = "notice"    # true but worth an operator's attention


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    tenant_id: str
    period: str
    detail: str
    observed: Any = None
    expected: Any = None


@dataclass
class ReconcileContext:
    """Everything a check may read, gathered ONCE per pass rather than per row.

    A check that fetches its own sources turns one pass into one fetch per row
    per check, and the reconciler then costs more the more carefully it looks --
    which is the pressure that makes people delete checks.
    """

    #: Active membership count per tenant, from one strongly consistent pass.
    membership_counts: dict[str, int] = field(default_factory=dict)
    #: The per-seat rate the deployment declares, or None if none is recorded.
    rate_in_force_microusd: Optional[int] = None
    #: Free-form space a later part's checks populate with their own sources
    #: (the sum of ACTIVE grants per target row, for instance) without this
    #: module having to know what they are.
    extra: dict[str, Any] = field(default_factory=dict)


#: (name, severity, callable) -> the callable yields Findings for one row.
PoolCheck = Callable[[dict[str, Any], ReconcileContext], Iterator[Finding]]
_REGISTRY: dict[str, tuple[str, PoolCheck]] = {}


def register_check(name: str, *, severity: str = SEVERITY_DEFECT):
    """Register a pool-row check under `name`. A later part calls this from its
    OWN module, so its checks arrive with the code that makes them meaningful."""
    def _wrap(fn: PoolCheck) -> PoolCheck:
        if name in _REGISTRY:
            raise RuntimeError(f"pool check {name!r} is already registered")
        _REGISTRY[name] = (severity, fn)
        return fn
    return _wrap


def registered_checks() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def missing_declared_checks() -> tuple[str, ...]:
    """Every check the closed-world declaration names that nobody registered.

    The other half of the declaration. An attribute is allowed to have no source
    comparison, but it has to say so as an exemption; naming a check that does
    not exist is how a comparison stops happening without anybody noticing.
    """
    from dynamo.pool_row_schema import POOL_ROW_ATTRIBUTES

    declared = {a.check for a in POOL_ROW_ATTRIBUTES.values() if a.check}
    return tuple(sorted(declared - set(_REGISTRY)))


# ---------------------------------------------------------------------------
# F1's checks
# ---------------------------------------------------------------------------
@register_check("seat_count_matches_membership")
def _seat_count_matches_membership(
        row: dict[str, Any], ctx: ReconcileContext) -> Iterator[Finding]:
    """The row's seat count against a live count of the tenant's memberships.

    THE check this reconciler exists for. It is the only one here that compares
    the row to something outside it, which is why it is the only one that can see
    a membership delta applied twice -- off by one seat AND one seat's money,
    consistently, so every equation over the row still balances.

    Only meaningful for the CURRENT period: the membership count is a fact about
    now, and a closed period's seat count is a fact about then. Comparing them
    would report every past month as broken.
    """
    from dynamo.pool_row_schema import SEAT_COUNT_ATTR
    from dynamo.tenant_budgets import current_period

    tenant_id = str(row.get("tenant_id") or "")
    period = _period_of(row)
    if period != current_period() or SEAT_COUNT_ATTR not in row:
        return
    stored = int(row[SEAT_COUNT_ATTR])
    live = int(ctx.membership_counts.get(tenant_id, 0))
    if stored != live:
        yield Finding(
            check="seat_count_matches_membership", severity=SEVERITY_DEFECT,
            tenant_id=tenant_id, period=period, observed=stored, expected=live,
            detail=(f"the row counts {stored} seat(s) and the tenant has {live} "
                    f"active membership(s); a difference of {stored - live} seat(s) "
                    f"is also {abs(stored - live)} seat(s) of ceiling, in the same "
                    f"direction, which no equation over this row can see"),
        )


@register_check("limit_identity")
def _limit_identity(row: dict[str, Any], ctx: ReconcileContext) -> Iterator[Finding]:
    """`pool_limit == baseline + coalesce(granted, 0)`.

    Written coalesced from the first day, so absence of a granted amount reads as
    zero and this holds on every row that exists today. That is deliberate: an
    identity that is only true once a later part ships is an identity that starts
    alarming on correct rows the day that part lands, and one whose coalesce
    branch is never exercised is one that has never been tested.
    """
    from dynamo.tenant_budgets import (
        baseline_microusd,
        expected_pool_limit_microusd,
        granted_microusd,
        is_seat_tracked,
    )

    stored = int(row.get("pool_limit_microusd", 0))
    expected = expected_pool_limit_microusd(row)
    if stored != expected:
        yield Finding(
            check="limit_identity", severity=SEVERITY_DEFECT,
            tenant_id=str(row.get("tenant_id") or ""), period=_period_of(row),
            observed=stored, expected=expected,
            detail=(f"the stored ceiling is {stored} but its composition is "
                    f"baseline {baseline_microusd(row)} "
                    f"({'seats' if is_seat_tracked(row) else 'operator figure'}) "
                    f"+ granted {granted_microusd(row)} = {expected}; a hand-edited "
                    f"row or a writer that moved one term and not the other"),
        )


@register_check("headroom_identity")
def _headroom_identity(row: dict[str, Any], ctx: ReconcileContext) -> Iterator[Finding]:
    """`headroom == limit - reserved - settled`, the invariant the admission gate
    reads. Intra-row on purpose: this one catches a writer that moved the ceiling
    without moving the headroom by the same amount, which is a different failure
    from the row disagreeing with its sources."""
    limit = int(row.get("pool_limit_microusd", 0))
    reserved = int(row.get("pool_reserved_microusd", 0))
    settled = int(row.get("pool_settled_microusd", 0))
    if "pool_headroom_microusd" not in row:
        yield Finding(
            check="headroom_identity", severity=SEVERITY_DEFECT,
            tenant_id=str(row.get("tenant_id") or ""), period=_period_of(row),
            observed=None, expected=limit - reserved - settled,
            detail="the row carries no headroom counter, so the admission gate "
                   "refuses every request against it until it is backfilled",
        )
        return
    stored = int(row["pool_headroom_microusd"])
    expected = limit - reserved - settled
    if stored != expected:
        yield Finding(
            check="headroom_identity", severity=SEVERITY_DEFECT,
            tenant_id=str(row.get("tenant_id") or ""), period=_period_of(row),
            observed=stored, expected=expected,
            detail=(f"headroom is {stored} but limit - reserved - settled is "
                    f"{expected}; every ceiling write must move headroom by exactly "
                    f"the amount it moves the ceiling"),
        )


@register_check("seat_rate_matches_rate_in_force")
def _seat_rate_matches_rate_in_force(
        row: dict[str, Any], ctx: ReconcileContext) -> Iterator[Finding]:
    """The rate this row's ceiling was computed at against the rate the deployment
    declares.

    The row's own copy is what makes an individual ceiling reproducible; the
    declared rate is what the boot check compares against. Two places holding one
    number drift, and this is the check that says when they have -- which is what
    lets the per-row copy exist at all.
    """
    from dynamo.pool_row_schema import SEAT_RATE_ATTR
    from dynamo.tenant_budgets import is_seat_tracked

    if ctx.rate_in_force_microusd is None:
        return   # nothing declared yet: a deployment the migration has not reached
    if SEAT_RATE_ATTR not in row:
        if is_seat_tracked(row):
            yield Finding(
                check="seat_rate_matches_rate_in_force", severity=SEVERITY_DEFECT,
                tenant_id=str(row.get("tenant_id") or ""), period=_period_of(row),
                observed=None, expected=ctx.rate_in_force_microusd,
                detail="a seat-tracked row with no stored rate: its ceiling is not "
                       "reproducible, because the rate it was computed at is only "
                       "whatever the reading process happens to be configured with",
            )
        return
    stored = int(row[SEAT_RATE_ATTR])
    if stored != ctx.rate_in_force_microusd:
        yield Finding(
            check="seat_rate_matches_rate_in_force", severity=SEVERITY_DEFECT,
            tenant_id=str(row.get("tenant_id") or ""), period=_period_of(row),
            observed=stored, expected=ctx.rate_in_force_microusd,
            detail=(f"this row's ceiling was computed at {stored} micro-USD/seat "
                    f"but the rate in force is {ctx.rate_in_force_microusd}; a rate "
                    f"change is a migration and this row did not get it"),
        )


@register_check("entitlement_outgrew_figure", severity=SEVERITY_NOTICE)
def _entitlement_outgrew_figure(
        row: dict[str, Any], ctx: ReconcileContext) -> Iterator[Finding]:
    """A hand-set figure that the seats have since overtaken.

    Not a defect -- the figure is what somebody chose. It is reported because it
    is the one thing an operator cannot see from the figure itself: the tenant has
    grown past the point where the default would have been more generous, and the
    figure is now doing the opposite of what it was probably set to do.
    """
    from dynamo.pool_row_schema import MANUAL_LIMIT_ATTR
    from dynamo.tenant_budgets import (
        current_period,
        is_seat_tracked,
        seat_term_microusd,
    )

    period = _period_of(row)
    if is_seat_tracked(row) or period != current_period():
        return
    figure = int(row[MANUAL_LIMIT_ATTR])
    entitlement = seat_term_microusd(row)
    if entitlement > figure:
        yield Finding(
            check="entitlement_outgrew_figure", severity=SEVERITY_NOTICE,
            tenant_id=str(row.get("tenant_id") or ""), period=period,
            observed=figure, expected=entitlement,
            detail=(f"held at {figure} micro-USD by hand while "
                    f"{int(row.get('seat_count', 0))} seat(s) now entitle it to "
                    f"{entitlement}; sending {{\"follow_seats\": true}} to the "
                    f"pool-budget endpoint returns it to the seat count"),
        )


@register_check("row_is_fully_declared")
def _row_is_fully_declared(
        row: dict[str, Any], ctx: ReconcileContext) -> Iterator[Finding]:
    """Every attribute on the row appears in the closed-world declaration.

    The declaration's test runs in the ordinary suite over the shapes the code
    writes; this is the same assertion against the shapes that actually exist,
    which is where an attribute written by something nobody remembered shows up.
    """
    from dynamo.pool_row_schema import unclassified_pool_attributes

    extra = unclassified_pool_attributes(row)
    if extra:
        yield Finding(
            check="row_is_fully_declared", severity=SEVERITY_DEFECT,
            tenant_id=str(row.get("tenant_id") or ""), period=_period_of(row),
            observed=sorted(extra), expected=[],
            detail=(f"{sorted(extra)} appear on this row and in no class of the "
                    f"declaration, so the rollover does not know whether to carry "
                    f"them, no check covers them, and the size gauge does not "
                    f"count them"),
        )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def _period_of(row: dict[str, Any]) -> str:
    sk = str(row.get("sk") or "")
    return sk.split("#", 1)[1] if "#" in sk else sk


def _iter_budget_rows(table) -> Iterator[dict[str, Any]]:
    from boto3.dynamodb.conditions import Attr

    kwargs: dict[str, Any] = {"FilterExpression": Attr("sk").begins_with("BUDGET#")}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            yield item
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return
        kwargs["ExclusiveStartKey"] = lek


def build_context() -> ReconcileContext:
    """Gather every source the registered checks read, once."""
    from dynamo.tenant_budgets import TenantBudgetsRepository
    from dynamo.user_tenants import UserTenantsRepository

    return ReconcileContext(
        # The same strongly consistent count the migration's backfill uses, from
        # the same method on the repository that owns memberships: two ways of
        # counting seats would eventually be two different numbers, and the
        # reconciler comparing against the other one would report the fleet broken.
        membership_counts=UserTenantsRepository().active_membership_counts(),
        rate_in_force_microusd=TenantBudgetsRepository().rate_in_force_microusd(),
    )


def reconcile_row(row: dict[str, Any], ctx: ReconcileContext) -> list[Finding]:
    """Run every registered check against one row."""
    out: list[Finding] = []
    for name in sorted(_REGISTRY):
        _severity, fn = _REGISTRY[name]
        out.extend(fn(row, ctx))
    return out


def reconcile_all() -> dict[str, Any]:
    """One full pass over every pool row. Returns a summary a caller can gate on.

    `checks_missing` is a hard failure and not a count: a declaration naming a
    check nobody registered means an attribute that looks covered and is not, and
    a pass that reported "clean" under those conditions would be worse than no
    pass at all.
    """
    from dynamo.tenant_budgets import TenantBudgetsRepository

    missing = missing_declared_checks()
    repo = TenantBudgetsRepository()
    ctx = build_context()
    rows = 0
    findings: list[Finding] = []
    for row in _iter_budget_rows(repo._table):
        rows += 1
        findings.extend(reconcile_row(row, ctx))
    defects = [f for f in findings if f.severity == SEVERITY_DEFECT]
    notices = [f for f in findings if f.severity == SEVERITY_NOTICE]
    return {
        "reconciler": "tenant_pool_ceiling",
        "rows": rows,
        "checks_run": list(registered_checks()),
        "checks_missing": list(missing),
        "defects": len(defects),
        "notices": len(notices),
        "clean": not defects and not missing,
        "defect_detail": [_as_dict(f) for f in defects[:50]],
        "notice_detail": [_as_dict(f) for f in notices[:50]],
    }


def _as_dict(f: Finding) -> dict[str, Any]:
    return {"check": f.check, "severity": f.severity, "tenant_id": f.tenant_id,
            "period": f.period, "observed": f.observed, "expected": f.expected,
            "detail": f.detail}


def handler(event=None, context=None):  # noqa: ARG001 -- Lambda signature
    """Scheduled entry point. Emits ONE structured line carrying the counts as
    embedded metrics, and the `tenant_id` of each finding IN THE LINE.

    The metrics carry no tenant dimension, deliberately: a per-tenant dimension on
    a metric filter that runs over every backend log line is unbounded
    cardinality. So the alarm is on the count and the log line names which tenants,
    which resolves in one query -- and is the right shape anyway, because one
    tenant with a broken ceiling is the incident, not a fleet average.
    """
    import time

    summary = reconcile_all()
    summary["_aws"] = {
        "CloudWatchMetrics": [{
            "Namespace": os.getenv("STRATOCLAVE_METRIC_NAMESPACE",
                                   "Stratoclave/Ledger"),
            "Dimensions": [[]],
            "Metrics": [
                {"Name": "PoolCeilingDefects"},
                {"Name": "PoolCeilingChecksMissing"},
                {"Name": "PoolCeilingNotices"},
            ],
        }],
        # Always stamped: a scheduled EventBridge event carries no timestamp, and
        # an EMF line without one is silently dropped -- which would leave the
        # alarm with no data and the gate it guards permanently green.
        "Timestamp": int(time.time() * 1000),
    }
    summary["event"] = "pool_ceiling_reconcile"
    summary["PoolCeilingDefects"] = summary["defects"]
    summary["PoolCeilingChecksMissing"] = len(summary["checks_missing"])
    summary["PoolCeilingNotices"] = summary["notices"]
    print(json.dumps(summary, default=str))

    # One line per defect, each naming its tenant. The summary above is what the
    # alarm counts; these are what the person the alarm woke up reads. The
    # tenant_id is on the LINE and never a metric dimension, because a dimension
    # per tenant on a filter over every log line is unbounded cardinality -- so
    # the alarm says "somewhere" and one query over these says where.
    for f in summary["defect_detail"]:
        print(json.dumps({
            "event": "pool_ceiling_defect",
            "tenant_id": f["tenant_id"],
            "period": f["period"],
            "check": f["check"],
            "detail": f["detail"],
            "observed": f["observed"],
            "expected": f["expected"],
            "defects": 1,
        }, default=str))
    return summary


if __name__ == "__main__":
    result = handler()
    raise SystemExit(0 if result["clean"] else 1)
