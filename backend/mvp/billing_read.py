"""Rating read surface (Layer 5-d): per-run billing breakdown.

Two endpoints, deliberately SEPARATE (not one endpoint with runtime role
branching) so redaction is guaranteed by TYPE, not by a denylist:

  GET /api/mvp/me/billing/runs/{run_id}
      The caller's own tenant (tenant_id from the auth context). Returns
      `RunBreakdownTenant`, which DOES NOT DEFINE the provider_cost / margin
      fields at all — they cannot leak because the model has no place to put
      them.

  GET /api/mvp/admin/billing/runs/{run_id}?tenant_id=...
      Returns `RunBreakdownAdmin` (adds provider_cost_microusd / margin_microusd).
      Requires `usage:read-all`.

Money is the ledger's frozen rating (Layer 5): every SETTLE / LATE_SETTLE
terminal carries a self-contained rating breakdown. This surface only READS and
projects it — no recomputation, no live-rate read.

Scope model: a run is queried by the client-supplied `x-sc-workflow-run-id`
against the run-index GSI, whose partition key embeds the tenant
(`TENANT#<id>#RUN#<run_id>`). The `me` endpoint pins tenant_id from the auth
context, so a caller can never reach another tenant's partition even by guessing
run_ids. An unknown / other-tenant run returns 404 (not 403) so run existence is
not an oracle.

Known limits (documented; none affect the money path):
  - A request sent WITHOUT an `x-sc-workflow-run-id` keys its ledger event on the
    hold_id fallback, so it is not queryable by a workflow run id (the edge does
    mint a `wr_...` id by default, so this only bites a caller that clears it).
  - A LATE_SETTLE reclaimed by the CROSS-PROCESS reaper (owner crashed) has no
    in-memory context, so its charge keys on the hold_id fallback and is NOT
    counted in a per-run total — a per-run breakdown can undercount for crashed
    requests (Fable L5d-e review F3). Reconciliation still balances at the
    tenant/period level; per-run is a convenience view, not the settlement total.
  - Two workflows (or a crafted request reusing another's id) that send the SAME
    workflow_run_id within one tenant share a per-run breakdown (F5). run_id is
    caller-supplied attribution, tenant-scoped; it is not an authorization key.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from dynamo import CreditLedgerRepository

from .authz import require_permission
from .deps import AuthenticatedUser, get_current_user


router = APIRouter(prefix="/api/mvp", tags=["mvp-billing"])


# ---------------------------------------------------------------------------
# Response models — redaction BY TYPE (allowlist), never a denylist
# ---------------------------------------------------------------------------


class RatingComponentView(BaseModel):
    """One token-type line of a charge. Charge-side only (no provider cost)."""

    model_config = ConfigDict(extra="forbid")
    tokens: int
    rate_microusd_per_mtok: int
    cost_microusd: int


class RunEventTenant(BaseModel):
    """A single money-move event in a run, as a TENANT may see it. Deliberately
    has NO provider_cost / margin fields — they cannot be serialized here."""

    model_config = ConfigDict(extra="forbid")
    event_type: str
    settle_reason: Optional[str] = None
    model_id: Optional[str] = None
    pricing_version: Optional[str] = None
    pricing_key: Optional[str] = None
    settled_microusd: int
    components: dict[str, RatingComponentView]
    ts_ms: int


class RunEventAdmin(RunEventTenant):
    """Admin view: adds the provider cost + margin (may be negative)."""

    provider_cost_microusd: Optional[int] = None
    margin_microusd: Optional[int] = None


class RunBreakdownTenant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    run_id: str
    total_settled_microusd: int
    events: list[RunEventTenant]


class RunBreakdownAdmin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    run_id: str
    total_settled_microusd: int
    total_provider_cost_microusd: Optional[int] = None
    total_margin_microusd: Optional[int] = None
    events: list[RunEventAdmin]


# ---------------------------------------------------------------------------
# Service — always builds the FULL breakdown; the endpoint picks the model
# ---------------------------------------------------------------------------


def _full_run_breakdown(tenant_id: str, run_id: str) -> Optional[dict]:
    """Assemble the full (unredacted) per-run breakdown from the ledger's frozen
    ratings, or None if the run has no money-move events (→ 404).

    Only terminal / late-settle events carry a rating; RESERVE events are skipped
    here (they grant credit, they are not a charge line)."""
    events = CreditLedgerRepository().events_for_run(tenant_id=tenant_id, run_id=run_id)
    rated = []
    total_settled = 0
    total_cost = 0
    # Total margin is only meaningful when EVERY rated event carries a provider
    # cost. A run mixing cost-bearing and unknown-cost events would otherwise
    # count the unknowns as zero-cost and OVERSTATE the run margin (Fable L5-d
    # review M4). `all_cost` stays True only if every rated event had a cost.
    all_cost = True
    for ev in events:
        etype = str(ev.get("event_type", ""))
        # RESERVE grants credit — not a charge line — so it is skipped.
        if etype == "RESERVE":
            continue
        raw = ev.get("rating")
        settled = int(ev.get("settled_delta_microusd", 0))
        if not raw:
            # A terminal (SETTLE/LATE_SETTLE/…) with NO rating — e.g. a pre-Layer-5
            # legacy terminal, or a RECLAIM (settled 0). Skipping it would drop its
            # settled_delta from the run total (Fable L5-d review-2 M-1, the same
            # fail-open-on-money as H3). Keep it as an error line that still counts
            # settled; a RECLAIM contributes 0 so it is harmless there too.
            if settled != 0:
                all_cost = False
                total_settled += settled
                rated.append({
                    "event_type": etype,
                    "settle_reason": "missing_rating",
                    "model_id": ev.get("model_id"),
                    "pricing_version": None,
                    "pricing_key": None,
                    "settled_microusd": settled,
                    "components": {},
                    "ts_ms": int(ev.get("ts_ms", 0)),
                    "provider_cost_microusd": None,
                    "margin_microusd": None,
                })
            continue
        try:
            rating = json.loads(raw)
        except (ValueError, TypeError):
            # A corrupted rating must NOT silently drop this event's settled_delta
            # from the run total (Fable L5-d review H3 — billing display must not
            # fail-open on money). Surface it as an error line that still carries
            # the settled amount, so the total stays truthful and the defect is
            # visible rather than under-reported.
            total_settled += settled
            all_cost = False  # a broken rating has no known cost
            rated.append({
                "event_type": str(ev.get("event_type", "")),
                "settle_reason": "unparseable_rating",
                "model_id": ev.get("model_id"),
                "pricing_version": None,
                "pricing_key": None,
                "settled_microusd": settled,
                "components": {},
                "ts_ms": int(ev.get("ts_ms", 0)),
                "provider_cost_microusd": None,
                "margin_microusd": None,
            })
            continue
        total_settled += settled
        pc = rating.get("provider_cost_microusd")
        mg = rating.get("margin_microusd")
        if pc is not None:
            total_cost += int(pc)
        else:
            all_cost = False
        rated.append({
            "event_type": str(ev.get("event_type", "")),
            "settle_reason": ev.get("settle_reason"),
            "model_id": ev.get("model_id"),
            "pricing_version": rating.get("pricing_version"),
            "pricing_key": rating.get("pricing_key"),
            "settled_microusd": settled,
            "components": rating.get("components", {}),
            "ts_ms": int(ev.get("ts_ms", 0)),
            "provider_cost_microusd": int(pc) if pc is not None else None,
            "margin_microusd": int(mg) if mg is not None else None,
        })
    if not rated:
        return None
    # Only surface run totals for cost/margin when EVERY rated event has a cost;
    # otherwise leave them null (per-event provider_cost is still shown, but a run
    # total would mislead by treating unknown-cost events as free — M4).
    return {
        "tenant_id": tenant_id,
        "run_id": run_id,
        "total_settled_microusd": total_settled,
        "total_provider_cost_microusd": total_cost if all_cost else None,
        "total_margin_microusd": (total_settled - total_cost) if all_cost else None,
        "events": rated,
    }


def _tenant_event(e: dict) -> RunEventTenant:
    """Explicit field copy into the tenant model (NEVER `**e` — that could carry
    provider_cost/margin through). The model also forbids extras as a backstop."""
    return RunEventTenant(
        event_type=e["event_type"],
        settle_reason=e.get("settle_reason"),
        model_id=e.get("model_id"),
        pricing_version=e.get("pricing_version"),
        pricing_key=e.get("pricing_key"),
        settled_microusd=e["settled_microusd"],
        components={k: RatingComponentView(**v) for k, v in e["components"].items()},
        ts_ms=e["ts_ms"],
    )


def _admin_event(e: dict) -> RunEventAdmin:
    return RunEventAdmin(
        event_type=e["event_type"],
        settle_reason=e.get("settle_reason"),
        model_id=e.get("model_id"),
        pricing_version=e.get("pricing_version"),
        pricing_key=e.get("pricing_key"),
        settled_microusd=e["settled_microusd"],
        components={k: RatingComponentView(**v) for k, v in e["components"].items()},
        ts_ms=e["ts_ms"],
        provider_cost_microusd=e.get("provider_cost_microusd"),
        margin_microusd=e.get("margin_microusd"),
    )


@router.get("/me/billing/runs/{run_id}", response_model=RunBreakdownTenant)
def me_run_billing(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    _perm: AuthenticatedUser = Depends(require_permission("usage:read-self")),
) -> RunBreakdownTenant:
    """The caller's own per-run billing. tenant_id is pinned from the auth
    context — a caller cannot reach another tenant's run (404 if the run is
    unknown OR belongs to another tenant; no existence oracle)."""
    full = _full_run_breakdown(user.org_id, run_id)
    if full is None:
        raise HTTPException(status_code=404, detail="run not found")
    return RunBreakdownTenant(
        tenant_id=full["tenant_id"],
        run_id=full["run_id"],
        total_settled_microusd=full["total_settled_microusd"],
        events=[_tenant_event(e) for e in full["events"]],
    )


@router.get("/admin/billing/runs/{run_id}", response_model=RunBreakdownAdmin)
def admin_run_billing(
    run_id: str,
    tenant_id: str = Query(..., description="tenant whose run to inspect"),
    _admin: AuthenticatedUser = Depends(require_permission("usage:read-all")),
) -> RunBreakdownAdmin:
    """Admin per-run billing including provider cost + margin."""
    full = _full_run_breakdown(tenant_id, run_id)
    if full is None:
        raise HTTPException(status_code=404, detail="run not found")
    return RunBreakdownAdmin(
        tenant_id=full["tenant_id"],
        run_id=full["run_id"],
        total_settled_microusd=full["total_settled_microusd"],
        total_provider_cost_microusd=full["total_provider_cost_microusd"],
        total_margin_microusd=full["total_margin_microusd"],
        events=[_admin_event(e) for e in full["events"]],
    )
