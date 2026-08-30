"""Exposure accounting for retained reservations, and the signal an operator needs.

WHAT THIS EXISTS FOR

`STRATOCLAVE_UNOBSERVED_HOLDS` now defaults on, so a reservation whose provider call
departed and whose outcome was never observed is HELD rather than handed back. That is
the correct record — an abandoned Bedrock call is billed for the full generation, and
returning the budget asserts the call was free. But it changes the failure mode. Under a
provider outage, retentions accumulate against a tenant's headroom, and until this module
existed the first signal an operator got was a refusal for an unrelated request. The
amount was reachable (an admin listing retained holds sums it) and nothing pushed it
anywhere, so nobody saw it unless they already suspected.

`charge-loss.md` section 7 names per-tenant and account exposure accounting plus
saturation alarms as the precondition for ever releasing an unobserved hold automatically.
This is not that release — nothing here gives budget back — but the accounting it asks for
is the same accounting, and retention needs it now rather than later.

WHAT THE SIGNAL IS

Not the held amount. A $10 retention is an emergency against a $20 pool and noise against
a $10,000 one, so the number that predicts a refusal is the FRACTION of the limit that
retentions are holding. Two things are reported alongside it because they answer different
questions:

  * `held_fraction` — how close this tenant is to being refused because of retentions.
    Rises in minutes during an outage.
  * `oldest_retention_age_seconds` — whether anyone is resolving them. A high fraction
    that is minutes old is an incident; the same fraction two weeks old is an operator
    who stopped looking, and no threshold on the fraction alone distinguishes those.

HOW IT IS EMITTED, AND WHY NOT MORE OFTEN

One structured log line, which the CDK turns into metric filters and alarms — the shape
`certificate_scheduler` already uses, so there is no new SDK on the request path and no
new infrastructure to schedule.

Computing it costs one bounded, strongly-consistent query, so it is not free and it is not
emitted per request. It is emitted when exposure CHANGES (a retention is taken, a
retention is resolved) and, so that a persistently high exposure keeps producing
datapoints rather than going silent and letting an alarm clear itself, on a sweep at most
once per tenant and period per `_EMIT_INTERVAL_SECONDS`. The throttle is in-process, so N
tasks emit up to N times per interval; that is a cost ceiling per task rather than a
correctness property, and the alarms are on maxima rather than counts for exactly that
reason.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from core.logging import get_logger

logger = get_logger(__name__)

#: How often one process will re-emit a tenant/period's standing exposure from a sweep.
#: Short enough that an outage shows up inside an alarm's evaluation window, long enough
#: that a busy tenant does not turn every sweep into an extra query.
_EMIT_INTERVAL_SECONDS = 60

#: `(tenant_id, period) -> monotonic seconds of the last emission from this process`.
#: Deliberately unbounded in name only: it is keyed by the tenants that actually HAVE
#: retentions, which is a small set by construction, and entries are rewritten rather
#: than appended.
_last_emitted: dict[tuple[str, str], float] = {}


def _retained_at_epoch(hold: dict[str, Any]) -> Optional[int]:
    """When this hold became retained, in epoch seconds, or None if it cannot be told.

    `hold_retain` writes `retained_at` as an ISO-8601 string, so the age is parsed from
    that. `expires_at` is the documented fallback and not merely a convenient one: the
    reaper retains a hold it met BECAUSE the hold had expired, so the expiry is within a
    sweep interval of the retention. A hold that has neither returns None and is left out
    of the age rather than counted as retained at the epoch, which would report an age of
    decades and make the staleness alarm meaningless the first time a field is missing.
    """
    raw = hold.get("retained_at")
    if raw:
        try:
            from datetime import datetime, timezone

            text = str(raw).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except (TypeError, ValueError):
            pass
    try:
        at = int(hold.get("expires_at"))
    except (TypeError, ValueError):
        return None
    return at if at > 0 else None


def exposure_figures(
    holds: list[dict[str, Any]],
    *,
    pool_limit_microusd: int,
    now_epoch: Optional[int] = None,
) -> dict[str, Any]:
    """The figures, from the retained holds and the limit they are held against.

    Separated from the emission and from storage so the arithmetic is testable without a
    table: the interesting cases are a zero limit (which must not divide) and a hold with
    no timestamp (which must not be read as "retained in 1970" and produce an age of
    fifty years).
    """
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    held = sum(int(h.get("amount_microusd", 0) or 0) for h in holds)
    limit = int(pool_limit_microusd or 0)

    ages: list[int] = []
    for h in holds:
        at = _retained_at_epoch(h)
        if at is None:
            continue
        ages.append(max(now - at, 0))

    return {
        "retained_holds": len(holds),
        "held_microusd": held,
        "pool_limit_microusd": limit,
        # A tenant with no pool limit cannot be refused for headroom, so the fraction is
        # not "infinite", it is undefined and reported as zero. The count and the amount
        # still carry the fact that something is retained.
        "held_fraction": round(held / limit, 6) if limit > 0 else 0.0,
        "oldest_retention_age_seconds": max(ages) if ages else 0,
    }


def emit_exposure(
    budgets: Any,
    tenant_id: str,
    period: str,
    *,
    reason: str,
    force: bool = False,
) -> Optional[dict[str, Any]]:
    """Read the standing exposure for one tenant/period and emit it. Returns the figures.

    `force=True` for the moments exposure actually changed (a retention taken, a retention
    resolved), which must never be throttled away — those are the edges an operator wants
    to see. Throttled otherwise.

    Never raises. This is observability on a path that moves money; a failure to report
    exposure must not fail a sweep or a resolution, and a silent exception here would be
    worse than a logged one, so the failure is logged at warning with the reason.
    """
    key = (tenant_id, period)
    if not force:
        last = _last_emitted.get(key)
        if last is not None and (time.monotonic() - last) < _EMIT_INTERVAL_SECONDS:
            return None
    try:
        holds = budgets.list_retained_holds(tenant_id=tenant_id, period=period)
    except Exception:  # noqa: BLE001
        logger.warning("retention_exposure_unreadable", tenant_id=tenant_id,
                       period=period, reason=reason)
        return None

    _last_emitted[key] = time.monotonic()
    if not holds and not force:
        # Nothing retained and nobody asked: emitting a zero every minute for every
        # tenant would drown the signal in the absence of one. A resolution that takes
        # the last retention away IS forced, so the metric does come back to zero.
        return None

    summary = None
    try:
        summary = budgets.pool_summary(tenant_id, period)
    except Exception:  # noqa: BLE001
        pass
    figures = exposure_figures(
        holds, pool_limit_microusd=int((summary or {}).get("pool_limit_microusd", 0)))

    logger.info(
        "retention_exposure",
        tenant_id=tenant_id,
        period=period,
        reason=reason,
        **figures,
    )
    return figures


def reset_for_test() -> None:
    """Drop the throttle so a test can emit twice in the same second."""
    _last_emitted.clear()
