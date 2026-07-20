"""Scheduled daily Savings Certificate issuer (litellm wedge slice-4, CDK leg).

The Lambda body the daily EventBridge rule invokes. It is the ONLY non-deterministic
boundary: it reads the day + issue timestamp from the EventBridge event `time` (NOT
a clock call), resolves the tenant set, and calls
certificate_store.issue_for_tenants — which is itself deterministic + write-once.

It emits the metrics the honesty alarms key on (Fable slice-4 (c)/(d)/(e)-2):
  * `CertificatesIssued` / `CertificatesFailed` — per-run counts.
  * `CertificateSkip_<reason>` — per skip reason, so a fleet-wide NO_TRAFFIC spike
    (ingestion outage masquerading as honest absence) and a per-tenant quiet day
    are distinguishable, and a series of skips for one tenant is countable across
    runs (until skip rows are persisted — the doc'd follow-up).
Metrics are emitted as EMF-style structured log lines (no extra SDK dependency);
the CDK stack turns them into metric filters + alarms.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from core.logging import get_logger

logger = get_logger(__name__)

# How many days back the run certifies: the settle window. day = event_day - N.
# The backend coverage gate is the second line of defence (a day still under-
# settled after N is skipped, not stamped final).
DEFAULT_SETTLE_WINDOW_DAYS = int(os.getenv("CERT_SETTLE_WINDOW_DAYS", "2"))


def _event_time_ms_and_day(event: Optional[dict[str, Any]], *,
                           settle_window_days: int) -> tuple[int, str]:
    """Derive (generated_at_ms, target_day 'YYYYMMDD') from the EventBridge event
    `time` (ISO-8601, e.g. '2026-07-17T03:00:00Z'). This is the sole clock
    boundary — the domain code never reads a clock. Raises if `time` is absent, so
    a mis-wired trigger fails loud rather than silently certifying the wrong day."""
    import datetime as _dt

    t = (event or {}).get("time")
    if not t:
        raise ValueError("EventBridge event has no `time`; cannot derive the "
                         "certificate day without reading a clock (by design).")
    dt = _dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    generated_at_ms = int(dt.timestamp() * 1000)
    target = (dt - _dt.timedelta(days=settle_window_days)).strftime("%Y%m%d")
    return generated_at_ms, target


def _resolve_tenant_ids() -> list[str]:
    """The tenant set to certify. Fable slice-4 (a): start with an EXPLICIT list
    (CERT_TENANT_IDS, comma-separated) so the schedule's coverage is a declared
    input, not an implicit dependency on a registry scan. If unset, fall back to
    enumerating the tenants table (list_all, paginated)."""
    explicit = os.getenv("CERT_TENANT_IDS", "").strip()
    if explicit:
        return [t.strip() for t in explicit.split(",") if t.strip()]
    from dynamo.tenants import TenantsRepository
    repo = TenantsRepository()
    out: list[str] = []
    cursor = None
    while True:
        items, cursor = repo.list_all(cursor=cursor, limit=100)
        out.extend(str(i["tenant_id"]) for i in items if i.get("tenant_id"))
        if not cursor:
            break
    return out


def handler(event=None, context=None):  # noqa: ARG001 — Lambda signature
    from . import certificate_store as cs

    settle_window = DEFAULT_SETTLE_WINDOW_DAYS
    generated_at_ms, day = _event_time_ms_and_day(event, settle_window_days=settle_window)
    tenant_ids = _resolve_tenant_ids()

    report = cs.issue_for_tenants(tenant_ids=tenant_ids, day=day,
                                  generated_at_ms=generated_at_ms)

    # per-skip-reason tally so a fleet-wide NO_TRAFFIC (outage) is separable from
    # scattered quiet days, and a single tenant's repeated skip is countable.
    skip_by_reason: dict[str, int] = {}
    for _tenant, reason in report.skipped:
        skip_by_reason[reason] = skip_by_reason.get(reason, 0) + 1

    expected = len(tenant_ids)
    # The structured line the CDK metric filters read. `expected` vs `issued` is
    # the silent-skip signal; `no_traffic_fraction` is the outage signal.
    no_traffic = skip_by_reason.get(cs.SKIP_NO_TRAFFIC, 0)
    logger.info(
        "certificate_batch_issued",
        day=day,
        expected=expected,
        issued=len(report.issued),
        skipped=len(report.skipped),
        failed=len(report.failed),
        skip_no_traffic=no_traffic,
        skip_unmatched_high=skip_by_reason.get(cs.SKIP_UNMATCHED_HIGH, 0),
        no_traffic_fraction=(round(no_traffic / expected, 4) if expected else 0.0),
        failed_tenants=[t for t, _ in report.failed][:50],
    )
    return {
        "day": day,
        "expected": expected,
        "issued": report.issued,
        "skipped": report.skipped,
        "failed": report.failed,
    }
