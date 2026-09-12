"""The discovery operator queue -- the third of the three places a discovery
blocker surfaces (the other two are the stored record itself, already
readable by anything that calls `mvp.discovery.records.list_discovered_records`
or runs `mvp.discovery.reconcile --json`, and the grant refusal in
`mvp.admin_entitlements`).

A discovered record can carry a blocker that is PERMANENT -- most concretely,
`no_agreement_offer`'s `not_marketplace_metered` subtype, which is the normal
shape for every AWS-billed family this account can see (measured against a
real account: 15 of the 75 discovered profiles). Listing every blocker here,
unfiltered, would make this queue permanently full of profiles nobody can do
anything about, and a queue nobody can clear is a queue nobody reads -- the
same failure mode `mvp.discovery.reconcile`'s own `--strict` gate exists to
avoid.

So this queue is scoped to TASK blockers only, and it decides "task" by
importing `mvp.discovery.reconcile.is_actionable_blocker` -- the exact
predicate `--strict` already classifies `actionable_blocker` findings with --
rather than writing a second classifier here. An operator staring at this
queue and an operator staring at that command's exit code must never
disagree about whether a given blocker is a task, and the only way to
guarantee that is to share the one function that decides it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .authz import require_permission
from .deps import AuthenticatedUser
from .discovery.queue import actionable_blockers_of
from .discovery.records import DiscoveredRecordStoreUnavailable, list_discovered_records

router = APIRouter(prefix="/api/mvp/admin/discovery", tags=["admin-discovery"])


class QueueBlocker(BaseModel):
    type: str
    subtype: str
    evidence: str
    first_seen: str
    last_seen: str


class QueueEntry(BaseModel):
    profile_id: str
    provider: str
    profile_scope: str
    model_family: str
    # Only the blockers on this profile that are actionable -- a permanent
    # blocker riding alongside an actionable one on the same profile is left
    # off this list, for the same reason it never earns the profile a place
    # in `entries` on its own.
    blockers: list[QueueBlocker]


class QueueResponse(BaseModel):
    entries: list[QueueEntry]


def _err_503_store_unavailable() -> HTTPException:
    # Same shape/convention as `admin_entitlements._err_503_store_unavailable`:
    # a `type` a client's retry logic can key on.
    return HTTPException(
        status_code=503,
        detail={
            "type": "discovered_record_store_unavailable",
            "message": "The discovered-record store is temporarily unavailable. Retry shortly.",
        },
    )


@router.get("/queue", response_model=QueueResponse)
def get_discovery_queue(
    actor: AuthenticatedUser = Depends(require_permission("models:discover")),
) -> QueueResponse:
    """Every discovered profile that, as of the last reconciliation pass,
    carries at least one actionable blocker -- the queue an operator works
    from, not a dump of every profile discovery has ever seen.
    """
    try:
        records = list_discovered_records()
    except DiscoveredRecordStoreUnavailable:
        raise _err_503_store_unavailable()
    entries: list[QueueEntry] = []
    for record in records:
        actionable = actionable_blockers_of(record)
        if not actionable:
            continue
        entries.append(QueueEntry(
            profile_id=record.profile_id,
            provider=record.provider,
            profile_scope=record.profile_scope,
            model_family=record.model_family,
            blockers=[
                QueueBlocker(
                    type=b.type, subtype=b.subtype, evidence=b.evidence,
                    first_seen=b.first_seen, last_seen=b.last_seen,
                )
                for b in actionable
            ],
        ))
    return QueueResponse(entries=entries)
